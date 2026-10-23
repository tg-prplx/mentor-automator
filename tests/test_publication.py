import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "publication_check", Path(__file__).resolve().parents[1] / "scripts/check_publication.py"
)
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


@pytest.mark.parametrize("name", ["cred.txt", ".env", "data/session.json", "output/private.png"])
def test_private_index_path_rejected_without_reading_blob(name, monkeypatch, capsys):
    def git(command):
        assert command == ["git", "ls-files", "--stage", "-z"], "private blob must never be opened"
        return b"100644 " + b"0" * 40 + b" 0\t" + name.encode() + b"\0"

    monkeypatch.setattr(publication.subprocess, "check_output", git)
    assert publication.audit() == 1
    assert name in capsys.readouterr().err


def test_secret_detection_reports_location_without_value(monkeypatch, capsys):
    fake_key = "sk-" + "synthetic" * 6

    def git(command):
        if command[1] == "ls-files":
            return b"100644 " + b"0" * 40 + b" 0\tsrc/demo.py\0"
        return ("KEY='" + fake_key + "'\n").encode()

    monkeypatch.setattr(publication.subprocess, "check_output", git)
    assert publication.audit() == 1
    output = capsys.readouterr()
    assert "src/demo.py:1: API key" in output.err
    assert fake_key not in output.err + output.out


def test_allowlisted_source_passes(monkeypatch):
    monkeypatch.setattr(
        publication.subprocess,
        "check_output",
        lambda command: (
            b"100644 " + b"0" * 40 + b" 0\tsrc/demo.py\0"
            if command[1] == "ls-files"
            else b"print('synthetic public source')\n"
        ),
    )
    assert publication.audit() == 0
