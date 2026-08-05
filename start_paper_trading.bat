@echo off
chcp 65001 >nul
title ETH v12 模拟盘
echo ================================================
echo  ETH v12 双ROC动量策略 - 实盘模拟盘
echo  (Ctrl+C 退出, 状态自动保存)
echo ================================================
cd /d "%~dp0"
"C:\Program Files\Python312\python.exe" -u paper_trading.py
pause
