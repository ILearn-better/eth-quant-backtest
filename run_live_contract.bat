@echo off
setlocal enabledelayedexpansion

:: ============================================================================
::  ETH v12 Dual-ROC Momentum Strategy - FUTURES Live Trading Launcher
::  合约(USDⓈ-M Futures)独立服务, 与现货 run_live.bat 互不影响
::  Usage: double-click run_live_contract.bat or run from command line
:: ============================================================================

title ETH v12 Live Trading System (Futures)

:: ---- Config ----
set "WX_SENDKEY=SCT391359TGd8xzPIRZUFTAfvUQOr4OH9D"
set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8081"
set "PROXY_HOST=127.0.0.1"
set "PROXY_PORT=7897"

cd /d "%PROJECT_DIR%"

echo.
echo ==================================================================
echo   ETH v12 Dual-ROC Momentum Strategy - FUTURES Live Trading
echo ==================================================================
echo.
echo   [Market]
echo     Type       : FUTURES (USD^S-M Futures / U本位永续)
echo     Symbol     : ETHUSDT
echo.
echo   [Strategy]
echo     Type       : v12 Dual-ROC + Volume Confirmation
echo     Long       : ROC8^>0 ^& ROC20^>0 ^& ROC8^>ROC20 ^& Vol^>VolMA20
echo     Short      : ROC8^<0 ^& ROC20^<0 ^& ROC8^<ROC20 ^& Vol^>VolMA20
echo     Capital    : 150 USDT / 3x Leverage
echo     Position   : 30%% per trade / 5 USDT Stop Loss
echo     Max Hold   : 72 x 1h bars (3 days)
echo.
echo   [Data Source]
echo     Klines     : wss://fstream.binance.com (WebSocket)
echo     Ticker     : wss://fstream.binance.com (1s realtime)
echo     History    : fapi.binance.com (REST, 300 bars backfill)
echo     Proxy      : %PROXY_HOST%:%PROXY_PORT% (WebSocket via proxy)
echo.
echo   [Notifications]
echo     WeChat     : ServerChan (configured, pushes on signal)
echo     Dashboard  : http://%HOST%:%PORT%  (合约)
echo     Spot panel : http://%HOST%:8080    (现货, 需另开 run_live.bat)
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

:: ---- Check Binance Futures API ----
echo   [CHECK] Testing Binance Futures API (fapi.binance.com) ...
powershell -NoProfile -Command "try{$r=Invoke-WebRequest -Uri 'https://fapi.binance.com/fapi/v1/ping' -TimeoutSec 5 -UseBasicParsing 2>$null;if($r.StatusCode -eq 200){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if %errorlevel% equ 0 (
    echo   [OK] Binance Futures API reachable
) else (
    echo   [WARN] Binance Futures API unreachable, will try proxy fallback
)

:: ---- Check port 8081 not occupied ----
echo   [CHECK] Checking port %PORT% availability ...
powershell -NoProfile -Command "try{$t=New-Object Net.Sockets.TcpClient;$t.ConnectAsync('127.0.0.1',%PORT%).Wait(1000);if($t.Connected){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if %errorlevel% equ 0 (
    echo   [WARN] Port %PORT% already in use! Another futures instance may be running.
    echo          Stop it first, or change SERVER_PORT in live_trader_contract.py
    pause
    exit /b 1
) else (
    echo   [OK] Port %PORT% is free
)

:: ---- Current ETH futures price ----
echo   [CHECK] Fetching current ETH futures price ...
for /f "delims=" %%i in ('powershell -NoProfile -Command "try{$r=Invoke-RestMethod -Uri 'https://fapi.binance.com/fapi/v1/ticker/price?symbol=ETHUSDT' -TimeoutSec 5 2>$null;if($r.price){[math]::Round([double]$r.price,2)}else{'N/A'}}catch{'N/A'}" 2^>nul') do set "ETH_PRICE=%%i"
if not "%ETH_PRICE%"=="N/A" (
    echo   [OK] Current ETH futures: %ETH_PRICE% USDT
) else (
    echo   [WARN] Cannot fetch current price
)

echo.
echo   ===============================================================
echo     Starting FUTURES live trading engine...
echo   ===============================================================
echo.
echo   >> Press Ctrl+C to stop
echo   >> Open http://%HOST%:%PORT% in browser for futures dashboard
echo.
echo   ---------------------------------------------------------------

:: ---- Launch ----
"%PYTHON%" live_trader_contract.py

:: ---- Exit message ----
echo.
echo   ===============================================================
echo     FUTURES live trading engine stopped.
echo   ===============================================================
pause
