@echo off
chcp 65001 >nul
cd /d "%~dp0"
title aBaiFreeGPT - 一键备份工具

echo ========================================================
echo   aBaiFreeGPT 一键备份工具 (GitHub & 本地 Git)
echo   当前仓库: https://github.com/SeiShonagon520/aBaiFreeGPT
echo ========================================================
echo.

echo [1/3] 检查代码改动状态...
git status -s

echo.
echo [2/3] 提交改动并推送至您的 GitHub 备份仓库...
git add .
set msg=auto-backup-%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%
set msg=%msg: =0%
git commit -m "backup: %msg%" 2>nul
git push origin main
git push origin --tags

echo.
echo [3/3] 备份完成！您的所有最新代码与配置已安全存放在：
echo       https://github.com/SeiShonagon520/aBaiFreeGPT
echo.
pause
