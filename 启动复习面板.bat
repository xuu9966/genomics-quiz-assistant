@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title 章节自测复习助手 - 本机面板（请保持本窗口开启）

echo ============================================
echo  章节自测复习助手 · 本机面板
echo  仅在本机运行（127.0.0.1），账号密码只在网页填写
echo ============================================
echo.

rem --- 1) 定位 Python 解释器：优先 py 启动器，其次 PATH 中的 python ---
set "PY="
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -c "import sys;assert sys.version_info>=(3,10)" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1
  if %errorlevel%==0 (
    python -c "import sys;assert sys.version_info>=(3,10)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  echo [错误] 未找到 Python 3.10+。请安装并在安装时勾选 "Add Python to PATH"。
  echo 安装后重新双击本脚本。
  echo.
  pause
  exit /b 1
)

rem --- 2) 首次使用：由模板生成 config.json（不含任何真实凭据）---
if not exist "config.json" (
  if exist "config.example.json" (
    copy /y "config.example.json" "config.json" >nul
    echo [提示] 已由 config.example.json 生成 config.json（已预配课程站点地址）。
    echo [提示] 通常无需修改；学号与密码稍后在网页表单里填写。
    echo.
  ) else (
    echo [错误] 缺少 config.example.json，请重新完整 clone 项目。
    pause
    exit /b 1
  )
)

rem --- 3) 前台启动面板；--open-browser 由 Python 打开实际端口 ---
echo 正在启动本机面板，浏览器将自动打开；若未自动打开请看下方的地址。
echo 面板只在本机可访问；关闭本窗口会停止面板，请先完成复习或保留窗口。
echo.
%PY% main.py serve --open-browser

rem --- 4) 任何异常退出都停在窗口里，避免闪退看不清原因 ---
echo.
echo 面板已停止。若上方出现错误信息，请截图反馈。
echo 若提示端口占用，程序已自动顺延到 8765 之后的端口，请以窗口显示的地址为准。
pause
