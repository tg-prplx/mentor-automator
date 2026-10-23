import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conftest import PROJECT

from mentor_automator.events import Events
from mentor_automator.panel import Panel


async def test_panel_render_and_buttons(environment):
    events = Events()
    agent = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock(), stop=AsyncMock())
    panel = Panel(events, agent, SimpleNamespace(scan_now=asyncio.Event()))
    url = await panel.open()
    page = await environment[2].context.new_page()
    try:
        # Browser's Moodle context intentionally rejects cross-origin documents;
        # use a separate isolated browser context for the local panel.
        await page.close()
        context = await environment[2].pw.chromium.launch(headless=True)
        page = await context.new_page(viewport={"width": 1280, "height": 1100})
        await page.goto(url)
        events.publish(
            "Ответ выбран и проверен в интерфейсе",
            state="PAUSED",
            filled=4,
            skipped=1,
            question="Вычислите период колебаний математического маятника при L = 1 м.",
            answer="1 · T ≈ 2,01 с",
            confidence=0.92,
            explanation="Период определяется формулой T = 2π√(L/g). При g = 9,81 м/с² получаем примерно 2,01 с.",
            timer="04:59",
            monitor="Проверка завершена: 3 теста",
        )
        await page.locator("#state").filter(has_text="PAUSED").wait_for()
        assert await page.locator("#confidence").inner_text() == "92%"
        await page.get_by_role("button", name="Пауза", exact=True).click()
        await page.locator("#result").filter(has_text="Команда выполнена").wait_for()
        assert agent.pause.await_count == 1
        await page.screenshot(path=str(PROJECT / "output/playwright/panel-demo.png"), full_page=True)
        await context.close()
    finally:
        await panel.close()
