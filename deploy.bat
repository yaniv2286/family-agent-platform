@echo off
setlocal EnableDelayedExpansion

:: Family Agent Platform — Unified Deploy Script
:: ==============================================
:: Deploys Family Agent Platform, NeuralTrader, and VolatilityHunter monitoring to the VPS.
:: This is the single entry point for all family-agent deployments.

set "SERVER=root@207.154.218.23"
set "PROJECT=%~dp0"
set "ARCHIVE=%TEMP%\koko-deploy-%RANDOM%.tar.gz"
set "NEURAL_REPO=D:\GitHub\NeuralTrader"
set "NEURAL_ARCHIVE=%TEMP%\neuraltrader-deploy-%RANDOM%.tar.gz"
set "HUNTER_REPO=D:\GitHub\VolatilityHunter"
set "HUNTER_ARCHIVE=%TEMP%\volatilityhunter-deploy-%RANDOM%.tar.gz"

:: ---------------------------------------------------------------------------
:: 1. SSH authentication precondition
:: ---------------------------------------------------------------------------
if not "%~1"=="" (
    set "SSH_KEY_PATH=%~1"
) else if not defined SSH_KEY_PATH (
    if defined SSH_KEY (
        set "SSH_KEY_PATH=%SSH_KEY%"
    ) else (
        set "SSH_KEY_PATH=%USERPROFILE%\.ssh\id_ed25519"
    )
)

echo [INFO] Using SSH key: %SSH_KEY_PATH%
if not exist "%SSH_KEY_PATH%" (
    echo [ERROR] SSH key not found: %SSH_KEY_PATH%
    echo [INFO] Pass the key as the first argument, or set SSH_KEY_PATH / SSH_KEY.
    pause
    exit /b 1
)

:: Correct quote handling for OpenSSH arguments in batch files
set "SSH_OPTS=-o IdentitiesOnly=yes -i "%SSH_KEY_PATH%""

echo [INFO] Testing SSH connectivity to %SERVER%...
ssh %SSH_OPTS% %SERVER% "echo [OK] SSH connectivity verified" >nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Cannot SSH to %SERVER% with key %SSH_KEY_PATH%
    pause
    exit /b 1
)

:: ---------------------------------------------------------------------------
:: 2. Koko / family-agent-platform package
:: ---------------------------------------------------------------------------
echo [INFO] Running Koko test suite...
python -m pytest "%PROJECT%\tests" -v --ignore="%PROJECT%\tests\test_live_llm.py"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Koko tests failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Packaging family-agent-platform...
tar -czf "%ARCHIVE%" -C "%PROJECT:~0,-1%" --exclude=".git" --exclude="data" --exclude="__pycache__" --exclude=".pytest_cache" --exclude="logs" --exclude="venv" --exclude=".venv" --exclude="*.db" --exclude="*.sqlite3" --exclude=".env" --exclude="cert.pem" --exclude="key.pem" .

echo [INFO] Uploading family-agent-platform archive...
scp %SSH_OPTS% "%ARCHIVE%" %SERVER%:/root/koko-deploy.tar.gz
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Koko SCP failed
    pause
    exit /b %ERRORLEVEL%
)

:: ---------------------------------------------------------------------------
:: 3. NeuralTrader package
:: ---------------------------------------------------------------------------
if not exist "%NEURAL_REPO%\main_orchestrator_ist.py" (
    echo [ERROR] NeuralTrader repository not found at %NEURAL_REPO%
    pause
    exit /b 1
)

