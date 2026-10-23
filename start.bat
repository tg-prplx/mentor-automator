@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo Не найден uv. В PowerShell выполните: winget install --id astral-sh.uv -e
    echo Затем заново откройте терминал и запустите start.bat.
    pause
    exit /b 1
)
echo Mentor Automator - установка зависимостей и запуск
uv sync --locked --no-dev --python 3.12
if errorlevel 1 goto failed
uv run --no-sync mentor --launch %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Запуск прерван. Ошибка показана выше.
pause
exit /b 1
