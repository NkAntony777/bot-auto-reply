@echo off
rem 重要：本文件必须保存为 ANSI/GBK 编码 + CRLF 换行，勿转存 UTF-8，否则中文会导致解析错乱闪退
rem 安全停止 wxbot：守护进程优雅退出（保存状态、微信还回主屏）+ 停看板 + 位置兜底
title wxbot 安全停止
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -X utf8 stop_wxbot.py %*
pause