echo [INFO] Running NeuralTrader py_compile sanity check...
python -m py_compile "%NEURAL_REPO%\main_orchestrator_ist.py" "%NEURAL_REPO%\scripts\run_full_backtest.py" "%NEURAL_REPO%\scripts\run_simulation.py" "%NEURAL_REPO%\core\utils\notifier.py" "%NEURAL_REPO%\core\utils\telegram_notifier.py"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] NeuralTrader syntax check failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Uploading NeuralTrader .env...
ssh %SSH_OPTS% %SERVER% "mkdir -p /root/neuraltrader"
scp %SSH_OPTS% "%NEURAL_REPO%\.env" %SERVER%:/root/neuraltrader/.env
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to upload NeuralTrader .env
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Checking market data seed on the VPS...
ssh %SSH_OPTS% %SERVER% "test -f /root/neuraltrader/data/raw/AAPL.parquet" >nul
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] No market data found on VPS. Building data/raw seed archive locally...
    set "DATA_ARCHIVE=%TEMP%\neuraltrader-data-%RANDOM%.tar.gz"
    tar -czf "!DATA_ARCHIVE!" -C "%NEURAL_REPO%" data\raw
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to build data/raw archive
        pause
        exit /b !ERRORLEVEL!
    )
    echo [INFO] Uploading market data seed. This may take several minutes...
    scp %SSH_OPTS% "!DATA_ARCHIVE!" %SERVER%:/root/neuraltrader-data.tar.gz
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to upload data/raw seed
        pause
        exit /b !ERRORLEVEL!
    )
    echo [INFO] Extracting market data seed on VPS...
    ssh %SSH_OPTS% %SERVER% "mkdir -p /root/neuraltrader && cd /root/neuraltrader && tar -xzf /root/neuraltrader-data.tar.gz && rm -f /root/neuraltrader-data.tar.gz"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to extract data/raw seed on VPS
        pause
        exit /b !ERRORLEVEL!
    )
)

echo [INFO] Packaging NeuralTrader...
tar -czf "%NEURAL_ARCHIVE%" -C "%NEURAL_REPO%" --exclude=".git" --exclude="data" --exclude="logs" --exclude="reports" --exclude="__pycache__" --exclude=".pytest_cache" --exclude="venv" --exclude=".venv" --exclude="*.db" --exclude="*.sqlite3" --exclude=".env" --exclude=".env.*" .

echo [INFO] Uploading NeuralTrader archive...
scp %SSH_OPTS% "%NEURAL_ARCHIVE%" %SERVER%:/root/neuraltrader-deploy.tar.gz
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] NeuralTrader SCP failed
    pause
    exit /b %ERRORLEVEL%
)

:: ---------------------------------------------------------------------------
:: 4. VolatilityHunter package
:: ---------------------------------------------------------------------------
if not exist "%HUNTER_REPO%\scripts\daily_trading_loop.py" (
    echo [ERROR] VolatilityHunter repository not found at %HUNTER_REPO%
    pause
    exit /b 1
)
if not exist "%HUNTER_REPO%\.env" (
    echo [ERROR] VolatilityHunter .env is missing. Deployment aborted.
    pause
    exit /b 1
)

