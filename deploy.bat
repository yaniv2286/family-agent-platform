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

tar -czf "%ARCHIVE%" --exclude=.git --exclude=data --exclude=.env --exclude=__pycache__ --exclude=.pytest_cache -C "%PROJECT%" .

scp "%ARCHIVE%" %SERVER%:/root/koko-deploy.tar.gz

ssh %SERVER% "cd /root && tar -xzf koko-deploy.tar.gz && docker compose down && docker compose up -d --build && docker compose ps && docker compose logs koko-backend --tail=20"

pause
