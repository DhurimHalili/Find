@echo off
title Roblox Acquisition CRM
cd /d "%~dp0"
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8780"
python crm_server.py
