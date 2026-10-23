"""Local prerequisite checks; never opens credentials, a site session, or the network."""

import asyncio
import shutil
import subprocess
from pathlib import Path

from playwright.async_api import async_playwright

from .llm import codex_args


async def doctor(settings, *, emit=print):
    ok = True
    for label, path in (
        ("Конфигурация .env", settings.root / ".env"),
        ("Файл cred.txt (только наличие)", settings.root / "cred.txt"),
        ("Селекторы Moodle", settings.path(settings.selectors_file)),
    ):
        found = path.is_file()
        emit(f"{'OK' if found else 'НЕТ'} · {label}")
        ok &= found
    if settings.llm_backend == "codex":
        try:
            # Uses the same Windows npm shim resolution as the real model adapter.
            args = codex_args(settings, Path.cwd())
            prefix = args[: args.index("exec")]
            result = await asyncio.to_thread(
                subprocess.run,
                prefix + ["login", "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
            found = result.returncode == 0
            emit("OK · Вход в Codex CLI" if found else "НЕТ · Выполните codex login")
            ok &= found
        except Exception:
            emit("НЕТ · Установите Codex CLI: npm install -g @openai/codex@0.160.0; затем codex login")
            ok = False
    else:
        emit("HTTP LLM: доступность сервера проверяется при запросе; ключи скрыты.")
    try:
        async with async_playwright() as playwright:
            # A fresh empty browser: never the user's or application's persistent profile.
            browser = await playwright.chromium.launch(
                headless=True,
                channel=None if settings.browser_channel == "chromium" else settings.browser_channel,
            )
            await browser.close()
        emit("OK · Браузер запускается с чистым временным профилем")
    except Exception:
        if settings.browser_channel == "chromium":
            emit("НЕТ · Установите браузер: uv run playwright install chromium")
        else:
            name = "Google Chrome" if settings.browser_channel == "chrome" else "Microsoft Edge"
            emit(f"НЕТ · Установите {name} или измените BROWSER_CHANNEL в .env")
        ok = False
    if not shutil.which("codex") and settings.llm_backend == "codex":
        emit("После установки CLI может понадобиться заново открыть терминал.")
    return ok
