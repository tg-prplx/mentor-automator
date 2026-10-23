import asyncio
import json
import os
import random
import re
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright


class FinalGate:
    """Browser-level fail-closed guard, independent of model answers and selector names."""

    def __init__(self):
        self.attempt_id = None
        self.expires = 0.0
        self.blocked = 0
        self.active_attempt = None
        self.final_write_used = False
        self.last_block_reason = None
        self.blocked_quiz = 0
        self.last_block_context = None

    def arm(self, attempt_id: str):
        self.attempt_id = attempt_id
        self.expires = time.monotonic() + 60
        self.final_write_used = False
        self.last_block_context = None

    def revoke(self):
        self.attempt_id = None
        self.expires = 0.0

    def allowed(self, attempt_id):
        return bool(attempt_id and self.attempt_id == attempt_id and time.monotonic() < self.expires)

    async def route(self, route):
        request = route.request
        u = urlsplit(request.url)
        query = parse_qs(u.query)
        body = parse_qs(request.post_data or "")
        content_type = request.headers.get("content-type", "")
        opaque_body = bool(request.post_data and "application/x-www-form-urlencoded" not in content_type)
        decoded = None
        if request.post_data and "application/json" in content_type:
            try:
                decoded = json.loads(request.post_data)
                if isinstance(decoded, dict):
                    body = {k: [str(v)] for k, v in decoded.items()}
                    opaque_body = False
            except (ValueError, TypeError):
                pass
        if request.post_data and content_type.startswith("multipart/form-data"):
            try:
                payload = request.post_data_buffer
                if not payload or len(payload) > 2_000_000:
                    raise ValueError("OPAQUE_FORM")
                message = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: "
                    + content_type.encode("ascii")
                    + b"\r\nMIME-Version: 1.0\r\n\r\n"
                    + payload
                )
                if not message.is_multipart() or message.defects:
                    raise ValueError("INVALID_FORM")
                body = {}
                routing_keys = {
                    "attempt",
                    "finishattempt",
                    "timeup",
                    "submitallandfinish",
                    "nextpage",
                    "next",
                }
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name in routing_keys:
                        body.setdefault(name, []).append(
                            (part.get_payload(decode=True) or b"").decode("utf-8")
                        )
                opaque_body = False
            except (ValueError, TypeError, UnicodeError):
                opaque_body = True
        params = {key: query.get(key, []) + body.get(key, []) for key in query.keys() | body.keys()}
        attempt = (params.get("attempt") or [""])[0]
        quiz_mutation = "/mod/quiz/" in u.path and request.method not in {"GET", "HEAD", "OPTIONS"}
        final = u.path.endswith("/mod/quiz/summary.php") or any(
            any(v.lower() not in {"0", "", "false"} for v in params.get(k, ["0"]))
            for k in ("finishattempt", "timeup", "submitallandfinish")
        )
        goes_to_summary = "-1" in params.get("nextpage", []) and any(
            value not in {"0", ""} for value in params.get("next", ["0"])
        )
        if goes_to_summary:
            final = True
        # Unknown quiz write endpoints are blocked rather than guessed.
        if quiz_mutation and not u.path.endswith(
            ("/processattempt.php", "/startattempt.php", "/autosave.ajax.php")
        ):
            final = True
        if quiz_mutation and opaque_body:
            final = True
        ambiguous_attempt = len(set(params.get("attempt", []))) > 1
        if ambiguous_attempt:
            final = True
        if self.active_attempt and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if u.path.endswith("/lib/ajax/service.php"):
                # Permit known read-only course discovery, not arbitrary Moodle services.
                readonly = {
                    "core_course_get_enrolled_courses_by_timeline_classification",
                    "core_course_get_enrolled_courses_with_action_events_by_timeline_classification",
                    "core_course_get_recent_courses",
                    "core_get_string",
                    "core_get_strings",
                    "core_get_component_strings",
                    "core_output_load_template",
                    "core_output_load_template_with_dependencies",
                    "core_session_time_remaining",
                    "core_session_touch",
                }
                final = not (
                    isinstance(decoded, list)
                    and decoded
                    and all(isinstance(item, dict) and item.get("methodname") in readonly for item in decoded)
                )
            if attempt and attempt != self.active_attempt:
                final = True
        completing = (
            (
                quiz_mutation
                or any(
                    any(v.lower() not in {"0", "", "false"} for v in params.get(k, ["0"]))
                    for k in ("finishattempt", "timeup", "submitallandfinish")
                )
            )
            and final
            and not (
                u.path.endswith("/processattempt.php")
                and goes_to_summary
                and not any(
                    any(v.lower() not in {"0", "", "false"} for v in params.get(k, ["0"]))
                    for k in ("finishattempt", "timeup", "submitallandfinish")
                )
            )
        )
        if final and (
            ambiguous_attempt or not self.allowed(attempt) or (completing and self.final_write_used)
        ):
            self.blocked += 1
            self.last_block_reason = "OPAQUE_FORM" if opaque_body else "FINAL_OR_AMBIGUOUS_REQUEST"
            quiz_ajax = isinstance(decoded, list) and any(
                isinstance(item, dict) and str(item.get("methodname", "")).startswith("mod_quiz_")
                for item in decoded
            )
            critical = quiz_mutation or u.path.endswith("/mod/quiz/summary.php") or quiz_ajax
            if critical:
                self.blocked_quiz += 1
            self.last_block_context = {
                "endpoint": "QUIZ_WRITE"
                if quiz_mutation
                else "QUIZ_AJAX"
                if quiz_ajax
                else "OTHER_AJAX"
                if u.path.endswith("/lib/ajax/service.php")
                else "NAVIGATION",
                "critical": critical,
                "opaque": opaque_body,
                "attempt_present": bool(attempt),
                "attempt_matches_active": attempt == self.active_attempt,
                "permit_active": self.allowed(self.attempt_id),
                "permit_matches_request": self.allowed(attempt),
                "final_write_used": self.final_write_used,
                "summary_navigation": goes_to_summary,
            }
            await route.abort("blockedbyclient")
        else:
            if completing:
                self.final_write_used = True
            await route.continue_()


