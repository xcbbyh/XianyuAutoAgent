@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 闲鱼 AutoAgent 控制台

python --version >nul 2>&1
if errorlevel 1 (
    echo 没有找到 Python，请先安装 Python 3.10 以上版本，安装时勾选 Add python.exe to PATH
    pause
    exit /b 1
)

if not exist ".deps_installed" (
    echo 首次运行，正在安装依赖，请稍等...
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo 依赖安装失败，请把上面的报错截图发给开发者
        pause
        exit /b 1
    )
    echo ok> .deps_installed
)

python webui.py
pause
