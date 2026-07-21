@echo off
REM ============================================================
REM  Onelap OTM 每日自动训练计划 - Windows 启动脚本
REM  在「任务计划程序」里调用本文件即可（见 DEPLOY.md 的 Windows 章节）。
REM
REM  若运行时提示“找不到 python”，把下面的 python 改成完整路径，例如：
REM    "C:\Users\你\AppData\Local\Programs\Python\Python313\python.exe"
REM  或改用 Python 启动器：  py -3 onelap_report.py --auto
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
python onelap_report.py --auto >> logs\auto.log 2>&1
