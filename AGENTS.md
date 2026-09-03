# Koko Project Notes

## Verification Check After Code Changes

Always run the mock test suite before considering a change complete to ensure no regressions were introduced.

```powershell
python -m pytest tests -v --ignore=tests/test_live_llm.py
```

This command runs the fast, no-cost functional tests (PIN auth, chat, anti-cheat hard cap, idempotency, mocked Whisper, and fuzzing). The live LLM integration tests in `tests/test_live_llm.py` are excluded by default.

## Deployment After Development

After completing and verifying any code change, deployment is part of the definition of done. Ask the user for explicit confirmation immediately before deployment because `deploy.bat` replaces files and restarts services on the production VPS. Once confirmed, run:

```powershell
.\deploy.bat
```

Do not report the development task as fully complete until deployment succeeds and the production services are healthy. If deployment fails, report the exact blocker and keep the task open. Never deploy when tests fail.
