from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import CallbackQuery, Chat, Message, Update, User

from mentor_automator.config import Settings
from mentor_automator.events import Events
from mentor_automator.models import Question, Quiz, State
from mentor_automator.storage import Store
from mentor_automator.telegram_bot import Telegram


async def test_manual_continue_is_bound_to_current_pause_and_cannot_replay(tmp_path):
    store = Store(tmp_path / "state.db")
    await store.open()
    await store.observe(
        Quiz(id="7", course="Demo", title="Demo", url="https://example.test/mod/quiz/view.php?id=7")
    )
    rid = await store.create_run("7")
    await store.update_run(rid, state=State.PAUSED, attempt_id="10")
    agent = SimpleNamespace(run_id=rid, manual_token="current-pause", busy=lambda: True)

    async def resume(run_id):
        assert run_id == rid
        agent.manual_token = None
        await store.update_run(rid, state=State.RUNNING)

    agent.resume = AsyncMock(side_effect=resume)
    tg = Telegram(
        Settings(
            telegram_bot_token="123456789:fictional_testing_token_123456789",
            telegram_chat_id=5,
            telegram_user_id=5,
        ),
        store,
        agent,
        Events(),
    )
    tg.bot.session.make_request = AsyncMock(return_value=True)
    tg.send = AsyncMock(return_value=SimpleNamespace(message_id=99))
    user = User(id=5, is_bot=False, first_name="Demo")
    message = Message(
        message_id=99, date=datetime.now(timezone.utc), chat=Chat(id=5, type="private"), from_user=user
    )
    try:
        await tg.manual_prompt(rid, Question(id="q", kind="unsupported", text="Synthetic widget"))
        data = tg.send.call_args.args[1].inline_keyboard[0][0].callback_data
        assert data == f"manual:{rid}:current-pause"

        async def press(token, update_id):
            cb = CallbackQuery(
                id=str(update_id),
                from_user=user,
                chat_instance="demo",
                message=message,
                data=f"manual:{rid}:{token}",
            )
            await tg.dp.feed_update(tg.bot, Update(update_id=update_id, callback_query=cb))

        await press("old-pause", 1)
        assert agent.resume.await_count == 0
        await press("current-pause", 2)
        assert agent.resume.await_count == 1
        await press("current-pause", 3)
        assert agent.resume.await_count == 1
        assert (await store.run(rid))["state"] == State.RUNNING
    finally:
        await tg.close()
        await store.close()


async def test_duplicate_start_callback_after_restart_cannot_start_again(tmp_path):
    path = tmp_path / "state.db"
    user = User(id=5, is_bot=False, first_name="Demo")
    message = Message(
        message_id=99, date=datetime.now(timezone.utc), chat=Chat(id=5, type="private"), from_user=user
    )
    cb = CallbackQuery(
        id="same-start-event", from_user=user, chat_instance="demo", message=message, data="start:7"
    )
    starts = AsyncMock()
    for _ in range(2):
        store = Store(path)
        await store.open()
        agent = SimpleNamespace(start=starts)
        tg = Telegram(
            Settings(
                telegram_bot_token="123456789:fictional_testing_token_123456789",
                telegram_chat_id=5,
                telegram_user_id=5,
            ),
            store,
            agent,
            Events(),
        )
        tg.bot.session.make_request = AsyncMock(return_value=True)
        try:
            await tg.dp.feed_update(tg.bot, Update(update_id=1, callback_query=cb))
        finally:
            await tg.close()
            await store.close()
    assert starts.await_count == 1
