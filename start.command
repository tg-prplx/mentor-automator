#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
# Finder starts with a smaller PATH than an interactive terminal.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
trap 'code=$?; if [ "$code" -ne 0 ]; then printf "\nЗапуск прерван. Ошибка показана выше.\n"; read -r -p "Enter — закрыть окно..." _ || true; fi' EXIT
if ! command -v uv >/dev/null 2>&1; then
    printf 'Не найден uv. Установите его по инструкции: https://docs.astral.sh/uv/getting-started/installation/\n'
    printf 'На macOS с Homebrew: brew install uv\n'
    exit 1
fi
printf 'Mentor Automator · установка зависимостей и запуск\n'
uv sync --locked --no-dev --python 3.12
uv run --no-sync mentor --launch "$@"
