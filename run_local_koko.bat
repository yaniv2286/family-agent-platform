@echo off
setlocal EnableDelayedExpansion

REM Koko local dev runner - no Docker, no Caddy
REM Runs the FastAPI backend directly on http://127.0.0.1:8000

set "PROJECT=%~dp0"
set "PROJECT_ROOT=%PROJECT:~0,-1%"

cd /d "%PROJECT_ROOT%"

echo [INFO] Running Koko mock test suite
python -m pytest tests -v --ignore=tests/test_live_llm.py
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Tests failed - not starting server
    pause
    exit /b !ERRORLEVEL!
)

echo [INFO] Starting Koko backend on http://127.0.0.1:8000
python main.py
