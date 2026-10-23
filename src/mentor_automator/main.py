import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from filelock import FileLock, Timeout

from .agent import Agent
from .auth import ensure_login
from .browser import Browser
from .checks import check_moodle, check_telegram, inspect_existing_attempt, inspect_quiz_start
from .config import Selectors, Settings
from .errors import ERRORS
from .events import Events
from .llm import LLM
from .models import Question
from .monitor import Monitor
from .moodle import Moodle
from .onboarding import needs_setup, setup
from .pairing import pair_telegram
from .panel import Panel
from .preflight import doctor
from .privacy import configure_logging
from .storage import Store
from .telegram_bot import Telegram


async def serve(settings, *, resume_existing=None, prepare_finish=None):
    if (
        not settings.telegram_bot_token.get_secret_value()
        or settings.telegram_chat_id <= 0
        or settings.telegram_user_id <= 0
    ):
        raise RuntimeError("Задайте TELEGRAM_BOT_TOKEN, положительные CHAT_ID и USER_ID личного чата")
    settings.data.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(settings.data / "application.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise RuntimeError("Приложение уже запущено с этим DATA_DIR") from None
    store, browser, panel, telegram, agent = None, None, None, None, None
    tasks = []
    try:
        selectors = Selectors(settings.path(settings.selectors_file))
        store = Store(settings.data / "mentor.sqlite3")
        await store.open()
        await store.recover()
        events = Events()
        browser = Browser(settings, selectors)
        await browser.open(headless=False)
        await ensure_login(browser.page, settings, selectors)
        await browser.save_session()
        moodle = Moodle(settings, selectors, browser)
        agent = Agent(settings, store, moodle, LLM(settings), events)
        latest = await store.status_run()
        if latest and latest["state"] not in {"COMPLETED", "STOPPED", "PLATFORM_CLOSED"}:
            agent.run_id = latest["id"]
            filled, skipped = await store.counts(latest["id"])
            events.publish(
                "Состояние восстановлено. "
                + (
                    ERRORS.get(latest.get("error_code"), "")
                    or "Для продолжения: /review или /resume; отправка запрещена"
                ),
                state=latest["state"],
                run_id=latest["id"],
                filled=filled,
                skipped=skipped,
            )
        telegram = Telegram(settings, store, agent, events)
        monitor = Monitor(settings, moodle, store, events, telegram.notifications)
        telegram.monitor = monitor
        if settings.show_panel:
            panel = Panel(events, agent, monitor)
            url = await panel.open()
            await browser.open_panel(url)
        print("Mentor запущен: видимый браузер и Telegram. Ctrl+C — выход.", flush=True)
        tasks = [
            asyncio.create_task(coro)
            for coro in (
                monitor.run(),
                telegram.run(),
                telegram.progress(),
                telegram.connection_watch(),
                agent.watch_timer(),
            )
        ]
        if resume_existing:
            row = await store.attempt_run(resume_existing)
            if (
                not row
                or row["attempt_id"] != resume_existing
                or row["state"] not in {"RECOVERY_REQUIRED", "ERROR", "PAUSED"}
            ):
                raise RuntimeError("EXISTING_ATTEMPT_NOT_RECOVERABLE")
            await agent.resume(row["id"])
        if prepare_finish:
            row = await store.attempt_run(prepare_finish)
            if not row:
                raise RuntimeError("NO_FINAL_WAIT_TO_RECOVER")
            await agent.prepare_finish(row["id"])
        # Unexpected death of any service stops the supervisor; no orphaned agent can submit.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        if agent:
            try:
                await agent.shutdown()
            except Exception:
                pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for resource in (panel, telegram, browser, store):
            if resource:
                try:
                    await resource.close()
                except Exception:
                    pass
        lock.release()


async def run_service(settings, *, resume_existing=None, prepare_finish=None):
    """Handle repeated terminal interrupts without abandoning SQLite/browser cleanup."""
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stopping.set))
    service = asyncio.create_task(
        serve(settings, resume_existing=resume_existing, prepare_finish=prepare_finish)
    )
    waiter = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait({service, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if service in done:
            service.result()
        else:
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
            print("Приложение остановлено. Незавершённые попытки не отправлялись.", flush=True)
    finally:
        waiter.cancel()
        if not service.done():
            service.cancel()
        await asyncio.gather(service, waiter, return_exceptions=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


async def check_codex():
    # No .env, real browser, cred.txt, or site session is used for this synthetic smoke test.
    settings = Settings(root=Path.cwd(), llm_retries=0)
    question = Question(
        id="demo",
        kind="single",
        text="Сколько будет 2+2?",
        options=[{"id": "0", "text": "3"}, {"id": "1", "text": "4"}],
    )
    answer = await LLM(settings).solve(question)
    print(json.dumps(answer.model_dump(), ensure_ascii=False, indent=2))


def main():
    configure_logging()
    parser = argparse.ArgumentParser(description="Mentor · локальный наблюдаемый Moodle-агент")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--setup", action="store_true", help="Мастер локальной настройки и привязки Telegram")
    parser.add_argument("--doctor", action="store_true", help="Проверить установку без входа в Moodle")
    parser.add_argument(
        "--launch", action="store_true", help="Настройка при первом запуске, затем обычный запуск"
    )
    parser.add_argument(
        "--check-codex", action="store_true", help="Фиктивный вопрос; не читает .env или cred.txt"
    )
    parser.add_argument(
        "--check-moodle",
        action="store_true",
        help="Runtime-проверка настоящего входа и мониторинга без запуска попыток и Telegram",
    )
    parser.add_argument("--hold", action="store_true", help="Оставить браузер и панель после --check-moodle")
    parser.add_argument(
        "--check-telegram",
        action="store_true",
        help="Проверить бот и отправить служебное уведомление владельцу",
    )
    parser.add_argument(
        "--pair-telegram",
        action="store_true",
        help="Привязать личный Telegram через одноразовую команду боту",
    )
    parser.add_argument(
        "--inspect-start", action="store_true", help="Диагностика кнопок последнего теста без начала попытки"
    )
    parser.add_argument(
        "--inspect-attempt",
        nargs="?",
        const="latest",
        metavar="ATTEMPT_ID",
        help="Чтение структуры указанной или последней существующей попытки без изменения ответов",
    )
    parser.add_argument(
        "--resume-existing",
        metavar="ATTEMPT_ID",
        help="Восстановить только существующую попытку с точно указанным ID; без новой попытки",
    )
    parser.add_argument(
        "--prepare-finish",
        metavar="ATTEMPT_ID",
        help="Проверить сохранённую попытку и выдать новое подтверждение без изменения ответов и отправки",
    )
    args = parser.parse_args()
    # Keep SQLite/profile private on POSIX. Windows uses the current user's inherited ACL.
    if os.name != "nt":
        os.umask(0o077)
    try:
        if args.setup:
            if not asyncio.run(setup(args.root.resolve())):
                sys.exit(1)
        elif args.doctor:
            if not asyncio.run(doctor(Settings.load(args.root.resolve()))):
                sys.exit(1)
        elif args.launch:
            settings = Settings.load(args.root.resolve())
            # Check before pairing or launching another browser; don't interrupt a running bot.
            settings.data.mkdir(parents=True, exist_ok=True)
            try:
                with FileLock(str(settings.data / "application.lock"), timeout=0):
                    pass
            except Timeout:
                print("Mentor уже запущен. Используйте своего Telegram-бота для управления.", file=sys.stderr)
                sys.exit(1)
            if needs_setup(settings) and not asyncio.run(setup(args.root.resolve())):
                sys.exit(1)
            settings = Settings.load(args.root.resolve())
            if not asyncio.run(doctor(settings)):
                sys.exit(1)
            asyncio.run(run_service(settings))
        elif args.check_codex:
            asyncio.run(check_codex())
        elif args.check_moodle:
            result = asyncio.run(check_moodle(Settings.load(args.root.resolve()), hold=args.hold))
            if not result["ok"]:
                sys.exit(1)
        elif args.inspect_attempt:
            if not asyncio.run(
                inspect_existing_attempt(
                    Settings.load(args.root.resolve()),
                    None if args.inspect_attempt == "latest" else args.inspect_attempt,
                )
            )["ok"]:
                sys.exit(1)
        elif args.inspect_start:
            if not asyncio.run(inspect_quiz_start(Settings.load(args.root.resolve())))["ok"]:
                sys.exit(1)
        elif args.pair_telegram:
            if not asyncio.run(pair_telegram(Settings.load(args.root.resolve()))):
                sys.exit(1)
        elif args.check_telegram:
            result = asyncio.run(check_telegram(Settings.load(args.root.resolve())))
            if not result["ok"]:
                sys.exit(1)
        else:
            asyncio.run(
                run_service(
                    Settings.load(args.root.resolve()),
                    resume_existing=args.resume_existing,
                    prepare_finish=args.prepare_finish,
                )
            )
    except KeyboardInterrupt:
        print("Остановлено. Незавершённая попытка не отправляется при выходе.")
    except Exception:
        print(
            "Запуск/служба остановлены. Проверьте конфигурацию, Codex login и браузер. Секретные детали ошибки скрыты.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
