@echo off
REM ============================================================================
REM  Start Open-LLM-VTuber (foreground, shows the console)
REM
REM  Use this for the first run so you can see any errors.
REM  For daily use, prefer start-watchdog.vbs which keeps it running in the
REM  background and restarts it automatically if it crashes.
REM
REM  NOTE: ASCII-only on purpose - .bat files with non-ASCII text are a common
REM  source of mojibake depending on the console code page.
REM ============================================================================

setlocal
cd /d "%~dp0.."

REM winget installs versioned folders, so a freshly opened shell may not have
REM uv / ffmpeg on PATH yet. Add them explicitly.
for /d %%d in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*") do set "PATH=%%d;%PATH%"
for /d %%d in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*") do (
    for /d %%e in ("%%d\ffmpeg-*") do set "PATH=%%e\bin;%PATH%"
)

where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found on PATH.
    echo         Run scripts\setup.ps1 first: it installs uv and ffmpeg.
    pause
    exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [WARN] ffmpeg not found. Audio conversion may fail.
    echo        Run scripts\setup.ps1 to install it.
    echo.
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment missing.
    echo         Run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
    pause
    exit /b 1
)

echo ============================================================
echo   Starting Open-LLM-VTuber
echo   Web UI: http://localhost:12393
echo   Close this window to stop the server.
echo ============================================================
echo.

uv run run_server.py

echo.
echo Server stopped.
pause
