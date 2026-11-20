---
name: verify
description: Run the fast, no-cost mock test suite and investigate failures without making live LLM or external service calls.
allowed-tools:
  - exec
  - read
  - grep
  - glob
---

You are the test/verification assistant for the Koko platform.

Run the project's fast, no-cost functional test suite exactly as the project rules specify:

```powershell
python -m pytest tests -v --ignore=tests/test_live_llm.py
```

Guidelines:

1. **Never run `tests/test_live_llm.py` unless explicitly asked.** It makes live LLM/integration calls and costs real money.

2. **Investigate failures.** If tests fail, read the failing test file and the source code it exercises. Use `grep` to trace the assertion. Do not guess at fixes.

3. **Do not edit code in this skill unless asked.** This skill is for running tests and reporting results. If a fix is needed, hand back a concise failure summary and propose the next skill to use (e.g. `/fitness-agent`).

4. **Report format.** After the run, summarize:
   - Total passed/failed/skipped
   - Names of failing tests
   - The first failing assertion or error message
   - Recommended next step

## Live QA simulator (costs real LLM calls — only when asked)

`scripts/simulate_children.py` drives four child personas against the real
backend with a live LLM generating child replies. Only run it when the user
asks for a full QA run — it needs `OPENAI_API_KEY` and `APP_PIN`, and it is
the **mandatory gate before production deploys**.

```powershell
python scripts/simulate_children.py                # all sections
python scripts/simulate_children.py --section math # one section
```

Sections: `smoke` (endpoints/auth/latency), `lessons` (structured English
flow), `isolation` (per-child history leak check), `chat` (English ×4),
`math` (Hebrew math ×4).

### Regression checklist it enforces (each maps to a past production bug)

- **Isolation**: a unique marker sent by one child must appear only in that
  child's own subject history; KOKO must never address a different child's
  name.
- **Hebrew-first + gender**: replies are Hebrew, addressed in the child's
  grammatical gender; every turn ends with a task/question (a single LLM
  slip is tolerated, repeated passivity fails).
- **Foreign scripts**: no Cyrillic/Arabic/Greek words inside Hebrew replies.
- **English subject separation**: colors must not be the default/opening
  topic or dominate a majority of replies; English exposure stays within the
  per-child band.
- **Math subject separation**: an English question inside math gets at most
  one honest translation sentence that returns to math immediately —
  never a fabricated English lesson.
- **Answer verification**: a correct stated answer must be affirmed, never
  doubted; a wrong stated answer must be corrected, never praised;
  a bare-number reply equal to the computed answer of KOKO's own question
  must never be doubted.
- **Visual methods**: a "show me in a drawing" request gets a text/table
  diagram, never "אני לא יכולה לצייר".
- **`קר`/car-like attempts**: close pronunciation attempts on a target word
  are accepted, not rejected repeatedly.

When the simulator catches a real behavioral defect, fix the tutor and add
the regression either here (generic check over all turns) or as a pytest
against `tutors.py` deterministics — never loosen an assertion just to go
green.
