import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User
from pydantic import ValidationError

from mentor_automator.config import Settings
from mentor_automator.events import Events
from mentor_automator.llm import DISABLED_FEATURES, LLM, codex_args
from mentor_automator.models import Answer, Question, Quiz, State
from mentor_automator.panel import Panel
from mentor_automator.privacy import clean, public_url
from mentor_automator.storage import Store
from mentor_automator.telegram_bot import Authorized, Telegram


async def test_openai_compatible_payload_and_json(monkeypatch):
    received = []

    async def request(req):
        received.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "choices": ["0"],
                                    "text": "",
                                    "explanation": "demo",
                                    "confidence": 0.8,
                                    "skip": False,
                                }
                            )
                        }
                    }
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(request))
    )
    llm = LLM(Settings(llm_backend="openai", llm_model="dummy"))
    q = Question(id="local-opaque-id", kind="single", text="demo", options=[{"id": "0", "text": "yes"}])
    answer = await llm.solve(q)
    assert answer.choices == ["0"]
    assert "local-opaque-id" not in json.dumps(received)
    assert not any(k in json.dumps(received) for k in ["cookie", "sesskey", "mentor.khai.edu"])
    assert received[0]["response_format"] == {"type": "json_object"}


def test_answer_validation():
    q = Question(id="1", kind="single", text="demo", options=[{"id": "0", "text": "yes"}])
    with pytest.raises(ValueError):
        Answer(choices=["99"], text="", explanation="", confidence=0.9, skip=False).validate_for(q)
    with pytest.raises(ValidationError):
        Answer(choices=["0"], text="", explanation="", confidence=1.1, skip=False)
    with pytest.raises(ValidationError):
        Answer(choices=["0", "0"], text="", explanation="", confidence=0.9, skip=False)
    numeric = Question(id="2", kind="numeric", text="demo")
    with pytest.raises(ValueError):
        Answer(choices=[], text="NaN", explanation="", confidence=0.9, skip=False).validate_for(numeric)


def test_codex_hardening_and_windows_npm_shim(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "shutil.which", lambda cmd: "C:/npm/codex.cmd" if cmd == "codex" else "C:/node/node.exe"
    )
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    args = codex_args(Settings(), tmp_path, image=True)
    assert args[0] == "C:/node/node.exe"
    assert args[1].endswith("codex.js")
    for feature in DISABLED_FEATURES:
        assert args[args.index(feature) - 1] == "--disable"
    assert "--ignore-user-config" in args and "--ignore-rules" in args and "--ephemeral" in args
    assert "--image" in args and "project_doc_max_bytes=0" in args


async def test_authorized_private_user_only():
    settings = Settings(telegram_chat_id=5, telegram_user_id=5)
    middleware = Authorized(settings)
    from datetime import datetime, timezone

    good = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=5, type="private"),
        from_user=User(id=5, is_bot=False, first_name="Demo"),
    )
    handler = AsyncMock(return_value="ok")
    assert await middleware(handler, good, {}) == "ok"
    bad_user = good.model_copy(update={"from_user": User(id=6, is_bot=False, first_name="Other")})
    assert await middleware(handler, bad_user, {}) is None
    bad_chat = good.model_copy(update={"chat": Chat(id=5, type="group", title="group")})
    assert await middleware(handler, bad_chat, {}) is None
    callback = SimpleNamespace(from_user=good.from_user, message=good)
    assert await middleware(handler, callback, {}) == "ok"
    assert handler.await_count == 2


async def test_notification_delivery_uncertainty_and_confirmation_failure(tmp_path):
    store = Store(tmp_path / "test.db")
    await store.open()
    settings = Settings(
        telegram_bot_token="123456789:fictional_testing_token_123456789",
        telegram_chat_id=5,
        telegram_user_id=5,
    )
    events = Events()
    agent = SimpleNamespace(notify_final=None)
    telegram = Telegram(settings, store, agent, events)
    try:
        quiz = Quiz(
            id="7",
            course="demo",
            title="demo",
            url="https://example.test/mod/quiz/view.php?id=7",
            available=True,
        )
        await store.observe(quiz)
        telegram.send_quiz = AsyncMock(
            side_effect=TelegramNetworkError(
                method=SendMessage(chat_id=5, text="demo"), message="fake timeout"
            )
        )
        await telegram.notifications()
        await telegram.notifications()
        assert telegram.send_quiz.await_count == 1
        assert (await store.one("SELECT status FROM notifications"))["status"] == "uncertain"
        rid = await store.create_run("7")
        await store.update_run(rid, attempt_id="10", state=State.AWAITING_FINAL_CONFIRMATION)
        telegram.send = AsyncMock(side_effect=ConnectionError("fictional"))
        with pytest.raises(ConnectionError):
            await telegram.final_prompt(rid)
        row = await store.run(rid)
        assert row["state"] == State.AWAITING_FINAL_CONFIRMATION and row["token_hash"] is None
    finally:
        await telegram.close()
        await store.close()


async def test_panel_capability_auth_and_live_updates():
    events = Events()
    agent = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock(), stop=AsyncMock())
    monitor = SimpleNamespace(scan_now=asyncio.Event())
    panel = Panel(events, agent, monitor)
    url = await panel.open()
    try:
        async with httpx.AsyncClient() as client:
            assert (await client.get(url.split("?")[0])).status_code == 403
            assert (await client.get(url)).status_code == 200
            base = url.split("?")[0]
            assert (await client.post(base + "control/pause")).status_code == 403
            headers = {"X-Panel-Token": panel.token, "Origin": "http://evil.example"}
            assert (await client.post(base + "control/pause", headers=headers)).status_code == 403
            headers.pop("Origin")
            assert (await client.post(base + "control/pause", headers=headers)).status_code == 200
            assert agent.pause.await_count == 1
            assert (await client.post(base + "control/confirm", headers=headers)).status_code == 409
            async with client.stream("GET", base + "events?token=" + panel.token) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        assert json.loads(line[5:])["state"] == "IDLE"
                        break
    finally:
        await panel.close()


def test_privacy_helpers():
    assert "SECRET" not in clean("sesskey=SECRET https://example.test/?token=SECRET")
    assert (
        public_url(
            "https://mentor.khai.edu/mod/quiz/view.php?id=7&sesskey=SECRET",
            "https://mentor.khai.edu",
            "/mod/quiz/view.php",
            {"id"},
        )
        == "https://mentor.khai.edu/mod/quiz/view.php?id=7"
    )
    with pytest.raises(ValueError):
        public_url(
            "https://evil.test/mod/quiz/view.php?id=7",
            "https://mentor.khai.edu",
            "/mod/quiz/view.php",
            {"id"},
        )
