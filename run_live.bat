@echo off
setlocal enabledelayedexpansion

:: ============================================================================
::  ETH v12 Dual-ROC Momentum Strategy - Live Trading Launcher
::  Usage: double-click run_live.bat or run from command line
:: ============================================================================

title ETH v12 Live Trading System

:: ---- Config ----
set "WX_SENDKEY=SCT391359TGd8xzPIRZUFTAfvUQOr4OH9D"
set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8080"
set "PROXY_HOST=127.0.0.1"
set "PROXY_PORT=7897"

cd /d "%PROJECT_DIR%"

echo.
echo ==================================================================
echo   ETH v12 Dual-ROC Momentum Strategy - Live Trading System
echo ==================================================================
echo.
echo   [Strategy]
echo     Type       : v12 Dual-ROC + Volume Confirmation
echo     Long       : ROC8>0 ^& ROC20>0 ^& ROC8>ROC20 ^& Vol>VolMA20
echo     Short      : ROC8^<0 ^& ROC20^<0 ^& ROC8^<ROC20 ^& Vol>VolMA20
echo     Capital    : 150 USDT / 3x Leverage
echo     Position   : 30%% per trade / 5 USDT Stop Loss
echo     Max Hold   : 72 x 1h bars (3 days)
echo     Backtest   : +140.51%%
echo.
echo   [Data Source]
echo     Klines     : wss://stream.binance.com:9443 (WebSocket)
echo     Ticker     : wss://stream.binance.com:9443 (1s realtime)
echo     History    : api.binance.com (REST, 300 bars backfill)
echo     Proxy      : %PROXY_HOST%:%PROXY_PORT% (WebSocket via proxy)
echo.
echo   [Notifications]
echo     WeChat     : ServerChan (configured, pushes on signal)
echo     Dashboard  : http://%HOST%:%PORT%
echo     API        : http://%HOST%:%PORT%/api/data
echo.
echo ==================================================================

:: ---- Check Python venv ----
if not exist "%PYTHON%" (
    echo   [ERR] Virtual env not found: %PYTHON%
    echo   Run: python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
echo   [OK] Python venv: %PYTHON%

:: ---- Check proxy ----
echo   [CHECK] Testing proxy %PROXY_HOST%:%PROXY_PORT% ...
powershell -NoProfile -Command "try {$t=New-Object Net.Sockets.TcpClient;$t.ConnectAsync('127.0.0.1',7897).Wait(3000);if($t.Connected){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if %errorlevel% equ 0 (
    echo   [OK] Proxy %PROXY_HOST%:%PROXY_PORT% reachable
) else (
    echo   [WARN] Proxy %PROXY_HOST%:%PROXY_PORT% NOT reachable, WebSocket may fail
)

:: ---- Check Binance API ----
echo   [CHECK] Testing Binance API ...
powershell -NoProfile -Command "try{$r=Invoke-WebRequest -Uri 'https://api.binance.com/api/v3/ping' -TimeoutSec 5 -UseBasicParsing 2>$null;if($r.StatusCode -eq 200){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if %errorlevel% equ 0 (
    echo   [OK] Binance API reachable
) else (
    echo   [WARN] Binance API unreachable, will try proxy fallback
)

:: ---- Current ETH price ----
echo   [CHECK] Fetching current ETH price ...
for /f "delims=" %%i in ('powershell -NoProfile -Command "try{$r=Invoke-RestMethod -Uri 'https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT' -TimeoutSec 5 2>$null;if($r.price){[math]::Round([double]$r.price,2)}else{'N/A'}}catch{'N/A'}" 2^>nul') do set "ETH_PRICE=%%i"
if not "%ETH_PRICE%"=="N/A" (
    echo   [OK] Current ETH: %ETH_PRICE% USDT
) else (
    echo   [WARN] Cannot fetch current price
)

echo.
echo   ===============================================================
echo     Starting live trading engine...
echo   ===============================================================
echo.
echo   >> Press Ctrl+C to stop
echo   >> Open http://%HOST%:%PORT% in browser for dashboard
echo.
echo   ---------------------------------------------------------------

:: ---- Launch ----
"%PYTHON%" live_trader.py

:: ---- Exit message ----
echo.
echo   ===============================================================
echo     Live trading engine stopped.
echo   ===============================================================
pause
