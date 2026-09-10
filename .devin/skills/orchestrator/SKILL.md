---
name: orchestrator
description: Work on the Orchestrator meta-agent - daily reports, Telegram feedback, scheduling, and the run-now endpoint.
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

You are the Orchestrator specialist for the Koko platform.

Scope: `orchestrator.py`, `scheduler.py`, `/api/orchestrator/run-now` in `main.py`, and the daily report flow.

When working on this skill:

1. **Privacy rule.** Raw chat message content is fetched only to compute derived engagement stats (`message_count`, `average_words_per_message`, `is_lazy_spam`). It is **never** returned, stored in reports, or sent to an LLM. Keep this invariant.

2. **Spam thresholds.** The per-subject thresholds (`SPAM_THRESHOLDS`) are the source of truth for lazy-spam detection. If you change them, update the inline rationale and any tests that assert on the thresholds.

3. **Hebrew labels.** Reports use `SUBJECT_LABELS = {"math": "חשבון", "english": "אנגלית"}` for Telegram display. Preserve the Hebrew output.

4. **Timezone.** Local time is driven by `APP_TIMEZONE` (default `Asia/Jerusalem`). UTC bounds for "today" are derived from that timezone.

5. **Telegram.** `send_telegram_message()` posts to the chat configured via `TELEGRAM_CHAT_ID`/`TELEGRAM_BOT_TOKEN`. `check_telegram_feedback()` polls for parent replies and maps them to `add_parent_feedback()` by detected subject.

6. **Scheduler.** `start_scheduler()` wires `run_daily_orchestration` and `check_telegram_feedback` into APScheduler jobs. Keep graceful shutdown in `stop_scheduler()`.

7. **Test before finishing.** Run the orchestrator-related tests, usually in:
   ```powershell
   python -m pytest tests/test_fitness_agent.py tests/test_endpoints.py -v -k orchestrator
   ```
   (Adjust the pytest `-k` filter based on the actual test names in the repo.)
