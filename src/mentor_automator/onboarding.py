"""First-run setup. Secrets are entered locally, never printed or sent to a model."""

import getpass
import os
import re
import warnings
from pathlib import Path

from dotenv import set_key
from filelock import FileLock

from .config import Settings
from .pairing import pair_telegram


def private_input(message):
    # getpass otherwise falls back to echoed input when no terminal is available.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(message)


def write_new_private(path: Path, text: str):
    """Never overwrite an existing credential file, including a symlink."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


async def setup(root: Path, *, secret=private_input, emit=print, pair=pair_telegram):
    settings = Settings.load(root)
    settings.data.mkdir(parents=True, exist_ok=True)
    # Pairing and changing configuration must not race an already running bot.
    with FileLock(str(settings.data / "application.lock"), timeout=0):
        emit("Первая настройка Mentor Automator. Ввод токена, логина и пароля скрыт.")
        env_path = root / ".env"
        if not env_path.exists():
            template = root / ".env.example"
            write_new_private(env_path, template.read_text(encoding="utf-8"))
        token = settings.telegram_bot_token.get_secret_value()
        if not token or token.startswith("123456789:"):
            emit("Создайте своего бота в Telegram через @BotFather → /newbot.")
            while True:
                token = secret("Токен Telegram-бота (ввод скрыт): ").strip()
                if re.fullmatch(r"[1-9][0-9]{5,15}:[A-Za-z0-9_-]{20,}", token):
                    set_key(env_path, "TELEGRAM_BOT_TOKEN", token, quote_mode="always")
                    break
                emit("Нужен полный токен BotFather без пробелов. Значение не выводится.")
        credential_path = root / "cred.txt"
        if not credential_path.exists():
            emit("Введите данные Moodle. Они сохранятся только в локальном cred.txt.")
            while True:
                username = secret("Логин Moodle (ввод скрыт): ")
                password = secret("Пароль Moodle (ввод скрыт): ")
                repeated = secret("Повторите пароль (ввод скрыт): ")
                if (
                    username
                    and password
                    and password == repeated
                    and not any(c in username + password for c in "\r\n\x00")
                ):
                    write_new_private(credential_path, f"username={username}\npassword={password}\n")
                    del username, password, repeated
                    break
                emit("Поля не должны быть пустыми; пароли должны совпадать. Попробуйте ещё раз.")
        else:
            emit("cred.txt уже существует: оставляю его без чтения и изменений.")
        settings = Settings.load(root)
        if (
            settings.telegram_chat_id <= 0
            or settings.telegram_user_id <= 0
            or settings.telegram_chat_id == 123456789
            or settings.telegram_user_id == 123456789
        ):
            emit("Откройте личный чат со своим ботом и отправьте команду /pair из следующей строки.")
            if not await pair(settings):
                emit("Привязка не завершена. Запустите настройку повторно; секреты вводить заново не нужно.")
                return False
        emit("Настройка сохранена. Финальная отправка теста всегда требует отдельного подтверждения.")
        return True


def needs_setup(settings):
    return (
        not (settings.root / ".env").exists()
        or not (settings.root / "cred.txt").exists()
        or not settings.telegram_bot_token.get_secret_value()
        or settings.telegram_bot_token.get_secret_value().startswith("123456789:")
        or settings.telegram_chat_id <= 0
        or settings.telegram_user_id <= 0
        or settings.telegram_chat_id == 123456789
        or settings.telegram_user_id == 123456789
    )
