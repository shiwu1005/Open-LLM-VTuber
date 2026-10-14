@echo off
REM ============================================================================
REM  Stop the watchdog (the background keeper of both services)
REM
REM  After stopping, LM Studio and Open-LLM-VTuber will no longer be restarted
REM  automatically. The services themselves keep running until you close them.
REM
REM  NOTE: ASCII-only on purpose - .bat files with non-ASCII text break
REM  depending on the console code page.
REM ============================================================================

setlocal

echo ============================================================
echo   Stopping Open-LLM-VTuber watchdog
echo ============================================================
echo.

powershell -NoProfile -Command ^
  "$p = @(Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -like '*-File*watchdog.ps1*' -and $_.CommandLine -notlike '*-Command*' });" ^
  "if ($p.Count -eq 0) { Write-Host '  Watchdog is not running.' }" ^
  "else { $p | ForEach-Object { Write-Host ('  Stopping watchdog PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } }"

echo.
echo Watchdog stopped.
echo The two services are still running - they just are not supervised anymore.
echo To restart supervision, run scripts\start-watchdog.vbs
echo.
pause
