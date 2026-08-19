@echo off
rem 重要：本文件必须保存为 ANSI/GBK 编码 + CRLF 换行，勿转存 UTF-8，否则中文会导致解析错乱闪退
title wxbot 一键启动
cd /d "%~dp0"

rem 优先用项目自带 .venv 的解释器，没有则退回系统 python
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :py_ok
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 Python：既没有 .venv\Scripts\python.exe，PATH 里也没有 python。
    echo 请先安装 Python 3.11+ 或重建虚拟环境后再试。
    pause
    exit /b 1
)
set "PY=python"

:py_ok
rem 与 wxbot_config.json 的 dashboard.port 保持一致
set "DASH_PORT=8788"

rem ===== [1/4] 虚拟屏：检测 + 交互式挂载（驱动在位时不需要管理员） =====
set "MON="
for /f %%n in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Screen]::AllScreens.Length" 2^>nul') do set "MON=%%n"
if "%MON%"=="" goto :vdisp_unknown1
if %MON% GEQ 2 (
    echo [OK] 当前已有 %MON% 个显示器（虚拟屏在线或存在物理副屏），微信将自动停靠副屏。
    goto :vdisp_ok
)
echo 当前只有 %MON% 个显示器：虚拟屏未启动（重启/掉线后会消失，属正常现象）。
:vdisp_ask
choice /C YN /M "是否现在挂载虚拟屏"
if errorlevel 2 goto :vdisp_skip
call "%~dp0enable_virtual_display.bat" --nopause
if errorlevel 1 goto :vdisp_cancel
timeout /t 3 /nobreak >nul
set "MON="
for /f %%n in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Screen]::AllScreens.Length" 2^>nul') do set "MON=%%n"
if "%MON%"=="" goto :vdisp_unknown2
if %MON% LSS 2 (
    echo [!] 挂载后仍只检测到 %MON% 个显示器，微信将停在主屏右下角小窗运行。
    goto :vdisp_ok
)
echo [OK] 虚拟屏已生效（当前 %MON% 个显示器），微信将自动停靠到虚拟屏。
goto :vdisp_ok
:vdisp_unknown1
echo [?] 无法检测显示器数量，建议按 Y 挂载虚拟屏。
goto :vdisp_ask
:vdisp_unknown2
echo [?] 无法复核显示器数量，wxbot 启动后会自行判断停靠位置。
goto :vdisp_ok
:vdisp_cancel
echo [!] 挂载失败：驱动可能未安装，请右键"以管理员身份运行"一次 enable_virtual_display.bat。
:vdisp_skip
echo 未启用虚拟屏：微信将停在主屏右下角小窗运行（截图/点击仍可用）。
:vdisp_ok

rem ===== [2/4] 状态看台 =====
echo.
echo [2/4] 启动状态看台 wxbot_dashboard.py (http://127.0.0.1:%DASH_PORT%) ...
start "wxbot dashboard" /min "%PY%" -X utf8 wxbot_dashboard.py --port %DASH_PORT%

rem ===== [3/4] 守护进程 =====
echo [3/4] 启动机器人守护进程 wxbot.py ...
start "wxbot daemon" /min "%PY%" -X utf8 wxbot.py

rem ===== [4/4] 浏览器 =====
echo [4/4] 等待看台就绪后打开浏览器 ...
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:%DASH_PORT%"

echo.
echo 已启动：
echo   - 守护进程：任务栏窗口 "wxbot daemon"（日志同时写入 wxbot_run.log）
echo   - 看台页面：http://127.0.0.1:%DASH_PORT%  （bot 挂了也能看到"已停止"）
echo 重复双击本脚本安全：wxbot 与看台都有单实例守卫，会先清掉旧实例。
echo 停止：关掉对应窗口即可。
echo.
echo 本窗口 5 秒后自动关闭（按任意键立即关闭）。
timeout /t 5 >nul
