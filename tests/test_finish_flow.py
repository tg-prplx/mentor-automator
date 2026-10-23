import asyncio
from unittest.mock import AsyncMock

import pytest
from test_browser_agent import setup_agent

from mentor_automator.models import State


@pytest.mark.parametrize("mode,delay", [("uk_delayed", 0), ("direct", 900), ("native", 0), ("bootstrap", 0)])
async def test_final_form_structure_delays_and_native_confirmation(environment, tmp_path, mode, delay):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    environment[-1].final_mode = mode
    environment[-1].summary_delay = delay
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 30)
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert environment[-1].summary_visits == 0
    assert environment[-1].submissions == 0
    token = await agent.store.issue_confirmation(rid, 300)
    await agent.confirm(rid, token)
    await asyncio.wait_for(agent.task, 30)
    row = await agent.store.run(rid)
    assert row["state"] == State.COMPLETED, row["error_code"]
    assert environment[-1].submissions == 1 and environment[-1].summary_visits == 1
    assert not agent.browser.gate.allowed("10")
    if mode == "bootstrap":
        assert set(environment[-1].ajax_calls) == {
            "core_output_load_template_with_dependencies",
            "core_get_strings",
        }
        assert agent.browser.gate.blocked >= 1 and agent.browser.gate.blocked_quiz == 0
        assert agent.browser.gate.last_block_context["endpoint"] == "OTHER_AJAX"
    with pytest.raises(RuntimeError):
        await agent.confirm(rid, token)


async def test_unrelated_native_confirm_denied_even_with_armed_permit(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    agent.browser.gate.arm("10")
    assert await agent.browser.page.evaluate("()=>confirm('Unrelated operation')") is False
    agent.browser.gate.revoke()
    assert environment[-1].submissions == 0


async def test_finish_failure_retains_stage_and_does_not_reuse_confirmation(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 30)
    agent.moodle.sel.values["finish"] = ".missing-finish"
    agent.moodle.sel.values["next"] = ".missing-next"
    token = await agent.store.issue_confirmation(rid, 300)
    await agent.confirm(rid, token)
    await asyncio.wait_for(agent.task, 10)
    row = await agent.store.run(rid)
    assert row["state"] == State.RECOVERY_REQUIRED
    assert row["error_code"] == "FINISH_NOT_FOUND" and row["stage"] == "PREPARING_FINISH"
    assert environment[-1].submissions == 0
    with pytest.raises(RuntimeError):
        await agent.confirm(rid, token)


async def test_explicit_final_wait_recovery_never_refills_or_resubmits(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 30)
    old_token = await agent.store.issue_confirmation(rid, 300)
    await agent.stop()
    newer = await agent.store.create_run(quiz.id)
    await agent.store.update_run(newer, state=State.PLATFORM_CLOSED)
    assert (await agent.store.status_run())["id"] == rid
    agent.llm.solve = AsyncMock(side_effect=AssertionError("Must not reanalyse"))
    agent.moodle.apply = AsyncMock(side_effect=AssertionError("Must not refill"))
    agent.notify_final = AsyncMock()
    before = (environment[-1].starts, len(environment[-1].saves), environment[-1].submissions)
    await agent.prepare_finish(rid)
    await asyncio.wait_for(agent.task, 15)
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert agent.run_id == rid and agent.notify_final.await_count == 1
    assert (environment[-1].starts, len(environment[-1].saves), environment[-1].submissions) == before
    assert not agent.browser.gate.allowed("10")
    with pytest.raises(RuntimeError):
        await agent.confirm(rid, old_token)


async def test_timer_does_not_classify_new_run_from_previous_review(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    await agent.browser.page.goto(agent.moodle.url("/mod/quiz/review.php?attempt=10"))
    agent.run_id = await agent.store.create_run(quiz.id)
    watcher = asyncio.create_task(agent.watch_timer())
    try:
        await asyncio.sleep(1.2)
        assert (await agent.store.run(agent.run_id))["state"] == State.STARTING
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
