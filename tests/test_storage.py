import asyncio

from mentor_automator.models import Quiz, State
from mentor_automator.storage import Store


async def test_dedup_change_and_history(tmp_path):
    store = Store(tmp_path / "state.db")
    await store.open()
    try:
        quiz = Quiz(
            id="7",
            course="Physics",
            title="Test",
            url="https://example.test/mod/quiz/view.php?id=7",
            available=False,
        )
        await store.observe(quiz)
        await store.observe(quiz)
        quiz.available = True
        await store.observe(quiz)
        await store.observe(quiz)
        rows = await store.all("SELECT * FROM notifications")
        assert len(rows) == 2
        assert [r["kind"] for r in rows] == ["new", "changed"]
    finally:
        await store.close()


async def test_single_use_bound_confirmation_and_restart(tmp_path):
    store = Store(tmp_path / "state.db")
    await store.open()
    try:
        rid = await store.create_run("7")
        await store.update_run(rid, attempt_id="10", state=State.AWAITING_FINAL_CONFIRMATION)
        token = await store.issue_confirmation(rid, 300)
        assert not await store.consume_confirmation(rid, token, "11")
        assert not await store.consume_confirmation(rid, "wrong", "10")
        results = await asyncio.gather(*[store.consume_confirmation(rid, token, "10") for _ in range(4)])
        assert sum(results) == 1
        await store.update_run(rid, state=State.AWAITING_FINAL_CONFIRMATION)
        token = await store.issue_confirmation(rid, 300)
        await store.recover()
        assert (await store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
        assert not await store.consume_confirmation(rid, token, "10")
        token = await store.issue_confirmation(rid, 0)
        assert not await store.consume_confirmation(rid, token, "10")
        await store.update_run(rid, state=State.RUNNING)
        await store.recover()
        assert (await store.run(rid))["state"] == State.RECOVERY_REQUIRED
    finally:
        await store.close()


async def test_parallel_launch_prevented(tmp_path):
    store = Store(tmp_path / "state.db")
    await store.open()
    try:
        outcomes = await asyncio.gather(store.create_run("7"), store.create_run("7"), return_exceptions=True)
        assert sum(isinstance(x, str) for x in outcomes) == 1
        assert sum(isinstance(x, RuntimeError) for x in outcomes) == 1
    finally:
        await store.close()
