"""Bind a Telegram private user via a one-time local challenge, without exposing credentials."""

import asyncio
import json
import secrets

from aiogram import Bot
from dotenv import set_key


async def pair_telegram(settings, *, timeout=300):
    bot = None
    try:
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        await bot.get_me()
        await bot.delete_webhook(drop_pending_updates=False)
        command = "/pair " + secrets.token_hex(4)
        # Fresh pairing challenge, never a token read from a file or account session.
        print(json.dumps({"pairing_command": command, "expires_in_seconds": timeout}), flush=True)
        offset = None
        async with asyncio.timeout(timeout):
            while True:
                updates = await bot.get_updates(offset=offset, timeout=10, allowed_updates=["message"])
                for update in updates:
                    offset = update.update_id + 1
                    message = update.message
                    if (
                        not message
                        or message.chat.type != "private"
                        or not message.from_user
                        or message.from_user.is_bot
                        or (message.text or "").strip() != command
                    ):
                        continue
                    path = settings.root / ".env"
                    set_key(path, "TELEGRAM_CHAT_ID", str(message.chat.id), quote_mode="never")
                    set_key(path, "TELEGRAM_USER_ID", str(message.from_user.id), quote_mode="never")
                    await bot.send_message(
                        message.chat.id,
                        "Mentor Automator привязан к твоему личному чату. Команды управления доступны только тебе. "
                        "Попытки тестов не запускались.",
                    )
                    print(json.dumps({"telegram_paired": True, "notification_sent": True}), flush=True)
                    return True
    except Exception as error:
        print(json.dumps({"telegram_paired": False, "error_type": type(error).__name__}), flush=True)
        return False
    finally:
        if bot:
            await bot.session.close()
