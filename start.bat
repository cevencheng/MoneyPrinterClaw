@echo off
setlocal enabledelayedexpansion
chcp 65001 >NUL
title MoneyPrinterClaw Launcher

echo ========================================
echo   MoneyPrinterClaw Launcher
echo ========================================
echo.

set "BACKEND_DIR=%~dp0app"
set "FRONTEND_DIR=%~dp0webui"

echo [1/4] Check port 8000 ...
netstat -ano ^| findstr ":8000 " ^| findstr LISTENING >NUL 2>&1
if !errorlevel! equ 0 (
    echo     Port 8000 in use, backend may be running
    goto check_frontend
)

echo.
echo [2/4] Starting backend uvicorn :8000 ...
start "MPC Backend" cmd /k "cd /d %BACKEND_DIR% && python -m uvicorn api_view.web_main:app --host 127.0.0.1 --port 8000"
echo     Backend started in new window

:check_frontend
echo.
echo [3/4] Check port 5200 ...
netstat -ano ^| findstr ":5200 " ^| findstr LISTENING >NUL 2>&1
if !errorlevel! equ 0 (
    echo     Port 5200 in use, frontend may be running
    goto wait
)

echo     Starting frontend next dev :5200 ...
start "MPC Frontend" cmd /k "cd /d %FRONTEND_DIR% && npx next dev --port 5200"
echo     Frontend started in new window

:wait
echo.
echo [4/4] Waiting for backend (max 30s) ...
for /l %%i in (1,1,30) do (
    curl -s http://127.0.0.1:8000/api/threads >NUL 2>&1
    if !errorlevel! equ 0 goto ready
    timeout /t 1 /nobreak >NUL
)
echo     Backend not ready, check backend window

:ready
echo     Services ready
echo.
echo ========================================
echo   Frontend:  http://localhost:5200
echo   Backend:   http://localhost:8000
echo ========================================
echo   Close: close the two popup windows
echo.

start http://localhost:5200
pause