class Browser:
    def __init__(self, settings, selectors, checkpoint=None, *, read_only=False, inspection_attempt=None):
        self.settings = settings
        self.selectors = selectors
        self.checkpoint = checkpoint or self.noop
        self.lock = asyncio.Lock()
        self.gate = FinalGate()
        self.context = None
        self.final_connection_check = None
        self.final_dialog_page = None
        self.local_panel_origin = None
        self.read_only = read_only
        self.inspection_attempt = inspection_attempt
        if read_only:
            self.gate.active_attempt = "READ_ONLY"

    async def noop(self):
        pass

    async def open(self, *, headless=False):
        # Debug protocols can log fill() values. Fail before reading credentials.
        if any(os.environ.get(k) for k in ("DEBUG", "PWDEBUG", "PLAYWRIGHT_DEBUG_LOG")):
            raise RuntimeError("Отключите DEBUG/PWDEBUG перед запуском")
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            str(self.settings.data / f"browser-profile-{self.settings.browser_channel}"),
            channel=None if self.settings.browser_channel == "chromium" else self.settings.browser_channel,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            service_workers="block",
        )
        self.context.set_default_timeout(15000)
        # Chromium may discard session cookies even with a persistent profile.
        # Restore locally saved cookies explicitly; this data never leaves the browser module.
        session_file = self.settings.data / "session.json"
        if session_file.exists():
            try:
                session = json.loads(session_file.read_text(encoding="utf-8"))
                await self.context.add_cookies(session.get("cookies", []))
            except Exception:
                # A corrupt/expired snapshot falls back to normal runtime authentication.
                pass
        base = urlsplit(self.settings.moodle_url)

        async def safe_route(route):
            target = urlsplit(route.request.url)
            if (target.scheme, target.netloc) == self.local_panel_origin:
                await route.continue_()
                return
            if self.read_only and "/mod/quiz/" in target.path:
                inspect_existing = (
                    target.path.endswith("/mod/quiz/attempt.php")
                    and route.request.method == "GET"
                    and self.inspection_attempt
                    and parse_qs(target.query).get("attempt") == [self.inspection_attempt]
                )
                if (
                    target.path.endswith(
                        ("/startattempt.php", "/attempt.php", "/processattempt.php", "/summary.php")
                    )
                    and not inspect_existing
                ) or route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                    self.gate.blocked += 1
                    await route.abort("blockedbyclient")
                    return
            if (target.scheme, target.netloc) != (base.scheme, base.netloc):
                if route.request.is_navigation_request():
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()
                return
            await self.gate.route(route)

        await self.context.route("**/*", safe_route)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.monitor_page = await self.context.new_page()
        self.context.on("dialog", lambda dialog: asyncio.create_task(self.handle_dialog(dialog)))

    async def handle_dialog(self, dialog):
        # Native confirm is permitted only inside an explicitly authorized final click.
        page = dialog.page
        current, expected = urlsplit(page.url), urlsplit(self.settings.moodle_url)
        attempt = parse_qs(current.query).get("attempt", [""])[0]
        authorized = (
            dialog.type == "confirm"
            and self.final_dialog_page is page
            and (current.scheme, current.netloc) == (expected.scheme, expected.netloc)
            and current.path in {"/mod/quiz/attempt.php", "/mod/quiz/summary.php"}
            and self.gate.allowed(attempt)
            and not self.gate.final_write_used
        )
        if authorized:
            await dialog.accept()
        else:
            await dialog.dismiss()

    async def open_panel(self, url):
        target = urlsplit(url)
        if target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port:
            raise ValueError("INVALID_LOCAL_PANEL")
        self.local_panel_origin = (target.scheme, target.netloc)
        self.panel_page = await self.context.new_page()
        await self.panel_page.goto(url, wait_until="domcontentloaded")
        await self.panel_page.bring_to_front()
        return self.panel_page

    async def close(self):
        self.gate.revoke()
        if self.context:
            try:
                await self.save_session()
            except Exception:
                pass
            await self.context.close()
        if hasattr(self, "pw"):
            await self.pw.stop()

    async def save_session(self):
        session = await self.context.storage_state()
        host = urlsplit(self.settings.moodle_url).hostname
        session["cookies"] = [
            cookie
            for cookie in session["cookies"]
            if host == cookie["domain"].lstrip(".") or host.endswith("." + cookie["domain"].lstrip("."))
        ]
        # Moodle authentication uses cookies; do not export unrelated local storage.
        session.pop("origins", None)
        path = self.settings.data / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(session), encoding="utf-8")
        temp.replace(path)

    async def move(self, locator):
        await self.checkpoint()
        await locator.scroll_into_view_if_needed()
        box = await locator.bounding_box()
        if not box:
            raise RuntimeError("ELEMENT_NOT_VISIBLE")
        # Presentation pacing only. No fingerprint changes or anti-bot bypass.
        await locator.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)
        await asyncio.sleep(random.uniform(self.settings.action_delay_min, self.settings.action_delay_max))
        await self.checkpoint()

    async def click(self, locator, *, final=False):
        await self.move(locator)
        if final:
            if self.final_connection_check:
                try:
                    await asyncio.wait_for(self.final_connection_check(), 5)
                except Exception:
                    self.gate.revoke()
                    raise RuntimeError("TELEGRAM_OFFLINE_FINAL_BLOCKED") from None
            attempt = parse_qs(urlsplit(locator.page.url).query).get("attempt", [""])[0]
            if not self.gate.allowed(attempt):
                raise RuntimeError("FINAL_AUTHORIZATION_REVOKED")
        else:
            label = await locator.evaluate(
                "e => [e.name,e.textContent,e.value,e.href,e.formAction].join(' ')"
            )
            if re.search(r"finish|summary\.php|законч|заверш|відправити|отправить", label, re.I):
                raise RuntimeError("FINAL_ACTION_REQUIRES_CONFIRMATION")
            if await locator.evaluate("e => !!e.form?.querySelector('[name=finishattempt][value=\"1\"]')"):
                raise RuntimeError("FINAL_FORM_REQUIRES_CONFIRMATION")
            if await locator.evaluate(
                "e => e.name === 'next' && e.form?.querySelector('[name=nextpage]')?.value === '-1'"
            ):
                raise RuntimeError("FINAL_NAVIGATION_REQUIRES_CONFIRMATION")
        try:
            if final:
                self.final_dialog_page = locator.page
            await locator.click()
        finally:
            self.final_dialog_page = None

    async def check(self, locator, checked):
        await self.move(locator)
        if await locator.is_checked() != checked:
            await locator.click()
        if await locator.is_checked() != checked:
            raise RuntimeError("ANSWER_NOT_APPLIED")

    async def fill(self, locator, text):
        await self.move(locator)
        await locator.fill(text)
        await locator.press("Tab")
        if await locator.input_value() != text:
            raise RuntimeError("ANSWER_NOT_APPLIED")

    async def drag(self, source, target, *, target_y=0.5, target_x=0.5, source_x=0.5, source_y=0.5):
        await self.move(source)
        start = await source.bounding_box()
        end = await target.bounding_box()
        if not start or not end:
            raise RuntimeError("ELEMENT_NOT_VISIBLE")
        viewport = source.page.viewport_size
        if not viewport or end["y"] < 0 or end["y"] + end["height"] > viewport["height"]:
            await self.checkpoint()
            await source.drag_to(
                target,
                target_position={"x": end["width"] * target_x, "y": end["height"] * target_y},
                source_position={"x": start["width"] * source_x, "y": start["height"] * source_y},
            )
            return
        mouse = source.page.mouse
        x, y = start["x"] + start["width"] * source_x, start["y"] + start["height"] * source_y
        tx, ty = end["x"] + end["width"] * target_x, end["y"] + end["height"] * target_y
        await mouse.move(x, y)
        await self.checkpoint()
        await mouse.down()
        try:
            for step in range(1, 21):
                await self.checkpoint()
                fraction = step / 20
                await mouse.move(x + (tx - x) * fraction, y + (ty - y) * fraction)
                await asyncio.sleep(0.015)
            await self.checkpoint()
        finally:
            await mouse.up()
