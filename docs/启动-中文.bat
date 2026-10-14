@echo off
REM ===========================================================================
REM  启动 Open-LLM-VTuber
REM
REM  作用：把 uv / uvx / ffmpeg 的路径补进 PATH，然后启动服务。
REM  为什么需要它：winget 装的 uv 和 ffmpeg 装在版本化目录里，只有新开的
REM  终端才会继承到 PATH；双击运行本脚本可以保证一定能找到这两个工具。
REM  缺少 ffmpeg 会导致"找不到音频文件"之类的错误。
REM ===========================================================================

set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe;%PATH%"
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;%PATH%"

cd /d "%~dp0"

echo ============================================
echo   正在启动 Open-LLM-VTuber ...
echo   启动完成后用 Chrome 打开:
echo       http://localhost:12393
echo   关闭本窗口即停止服务。
echo ============================================
echo.

"%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe" run run_server.py

echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
