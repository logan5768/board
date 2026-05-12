@echo off
REM Заливает aiboard на VDS и запускает установщик.
REM Требуется OpenSSH (встроен в Windows 10/11): команды scp и ssh.
REM Авторизация — по ключу (рекомендуется) или паролем (запросит).
REM
REM Использование:
REM     deploy.bat                       (под root@board.spirtvpn.ru:22)
REM     deploy.bat user@host             (другой логин/хост)
REM     deploy.bat user@host 2222        (нестандартный SSH-порт)

setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0\.."

set TARGET=%1
if "%TARGET%"=="" set TARGET=root@board.spirtvpn.ru

set PORT=%2
if "%PORT%"=="" set PORT=22

set REMOTE_DIR=/root/aiboard-src

echo === target: %TARGET% (port %PORT%)
echo === remote dir: %REMOTE_DIR%
echo.

echo === 1/3  ensure remote dir exists
ssh -p %PORT% %TARGET% "mkdir -p %REMOTE_DIR%/deploy"
if errorlevel 1 goto :fail

echo === 2/3  scp files
scp -P %PORT% proxy.py index.html test-api.html %TARGET%:%REMOTE_DIR%/
if errorlevel 1 goto :fail
scp -P %PORT% deploy\install.sh deploy\aiproxy.service deploy\Caddyfile %TARGET%:%REMOTE_DIR%/deploy/
if errorlevel 1 goto :fail

echo === 3/3  run installer on the server
ssh -p %PORT% %TARGET% "cd %REMOTE_DIR% && bash deploy/install.sh"
if errorlevel 1 goto :fail

echo.
echo === Done. Open https://board.spirtvpn.ru
pause
exit /b 0

:fail
echo.
echo *** Deploy failed (errorlevel=%errorlevel%). See messages above.
pause
exit /b 1
