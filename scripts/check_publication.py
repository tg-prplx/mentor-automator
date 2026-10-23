"""Audit ONLY files in Git's index. Never open runtime files in the working tree."""

import re
import subprocess
import sys
from pathlib import PurePosixPath

ROOT_FILES = {
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "pyproject.toml",
    "uv.lock",
    "cred.example.txt",
    "start.command",
    "start.bat",
}
FAKE_TOKEN = "123456789:fictional_testing_token_123456789"
PATTERNS = {
    "Telegram token": re.compile(r"\b[0-9]{6,16}:[A-Za-z0-9_-]{30,60}\b"),
    "API key": re.compile(r"\b(?:sk-(?:proj-)?|gh[pousr]_)[A-Za-z0-9_-]{20,}\b"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Local home path": re.compile(r"(?:/" + r"Users/[^/\s]+/|[A-Za-z]:\\Users\\[^\\\s]+\\)"),
    "Session URL": re.compile(
        r"https?://[^\s\"<>]+[?&](?:sesskey|token|password|access_token)=[A-Za-z0-9._~-]{8,}"
    ),
}


def allowed_path(name):
    path = PurePosixPath(name)
    if name in ROOT_FILES:
        return True
    if path.parts[0] in {"src", "tests", "scripts"}:
        return path.suffix == ".py" and "__pycache__" not in path.parts
    if path.parts[0] == "config":
        return name == "config/selectors.json"
    return path.parts[:2] == (".github", "workflows") and path.suffix in {".yml", ".yaml"}


def audit():
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"]).split(b"\0")
    errors = []
    checked = 0
    for entry in filter(None, entries):
        metadata, raw_name = entry.split(b"\t", 1)
        mode, _, stage = metadata.decode().split()
        name = raw_name.decode("utf-8")
        # Reject paths/modes BEFORE reading a blob. Even a force-added cred.txt is never read.
        if not allowed_path(name) or mode not in {"100644", "100755"} or stage != "0":
            errors.append(f"{name}: файл вне списка публикации или недопустимый режим")
            continue
        content = subprocess.check_output(["git", "show", ":" + name]).decode("utf-8")
        for number, line in enumerate(content.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                if any(match.group() != FAKE_TOKEN for match in pattern.finditer(line)):
                    # Report the location and class, never the matching value.
                    errors.append(f"{name}:{number}: {label}")
        checked += 1
    if not entries or not checked:
        errors.append("Нет файлов в индексе Git для проверки")
    if errors:
        print("Публикация заблокирована:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print(f"Проверено файлов в Git: {checked}. Закрытые пути и известные форматы секретов не найдены.")
    return 0


if __name__ == "__main__":
    sys.exit(audit())
