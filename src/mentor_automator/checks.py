"""Runtime verification of the ready application, never a credential-inspection utility.

Only auth.ensure_login may open cred.txt. Reports expose counters/status codes only.
No account names, HTML, browser state, tokens, or raw exceptions are returned.
"""

import asyncio
import json

from aiogram import Bot
from filelock import FileLock

from .auth import ensure_login
from .browser import Browser
from .config import Selectors
from .events import Events
from .moodle import Moodle
from .panel import Panel
from .storage import Store


class ReadOnlyControls:
    async def pause(self):
        raise RuntimeError("READ_ONLY_CHECK")

    async def resume(self):
        raise RuntimeError("READ_ONLY_CHECK")

    async def stop(self):
        raise RuntimeError("READ_ONLY_CHECK")


async def check_moodle(settings, *, hold=False, headless=False, show_panel=True):
    """Real login and two read-only scans. Telegram and LLM are never invoked."""
    settings.data.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(settings.data / "application.lock"), timeout=0)
    browser, store, panel, panel_browser = None, None, None, None
    report = {"mode": "read_only", "stage": "startup", "ok": False}
    with lock:
        try:
            selectors = Selectors(settings.path(settings.selectors_file))
            browser = Browser(settings, selectors, read_only=True)
            await browser.open(headless=headless)
            report["stage"] = "authentication"
            print(json.dumps({"check_stage": "authentication"}), flush=True)
            # Normal runtime entry; no developer/test code accesses the credential file.
            login_status = await asyncio.wait_for(ensure_login(browser.page, settings, selectors), 75)
            report["authentication"] = login_status
            print(json.dumps({"authentication": login_status}), flush=True)
            await browser.save_session()
            moodle = Moodle(settings, selectors, browser)
            report["stage"] = "monitoring"
            print(json.dumps({"check_stage": "monitoring"}), flush=True)
            quizzes = await asyncio.wait_for(moodle.scan(), 300)
            report.update(moodle.last_scan_stats)
            report["available"] = sum(q.available is True for q in quizzes)
            report["unavailable"] = sum(q.available is False for q in quizzes)
            report["availability_unknown"] = sum(q.available is None for q in quizzes)
            report["deadlines_detected"] = sum(q.deadline != "Не указан в интерфейсе" for q in quizzes)
            report["time_limits_detected"] = sum(q.time_limit != "Не указан в интерфейсе" for q in quizzes)
            report["attempt_limits_detected"] = sum(q.attempts_remaining is not None for q in quizzes)
            store = Store(settings.data / "mentor.sqlite3")
            await store.open()
            for quiz in quizzes:
                await store.observe(quiz)
            before = await store.one("SELECT COUNT(*) count FROM notifications")
            # Repeat the live scan to check stable parsing and notification de-duplication.
            repeated = await asyncio.wait_for(moodle.scan(), 300)
            for quiz in repeated:
                await store.observe(quiz)
            after = await store.one("SELECT COUNT(*) count FROM notifications")
            report["repeat_scan_duplicates"] = after["count"] - before["count"]
            report["repeat_scan_same_ids"] = {q.id for q in quizzes} == {q.id for q in repeated}
            report["stage"] = "panel"
            events = Events()
            events.publish(
                "Вход и два обхода настоящего Moodle проверены. Попытки не запускались.",
                state="READ_ONLY_VERIFIED",
                question="Проверка настоящего Moodle завершена",
                explanation=f"Найдено курсов: {report['courses']}; тестов: {report['quizzes']}. "
                f"Доступно: {report['available']}. Повторных уведомлений: {report['repeat_scan_duplicates']}.",
                monitor="Режим проверки: запуск и отправка попыток запрещены сетевым перехватчиком",
            )
            controls = ReadOnlyControls()
            controls.scan_now = asyncio.Event()
            panel = Panel(events, controls, controls)
            panel_url = await panel.open()
            # Separate blank profile: never screenshot or inspect the authenticated Moodle UI.
            if hold and show_panel:
                page = await browser.open_panel(panel_url)
            else:
                panel_browser = await browser.pw.chromium.launch(headless=True)
                page = await panel_browser.new_page(viewport={"width": 1280, "height": 1000})
                await page.goto(panel_url)
            await page.locator("#state").filter(has_text="READ_ONLY_VERIFIED").wait_for()
            report["panel_rendered"] = True
            report["stage"] = "complete"
            report["ok"] = True
            report["attempts_started"] = 0
            report["attempts_submitted"] = 0
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if hold:
                await asyncio.Event().wait()
            return report
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report["error_type"] = type(error).__name__
            report["page_closed"] = browser.page.is_closed() if browser and hasattr(browser, "page") else None
            report.update(getattr(locals().get("moodle"), "last_scan_stats", {}))
            # Whitelisted structure counts are safe even if authentication failed.
            if browser and browser.context:
                try:
                    report["diagnostics"] = await Moodle(settings, selectors, browser).diagnostics(
                        browser.page
                    )
                except Exception:
                    pass
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return report
        finally:
            if panel_browser:
                await panel_browser.close()
            if panel:
                await panel.close()
            if store:
                await store.close()
            if browser:
                await browser.close()


