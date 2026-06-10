@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Spirt AI-доска запускается...
echo Откроется в браузере: http://localhost:8787
echo Чтобы закрыть — закройте это окно.
echo.
py proxy.py
pause
