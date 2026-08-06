@echo off
REM Double-click to launch Notula (or run run.bat from a terminal).
REM Creates the app venv on first run; that venv only needs the GUI/capture
REM packages in requirements-win.txt.
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
REM The stamp, not the venv, is what records "dependencies are installed".
REM `python -m venv` creates python.exe BEFORE pip runs, so using the venv as the
REM sentinel means a failed or interrupted install is never retried: every later
REM launch skips straight to pythonw.exe, which dies on the missing import with
REM no console to say so. Silent, permanent, and baffling.
set "STAMP=.venv\.deps-ok"

if not exist "%STAMP%" (
  if not exist "%PY%" (
    echo First run: creating the app venv and installing dependencies...
    py -3 -m venv .venv 2>nul
    if not exist "%PY%" python -m venv .venv
    if not exist "%PY%" (
      echo.
      echo Could not create a virtual environment. Install Python 3.11+ from
      echo python.org ^(tick "Add python.exe to PATH"^) and run this again.
      pause
      exit /b 1
    )
  )
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet -r requirements-win.txt
  if errorlevel 1 (
    echo.
    echo Dependency install failed. Run this to see why:
    echo   .venv\Scripts\pip install -r requirements-win.txt
    pause
    exit /b 1
  )
  break > "%STAMP%"
)

REM pythonw has no console window. Pass --debug to keep one, so tracebacks and
REM whisper's stderr are visible while you're setting the machine up.
if /i "%~1"=="--debug" (
  shift
  "%PY%" notula_win.py %*
) else (
  start "" "%~dp0.venv\Scripts\pythonw.exe" notula_win.py %*
)
exit /b 0
