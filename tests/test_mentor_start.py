import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, MessageEntity, Update, User
from test_browser_agent import setup_agent

from mentor_automator.config import Settings
from mentor_automator.events import Events
from mentor_automator.models import State
from mentor_automator.telegram_bot import Telegram


async def test_mentor_label_hidden_confirmation_and_delayed_modal(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    state = environment[-1]
    state.start_mode = "mentor"
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 20)
    row = await agent.store.run(rid)
    assert row["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert row["attempt_id"] == "10" and row["error_code"] is None
    assert row["stage"] == "FINAL_CONFIRMATION"
    assert state.starts == 1 and state.submissions == 0


async def test_moodle_popup_attempt_adopted(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    environment[-1].start_mode = "popup"
    original = agent.browser.page
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 20)
    assert agent.browser.page is not original
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert environment[-1].starts == 1


async def test_failed_start_reports_reason_and_same_run_can_retry(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    state = environment[-1]
    state.available = False
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 10)
    row = await agent.store.run(rid)
    assert row["state"] == State.ERROR and row["attempt_id"] is None
    assert row["stage"] == "LOCATING_START" and row["error_code"] == "START_NOT_FOUND"
    assert state.starts == 0
    state.available = True
    state.start_mode = "mentor"
    assert await agent.start(quiz.id) == rid
    await asyncio.wait_for(agent.task, 20)
    assert state.starts == 1
    assert (await agent.store.run(rid))["state"] == State.AWAITING_FINAL_CONFIRMATION
    assert len(await agent.store.all("SELECT id FROM runs")) == 1


async def test_resume_without_id_never_creates_a_new_attempt(environment, tmp_path):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    state = environment[-1]
    state.available = False
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 10)
    state.available = True
    await agent.resume()
    await asyncio.wait_for(agent.task, 10)
    row = await agent.store.run(rid)
    assert row["error_code"] == "NO_EXISTING_ATTEMPT"
    assert state.starts == 0 and state.submissions == 0


@pytest.mark.parametrize("closed", [False, True])
async def test_status_explains_failed_start_and_offers_one_explicit_retry(environment, tmp_path, closed):
    agent, _, quiz = await setup_agent(environment, tmp_path)
    environment[-1].available = False
    rid = await agent.start(quiz.id)
    await asyncio.wait_for(agent.task, 10)
    if closed:
        await agent.store.update_run(rid, state=State.PLATFORM_CLOSED, attempt_id="10")
    tg = Telegram(
        Settings(
            telegram_bot_token="123456789:fictional_testing_token_123456789",
            telegram_chat_id=5,
            telegram_user_id=5,
        ),
        agent.store,
        SimpleNamespace(),
        Events(),
    )
    tg.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=5, type="private"),
        from_user=User(id=5, is_bot=False, first_name="Demo"),
        text="/status",
        entities=[MessageEntity(type="bot_command", offset=0, length=7)],
    )
    try:
        await tg.dp.feed_update(tg.bot, Update(update_id=1, message=message))
        response = tg.bot.send_message.call_args.kwargs
        assert "None" not in response["text"]
        assert "неподтверждённой доставкой" not in response["text"]
        assert tg.bot.send_message.await_count == 1
        if closed:
            assert "PLATFORM_CLOSED" in response["text"]
            assert "Таймер: попытка закрыта" in response["text"]
            assert response["reply_markup"] is None
            assert "Причина:" not in response["text"]
            assert "Этап:" not in response["text"]
            return
        assert "Этап:" in response["text"] and "Причина:" in response["text"]
        assert "ID ещё не получен" in response["text"]
        buttons = response["reply_markup"].inline_keyboard
        assert len(buttons) == 1 and len(buttons[0]) == 1
        assert buttons[0][0].callback_data == f"start:{quiz.id}"
    finally:
        await tg.close()
