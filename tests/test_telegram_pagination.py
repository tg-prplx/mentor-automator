from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from mentor_automator.config import Settings
from mentor_automator.events import Events
from mentor_automator.models import Quiz
from mentor_automator.storage import Store
from mentor_automator.telegram_bot import Telegram


async def test_tests_command_29_items_one_message_and_edit_pages(tmp_path):
    store = Store(tmp_path / "state.db")
    await store.open()
    for i in range(29):
        await store.observe(
            Quiz(
                id=str(i),
                course="Дисциплина",
                title=f"Тест {i}",
                url=f"https://mentor.khai.edu/mod/quiz/view.php?id={i}",
                available=True,
            )
        )
    telegram = Telegram(
        Settings(
            telegram_bot_token="123456789:fictional_testing_token_123456789",
            telegram_chat_id=5,
            telegram_user_id=5,
        ),
        store,
        SimpleNamespace(),
        Events(),
    )
    telegram.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=100))
    telegram.bot.edit_message_text = AsyncMock()
    telegram.bot.answer_callback_query = AsyncMock()
    telegram.bot.session.make_request = AsyncMock(return_value=True)
    user = User(id=5, is_bot=False, first_name="Demo")
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=5, type="private"),
        from_user=user,
        text="/tests",
        entities=[MessageEntity(type="bot_command", offset=0, length=6)],
    )
    try:
        await telegram.dp.feed_update(telegram.bot, Update(update_id=1, message=message))
        assert telegram.bot.send_message.await_count == 1
        first = telegram.bot.send_message.call_args.kwargs
        assert "Страница 1/6" in first["text"]
        assert len(first["text"]) < 4000
        assert len([r for r in first["reply_markup"].inline_keyboard if any(b.url for b in r)]) == 5
        sent = message.model_copy(update={"message_id": 100})
        callback = CallbackQuery(
            id="demo", from_user=user, chat_instance="demo", message=sent, data="tests:all:1"
        )
        telegram.next_message_at = 0
        await telegram.dp.feed_update(telegram.bot, Update(update_id=2, callback_query=callback))
        assert telegram.bot.send_message.await_count == 1
        assert telegram.bot.edit_message_text.await_count == 1
        assert "Страница 2/6" in telegram.bot.edit_message_text.call_args.kwargs["text"]
        assert telegram.bot.edit_message_text.call_args.kwargs["message_id"] == 100
        telegram.next_message_at = 0
        await telegram.dp.feed_update(telegram.bot, Update(update_id=3, message=message))
        assert telegram.bot.send_message.await_count == 1
        assert telegram.bot.edit_message_text.await_count == 2
    finally:
        await telegram.close()
        await store.close()


async def test_bulk_notifications_are_one_digest(tmp_path):
    store = Store(tmp_path / "state.db")
    await store.open()
    for i in range(29):
        await store.observe(
            Quiz(
                id=str(i),
                course="Demo",
                title=f"Quiz {i}",
                url=f"https://mentor.khai.edu/mod/quiz/view.php?id={i}",
                available=i < 12,
            )
        )
    telegram = Telegram(
        Settings(
            telegram_bot_token="123456789:fictional_testing_token_123456789",
            telegram_chat_id=5,
            telegram_user_id=5,
        ),
        store,
        SimpleNamespace(),
        Events(),
    )
    telegram.send = AsyncMock(return_value=SimpleNamespace(message_id=42))
    try:
        await telegram.notifications()
        await telegram.notifications()
        assert telegram.send.await_count == 1
        assert (await store.one("SELECT COUNT(*) n FROM notifications WHERE status='sent'"))["n"] == 12
        assert (await store.one("SELECT COUNT(*) n FROM notifications WHERE status='suppressed'"))["n"] == 17
    finally:
        await telegram.close()
        await store.close()
