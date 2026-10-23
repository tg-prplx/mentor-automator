import asyncio
import json

import pytest
from conftest import SENTINEL

from mentor_automator.agent import Agent
from mentor_automator.auth import ensure_login
from mentor_automator.events import Events
from mentor_automator.models import Answer, State
from mentor_automator.moodle import Moodle, StructureError


async def login_demo(env, tmp_path):
    settings, selectors, browser, _, state = env
    # Explicit fake file; never touches root cred.txt or .env.
    fake = tmp_path / "fictional-login.txt"
    fake.write_text("username=demo_student\npassword=fictional=pass\n", encoding="utf-8")
    await ensure_login(browser.page, settings, selectors, credential_file=fake)
    return fake


class FakeLLM:
    def __init__(self):
        self.payloads = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def solve(self, question, image=None):
        self.payloads.append(json.dumps(question.payload(), ensure_ascii=False))
        self.started.set()
        await self.release.wait()
        values = {"q1": (["1"], ""), "q2": (["0", "2"], ""), "q3": ([], "Луна"), "q4": ([], "2.5")}
        choices, text = values[question.id]
        return Answer(choices=choices, text=text, explanation="Фиктивный ответ", confidence=0.9, skip=False)


async def setup_agent(env, tmp_path):
    await login_demo(env, tmp_path)
    s, sel, browser, store, state = env
    moodle = Moodle(s, sel, browser)
    quizzes = await moodle.scan()
    assert len(quizzes) == 1
    quiz = quizzes[0]
    assert "sesskey" not in quiz.url
    assert quiz.available is True and quiz.attempts_remaining == 2
    assert "20 minutes" in quiz.time_limit and "30 October" in quiz.deadline
    assert state.starts == 0  # monitoring never starts an attempt
    await store.observe(quiz)
    llm = FakeLLM()
    agent = Agent(s, store, moodle, llm, Events())
    return agent, llm, quiz


async def test_fake_login_session_reused(environment, tmp_path):
    fake = await login_demo(environment, tmp_path)
    s, sel, browser, _, state = environment
    fake.unlink()  # a second login must not need any credential file
    await ensure_login(browser.page, s, sel, credential_file=fake)
    assert state.login_count == 1


async def test_session_cookie_survives_browser_restart(environment, tmp_path):
    fake = await login_demo(environment, tmp_path)
    s, sel, browser, _, state = environment
    await browser.close()
    fake.unlink()
    await browser.open(headless=True)
    await ensure_login(browser.page, s, sel, credential_file=fake)
    assert state.login_count == 1


