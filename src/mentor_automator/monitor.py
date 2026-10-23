import asyncio


class Monitor:
    def __init__(self, settings, moodle, store, events, notify):
        self.s, self.moodle, self.store, self.events, self.notify = settings, moodle, store, events, notify
        self.scan_now = asyncio.Event()

    async def run(self):
        while True:
            next_check = asyncio.get_running_loop().time() + self.s.poll_seconds
            self.scan_now.clear()
            try:
                # Independent page: monitoring continues while the agent waits for a model or user.
                quizzes = await self.moodle.scan()
                for quiz in quizzes:
                    await self.store.observe(quiz)
                self.events.publish(monitor=f"Проверка завершена: {len(quizzes)} тестов")
                await self.notify()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.events.publish(
                    "Ошибка мониторинга; повтор по расписанию. /diagnostics для селекторов",
                    monitor="Ошибка или истекла сессия",
                )
            try:
                delay = max(1, next_check - asyncio.get_running_loop().time())
                await asyncio.wait_for(self.scan_now.wait(), timeout=delay)
            except TimeoutError:
                pass
