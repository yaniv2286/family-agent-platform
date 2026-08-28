# Koko Project Notes

## Verification Check After Code Changes

Always run the mock test suite before considering a change complete to ensure no regressions were introduced.

```powershell
python -m pytest tests -v --ignore=tests/test_live_llm.py
```

This command runs the fast, no-cost functional tests (PIN auth, chat, anti-cheat hard cap, idempotency, mocked Whisper, and fuzzing). The live LLM integration tests in `tests/test_live_llm.py` are excluded by default.
