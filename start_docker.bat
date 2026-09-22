@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ChatGPT-Account-Pool - Docker 重新构建与启动

echo ========================================================
echo   正在以 Docker 模式构建并启动 ChatGPT-Account-Pool...
echo   访问地址: http://127.0.0.1:8000
echo ========================================================
echo.

docker compose up -d --build

if errorlevel 1 (
    echo.
    echo [错误] Docker 启动失败，请检查 Docker Desktop 是否已开启！
    pause
    exit /b 1
)

echo.
echo [成功] 容器已基于当前最新代码重新构建并启动！
pause
