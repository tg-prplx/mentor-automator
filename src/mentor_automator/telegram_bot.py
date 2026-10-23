import asyncio
import json
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .errors import ERRORS, STAGES
from .models import Quiz, State


class Authorized(BaseMiddleware):
    def __init__(self, settings):
        self.s = settings

    async def __call__(self, handler, event, data):
        message = getattr(event, "message", None) or event
        user = getattr(event, "from_user", None)
        chat = getattr(message, "chat", None)
        if (
            not user
            or not chat
            or user.id != self.s.telegram_user_id
            or chat.id != self.s.telegram_chat_id
            or chat.type != "private"
        ):
            return None
        return await handler(event, data)


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
        ]
    )


class Telegram:
    def __init__(self, settings, store, agent, events):
        self.s, self.store, self.agent, self.events = settings, store, agent, events
        self.bot = Bot(settings.telegram_bot_token.get_secret_value())
        self.dp = Dispatcher()
        self.router = Router()
        self.router.message.outer_middleware(Authorized(settings))
        self.router.callback_query.outer_middleware(Authorized(settings))
        self.dp.include_router(self.router)
        self.monitor = None
        self.delivery_lock = asyncio.Lock()
        self.message_lock = asyncio.Lock()
        self.next_message_at = 0.0
        self.tests_message_id = None
        self.agent.notify_final = self.final_prompt
        self.agent.notify_manual = self.manual_prompt
        if hasattr(self.agent, "browser"):
            self.agent.browser.final_connection_check = self.bot.get_me
        self.register()

    async def send(self, text, markup=None):
        return await self.message_call(
            self.bot.send_message,
            chat_id=self.s.telegram_chat_id,
            text=text[:4000],
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def message_call(self, operation, **kwargs):
        async with self.message_lock:
            await asyncio.sleep(max(0, self.next_message_at - time.monotonic()))
            try:
                return await operation(**kwargs)
            except TelegramRetryAfter as error:
                self.next_message_at = time.monotonic() + error.retry_after
                raise
            finally:
                self.next_message_at = max(self.next_message_at, time.monotonic() + 1.1)

    async def tests_page(self, scope="available", page=0, *, message_id=None):
        quizzes = await self.store.quizzes()
        available = [quiz for quiz in quizzes if quiz.available is True]
        visible = quizzes if scope == "all" else available
        scope = "all" if scope == "all" else "available"
        pages = max(1, (len(visible) + 4) // 5)
        page = min(max(page, 0), pages - 1)
        lines = [f"Тесты · доступны {len(available)} из {len(quizzes)}", f"Страница {page + 1}/{pages}"]
        rows = []
        for number, quiz in enumerate(visible[page * 5 : page * 5 + 5], start=page * 5 + 1):
            status = {True: "доступен", False: "недоступен", None: "доступность неизвестна"}[quiz.available]
            lines.append(
                f"\n{number}. {quiz.course[:100]}\n{quiz.title[:180]} · {status}\nСрок: {quiz.deadline[:140]}"
            )
            buttons = [InlineKeyboardButton(text=f"Открыть №{number}", url=quiz.url)]
            if quiz.available is True:
                buttons.append(
                    InlineKeyboardButton(text=f"Пройти №{number}", callback_data=f"start:{quiz.id}")
                )
            rows.append(buttons)
        if not visible:
            lines.append("\nДоступных тестов пока не найдено." if quizzes else "\nСписок пуст. /scan")
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text="←", callback_data=f"tests:{scope}:{page - 1}"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="→", callback_data=f"tests:{scope}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append(
            [
                InlineKeyboardButton(text=f"Доступные ({len(available)})", callback_data="tests:available:0"),
                InlineKeyboardButton(text=f"Все ({len(quizzes)})", callback_data="tests:all:0"),
            ]
        )
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        text = "\n".join(lines)
        target = message_id or self.tests_message_id
        if target:
            try:
                await self.message_call(
                    self.bot.edit_message_text,
                    chat_id=self.s.telegram_chat_id,
                    message_id=target,
                    text=text,
                    reply_markup=markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                self.tests_message_id = target
                return
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
                if message_id:
                    return
        message = await self.send(text, markup)
        self.tests_message_id = message.message_id

    async def safe_reply(self, event, operation):
        try:
            await operation()
            return True
        except Exception as error:
            messages = {
                "AGENT_BUSY": "Агент уже выполняет задачу.",
                "ACTIVE_RUN_EXISTS": "Есть незавершённый запуск. Проверьте /status; /stop отменяет его.",
                "QUIZ_NOT_AVAILABLE": "Тест сейчас недоступен. Обновите список через /scan.",
                "NO_RECOVERABLE_RUN": "Нет попытки для восстановления.",
                "NOT_AWAITING_CONFIRMATION": "Эта попытка сейчас не ожидает завершения.",
            }
            code = str(error).partition(":")[0]
            await self.send(
                messages.get(
                    code, ERRORS.get(code, "Команда не выполнена. /status показывает этап и причину сбоя.")
                )
            )
            return False

    def register(self):
        r = self.router

        @r.message(Command("start", "help"))
        async def help_command(message):
            await self.send(
                "Mentor · наблюдаемый агент\n/tests — тесты\n/status — состояние\n"
                "/pause — пауза\n/resume — продолжить/восстановить\n/review — повторная проверка и новое подтверждение\n"
                "/stop — остановить без отправки\n/scan — запросить мониторинг\n/diagnostics — счётчики селекторов\n"
                "Тест запускается только кнопкой «пройти тест». Завершение — только отдельной кнопкой для попытки."
            )

        @r.message(Command("tests"))
        async def tests(message):
            await self.tests_page()

        @r.callback_query(F.data.startswith("tests:"))
        async def tests_callback(callback):
            await callback.answer()
            try:
                _, scope, page = callback.data.split(":", 2)
                await self.tests_page(scope, int(page), message_id=callback.message.message_id)
            except (ValueError, AttributeError):
                return

        @r.message(Command("status"))
        async def status(message):
            row = await self.store.run(self.agent.run_id) if getattr(self.agent, "run_id", None) else None
            row = row or await self.store.status_run()
            n, skipped = await self.store.counts(row["id"]) if row else (0, 0)
            closed = row and row["state"] in {State.COMPLETED, State.PLATFORM_CLOSED}
            timer = "попытка закрыта" if closed else self.events.snapshot["timer"]
            text = (
                f"Состояние: {row['state'] if row else 'IDLE'}\n"
                f"Попытка: {(row['attempt_id'] or 'ID ещё не получен') if row else 'не запускалась'}\n"
                f"Заполнено: {n}; пропущено: {skipped}\n"
                f"Таймер: {timer}\n{self.events.snapshot['monitor']}"
            )
            markup = None
            if row and row.get("stage") and not closed:
                text += "\nЭтап: " + STAGES.get(row["stage"], "не определён")
            if (
                row
                and row.get("error_code")
                and row["state"] in {State.ERROR, State.RECOVERY_REQUIRED, State.PAUSED}
            ):
                text += "\nПричина: " + ERRORS.get(row["error_code"], "Действие не выполнено.")
                if not row["attempt_id"]:
                    markup = keyboard([[("Повторить запуск", f"start:{row['quiz_id']}")]])
                    text += "\nПовторный запуск — только кнопкой ниже. /resume ищет уже существующую попытку."
                else:
                    markup = keyboard([[("Восстановить попытку", f"resume:{row['id']}")]])
            await self.send(text, markup)

        @r.message(Command("pause"))
        async def pause(message):
            await self.agent.pause()
            await self.send("Пауза. Таймер Moodle продолжает идти.")

        @r.message(Command("resume"))
        async def resume(message):
            await self.safe_reply(message, self.agent.resume)

        @r.message(Command("review"))
        async def review(message):
            await self.safe_reply(message, lambda: self.agent.resume(review=True))

        @r.message(Command("stop"))
        async def stop(message):
            await self.agent.stop()
            await self.send(
                "Агент остановлен. Попытка остаётся на сайте; Moodle может закрыть её по времени."
            )

        @r.message(Command("scan"))
        async def scan(message):
            self.monitor.scan_now.set()
            await self.send("Проверка курсов запрошена.")

        @r.message(Command("diagnostics"))
        async def diagnostics(message):
            counts = await self.agent.moodle.diagnostics(self.agent.browser.page)
            counts["notification_history"] = {
                r["status"]: r["n"]
                for r in await self.store.all("SELECT status,COUNT(*) n FROM notifications GROUP BY status")
            }
            await self.send(json.dumps(counts, ensure_ascii=False, indent=2))

        @r.callback_query(F.data.startswith("start:"))
        async def start_callback(callback):
            await callback.answer()
            if not await self.store.claim_telegram_action(callback.id):
                return
            await self.safe_reply(callback, lambda: self.agent.start(callback.data.split(":", 1)[1]))

        @r.callback_query(F.data.startswith("confirm:"))
        async def confirm_callback(callback):
            await callback.answer()
            _, rid, token = callback.data.split(":", 2)
            accepted = await self.safe_reply(callback, lambda: self.agent.confirm(rid, token))
            if accepted:
                try:
                    await self.message_call(
                        self.bot.edit_message_reply_markup,
                        chat_id=self.s.telegram_chat_id,
                        message_id=callback.message.message_id,
                        reply_markup=None,
                    )
                except Exception:
                    pass

        @r.callback_query(F.data.startswith("resume:"))
        async def resume_callback(callback):
            await callback.answer()
            rid = callback.data.split(":", 1)[1]
            await self.safe_reply(callback, lambda: self.agent.resume(rid))

        @r.callback_query(F.data.startswith("review:"))
        async def review_callback(callback):
            await callback.answer()
            rid = callback.data.split(":", 1)[1]
            await self.safe_reply(callback, lambda: self.agent.resume(rid, review=True))

        @r.callback_query(F.data.startswith("cancel:"))
        async def cancel_callback(callback):
            await callback.answer()
            await self.safe_reply(callback, lambda: self.agent.stop(callback.data.split(":", 1)[1]))

        @r.callback_query(F.data.startswith("manual:"))
        async def manual_callback(callback):
            await callback.answer()
            parts = callback.data.split(":", 2)
            if len(parts) != 3:
                return
            _, rid, token = parts
            if rid != self.agent.run_id or token != self.agent.manual_token:
                return
            row = await self.store.run(rid)
            if row and row["state"] == State.PAUSED and self.agent.busy():
                await self.safe_reply(callback, lambda: self.agent.resume(rid))

    async def manual_prompt(self, rid, question):
        await self.send(
            "Агент на паузе: требуется ручная проверка вопроса в браузере.\n"
            + question.text[:700]
            + "\nЗаполните ответ вручную или оставьте пустым, затем продолжите. "
            "В счётчике он будет отмечен как пропущенный агентом. Таймер Moodle продолжает идти.",
            keyboard(
                [
                    [("Продолжить после ручной проверки", f"manual:{rid}:{self.agent.manual_token}")],
                    [("Остановить", f"cancel:{rid}")],
                ]
            ),
        )

    async def send_quiz(self, quiz, prefix=""):
        available = {True: "доступен", False: "недоступен", None: "нужна проверка в браузере"}[quiz.available]
        text = (
            f"{prefix}{quiz.course}\n{quiz.title}\nСтатус: {available}\nДедлайн: {quiz.deadline}\n"
            f"Лимит: {quiz.time_limit}\nОсталось попыток: {quiz.attempts_remaining if quiz.attempts_remaining is not None else 'не указано'}\n"
            f"{quiz.url}"
        )
        markup = keyboard([[("пройти тест", f"start:{quiz.id}")]]) if quiz.available is True else None
        return await self.send(text, markup)

    async def notifications(self):
        async with self.delivery_lock:
            eligible = []
            for row in await self.store.all("SELECT * FROM notifications WHERE status='pending' ORDER BY id"):
                quiz = Quiz.model_validate_json(row["data"])
                if row["kind"] == "new" and quiz.available is not True:
                    await self.store.notification_status(row["id"], "suppressed")
                    continue
                eligible.append((row, quiz))
            if not eligible:
                return
            for row, _ in eligible:
                await self.store.notification_status(row["id"], "sending")
            try:
                if len(eligible) == 1:
                    row, quiz = eligible[0]
                    message = await self.send_quiz(
                        quiz, "Новый тест\n" if row["kind"] == "new" else "Тест изменился\n"
                    )
                else:
                    text = f"Обновления тестов: {len(eligible)}\n" + "\n".join(
                        f"• {quiz.course[:70]}: {quiz.title[:120]}" for _, quiz in eligible[:5]
                    )
                    message = await self.send(text, keyboard([[("Посмотреть тесты", "tests:available:0")]]))
                for row, _ in eligible:
                    await self.store.notification_status(row["id"], "sent", message.message_id)
            except TelegramRetryAfter:
                for row, _ in eligible:
                    await self.store.notification_status(row["id"], "pending")
            except TelegramBadRequest:
                for row, _ in eligible:
                    await self.store.notification_status(row["id"], "rejected")
            except Exception:
                # Ambiguous delivery is never retried automatically.
                for row, _ in eligible:
                    await self.store.notification_status(row["id"], "uncertain")

    async def final_prompt(self, rid):
        filled, skipped = await self.store.counts(rid)
        token = await self.store.issue_confirmation(rid, self.s.confirmation_ttl)
        row = await self.store.run(rid)
        markup = keyboard(
            [
                [("подтвердить завершение", f"confirm:{rid}:{token}")],
                [("продолжить проверку", f"review:{rid}"), ("отменить", f"cancel:{rid}")],
            ]
        )
        try:
            await self.send(
                f"Обработка вопросов завершена.\nПопытка {row['attempt_id']}\n"
                f"Заполнено: {filled}; пропущено: {skipped}.\n"
                f"Осталось: {self.events.snapshot['timer']}.\n"
                f"Разрешить завершение этой попытки? Кнопка действует {self.s.confirmation_ttl // 60} мин.\n"
                "Без ответа отправки не будет. Moodle может закрыть попытку по времени.",
                markup,
            )
        except Exception:
            await self.store.revoke(rid)
            raise

    async def progress(self):
        queue = self.events.subscribe()
        last_edit = 0
        last_state = None
        previous_warning = None
        try:
            while True:
                snapshot = await queue.get()
                if snapshot.get("warning") and snapshot["warning"] != previous_warning:
                    previous_warning = snapshot["warning"]
                    try:
                        await self.send(
                            "Осталось " + previous_warning + ". Moodle может закрыть попытку автоматически."
                        )
                    except Exception:
                        pass
                rid = snapshot["run_id"]
                if not rid or (snapshot["state"] == last_state and time.monotonic() - last_edit < 3):
                    continue
                last_edit = time.monotonic()
                last_state = snapshot["state"]
                row = await self.store.run(rid)
                text = (
                    f"Попытка {row['attempt_id'] or 'запускается'} · {snapshot['state']}\n"
                    f"Заполнено: {snapshot['filled']}; пропущено: {snapshot['skipped']}\n"
                    f"Время: {snapshot['timer']}\n" + "\n".join(snapshot["actions"][-3:])
                )
                try:
                    if row["message_id"]:
                        await self.message_call(
                            self.bot.edit_message_text,
                            chat_id=self.s.telegram_chat_id,
                            message_id=row["message_id"],
                            text=text[:4000],
                        )
                    else:
                        message = await self.send(text)
                        await self.store.update_run(rid, message_id=message.message_id)
                except TelegramBadRequest:
                    pass
                except Exception:
                    await self.agent.pause("Потеря связи с Telegram; требуется /resume или /review")
        finally:
            self.events.subscribers.discard(queue)

    async def connection_watch(self):
        while True:
            await asyncio.sleep(15)
            try:
                await self.bot.get_me()
                await self.notifications()
            except Exception:
                if self.agent.run_id:
                    await self.agent.pause("Telegram недоступен. Подтверждение отозвано")

    async def run(self):
        # aiogram retries long-poll network errors. No webhook/public IP/server is needed.
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(
            self.bot,
            handle_signals=False,
            close_bot_session=False,
            allowed_updates=["message", "callback_query"],
            polling_timeout=20,
            handle_as_tasks=False,
        )

    async def close(self):
        await self.bot.session.close()
