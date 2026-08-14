@echo off
setlocal enabledelayedexpansion

:: ============================================================================
::  Alpha 因子实验室 - Launcher
::  世坤(WorldQuant)风格因子回测平台, 独立服务 8082
::  用法: 双击 run_alpha.bat 或命令行运行
:: ============================================================================

title Alpha 因子实验室 (Port 8082)

:: ---- Config ----
set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8082"

cd /d "%PROJECT_DIR%"

echo.
echo ==================================================================
echo   Alpha 因子实验室 - WorldQuant 风格因子回测平台
echo ==================================================================
echo.
echo   [功能]
echo     - 世坤式表达式: rank(ts_delta(close,5)) - rank(ts_delta(close,10))
echo     - 算子库: 22 个 (rank/ts_mean/ts_std/ts_corr/zscore/...)
echo     - 回测  : z-score 连续仓位 + 手续费 + 指标(夏普/IC/换手/Fitness)
echo     - 因子库: 保存 / 列表 / 加载 / 删除
echo.
echo   [入口]
echo     Dashboard  : http://%HOST%:%PORT%/alpha
echo     API        : http://%HOST%:%PORT%/alpha/api/backtest
echo.
echo   [数据]
echo     现货 data/ETHUSDT-1h.csv / 1d.csv, 合约 data/futures/ETHUSDT-1h.csv
echo.
echo ==================================================================

:: ---- Check Python venv ----
if not exist "%PYTHON%" (
    echo   [ERR] Virtual env not found: %PYTHON%
    pause
    exit /b 1
)

:: ---- Check port not occupied ----
powershell -NoProfile -Command "try{$t=New-Object Net.Sockets.TcpClient;$t.ConnectAsync('127.0.0.1',%PORT%).Wait(1000);if($t.Connected){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if %errorlevel% equ 0 (
    echo   [WARN] Port %PORT% already in use! Another instance may be running.
    pause
    exit /b 1
)

echo.
echo   ===============================================================
echo     Starting Alpha lab engine...
echo   ===============================================================
echo   >> Press Ctrl+C to stop
echo   >> Open http://%HOST%:%PORT%/alpha in browser
echo.

:: ---- Launch ----
"%PYTHON%" alpha_lab.py

:: ---- Exit message ----
echo.
echo   ===============================================================
echo     Alpha lab engine stopped.
echo   ===============================================================
pause
