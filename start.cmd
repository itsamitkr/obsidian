@echo off
rem Start the Control Panel (upload files, search, watcher) at http://127.0.0.1:8000
rem Stop it with Ctrl+C or by closing this window.
cd /d "%~dp0"
if not exist ".venv\Scripts\knowledge.exe" (
    echo Not installed yet. Run install.cmd first.
    echo %CMDCMDLINE% | find /i "/c" >nul && pause
    exit /b 1
)
".venv\Scripts\knowledge.exe" panel %*
