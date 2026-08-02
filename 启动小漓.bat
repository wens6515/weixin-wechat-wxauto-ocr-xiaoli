@echo off
chcp 65001 >nul
echo 正在启动小漓（合并版 · 天枢任务桥）...
cd /d D:\AI\小漓
call D:\AI\wxauto-mcp\wechat4_env\Scripts\activate
python xiaoli_bot.py --run
pause
