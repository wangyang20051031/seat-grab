@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在一键签到（263 座 / 账号1）...
echo.

REM 优先用 python，找不到再试 py 启动器
where python >nul 2>nul
if %errorlevel%==0 (
    python oneclick.py signin
) else (
    py oneclick.py signin
)

echo.
echo ====================================
echo 处理结束，结果已弹窗并推送到微信。
echo ====================================
pause
