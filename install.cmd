@echo off
rem One-shot install on Windows (current user, no admin): double-click, or run
rem `install.cmd` in a terminal. Options: -NoObsidian  -NoLlm  -LlmModel <name>
rem Bypasses the PowerShell script policy for this one script only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set RC=%ERRORLEVEL%
rem Keep the window open when double-clicked so the result is readable.
echo %CMDCMDLINE% | find /i "/c" >nul && pause
exit /b %RC%
