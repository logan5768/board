@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting local CORS proxy on http://localhost:8787
echo Upstream: https://api.zenoid.space
echo.
echo In aiboard settings (cog icon) set Endpoint to: http://localhost:8787
echo Press Ctrl+C to stop.
echo.
py proxy.py
pause
