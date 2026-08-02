@echo off
chcp 65001 >nul
echo 正在启动小漓（桌面版）...
cd /d D:\AI\小漓
call .venv\Scripts\activate
python xiaoli_desktop\xiaoli_gui.py
pause
