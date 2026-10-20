@echo off
rem ===============================================================
rem  Scheduled-task entry point: one report run, one place to get it right.
rem
rem  Why a .cmd instead of putting the command into Task Scheduler:
rem  cwd, PYTHONUTF8, venv path and log redirection must all be correct
rem  together. A quoting mistake inside the task XML shows up as
rem  "the task ran but produced nothing" with no error visible in the GUI.
rem  One wrapper = manual runs and scheduled runs use the SAME entry.
rem
rem  Exit codes are passed through unchanged (0 ok/already-ran, 1 failed,
rem  2 config, 3 login expired, 4 another instance is running).
rem  Task Scheduler reads exactly this as "last run result".
rem
rem  ASCII only: cmd.exe does not understand a UTF-8 BOM and reads the
rem  file in the OEM code page. Chinese text here would be garbled.
rem ===============================================================
setlocal
set "PYTHONUTF8=1"

rem %~dp0 ends with a backslash; go up one level to the repo root.
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [myAgent] venv python not found: %CD%\.venv\Scripts\python.exe 1>&2
    echo [myAgent] Create .venv and run: pip install -e ".[dev]" 1>&2
    exit /b 2
)

if not exist "var\logs" mkdir "var\logs"

rem task.log = process-level stdout/stderr (config errors, login hints).
rem app.log  = structured business events. Both are needed.
".venv\Scripts\python.exe" -m app.main report >> "var\logs\task.log" 2>&1
exit /b %ERRORLEVEL%
