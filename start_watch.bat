@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Arcus-Entropy 本地对冲
echo ========================================
echo  本地对冲循环（实盘）
echo  密钥只读取本目录的 API.txt，不会上传
echo  不要关闭本窗口。按 Ctrl+C 可停止
echo ========================================
py watch.py
echo.
pause
