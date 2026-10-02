@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Python environment is missing. Run setup first.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0bot.py" check
set "TaskBotExitCode=%errorlevel%"
echo.
pause
exit /b %TaskBotExitCode%
