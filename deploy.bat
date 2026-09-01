@echo off
setlocal

set "SERVER=root@207.154.218.23"
set "PROJECT=%~dp0"
set "ARCHIVE=%TEMP%\koko-deploy-%RANDOM%.zip"

python -m pytest "%PROJECT%\tests" -v --ignore="%PROJECT%\tests\test_live_llm.py"
if %ERRORLEVEL% NEQ 0 (
    pause
    exit /b %ERRORLEVEL%
)

powershell -NoProfile -Command "Push-Location '%PROJECT:~0,-1%'; $base = (Get-Location).Path.Length; $files = Get-ChildItem -Recurse -File | Where-Object { $_.FullName -notmatch '\\\.git\\' -and $_.FullName -notmatch '\\data\\' -and $_.FullName -notmatch '\\__pycache__\\' -and $_.FullName -notmatch '\\\.pytest_cache\\' -and $_.FullName -notmatch '\\logs\\' -and $_.FullName -notmatch '\\venv\\' -and $_.FullName -notmatch '\\\.venv\\' -and $_.Extension -ne '.db' -and $_.Extension -ne '.sqlite3' -and $_.Name -ne '.env' -and $_.Name -ne 'cert.pem' -and $_.Name -ne 'key.pem' } | ForEach-Object { $_.FullName.Substring($base + 1) }; Compress-Archive -LiteralPath $files -DestinationPath '%ARCHIVE%' -Force; Pop-Location"

scp "%ARCHIVE%" %SERVER%:/root/koko-deploy.zip

ssh %SERVER% "cd /root && rm -rf venv .venv __pycache__ .pytest_cache logs && rm -f koko-deploy.tar.gz *.py *.db *.sqlite3 && unzip -o koko-deploy.zip && rm -f koko-deploy.zip && docker compose down && docker compose up -d --build && docker compose ps && docker compose logs -f"
