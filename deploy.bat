@echo off & setlocal EnableDelayedExpansion

:: VolatilityHunter is disabled for remote deployment by default (run locally).
:: Set DEPLOY_VOLATILITYHUNTER=1 to also deploy it to the VPS.
if not defined DEPLOY_VOLATILITYHUNTER set "DEPLOY_VOLATILITYHUNTER=0"

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

:: Use Windows system tar so archive paths like C:\Users\... are not
:: misinterpreted as remote hosts by MSYS tar ("Cannot connect to C:").
set "TAR=C:\Windows\System32\tar.exe"

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

echo [INFO] Running Koko startup validator...
python "%PROJECT%\scripts\startup_validator.py"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Koko startup validator failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Checking Koko docker compose configuration...
docker compose -f "%PROJECT%\docker-compose.yml" config >nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Koko docker-compose.yml is invalid. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Writing deployment metadata...
python -c "import json; from datetime import datetime, timezone; from pathlib import Path; Path(r'%PROJECT%build_info.json').write_text(json.dumps({'deployed_at': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to write deployment metadata. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Packaging family-agent-platform...
python "%PROJECT%scripts\create_deploy_archive.py" "%PROJECT:~0,-1%" "%ARCHIVE%" --exclude ".git" --exclude "data" --exclude "__pycache__" --exclude ".pytest_cache" --exclude "logs" --exclude "venv" --exclude ".venv" --exclude "*.db" --exclude "*.sqlite3" --exclude ".env" --exclude "cert.pem" --exclude "key.pem"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Koko packaging failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Uploading family-agent-platform archive...
scp %SSH_OPTS% "%ARCHIVE%" %SERVER%:/root/koko-deploy.tar.gz
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Koko SCP failed
    pause
    exit /b %ERRORLEVEL%
)

if not exist "%PROJECT%\.env" (
    echo [ERROR] Koko .env is missing. Deployment aborted.
    pause
    exit /b 1
)
echo [INFO] Uploading Koko .env...
ssh %SSH_OPTS% %SERVER% "mkdir -p /root/koko"
scp %SSH_OPTS% "%PROJECT%\.env" %SERVER%:/root/koko/.env
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to upload Koko .env
    pause
    exit /b %ERRORLEVEL%
)

:: ---------------------------------------------------------------------------
:: 2b. Detect whether NeuralTrader code changed since the last deploy. When it
:: did not, the entire NeuralTrader section (checks, packaging, upload, remote
:: rebuild) is skipped - it is the slowest part of a no-op deploy.
:: ---------------------------------------------------------------------------
python "%PROJECT%scripts\hash_tree.py" "%NEURAL_REPO%" ".git" "data" "logs" "reports" "__pycache__" ".pytest_cache" "venv" ".venv" > "%TEMP%\nt-local-hash.txt"
set /p NEURAL_HASH=<"%TEMP%\nt-local-hash.txt"
ssh %SSH_OPTS% %SERVER% "cat /root/neuraltrader/.deploy_hash 2>nul" > "%TEMP%\nt-remote-hash.txt"
set /p NEURAL_REMOTE_HASH=<"%TEMP%\nt-remote-hash.txt"
if "%NEURAL_HASH%"=="%NEURAL_REMOTE_HASH%" (
    set "NEURAL_CHANGED=0"
    echo [INFO] NeuralTrader unchanged since last deploy - skipping its build entirely.
) else (
    set "NEURAL_CHANGED=1"
)

:: ---------------------------------------------------------------------------
:: 3. NeuralTrader package
:: ---------------------------------------------------------------------------
if "%NEURAL_CHANGED%"=="0" goto :after_neural
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

echo [INFO] Running NeuralTrader startup validator...
python "%NEURAL_REPO%\scripts\startup_validator.py"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] NeuralTrader startup validator failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Checking NeuralTrader docker compose configuration...
docker compose -f "%NEURAL_REPO%\docker-compose.yml" config >nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] NeuralTrader docker-compose.yml is invalid. Deployment aborted.
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
    !TAR! -czf "!DATA_ARCHIVE!" -C "%NEURAL_REPO%" data\raw
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
python "%PROJECT%scripts\create_deploy_archive.py" "%NEURAL_REPO%" "%NEURAL_ARCHIVE%" --exclude ".git" --exclude "data" --exclude "logs" --exclude "reports" --exclude "__pycache__" --exclude ".pytest_cache" --exclude "venv" --exclude ".venv" --exclude "*.db" --exclude "*.sqlite3" --exclude ".env" --exclude ".env.*"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] NeuralTrader packaging failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Uploading NeuralTrader archive...
scp %SSH_OPTS% "%NEURAL_ARCHIVE%" %SERVER%:/root/neuraltrader-deploy.tar.gz
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] NeuralTrader SCP failed
    pause
    exit /b %ERRORLEVEL%
)

:after_neural
:: ---------------------------------------------------------------------------
:: 4. VolatilityHunter package
:: ---------------------------------------------------------------------------
if "%DEPLOY_VOLATILITYHUNTER%"=="0" goto :deploy_stacks
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

echo [INFO] Running VolatilityHunter startup validator...
python "%HUNTER_REPO%\scripts\startup_validator.py"
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] VolatilityHunter startup validator failed. Deployment aborted.
    pause
    exit /b %ERRORLEVEL%
)

