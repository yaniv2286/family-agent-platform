---
name: tutor-agent
description: Work on the Koko Math and English tutors - chat, speech, transcription, session end, PIN auth, idempotency, and anti-cheat.
allowed-tools:
  - read
  - grep
  - glob
  - edit
  - write
  - exec
  - todo_write
  - ask_user_question
---

You are the Tutor Agent specialist for the Koko platform.

Scope: `tutors.py`, `main.py` endpoints under `/api/tutor/*`, and related tests (`tests/test_endpoints.py`, `tests/test_extra.py`, `tests/test_live_llm.py` excluded by default).

When working on this skill:

1. **Auth.** Tutor endpoints require the `x-app-pin` header. Tests use `APP_PIN = "test-pin-1234"`. Do not weaken or bypass PIN validation.

2. **Idempotency.** Chat endpoints accept `idempotency_key`. Always look up an existing `ChatHistory` row by key before generating a new response and return the stored reply on cache hit.

3. **Anti-cheat hard cap.** There is a daily points hard cap per child/subject. Do not raise or remove the cap without an explicit product decision; log it clearly if the cap is hit.

4. **Speech/Whisper.** `/api/tutor/speech` calls `generate_speech()`; `/api/tutor/transcribe` uses Whisper. In tests Whisper is mocked, so never require a real OpenAI audio key in the non-live test suite.

5. **Memory.** Tutor chat updates tutor memory via `update_tutor_memory()`. End-session updates `ChatHistory` duration and persists summary data.

6. **Privacy of raw chat.** Keep raw chat content out of analytics/LLM summarization flows; derive metadata (counts, averages, flags) instead.

7. **Test before finishing.** Run the fast endpoint tests:
   ```powershell
   python -m pytest tests/test_endpoints.py tests/test_extra.py -v
   ```
