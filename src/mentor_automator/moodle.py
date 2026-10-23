import asyncio
import re
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from .markers import apply_markers
from .models import Quiz
from .privacy import clean, public_url
from .questions import QuestionActions, extract, screenshot


class StructureError(RuntimeError):
    pass


class Moodle:
    def __init__(self, settings, selectors, browser):
        self.s, self.sel, self.b = settings, selectors, browser
        self.last_scan_stats = {}

    def url(self, path):
        return self.s.moodle_url.rstrip("/") + path

    async def links(self, page, key, path, keys):
        found = {}
        for link in await page.locator(self.sel[key]).all():
            href = await link.get_attribute("href")
            if not href:
                continue
            from urllib.parse import urljoin

            try:
                safe = public_url(urljoin(page.url, href), self.s.moodle_url, path, keys)
                found[safe] = clean(await link.inner_text(), 300).strip()
            except ValueError:
                continue
        return found

    async def scan(self):
        """Read-only navigation: never click start, continue, or attempt links."""
        page = self.b.monitor_page
        await page.goto(self.url("/my/courses.php"), wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
        await self.assert_logged_in(page)
        courses = await self.links(page, "courses", "/course/view.php", {"id"})
        seen_pages = set()
        for _ in range(100):
            signature = (page.url, tuple(sorted(courses)))
            if signature in seen_pages:
                break
            seen_pages.add(signature)
            controls = page.locator(self.sel["course_next"] + "," + self.sel["course_more"])
            enabled = [
                control
                for control in await controls.all()
                if await control.is_visible() and await control.is_enabled()
            ]
            if not enabled:
                break
            # Only a course-list pagination control, never an activity/attempt control.
            await enabled[0].click()
            await page.wait_for_timeout(750)
            await self.assert_logged_in(page)
            courses.update(await self.links(page, "courses", "/course/view.php", {"id"}))
        for source in ("/my/", "/"):
            if source == "/" and courses:
                continue
            await page.goto(self.url(source), wait_until="domcontentloaded")
            courses.update(await self.links(page, "courses", "/course/view.php", {"id"}))
        for cid in self.s.course_ids.split(","):
            if cid.strip().isdigit():
                courses.setdefault(self.url(f"/course/view.php?id={cid.strip()}"), f"Курс {cid.strip()}")
        if not courses:
            raise StructureError("COURSES_NOT_FOUND: задайте COURSE_IDS или селектор courses")
        self.last_scan_stats = {"courses": len(courses)}
        quizzes = {}
        for course_url, course_name in courses.items():
            await page.goto(course_url, wait_until="domcontentloaded")
            await self.assert_logged_in(page)
            found = await self.links(page, "quizzes", "/mod/quiz/view.php", {"id"})
            for url, name in found.items():
                qid = parse_qs(urlsplit(url).query).get("id", [""])[0]
                if qid:
                    quizzes[qid] = Quiz(id=qid, course=course_name, title=name or f"Тест {qid}", url=url)
            # Moodle quiz index exposes modules in collapsed/lazy course sections.
            cid = parse_qs(urlsplit(course_url).query)["id"][0]
            await page.goto(self.url(f"/mod/quiz/index.php?id={cid}"), wait_until="domcontentloaded")
            for url, name in (await self.links(page, "quizzes", "/mod/quiz/view.php", {"id"})).items():
                qid = parse_qs(urlsplit(url).query).get("id", [""])[0]
                if qid:
                    quizzes[qid] = Quiz(id=qid, course=course_name, title=name or f"Тест {qid}", url=url)
        for quiz in quizzes.values():
            await page.goto(quiz.url, wait_until="domcontentloaded")
            await self.assert_logged_in(page)
            await self.metadata(page, quiz)
        self.last_scan_stats["quizzes"] = len(quizzes)
        return list(quizzes.values())

    async def assert_logged_in(self, page):
        if "/login/" in urlsplit(page.url).path or await page.locator(self.sel["password"]).count():
            raise StructureError("SESSION_EXPIRED: выполните вход в видимом браузере")

    async def metadata(self, page, quiz):
        regions = await page.locator(self.sel["quiz_info"]).all_inner_texts()
        # Dedicated content regions only; never read body/profile/session fields.
        lines = [clean(line.strip(), 600) for text in regions for line in text.splitlines() if line.strip()]

        def matching(pattern):
            return " · ".join(dict.fromkeys(line for line in lines if re.search(pattern, line, re.I)))[:1200]

        quiz.deadline = matching(r"clos|due|закр|дедлайн|термін|срок|кінцев") or "Не указан в интерфейсе"
        quiz.time_limit = (
            matching(r"time limit|обмеження.*час|ограничение.*врем|ліміт часу") or "Не указан в интерфейсе"
        )
        quiz.restrictions = matching(
            r"not available|unavailable|недоступ|не доступ|password|парол|відкри|откр|opens|attempts? allowed|дозволено спроб|попыток разрешено"
        )
        start = page.locator(self.sel["start"])
        cont = page.locator(self.sel["continue"])
        quiz.available = None
        for button in await start.all() + await cont.all():
            if await button.is_visible():
                quiz.available = await button.is_enabled()
                if quiz.available:
                    break
        if quiz.available is None and matching(
            r"not available|no more attempts|недоступ|спроб.*не залиш|більше.*спроб|попыт.*исчерп"
        ):
            quiz.available = False
        rem = matching(r"remaining|залиш|остал")
        m = re.search(r"(?:remaining|залиш\w*|остал\w*)\D{0,25}(\d+)", rem, re.I)
        if not m:
            m = re.search(r"(\d+)\s*(?:attempts? remaining|спроб.*залиш|попыт.*остал)", rem, re.I)
        if m:
            quiz.attempts_remaining = int(m[1])
        else:
            allowed = matching(r"attempts allowed|дозволено спроб|кількість спроб|попыток разрешено")
            total = re.search(r"(?:allowed|спроб|разрешено)\D{0,10}(\d+)", allowed, re.I)
            rows = page.locator(".quizattemptsummary tbody tr")
            if total and await rows.count():
                quiz.attempts_remaining = max(0, int(total[1]) - await rows.count())

    async def start(self, quiz, *, on_stage=None, allow_new=True):
        async def stage(value):
            if on_stage:
                await on_stage(value)

        page = self.b.page
        await stage("OPENING_QUIZ")
        await page.bring_to_front()
        await page.goto(quiz.url, wait_until="domcontentloaded")
        await self.assert_logged_in(page)
        await stage("LOCATING_START")
        existing_pages = set(self.b.context.pages)
        # Existing active attempt always takes priority over consuming another attempt.
        for key in ("continue", "start") if allow_new else ("continue",):
            for loc in await page.locator(self.sel[key]).all():
                if await loc.is_visible() and await loc.is_enabled():
                    await stage("OPENING_PREFLIGHT")
                    await self.b.click(loc)
                    break
            else:
                continue
            break
        else:
            raise StructureError("START_NOT_FOUND" if allow_new else "NO_EXISTING_ATTEMPT")
        # Moodle may render a delayed modal or open its configured attempt popup.
        # Never press a second start button when the first navigation is uncertain.
        deadline = asyncio.get_running_loop().time() + 30
        confirmed = False
        origin = urlsplit(self.s.moodle_url)
        await stage("WAITING_FOR_ATTEMPT")
        while asyncio.get_running_loop().time() < deadline:
            await self.b.checkpoint()
            for popup in self.b.context.pages:
                if popup in existing_pages or popup.is_closed():
                    continue
                url = urlsplit(popup.url)
                if (url.scheme, url.netloc) == (origin.scheme, origin.netloc) and url.path.endswith(
                    ("/mod/quiz/attempt.php", "/mod/quiz/startattempt.php")
                ):
                    page = self.b.page = popup
                    await page.bring_to_front()
            if page.is_closed():
                raise StructureError("BROWSER_CLOSED")
            if urlsplit(page.url).path.endswith("/mod/quiz/attempt.php"):
                await page.wait_for_load_state("domcontentloaded")
                return self.attempt_identity()
            if await page.locator("input[type=password]:visible").count():
                raise StructureError("PREFLIGHT_REQUIRES_MANUAL_INPUT")
            if not confirmed:
                buttons = [
                    loc
                    for loc in await page.locator(self.sel["preflight"]).all()
                    if await loc.is_visible() and await loc.is_enabled()
                ]
                if buttons:
                    await stage("CONFIRMING_START")
                    confirmed = True
                    await self.b.click(buttons[0])
                    await stage("WAITING_FOR_ATTEMPT")
            await asyncio.sleep(0.1)
        raise StructureError("ATTEMPT_LOAD_TIMEOUT")

    def attempt_identity(self):
        safe = public_url(
            self.b.page.url, self.s.moodle_url, "/mod/quiz/attempt.php", {"attempt", "cmid", "page"}
        )
        attempt = parse_qs(urlsplit(safe).query).get("attempt", [""])[0]
        if not attempt:
            raise StructureError("ATTEMPT_NOT_FOUND")
        return attempt, safe

    async def questions(self):
        await self.b.page.wait_for_load_state("domcontentloaded")
        await self.assert_logged_in(self.b.page)
        # URL navigation can commit before Moodle has rendered any question nodes.
        # Locator.all()/count() are snapshots and must not be used as readiness waits.
        try:
            await self.b.page.locator(self.sel["questions"]).first.wait_for(state="visible", timeout=15000)
        except BrowserTimeout:
            raise StructureError("QUESTIONS_NOT_FOUND") from None
        result = []
        descriptions = 0
        for index, loc in enumerate(await self.b.page.locator(self.sel["questions"]).all()):
            question = await extract(
                loc, {"text": self.sel["question_text"], "answers": self.sel["answer_region"]}
            )
            if question.kind == "description":
                descriptions += 1
                continue
            if not question.text:
                raise StructureError("QUESTION_TEXT_NOT_FOUND")
            if not re.fullmatch(r"[\w:-]{1,150}", question.id):
                raise StructureError("STABLE_QUESTION_ID_NOT_FOUND")
            result.append((question, index))
        if not result and not descriptions:
            raise StructureError("QUESTIONS_NOT_FOUND: выполните diagnostics и адаптируйте selectors.json")
        return result

    def question_locator(self, index):
        return self.b.page.locator(self.sel["questions"]).nth(index)

    async def image(self, index):
        return await screenshot(self.question_locator(index), self.b.checkpoint)

    async def apply(self, question, index, answer):
        fresh = await self.questions()
        current = next((q for q, i in fresh if i == index), None)
        if current is None or current.fingerprint != question.fingerprint:
            raise StructureError("QUESTION_CHANGED: ответ не применён")
        answer.validate_for(question)
        if question.kind in {"single", "multiple"}:
            controls = self.question_locator(index).locator("[data-mentor-basic]")
            if question.kind == "multiple":
                for i in range(await controls.count()):
                    await self.b.check(controls.nth(i), str(i) in answer.choices)
            else:
                await self.b.check(controls.nth(int(answer.choices[0])), True)
            actual = [str(i) for i in range(await controls.count()) if await controls.nth(i).is_checked()]
            if set(actual) != set(answer.choices):
                raise StructureError("ANSWER_VERIFICATION_FAILED")
        elif question.kind in {"text", "numeric"}:
            await self.b.fill(self.question_locator(index).locator("[data-mentor-basic]"), answer.text)
        elif question.kind in {"matching", "embedded"}:
            await QuestionActions(self.b).slots(self.question_locator(index), question, answer)
        elif question.kind == "ordering":
            await QuestionActions(self.b).ordering(self.question_locator(index), question, answer)
        elif question.kind == "dragdrop":
            await QuestionActions(self.b).dragdrop(self.question_locator(index), question, answer)
        elif question.kind == "markers":
            await apply_markers(self.b, self.question_locator(index), answer)
        else:
            raise StructureError("UNSUPPORTED_QUESTION")
        # Moodle autosave is asynchronous; UI verification above is distinct from server persistence.
        await asyncio.sleep(0.35)

    async def next_page(self):
        page = self.b.page
        before = self.attempt_identity()
        for loc in await page.locator(self.sel["next"]).all():
            if not await loc.is_visible() or not await loc.is_enabled():
                continue
            # Last-page "Finish attempt" is never clicked before explicit confirmation.
            label = await loc.evaluate("e => [e.value,e.textContent,e.href].join(' ')")
            if re.search(r"finish|summary\.php|заверш|законч", label, re.I):
                return False
            if await loc.evaluate(
                "e => e.name === 'next' && e.form?.querySelector('[name=nextpage]')?.value === '-1'"
            ):
                return False
            original_url = page.url
            blocked_before = self.b.gate.blocked
            try:
                await self.b.click(loc)
                await page.wait_for_url(
                    lambda url: str(url) != original_url,
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                if await self.is_submitted():
                    raise StructureError("ATTEMPT_ALREADY_CLOSED")
                after = self.attempt_identity()
            except (BrowserError, ValueError) as error:
                if self.b.gate.blocked > blocked_before:
                    raise StructureError("SAVE_REQUEST_BLOCKED") from error
                if not page.is_closed() and await self.is_submitted():
                    raise StructureError("ATTEMPT_ALREADY_CLOSED") from error
                raise StructureError("PAGE_DID_NOT_ADVANCE") from error
            if after[0] != before[0] or after[1] == before[1]:
                raise StructureError("PAGE_DID_NOT_ADVANCE")
            return True
        current_page = int(parse_qs(urlsplit(page.url).query).get("page", ["0"])[0])
        for href in await page.locator(self.sel["navigation"]).evaluate_all("nodes => nodes.map(n=>n.href)"):
            target_page = parse_qs(urlsplit(href).query).get("page", ["0"])[0]
            if target_page.isdigit() and int(target_page) > current_page:
                raise StructureError("NEXT_NOT_FOUND_WITH_REMAINING_PAGES")
        if not any([await x.is_visible() for x in await page.locator(self.sel["finish"]).all()]):
            raise StructureError("CANNOT_IDENTIFY_LAST_PAGE")
        return False

    async def timer(self):
        loc = self.b.page.locator(self.sel["timer"])
        return clean(await loc.first.inner_text(), 100).strip() if await loc.count() else "Не указан"

    async def finish(self, attempt_id, *, on_stage=None):
        page = self.b.page
        blocked_before = self.b.gate.blocked_quiz

        async def stage(value):
            if on_stage:
                await on_stage(value)

        def location():
            current, expected = urlsplit(page.url), urlsplit(self.s.moodle_url)
            if (
                (current.scheme, current.netloc) != (expected.scheme, expected.netloc)
                or parse_qs(current.query).get("attempt") != [attempt_id]
                or current.path
                not in {"/mod/quiz/attempt.php", "/mod/quiz/summary.php", "/mod/quiz/review.php"}
            ):
                raise StructureError("FINAL_ATTEMPT_MISMATCH")
            return current.path

        async def visible(selector):
            return [
                item
                for item in await page.locator(selector).all()
                if await item.is_visible() and await item.is_enabled()
            ]

        async def wait_step(*, summary=False, modal=False, error):
            deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < deadline:
                await self.b.checkpoint()
                if not self.b.gate.allowed(attempt_id):
                    raise StructureError("FINAL_NOT_AUTHORIZED")
                if self.b.gate.blocked_quiz > blocked_before:
                    raise StructureError("SUBMISSION_REQUEST_BLOCKED")
                try:
                    path = location()
                    if path.endswith("/review.php") and await self.is_submitted():
                        return "complete", None
                    if modal:
                        controls = await visible(self.sel["confirm_submit"])
                        if controls:
                            return "modal", controls[-1]
                    if summary and path.endswith("/summary.php"):
                        controls = await visible(self.sel["submit_all"])
                        if controls:
                            return "summary", controls[0]
                except BrowserError:
                    if page.is_closed():
                        raise StructureError("BROWSER_CLOSED") from None
                await asyncio.sleep(0.1)
            if summary and urlsplit(page.url).path.endswith("/summary.php"):
                raise StructureError("SUBMIT_NOT_FOUND")
            raise StructureError(error)

        if not self.b.gate.allowed(attempt_id):
            raise StructureError("FINAL_NOT_AUTHORIZED")
        path = location()
        await stage("PREPARING_FINISH")
        if path.endswith("/review.php") and await self.is_submitted():
            return
        if path.endswith("/attempt.php"):
            # Posting the last-page Next saves the current answers before opening summary.
            last_next = []
            for control in await visible(self.sel["next"]):
                if await control.evaluate("e=>e.form?.querySelector('[name=nextpage]')?.value==='-1'"):
                    last_next.append(control)
            controls = last_next or await visible(self.sel["finish"])
            if not controls:
                raise StructureError("FINISH_NOT_FOUND")
            await self.b.click(controls[0], final=True)
        await stage("SUMMARY_READY")
        step, control = await wait_step(summary=True, modal=True, error="FINISH_NAVIGATION_TIMEOUT")
        if step == "complete":
            return
        if step == "summary":
            await self.b.click(control, final=True)
            step, control = await wait_step(modal=True, error="SUBMISSION_NOT_CONFIRMED")
            if step == "complete":
                return
        await stage("CONFIRMING_FINISH")
        await self.b.click(control, final=True)
        await stage("VERIFYING_FINISH")
        await wait_step(error="SUBMISSION_NOT_CONFIRMED")

    async def is_submitted(self):
        return bool(await self.b.page.locator(self.sel["submitted"]).count())

    async def diagnostics(self, page):
        # Counts only: no URLs, DOM dumps, attribute values, input values, cookies or screenshots.
        counts = {}
        for name, selector in self.sel.values.items():
            if page.is_closed():
                return {"page_closed": True, "selector_counts": counts}
            try:
                counts[name] = await page.locator(selector).count()
            except Exception:
                counts[name] = "unavailable"
        return {"selector_counts": counts}
