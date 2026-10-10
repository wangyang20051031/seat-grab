@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在一键签退（263 / 258 两个座位）...
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python oneclick.py signback
) else (
    py oneclick.py signback
)

echo.
echo ====================================
echo 处理结束，结果已弹窗并推送到微信。
echo ====================================
pause
