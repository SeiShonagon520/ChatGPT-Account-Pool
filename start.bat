@echo off
chcp 65001 >nul
cd /d "%~dp0"
title aBaiFreeGPT - 账号管理服务

echo ========================================================
echo   正在启动 aBaiFreeGPT (本地 Python 原生模式)...
echo   访问地址: http://127.0.0.1:8000
echo ========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv 虚拟环境，请先初始化 Python 环境。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py

if errorlevel 1 (
    echo.
    echo [提示] 服务异常退出，请查看上方报错信息。
    pause
)