async def test_visual_question_crop(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    questions = await agent.moodle.questions()
    assert questions[0][0].visual
    image = await agent.moodle.image(0)
    assert image.startswith(b"\x89PNG")


async def test_no_finalization_on_network_probe_failure(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 20)

    async def offline():
        raise ConnectionError("fictional")

    agent.browser.final_connection_check = offline
    token = await agent.store.issue_confirmation(rid, 300)
    await agent.confirm(rid, token)
    await asyncio.wait_for(agent.task, 10)
    assert environment[-1].submissions == 0
    assert (await agent.store.run(rid))["state"] == State.RECOVERY_REQUIRED
    assert not agent.browser.gate.allowed("10")


async def test_pause_revokes_waiting_confirmation(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 20)
    token = await agent.store.issue_confirmation(rid, 300)
    await agent.pause()
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
    with pytest.raises(RuntimeError):
        await agent.confirm(rid, token)
    assert environment[-1].submissions == 0


@pytest.mark.parametrize("delayed_multipart", [False, True])
async def test_full_multpage_agent_and_guard(environment, tmp_path, delayed_multipart):
    if delayed_multipart:
        environment[-1].render_delay = 800
        environment[-1].form_enctype = "multipart/form-data"
    agent, llm, quiz = await setup_agent(environment, tmp_path)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 30)
    row = await agent.store.run(rid)
    assert row["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert await agent.store.counts(rid) == (4, 1)
    assert environment[-1].submissions == 0
    assert len(environment[-1].saves) == 2
    assert environment[-1].saves[0]["q1"] == "1"
    assert environment[-1].saves[1] == {"q3": "Луна", "q4": "2.5"}
    assert "x^2" in llm.payloads[0]
    assert " | " in llm.payloads[2]
    assert all(SENTINEL not in payload and "sesskey" not in payload for payload in llm.payloads)
    # Both a mislabelled direct HTTP form and a summary link are blocked before approval.
    result = await agent.browser.page.evaluate("""async()=>{try{return (await fetch('/mod/quiz/processattempt.php',
       {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
       body:'attempt=10&finishattempt=1'})).status}catch{return 'blocked'}}""")
    assert result == "blocked"
    result = await agent.browser.page.evaluate(
        """async()=>{try{return (await fetch('/mod/quiz/summary.php?attempt=10')).status}catch{return 'blocked'}}"""
    )
    assert result == "blocked"
    assert agent.browser.gate.blocked >= 2
    token = await agent.store.issue_confirmation(rid, 300)
    with pytest.raises(RuntimeError):
        await agent.confirm(rid, "wrong")
    await agent.confirm(rid, token)
    await asyncio.wait_for(agent.task, 20)
    assert (await agent.store.run(rid))["state"] == State.COMPLETED
    assert environment[-1].submissions == 1
    assert not agent.browser.gate.allowed("10")
    with pytest.raises(RuntimeError):
        await agent.confirm(rid, token)


async def test_platform_closure_during_next_page_is_terminal(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    environment[-1].close_on_next = True
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 20)
    row = await agent.store.run(rid)
    assert row["state"] == State.PLATFORM_CLOSED
    assert row["error_code"] == "ATTEMPT_ALREADY_CLOSED"
    assert environment[-1].starts == 1 and environment[-1].submissions == 0
    with pytest.raises(RuntimeError):
        await agent.resume()


async def test_opaque_save_reports_block_instead_of_generic_browser_error(environment, tmp_path):
    environment[-1].form_enctype = "text/plain"
    agent, _, quiz = await setup_agent(environment, tmp_path)
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 25)
    row = await agent.store.run(rid)
    assert row["state"] == State.ERROR
    assert row["error_code"] == "SAVE_REQUEST_BLOCKED"
    assert environment[-1].saves == [] and environment[-1].submissions == 0


async def test_pause_during_llm_then_resume(environment, tmp_path):
    agent, llm, quiz = await setup_agent(environment, tmp_path)
    llm.release.clear()
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(llm.started.wait(), 10)
    await agent.pause()
    llm.release.set()
    await asyncio.sleep(0.3)
    assert await agent.store.counts(rid) == (0, 0)
    assert not await agent.browser.page.locator("#q1_1").is_checked()
    await agent.resume()
    await asyncio.wait_for(agent.task, 20)
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert environment[-1].submissions == 0


async def test_content_change_and_shuffled_options(environment, tmp_path):
    agent, llm, quiz = await setup_agent(environment, tmp_path)
    await agent.moodle.start(quiz)
    question, index = (await agent.moodle.questions())[0]
    answer = await llm.solve(question)
    await agent.browser.page.locator("#q1 label").first.evaluate("e => e.textContent='changed'")
    with pytest.raises(StructureError):
        await agent.moodle.apply(question, index, answer)
    await agent.browser.page.locator("#q1 .answer").evaluate("e=>e.append(e.firstElementChild)")
    fresh, _ = (await agent.moodle.questions())[0]
    assert fresh.options[0].text == "4"
    await agent.moodle.apply(
        fresh, 0, Answer(choices=["0"], text="", explanation="demo", confidence=0.9, skip=False)
    )
    assert await agent.browser.page.locator("#q1_1").is_checked()


async def test_telegram_error_keeps_waiting(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)

    async def failure(rid):
        raise ConnectionError("fictional")

    agent.notify_final = failure
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 20)
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert environment[-1].submissions == 0
    await agent.shutdown()