echo [INFO] Checking VolatilityHunter docker compose configuration...
docker compose -f "%HUNTER_REPO%\docker-compose.yml" config >nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] VolatilityHunter docker-compose.yml is invalid. Deployment aborted.
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
%TAR% -czf "%HUNTER_ARCHIVE%" -C "%HUNTER_REPO%" --exclude=".git" --exclude="data" --exclude="logs" --exclude="reports" --exclude="archive" --exclude="__pycache__" --exclude=".pytest_cache" --exclude="venv" --exclude=".venv" --exclude="*.db" --exclude="*.sqlite3" --exclude=".env" --exclude=".env.*" .
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

:deploy_stacks
:: ---------------------------------------------------------------------------
:: 5. Deploy Koko and NeuralTrader stacks on the VPS
:: ---------------------------------------------------------------------------
if "%NEURAL_CHANGED%"=="0" goto :deploy_koko_only
echo [INFO] Deploying Koko and NeuralTrader stacks on %SERVER% - building in parallel...
ssh %SSH_OPTS% %SERVER% "gzip -t /root/koko-deploy.tar.gz && gzip -t /root/neuraltrader-deploy.tar.gz && docker network inspect family-agents-net >/dev/null 2>&1 || docker network create family-agents-net; ( mkdir -p /root/koko && cd /root/koko && rm -rf venv .venv __pycache__ .pytest_cache logs && tar -xzf /root/koko-deploy.tar.gz && rm -f /root/koko-deploy.tar.gz && if [ -d /root/data/koko ] && [ ^! -d /root/koko/data/koko ]; then mkdir -p /root/koko/data && mv /root/data/koko /root/koko/data/koko; fi && docker compose up -d --build && docker compose ps ) & KPID=$^!; ( mkdir -p /root/neuraltrader && cd /root/neuraltrader && rm -rf core scripts models docs temp *.py *.toml *.txt *.sh *.yml *.md && tar -xzf /root/neuraltrader-deploy.tar.gz && rm -f /root/neuraltrader-deploy.tar.gz && docker compose -f docker-compose.yml -p neuraltrader down && docker compose -f docker-compose.yml -p neuraltrader up -d --build && docker compose -p neuraltrader ps ) & NPID=$^!; wait $KPID; KOKO_RC=$?; wait $NPID; NT_RC=$?; if [ $KOKO_RC -ne 0 ] || [ $NT_RC -ne 0 ]; then exit 1; fi; echo %NEURAL_HASH% > /root/neuraltrader/.deploy_hash"
goto :after_stack_deploy

:deploy_koko_only
echo [INFO] Deploying Koko stack on %SERVER%...
ssh %SSH_OPTS% %SERVER% "set -e; gzip -t /root/koko-deploy.tar.gz; docker network inspect family-agents-net >/dev/null 2>&1 || docker network create family-agents-net; mkdir -p /root/koko && cd /root/koko && rm -rf venv .venv __pycache__ .pytest_cache logs && tar -xzf /root/koko-deploy.tar.gz && rm -f /root/koko-deploy.tar.gz && if [ -d /root/data/koko ] && [ ^! -d /root/koko/data/koko ]; then mkdir -p /root/koko/data && mv /root/data/koko /root/koko/data/koko; fi && docker compose up -d --build && docker compose ps"

:after_stack_deploy

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Remote deployment failed
    pause
    exit /b %ERRORLEVEL%
)

if "%DEPLOY_VOLATILITYHUNTER%"=="1" (
    echo [INFO] Deploying VolatilityHunter stack on %SERVER%...
    ssh %SSH_OPTS% %SERVER% "mkdir -p /root/volatilityhunter && cd /root/volatilityhunter && rm -rf src scripts config *.py *.toml *.txt *.sh *.yml *.md && tar -xzf /root/volatilityhunter-deploy.tar.gz && rm -f /root/volatilityhunter-deploy.tar.gz && docker compose -f docker-compose.yml -p volatilityhunter down && docker compose -f docker-compose.yml -p volatilityhunter up -d --build && docker compose -p volatilityhunter ps"

    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] VolatilityHunter remote deployment failed
        pause
        exit /b %ERRORLEVEL%
    )
)

:: ---------------------------------------------------------------------------
:: 6. Remote smoke tests
:: ---------------------------------------------------------------------------
echo [INFO] Waiting for services to start...
ping -n 21 127.0.0.1 >nul

echo [INFO] Running remote health smoke tests...
ssh %SSH_OPTS% %SERVER% "curl -fsS --max-time 10 https://lugassy-agents.duckdns.org/api/health || echo '[SMOKE] Koko public health check failed'"
ssh %SSH_OPTS% %SERVER% "docker compose -p koko ps" || echo "[SMOKE] koko stack status check failed"
ssh %SSH_OPTS% %SERVER% "docker compose -p neuraltrader ps" || echo "[SMOKE] neuraltrader stack status check failed"
if "%DEPLOY_VOLATILITYHUNTER%"=="1" ssh %SSH_OPTS% %SERVER% "docker compose -p volatilityhunter ps" || echo "[SMOKE] volatilityhunter stack status check failed"

echo [OK] Deployment complete.
pause