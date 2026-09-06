@echo off
chcp 65001 >nul
cd /d %~dp0
python -m pip install -q pyinstaller
python -m PyInstaller --noconfirm autofish.spec
if errorlevel 1 exit /b 1
copy /y "使用说明.txt" "dist\AutoFish\使用说明.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Path 'dist\AutoFish' -DestinationPath 'dist\AutoFish-windows.zip' -Force"
echo.
echo 完成：dist\AutoFish-windows.zip
echo 解压后双击 AutoFish.exe
pause
