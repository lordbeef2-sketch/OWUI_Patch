@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_openwebui.ps1" %*
exit /b %errorlevel%
