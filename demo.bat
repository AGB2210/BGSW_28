@echo off
rem Start the OpenTelemetry Demo containers in WSL. Ctrl+C or closing this window stops them.
title OpenTelemetry Demo
wsl.exe -d Ubuntu --cd "%~dp0." -- bash scripts/demo.sh run
if errorlevel 1 pause
