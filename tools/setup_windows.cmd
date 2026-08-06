@echo off
REM Double-clickable wrapper for setup_windows.ps1.
REM PowerShell refuses to run unsigned .ps1 files by default, which makes
REM double-clicking the script itself fail with a security error rather than
REM anything actionable; -ExecutionPolicy Bypass applies to this process only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1" %*
echo.
pause
