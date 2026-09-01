@echo off
setlocal

set "SERVER=root@207.154.218.23"
set "PROJECT=%~dp0"
set "ARCHIVE=%TEMP%\koko-deploy-%RANDOM%.tar.gz"

python -m pytest "%PROJECT%\tests" -v --ignore="%PROJECT%\tests\test_live_llm.py"
if %ERRORLEVEL% NEQ 0 (
    pause
    exit /b %ERRORLEVEL%
)

tar -czf "%ARCHIVE%" -C "%PROJECT:~0,-1%" --exclude=".git" --exclude="data" --exclude="__pycache__" --exclude=".pytest_cache" --exclude="logs" --exclude="venv" --exclude=".venv" --exclude="*.db" --exclude="*.sqlite3" --exclude=".env" --exclude="cert.pem" --exclude="key.pem" .

scp "%ARCHIVE%" %SERVER%:/root/koko-deploy.tar.gz

ssh %SERVER% "cd /root && rm -rf venv .venv __pycache__ .pytest_cache logs && rm -f koko-deploy.zip dataclasses.py numbers.py *.py *.db *.sqlite3 && tar -xzf koko-deploy.tar.gz && rm -f koko-deploy.tar.gz && docker compose down && docker compose up -d --build && docker compose ps && docker compose logs -f"
