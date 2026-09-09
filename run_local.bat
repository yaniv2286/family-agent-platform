@echo off
setlocal EnableDelayedExpansion

set "PROJECT=%~dp0"
set "PROJECT_ROOT=%PROJECT:~0,-1%"
set "NEURAL_REPO=D:\GitHub\NeuralTrader"
set "HUNTER_REPO=D:\GitHub\VolatilityHunter"
set "SKIP_TESTS=0"

if "%~1"=="--skip-tests" set "SKIP_TESTS=1"

echo [INFO] Local stack runner
echo [INFO] family-agent-platform: %PROJECT_ROOT%
echo [INFO] NeuralTrader:          %NEURAL_REPO%
echo [INFO] VolatilityHunter:      %HUNTER_REPO%

if "%SKIP_TESTS%"=="1" (
    echo [INFO] Skipping local tests --skip-tests supplied
) else (
    echo [INFO] Running Koko mock test suite
    python -m pytest "%PROJECT_ROOT%\tests" -v --ignore="%PROJECT_ROOT%\tests\test_live_llm.py"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Koko tests failed Local start aborted
        pause
        exit /b !ERRORLEVEL!
    )
)

if not exist "%NEURAL_REPO%\main_orchestrator_ist.py" (
    echo [ERROR] NeuralTrader repository not found at %NEURAL_REPO%
    pause
    exit /b 1
)

if not exist "%HUNTER_REPO%\scripts\daily_trading_loop.py" (
    echo [ERROR] VolatilityHunter repository not found at %HUNTER_REPO%
    pause
    exit /b 1
)

echo [INFO] Building and starting family-agent-platform locally
cd /d "%PROJECT_ROOT%"
docker compose down
docker compose up -d --build
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] family-agent-platform failed to start
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Building and starting NeuralTrader locally
cd /d "%NEURAL_REPO%"
docker compose -p neuraltrader down
docker compose -p neuraltrader up -d --build
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] NeuralTrader failed to start
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Building and starting VolatilityHunter locally
cd /d "%HUNTER_REPO%"
docker compose -p volatilityhunter down
docker compose -p volatilityhunter up -d --build
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] VolatilityHunter failed to start
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Container status
echo --- family-agent-platform ---
cd /d "%PROJECT_ROOT%"
docker compose ps

echo --- NeuralTrader ---
cd /d "%NEURAL_REPO%"
docker compose -p neuraltrader ps

echo --- VolatilityHunter ---
cd /d "%HUNTER_REPO%"
docker compose -p volatilityhunter ps

echo [OK] All local stacks are running
echo [INFO] Local service endpoints
echo        - Koko UI/API http://localhost
echo        - NeuralTrader control http://localhost:8000
echo        - VolatilityHunter internal on family-agents-net
echo [INFO] Use Ctrl+C or run docker compose down commands to stop
echo        docker compose logs -f in each repo to tail logs
pause
