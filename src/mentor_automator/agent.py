import asyncio
import json
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .errors import ERRORS, STAGES, safe_error
from .models import Answer, State
from .questions import QuestionError


class Agent:
    def __init__(self, settings, store, moodle, llm, events):
        self.s, self.store, self.moodle, self.llm, self.events = settings, store, moodle, llm, events
        self.browser = moodle.b
        self.browser.checkpoint = self.checkpoint
        self.ready = asyncio.Event()
        self.ready.set()
        self.task = None
        self.run_id = None
        self.control_lock = asyncio.Lock()
        self.notify_final = None
        self.notify_manual = None
        self.manual_token = None

    async def checkpoint(self):
        await self.ready.wait()

    async def state(self, value, action=None):
        if value in {State.STARTING, State.RUNNING} and not self.ready.is_set():
            value = State.PAUSED
        if self.run_id:
            await self.store.update_run(self.run_id, state=value)
        self.events.publish(action, state=str(value), run_id=self.run_id or "")

    async def stage(self, value):
        if value not in STAGES:
            raise ValueError("UNKNOWN_STAGE")
        await self.store.update_run(self.run_id, stage=value)

    def busy(self):
        return self.task is not None and not self.task.done()

    async def solve_question(self, question, index):
        try:
            if question.visual and self.s.llm_vision:
                await self.stage("READING_IMAGE")
            image = await self.moodle.image(index) if question.visual and self.s.llm_vision else None
        except QuestionError:
            return Answer(
                choices=[],
                text="",
                explanation="Изображение или область вопроса требуют ручной проверки",
                confidence=0.0,
                skip=True,
            )
        self.events.publish("Ожидаю ответ Codex" if self.s.llm_backend == "codex" else "Ожидаю ответ модели")
        await self.stage("MODEL")
        return await self.llm.solve(question, image)

    async def start(self, quiz_id):
        async with self.control_lock:
            if self.busy():
                raise RuntimeError("AGENT_BUSY")
            quiz = await self.store.quiz(quiz_id)
            if not quiz or quiz.available is False:
                raise RuntimeError("QUIZ_NOT_AVAILABLE")
            current = await self.store.run(self.run_id) if self.run_id else None
            if (
                current
                and current["quiz_id"] == quiz_id
                and not current["attempt_id"]
                and current["state"] in {State.ERROR, State.RECOVERY_REQUIRED}
            ):
                await self.store.update_run(self.run_id, state=State.STARTING, error_code=None)
            else:
                self.run_id = await self.store.create_run(quiz_id)
            self.events.publish(filled=0, skipped=0, question="", answer="", confidence=None, explanation="")
            self.ready.set()
            self.task = asyncio.create_task(self.run(quiz, new=True))
            return self.run_id

    async def pause(self, reason="Пауза по команде пользователя"):
        self.manual_token = None
        self.ready.clear()
        self.browser.gate.revoke()
        if self.run_id:
            await self.store.revoke(self.run_id)
            row = await self.store.run(self.run_id)
            if row["state"] == State.SUBMITTING:
                if self.busy():
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
                await self.state(State.RECOVERY_REQUIRED, "Завершение прервано. Проверьте сайт вручную")
                return
            # Waiting must stay waiting; no resume command may convert it into submission.
            if row["state"] not in {
                State.AWAITING_FINAL_CONFIRMATION,
                State.COMPLETED,
                State.STOPPED,
                State.PLATFORM_CLOSED,
            }:
                await self.state(State.PAUSED, reason)
            else:
                self.events.publish(reason)

    async def resume(self, rid=None, *, review=False):
        async with self.control_lock:
            rid = rid or self.run_id
            if not rid:
                latest = await self.store.latest_run()
                rid = latest["id"] if latest else None
            row = await self.store.run(rid) if rid else None
            if not row or row["state"] in {State.COMPLETED, State.STOPPED, State.PLATFORM_CLOSED}:
                raise RuntimeError("NO_RECOVERABLE_RUN")
            self.run_id = rid
            self.manual_token = None
            await self.store.revoke(rid)
            if self.busy():
                if row["state"] != State.PAUSED:
                    raise RuntimeError("AGENT_BUSY")
                self.ready.set()
                await self.state(State.RUNNING, "Выполнение возобновлено")
                return
            if not row["attempt_id"] or not row["url"]:
                quiz = await self.store.quiz(row["quiz_id"])
                self.ready.set()
                self.task = asyncio.create_task(self.run(quiz, new=True, allow_new=False))
                return
            quiz = await self.store.quiz(row["quiz_id"])
            self.ready.set()
            self.task = asyncio.create_task(self.run(quiz, new=False, review=review))

    async def stop(self, rid=None):
        async with self.control_lock:
            self.manual_token = None
            if rid and rid != self.run_id:
                raise RuntimeError("STALE_CONTROL")
            self.browser.gate.revoke()
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            if not self.run_id:
                row = await self.store.latest_run()
                self.run_id = row["id"] if row else None
            if self.run_id:
                await self.store.revoke(self.run_id)
                await self.state(State.STOPPED, "Агент остановлен; попытка на сайте не отправлена программой")
            self.ready.set()

    async def prepare_finish(self, rid):
        """Explicit recovery of final waiting only. Never fill, start or submit an attempt."""
        async with self.control_lock:
            if self.busy():
                raise RuntimeError("AGENT_BUSY")
            row = await self.store.run(rid)
            final_stages = {
                "FINAL_CONFIRMATION",
                "PREPARING_FINISH",
                "SUMMARY_READY",
                "CONFIRMING_FINISH",
                "VERIFYING_FINISH",
                "SUBMITTING",
            }
            if (
                not row
                or not row["attempt_id"]
                or not row["url"]
                or row["stage"] not in final_stages
                or row["state"] in {State.COMPLETED, State.PLATFORM_CLOSED}
            ):
                raise RuntimeError("NO_FINAL_WAIT_TO_RECOVER")
            other = await self.store.one(
                "SELECT id FROM runs WHERE id<>? AND state NOT IN (?,?,?) LIMIT 1",
                (rid, State.COMPLETED, State.PLATFORM_CLOSED, State.STOPPED),
            )
            if other:
                raise RuntimeError("ACTIVE_RUN_EXISTS")
            self.run_id = rid
            self.ready.set()
            self.browser.gate.revoke()
            await self.store.revoke(rid)
            self.task = asyncio.create_task(self.restore_final_wait(row))

    async def restore_final_wait(self, row):
        try:
            async with self.browser.lock:
                await self.stage("RESTORING_FINAL_WAIT")
                await self.state(State.STARTING, "Проверяю сохранённую попытку; ответы не изменяются")
                await self.browser.page.goto(row["url"], wait_until="domcontentloaded")
                await self.moodle.assert_logged_in(self.browser.page)
                if await self.moodle.is_submitted():
                    await self.state(State.PLATFORM_CLOSED, "Moodle уже показывает закрытую попытку")
                    return
                if self.moodle.attempt_identity()[0] != row["attempt_id"]:
                    raise RuntimeError("FINAL_ATTEMPT_MISMATCH")
                self.browser.gate.active_attempt = row["attempt_id"]
                await self.moodle.questions()
                filled, skipped = await self.store.counts(self.run_id)
                self.events.publish(filled=filled, skipped=skipped, timer=await self.moodle.timer())
                await self.store.update_run(self.run_id, error_code=None, error_context=None)
                await self.stage("FINAL_CONFIRMATION")
                await self.state(
                    State.AWAITING_FINAL_CONFIRMATION, "Попытка открыта. Ожидаю нового подтверждения отправки"
                )
                if self.notify_final:
                    await self.notify_final(self.run_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.browser.gate.revoke()
            await self.store.revoke(self.run_id)
            code = safe_error(error, self.browser)
            await self.store.update_run(self.run_id, error_code=code)
            current = await self.store.run(self.run_id)
            if current["state"] != State.AWAITING_FINAL_CONFIRMATION:
                await self.state(State.RECOVERY_REQUIRED, ERRORS[code])
            else:
                self.events.publish("Telegram недоступен. Ожидание сохранено; отправка запрещена")

    async def run(self, quiz, *, new, review=False, allow_new=True):
        try:
            async with self.browser.lock:
                await self.store.update_run(self.run_id, error_code=None, error_context=None)
                await self.state(State.STARTING, "Открываю тест в браузере")
                await self.checkpoint()
                if new:
                    attempt, url = await self.moodle.start(quiz, on_stage=self.stage, allow_new=allow_new)
                    await self.store.update_run(self.run_id, attempt_id=attempt, url=url)
                else:
                    await self.stage("OPENING_QUIZ")
                    row = await self.store.run(self.run_id)
                    u = urlsplit(row["url"])
                    args = parse_qs(u.query)
                    args["page"] = ["0"]
                    url = urlunsplit((u.scheme, u.netloc, u.path, urlencode(args, doseq=True), ""))
                    await self.browser.page.goto(url, wait_until="domcontentloaded")
                    if await self.moodle.is_submitted():
                        await self.state(State.PLATFORM_CLOSED, "Платформа уже закрыла попытку")
                        return
                    if self.moodle.attempt_identity()[0] != row["attempt_id"]:
                        raise RuntimeError("RECOVERY_ATTEMPT_MISMATCH")
                self.browser.gate.active_attempt = (await self.store.run(self.run_id))["attempt_id"]
                await self.state(State.RUNNING, "Читаю вопросы")
                visited = set()
                while True:
                    await self.checkpoint()
                    _, url = self.moodle.attempt_identity()
                    if url in visited:
                        raise RuntimeError("NAVIGATION_LOOP")
                    visited.add(url)
                    await self.store.update_run(self.run_id, url=url)
                    await self.stage("READING_QUESTIONS")
                    questions = await self.moodle.questions()
                    for question, index in questions:
                        await self.checkpoint()
                        self.events.publish(
                            "Читаю текущий вопрос",
                            question=question.text,
                            answer="",
                            confidence=None,
                            explanation="",
                        )
                        existing = await self.store.one(
                            "SELECT * FROM progress WHERE run_id=? AND question_id=?",
                            (self.run_id, question.id),
                        )
                        if (
                            existing
                            and existing["filled"]
                            and existing["fingerprint"] == question.fingerprint
                            and not review
                        ):
                            # Re-apply known validated answer, then verify actual DOM state during recovery.
                            answer = Answer.model_validate(json.loads(existing["data"]))
                        elif question.kind == "unsupported":
                            answer = Answer(
                                choices=[],
                                text="",
                                explanation="Тип вопроса требует ручного ответа",
                                confidence=0.0,
                                skip=True,
                            )
                        else:
                            answer = await self.solve_question(question, index)
                        if (
                            answer.skip or answer.confidence < self.s.min_confidence
                        ) and self.s.uncertain_answer_policy == "pause":
                            await self.stage("MANUAL_INPUT")
                            self.events.publish(explanation=answer.explanation, confidence=answer.confidence)
                            await self.pause(
                                "Нужен ручной ответ: неизвестный виджет, изображение или недостаточная уверенность"
                            )
                            self.manual_token = secrets.token_hex(6)
                            if self.notify_manual:
                                await self.notify_manual(self.run_id, question)
                        await self.checkpoint()
                        filled = not answer.skip and answer.confidence >= self.s.min_confidence
                        if filled:
                            await self.stage("APPLYING_ANSWER")
                            await self.moodle.apply(question, index, answer)
                        await self.store.progress(
                            self.run_id, question.id, question.fingerprint, answer.model_dump(), filled
                        )
                        n, skipped = await self.store.counts(self.run_id)
                        self.events.publish(
                            "Ответ выбран и проверен в интерфейсе" if filled else "Вопрос пропущен",
                            filled=n,
                            skipped=skipped,
                            answer=answer.display(question),
                            confidence=answer.confidence,
                            explanation=answer.explanation,
                        )
                    await self.checkpoint()
                    await self.stage("NEXT_PAGE")
                    if not await self.moodle.next_page():
                        break
                await self.stage("FINAL_CONFIRMATION")
                await self.state(
                    State.AWAITING_FINAL_CONFIRMATION,
                    "Все доступные страницы обработаны. Ожидаю отдельного подтверждения в Telegram",
                )
                if self.notify_final:
                    await self.notify_final(self.run_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.browser.gate.revoke()
            await self.store.revoke(self.run_id)
            code = safe_error(error, self.browser)
            await self.store.update_run(self.run_id, error_code=code)
            if code == "ATTEMPT_ALREADY_CLOSED":
                await self.state(State.PLATFORM_CLOSED, ERRORS[code])
                return
            row = await self.store.run(self.run_id)
            if row["state"] != State.AWAITING_FINAL_CONFIRMATION:
                await self.state(
                    State.ERROR,
                    "Ошибка: " + ERRORS[code],
                )
            else:
                self.events.publish("Telegram недоступен. Ожидание сохранено; завершение запрещено")

    async def confirm(self, rid, token):
        async with self.control_lock:
            if rid != self.run_id or self.busy() or not self.ready.is_set():
                raise RuntimeError("CONFIRM_NOT_READY")
            row = await self.store.run(rid)
            if not row or row["state"] != State.AWAITING_FINAL_CONFIRMATION:
                raise RuntimeError("NOT_AWAITING_CONFIRMATION")
            attempt, _ = self.moodle.attempt_identity()
            if attempt != row["attempt_id"]:
                raise RuntimeError("ATTEMPT_MISMATCH")
            if not await self.store.consume_confirmation(rid, token, attempt):
                raise RuntimeError("CONFIRMATION_EXPIRED_OR_USED: /review для нового подтверждения")
            self.task = asyncio.create_task(self.submit(attempt))

    async def submit(self, attempt):
        try:
            async with self.browser.lock:
                await self.state(State.SUBMITTING, "Получено одноразовое подтверждение для этой попытки")
                self.browser.gate.arm(attempt)
                await self.moodle.finish(attempt, on_stage=self.stage)
                await self.state(
                    State.COMPLETED, "Moodle подтвердил отправку попытки; правильность ответов не проверена"
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = safe_error(error, self.browser)
            await self.store.update_run(
                self.run_id, error_code=code, error_context=json.dumps(self.browser.gate.last_block_context)
            )
            await self.state(
                State.RECOVERY_REQUIRED,
                ERRORS[code] + " Автоматического повтора отправки нет.",
            )
        finally:
            self.browser.gate.revoke()

    async def watch_timer(self):
        warned = set()
        while True:
            await asyncio.sleep(1)
            if not self.run_id:
                continue
            try:
                row = await self.store.run(self.run_id)
                # Until start() binds an ID, the browser may still show the previous review page.
                # Never classify a new run using the old page.
                if not row["attempt_id"]:
                    continue
                if row["state"] in {State.STOPPED, State.COMPLETED, State.PLATFORM_CLOSED}:
                    continue
                if row["state"] != State.SUBMITTING and await self.moodle.is_submitted():
                    if self.busy():
                        self.task.cancel()
                        await asyncio.gather(self.task, return_exceptions=True)
                    self.browser.gate.revoke()
                    await self.store.revoke(self.run_id)
                    await self.state(State.PLATFORM_CLOSED, "Попытка закрыта платформой или вручную")
                    continue
                timer = await self.moodle.timer()
                self.events.publish(timer=timer)
                parts = re.fullmatch(r"\s*(\d+):(\d{2})(?::(\d{2}))?\s*", timer)
                seconds = None
                if parts:
                    seconds = (
                        (int(parts[1]) * 3600 + int(parts[2]) * 60 + int(parts[3]))
                        if parts[3]
                        else int(parts[1]) * 60 + int(parts[2])
                    )
                for threshold in (300, 60):
                    key = (self.run_id, threshold)
                    if seconds is not None and seconds <= threshold and key not in warned:
                        warned.add(key)
                        self.events.publish(
                            f"Осталось {timer}. Moodle может автоматически закрыть попытку", warning=timer
                        )
            except Exception:
                # Lost browser must never leave a live approval.
                self.browser.gate.revoke()
                await self.store.revoke(self.run_id)
                if self.busy():
                    await self.pause("Ошибка браузера. Выполнение приостановлено")

    async def shutdown(self):
        if self.busy():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.run_id:
            await self.store.revoke(self.run_id)
        self.browser.gate.revoke()
