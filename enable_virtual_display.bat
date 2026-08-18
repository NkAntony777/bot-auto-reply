@echo off
rem 一键激活 Amyuni 虚拟显示器（需要管理员权限）
rem 用途：重启/掉线后虚拟屏消失时，右键"以管理员身份运行"本脚本
rem 首次安装：cd 到 %TEMP%\usbmmidd\usbmmidd_v2 后执行
rem   deviceinstaller64 install usbmmidd.inf usbmmidd
rem   deviceinstaller64 enableidd 1
cd /d "%TEMP%\usbmmidd\usbmmidd_v2" 2>nul || (
  echo [!] 驱动目录不存在，请先解压 usbmmidd_v2.zip 到 %%TEMP%%\usbmmidd
  pause & exit /b 1
)
echo 正在激活虚拟显示器...
deviceinstaller64 enableidd 1
if errorlevel 1 (
  deviceinstaller64 install usbmmidd.inf usbmmidd
  deviceinstaller64 enableidd 1
)
echo.
echo 完成。当前监视器数：
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Screen]::AllScreens.Length"
pause