echo [INFO] Running VolatilityHunter py_compile sanity check...
python -m py_compile "%HUNTER_REPO%\scripts\daily_trading_loop.py" "%HUNTER_REPO%\scripts\update_data.py" "%HUNTER_REPO%\scripts\functional_health_check.py" "%HUNTER_REPO%\scripts\live_reconciliation.py" "%HUNTER_REPO%\src\brokerage_interface.py" "%HUNTER_REPO%\src\email_notifier.py" "%HUNTER_REPO%\src\telegram_notifier.py" "%HUNTER_REPO%\src\strategy_engine.py"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] VolatilityHunter syntax check failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Uploading VolatilityHunter .env...
ssh %SSH_OPTS% %SERVER% "mkdir -p /root/volatilityhunter"
scp %SSH_OPTS% "%HUNTER_REPO%\.env" %SERVER%:/root/volatilityhunter/.env
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to upload VolatilityHunter .env.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Checking VolatilityHunter market data seed on the VPS...
ssh %SSH_OPTS% %SERVER% "test -f /root/volatilityhunter/data/portfolio.json" >nul
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] No VolatilityHunter state found on VPS. Building data seed archive locally...
    set "DATA_ARCHIVE=%TEMP%\volatilityhunter-data-%RANDOM%.tar.gz"
    tar -czf "!DATA_ARCHIVE!" -C "%HUNTER_REPO%" data\raw 2>nul || echo [INFO] data\raw not present locally, skipping seed.
    if exist "!DATA_ARCHIVE!" (
        echo [INFO] Uploading VolatilityHunter data seed. This may take several minutes...
        scp %SSH_OPTS% "!DATA_ARCHIVE!" %SERVER%:/root/volatilityhunter-data.tar.gz
        if !ERRORLEVEL! NEQ 0 (
            echo [ERROR] Failed to upload VolatilityHunter data seed
            pause
            exit /b !ERRORLEVEL!
        )
        echo [INFO] Extracting VolatilityHunter data seed on VPS...
        ssh %SSH_OPTS% %SERVER% "mkdir -p /root/volatilityhunter && cd /root/volatilityhunter && tar -xzf /root/volatilityhunter-data.tar.gz && rm -f /root/volatilityhunter-data.tar.gz"
        if !ERRORLEVEL! NEQ 0 (
            echo [ERROR] Failed to extract VolatilityHunter data seed on VPS
            pause
            exit /b !ERRORLEVEL!
        )
    )
)

echo [INFO] Packaging VolatilityHunter without mutable trading state...
tar -czf "%HUNTER_ARCHIVE%" -C "%HUNTER_REPO%" --exclude=".git" --exclude="data" --exclude="logs" --exclude="reports" --exclude="archive" --exclude="__pycache__" --exclude=".pytest_cache" --exclude="venv" --exclude=".venv" --exclude="*.db" --exclude="*.sqlite3" --exclude=".env" --exclude=".env.*" .
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] VolatilityHunter packaging failed.
    pause
    exit /b %ERRORLEVEL%
)
scp %SSH_OPTS% "%HUNTER_ARCHIVE%" %SERVER%:/root/volatilityhunter-deploy.tar.gz
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] VolatilityHunter SCP failed.
    pause
    exit /b %ERRORLEVEL%
)

:: ---------------------------------------------------------------------------
:: 5. Deploy all three stacks on the VPS
:: ---------------------------------------------------------------------------
echo [INFO] Deploying all stacks on %SERVER%...
ssh %SSH_OPTS% %SERVER% "docker network inspect family-agents-net >/dev/null 2>&1 || docker network create family-agents-net; cd /root && rm -rf venv .venv __pycache__ .pytest_cache logs && rm -f koko-deploy.zip dataclasses.py numbers.py *.py *.db *.sqlite3 && tar -xzf koko-deploy.tar.gz && rm -f koko-deploy.tar.gz && docker compose down && docker compose up -d --build && docker compose ps && mkdir -p /root/neuraltrader && cd /root/neuraltrader && rm -rf core scripts models docs temp *.py *.toml *.txt *.sh *.yml *.md && tar -xzf /root/neuraltrader-deploy.tar.gz && rm -f /root/neuraltrader-deploy.tar.gz && docker compose -f docker-compose.yml -p neuraltrader down && docker compose -f docker-compose.yml -p neuraltrader up -d --build && docker compose -p neuraltrader ps && mkdir -p /root/volatilityhunter && cd /root/volatilityhunter && rm -rf src scripts config *.py *.toml *.txt *.sh *.yml *.md && tar -xzf /root/volatilityhunter-deploy.tar.gz && rm -f /root/volatilityhunter-deploy.tar.gz && docker compose -f docker-compose.yml -p volatilityhunter down && docker compose -f docker-compose.yml -p volatilityhunter up -d --build && docker compose -p volatilityhunter ps"

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Remote deployment failed
    pause
    exit /b %ERRORLEVEL%
)

echo [OK] Deployment complete.
pause