@echo off
rem Start the On-Call Console and Action API in WSL. Ctrl+C or closing this window stops them.
title On-Call Agent
wsl.exe -d Ubuntu --cd "%~dp0." -- bash scripts/serve.sh
if errorlevel 1 pause