async def check_telegram(settings):
    """Real Telegram API check; no token, IDs, account details or raw exceptions in output."""
    missing = []
    if not settings.telegram_bot_token.get_secret_value():
        missing.append("TELEGRAM_BOT_TOKEN")
    if settings.telegram_chat_id <= 0:
        missing.append("TELEGRAM_CHAT_ID")
    if settings.telegram_user_id <= 0:
        missing.append("TELEGRAM_USER_ID")
    report = {"mode": "telegram_check", "ok": False, "missing_fields": missing}
    if missing:
        print(json.dumps(report), flush=True)
        return report
    bot = None
    try:
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        report["stage"] = "token"
        me = await asyncio.wait_for(bot.get_me(), 20)
        report["bot_authenticated"] = me.is_bot
        report["stage"] = "chat"
        chat = await asyncio.wait_for(bot.get_chat(settings.telegram_chat_id), 20)
        report["private_chat"] = chat.type == "private"
        if not report["private_chat"]:
            raise RuntimeError("PRIVATE_CHAT_REQUIRED")
        report["stage"] = "long_polling"
        await asyncio.wait_for(bot.get_updates(timeout=1, limit=1), 15)
        report["long_polling_available"] = True
        report["stage"] = "notification"
        await asyncio.wait_for(
            bot.send_message(
                settings.telegram_chat_id,
                "Проверка Mentor Automator: настоящий Telegram подключён, long polling доступен. "
                "Это служебное уведомление. Попытки тестов не запускались и не отправлялись.",
            ),
            20,
        )
        report["notification_sent"] = True
        report["stage"] = "complete"
        report["ok"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        if report.get("stage") == "chat":
            report["chat_id_is_example"] = settings.telegram_chat_id == 123456789
            report["user_id_is_example"] = settings.telegram_user_id == 123456789
            if "chat not found" in str(getattr(error, "message", "")).lower():
                report["error_code"] = "CHAT_NOT_FOUND"
    finally:
        if bot:
            await bot.session.close()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


async def inspect_quiz_start(settings):
    """Read the failed quiz's start controls only. Never click or open attempt endpoints."""
    browser, store = None, None
    report = {"mode": "inspect_start", "ok": False}
    lock = FileLock(str(settings.data / "application.lock"), timeout=0)
    with lock:
        try:
            selectors = Selectors(settings.path(settings.selectors_file))
            store = Store(settings.data / "mentor.sqlite3")
            await store.open()
            run = await store.latest_run()
            quiz = await store.quiz(run["quiz_id"]) if run else None
            if not quiz:
                raise RuntimeError("NO_QUIZ")
            browser = Browser(settings, selectors, read_only=True)
            await browser.open(headless=False)
            await ensure_login(browser.page, settings, selectors)
            await browser.page.goto(quiz.url, wait_until="domcontentloaded")
            await browser.page.wait_for_timeout(1000)
            moodle = Moodle(settings, selectors, browser)
            report["visible_start_controls"] = sum(
                [
                    await loc.is_visible() and await loc.is_enabled()
                    for loc in await browser.page.locator(selectors["start"]).all()
                ]
            )
            report["visible_continue_controls"] = sum(
                [
                    await loc.is_visible() and await loc.is_enabled()
                    for loc in await browser.page.locator(selectors["continue"]).all()
                ]
            )
            report["controls"] = await browser.page.locator("form[action*='/mod/quiz/']").evaluate_all(r"""forms=>forms.map(f=>({
                method:f.method,action_path:new URL(f.action).pathname,enctype:f.enctype,
                controls:[...f.querySelectorAll('button,input[type=submit]')].map(b=>({
                    tag:b.tagName,type:b.type,id:b.id,name:b.name,
                    label:(b.textContent||b.value||'').trim().slice(0,120),
                    visible:!!(b.offsetWidth||b.offsetHeight||b.getClientRects().length)
                }))
            }))""")
            report["diagnostics"] = await moodle.diagnostics(browser.page)
            report["ok"] = True
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return report
        except Exception as error:
            report["error_type"] = type(error).__name__
            print(json.dumps(report), flush=True)
            return report
        finally:
            if browser:
                await browser.close()
            if store:
                await store.close()


async def inspect_existing_attempt(settings, attempt_id=None):
    browser, store = None, None
    report = {"mode": "inspect_existing_attempt", "ok": False}
    with FileLock(str(settings.data / "application.lock"), timeout=0):
        try:
            store = Store(settings.data / "mentor.sqlite3")
            await store.open()
            row = await store.attempt_run(attempt_id) if attempt_id else await store.latest_run()
            if not row or not row["attempt_id"] or not row["url"]:
                raise RuntimeError("NO_EXISTING_ATTEMPT")
            selectors = Selectors(settings.path(settings.selectors_file))
            browser = Browser(settings, selectors, read_only=True, inspection_attempt=row["attempt_id"])
            await browser.open(headless=False)
            await ensure_login(browser.page, settings, selectors)
            await browser.page.goto(row["url"], wait_until="domcontentloaded")
            moodle = Moodle(settings, selectors, browser)
            try:
                await browser.page.locator(selectors["questions"]).first.wait_for(
                    state="visible", timeout=10000
                )
            except Exception:
                pass
            report["structure"] = await browser.page.evaluate("""()=>({
                ready:document.readyState,
                attempt_page:location.pathname.endsWith('/mod/quiz/attempt.php'),
                review_page:location.pathname.endsWith('/mod/quiz/review.php'),
                question_nodes:document.querySelectorAll('.que').length,
                question_text_nodes:document.querySelectorAll('.qtext').length,
                answer_nodes:document.querySelectorAll('.answer').length,
                response_forms:document.querySelectorAll('#responseform').length,
                iframes:document.querySelectorAll('iframe').length,
                process_forms:[...document.querySelectorAll('form[action*=processattempt]')].map(f=>({
                    enctype:f.enctype,
                    controls:f.querySelectorAll('input:not([type=hidden]),textarea,select').length,
                    child_classes:[...f.children].map(e=>({tag:e.tagName,classes:e.className})).slice(0,20)
                }))
            })""")
            report["diagnostics"] = await moodle.diagnostics(browser.page)
            report["final_controls"] = await browser.page.locator(
                "#responseform, .submitbtns, .quizattempt, #mod_quiz_navblock, form[action*='/mod/quiz/']"
            ).evaluate_all("""regions=>[...new Set(regions.flatMap(r=>[...r.querySelectorAll('button,input[type=submit],a.endtestlink,a[href*=summary]')]))].map(e=>({
                tag:e.tagName,name:e.name||'',label:(e.textContent||e.value||'').trim().slice(0,150),
                visible:!!e.getClientRects().length,
                is_last_next:e.name==='next'&&e.form?.querySelector('[name=nextpage]')?.value==='-1',
                action_path:e.form?new URL(e.form.action).pathname:null
            }))""")
            if report["structure"]["question_nodes"]:
                try:
                    questions = await moodle.questions()
                    report["parsed_questions"] = len(questions)
                    report["question_types"] = [q.kind for q, _ in questions]
                except Exception as error:
                    from .errors import safe_error

                    report["parser_error"] = safe_error(error, browser)
            report["ok"] = True
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return report
        except Exception as error:
            report["error_type"] = type(error).__name__
            print(json.dumps(report), flush=True)
            return report
        finally:
            if browser:
                await browser.close()
            if store:
                await store.close()
