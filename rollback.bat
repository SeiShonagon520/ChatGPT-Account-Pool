@echo off
chcp 65001 >nul
cd /d "%~dp0"
title aBaiFreeGPT - 版本管理与回退工具

echo ========================================================
echo   aBaiFreeGPT 版本管理与回退工具
echo ========================================================
echo.
echo 当前本地可用版本标签：
git tag -n1
echo.
echo 当前提交记录 (最近 5 次)：
git log -n 5 --oneline
echo.
echo 如需一键回退到稳定版本 v1.2.0，输入 Y 确认回退，或按任意键退出：
set /p opt="确认回退到 v1.2.0 吗？[Y/N]: "
if /i "%opt%"=="Y" (
    git checkout v1.2.0
    echo [成功] 已回退至 v1.2.0 稳定状态！
) else (
    echo [取消] 未执行回退。
)
pause
