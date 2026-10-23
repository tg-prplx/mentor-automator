from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from filelock import FileLock, Timeout

from mentor_automator.config import Settings
from mentor_automator.onboarding import needs_setup, setup, write_new_private
from mentor_automator.preflight import doctor

TOKEN = "987654321:" + "fictional_setup_test_" * 2


def template(root):
    (root / ".env.example").write_text(
        "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=0\nTELEGRAM_USER_ID=0\n", encoding="utf-8"
    )


async def test_first_run_writes_private_files_and_pairs_without_echoing_secrets(tmp_path, capsys):
    template(tmp_path)
    values = iter([TOKEN, "synthetic_student", "fictional=secret", "fictional=secret"])

    async def paired(settings):
        assert settings.telegram_bot_token.get_secret_value() == TOKEN
        return True

    assert await setup(tmp_path, secret=lambda _: next(values), pair=paired)
    assert (tmp_path / "cred.txt").read_text() == "username=synthetic_student\npassword=fictional=secret\n"
    assert Settings.load(tmp_path).telegram_bot_token.get_secret_value() == TOKEN
    output = capsys.readouterr().out
    assert not any(value in output for value in [TOKEN, "synthetic_student", "fictional=secret"])
    if __import__("os").name != "nt":
        assert (tmp_path / "cred.txt").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


async def test_existing_credentials_are_never_opened_or_overwritten(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=5\nTELEGRAM_USER_ID=5\n", encoding="utf-8"
    )
    credentials = tmp_path / "cred.txt"
    credentials.write_text("synthetic-existing-do-not-read", encoding="utf-8")
    original = credentials.stat()
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == credentials:
            raise AssertionError("onboarding must not open an existing credential file")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    pair = AsyncMock()
    assert await setup(tmp_path, secret=lambda _: pytest.fail("unexpected prompt"), pair=pair)
    pair.assert_not_awaited()
    assert credentials.stat().st_mtime_ns == original.st_mtime_ns
    assert not needs_setup(Settings.load(tmp_path))


async def test_setup_refuses_to_compete_with_running_application(tmp_path):
    template(tmp_path)
    (tmp_path / "data").mkdir()
    with FileLock(str(tmp_path / "data/application.lock"), timeout=0):
        with pytest.raises(Timeout):
            await setup(tmp_path, secret=lambda _: pytest.fail("must refuse before asking for secrets"))
    assert not (tmp_path / ".env").exists()


async def test_failed_pairing_is_not_reported_as_success_and_can_be_retried(tmp_path):
    template(tmp_path)
    values = iter([TOKEN, "demo", "fake-password", "fake-password"])
    pair = AsyncMock(return_value=False)
    assert not await setup(tmp_path, secret=lambda _: next(values), pair=pair)
    assert needs_setup(Settings.load(tmp_path))
    assert not await setup(
        tmp_path, secret=lambda _: pytest.fail("existing secrets must be reused"), pair=pair
    )
    assert pair.await_count == 2


def test_new_private_file_refuses_overwrite(tmp_path):
    path = tmp_path / "synthetic-secret.txt"
    write_new_private(path, "original synthetic value")
    with pytest.raises(FileExistsError):
        write_new_private(path, "replacement")
    assert path.read_text() == "original synthetic value"


async def test_doctor_checks_clean_browser_without_opening_credentials(environment, monkeypatch):
    settings = environment[0]
    settings.llm_backend = "openai"
    credentials = settings.root / "cred.txt"
    credentials.write_text("synthetic-unused-credentials", encoding="utf-8")
    (settings.root / ".env").touch()
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == credentials:
            raise AssertionError("doctor must only check that the file exists")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    messages = []
    assert await doctor(settings, emit=messages.append)
    assert any("чистым временным профилем" in message for message in messages)
    assert environment[-1].login_count == 0 and environment[-1].starts == 0
