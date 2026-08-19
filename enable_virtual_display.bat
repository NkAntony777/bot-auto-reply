@echo off
rem 挂载/卸载 Amyuni 虚拟显示器（enableidd 不需要管理员权限）
rem 用途：重启/掉线后虚拟屏消失时，双击本脚本重新挂载
rem 仅首次安装驱动需要管理员：右键"以管理员身份运行"本脚本，
rem 脚本会自动执行 install + enableidd（之后日常挂载不再需要管理员）
rem 卸载虚拟屏：deviceinstaller64 enableidd 0
rem 参数：--nopause 表示不等待按键（供 start_wxbot.bat 调用）
set "NOPAUSE="
if /i "%~1"=="--nopause" set "NOPAUSE=1"
cd /d "%TEMP%\usbmmidd\usbmmidd_v2" 2>nul
if not errorlevel 1 goto :found
cd /d "E:\tmp_usbmmidd\usbmmidd_v2" 2>nul
if not errorlevel 1 goto :found
echo [!] 找不到 usbmmidd 驱动目录（%%TEMP%%\usbmmidd\usbmmidd_v2 与 E:\tmp_usbmmidd\usbmmidd_v2 均不存在）
if not defined NOPAUSE pause
exit /b 1
:found
echo 正在挂载虚拟显示器...
deviceinstaller64 enableidd 1
if not errorlevel 1 goto :done
echo [!] enableidd 失败，尝试安装驱动（需要本脚本以管理员身份运行）...
deviceinstaller64 install usbmmidd.inf usbmmidd
deviceinstaller64 enableidd 1
if errorlevel 1 (
  echo [!] 仍失败：请右键"以管理员身份运行"本脚本重试一次（仅首次需要）。
  if not defined NOPAUSE pause
  exit /b 1
)
:done
echo.
echo 完成。当前监视器数：
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Screen]::AllScreens.Length"
if not defined NOPAUSE pause
