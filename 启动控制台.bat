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

echo 正在检查依赖，首次运行需要几分钟...
python -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo 依赖安装失败，请把上面的报错截图发给开发者
    pause
    exit /b 1
)

python webui.py
pause
