# Koko Project Notes

## Verification Check After Code Changes

Always run the mock test suite before considering a change complete to ensure no regressions were introduced.

```powershell
python -m pytest tests -v --ignore=tests/test_live_llm.py
```

This command runs the fast, no-cost functional tests (PIN auth, chat, anti-cheat hard cap, idempotency, mocked Whisper, and fuzzing). The live LLM integration tests in `tests/test_live_llm.py` are excluded by default.

## Mobile Layout Verification

`scripts/check_mobile.py` exercises the real HTML with mocked API responses and touch-enabled phone viewports. It requires the optional local package `playwright==1.56.0` and installed Chrome. Run from the repository root:

```powershell
python scripts/check_mobile.py
python -m playwright install webkit
python scripts/check_mobile.py --browser webkit
```

The script does not need a running backend or make live LLM calls. It checks layout and basic touch interactions, not real microphone permissions, soft keyboards, audio quality, or complete tutoring conversations on a physical phone.

`scripts/check_voice.py` uses mocked browser capture APIs and mocked server responses to check recognition-end submission, mobile recorder selection, manual submission, cancellation, delayed microphone permissions, and suspended audio analysers. Run `python scripts/check_voice.py` and `python scripts/check_voice.py --browser webkit` with the same optional Playwright installation. These are lifecycle regressions, not an acoustic or real-device microphone test.

## Async SQLAlchemy: never read ORM attributes after commit

`SessionLocal` uses the default `expire_on_commit=True`. Reading any ORM
attribute after `await db.commit()` triggers a lazy reload, which performs sync
IO inside the async request and raises `sqlalchemy.exc.MissingGreenlet` — a
hard HTTP 500. This shipped three broken Coach endpoints once already.

Either capture plain Python locals **before** the commit, or `await db.refresh(obj)`
after it. Note the commit itself succeeds, so the failure mode is "data saved,
client sees an error" — easy to miss without an endpoint-level test.

## Tests must call endpoints, not re-implement them

Do not work around test-harness errors by reproducing endpoint logic with raw DB
queries in the test file. That is how the `MissingGreenlet` bugs above stayed
hidden behind a fully green suite. If a test cannot reach the endpoint through
`client`, treat it as a bug in the endpoint.

## Deployment After Development

After completing and verifying any code change, deployment is part of the definition of done. Ask the user for explicit confirmation immediately before deployment because `deploy.bat` replaces files and restarts services on the production VPS. Once confirmed, run:

```powershell
.\deploy.bat
```

Do not report the development task as fully complete until deployment succeeds and the production services are healthy. If deployment fails, report the exact blocker and keep the task open. Never deploy when tests fail.

## Built-In Agents and Remote Agent Configuration

The following agents are built into the Koko backend and run in-process:

- `koko_math` and `koko_english` — accessed via `/api/tutor/chat` and `/api/tutor/english`.
- `fitness` — accessed via `/api/fitness/*` endpoints.
- `nutrition` — currently an event-only collaborator triggered by fitness workouts.

By default the `AgentConnector` returns a local snapshot for these agents instead of making an outbound HTTP call. To override this and route control-plane commands to a remote instance, set:

```powershell
$env:AGENT_KOKO_MATH_URL = "http://localhost:8000"
$env:AGENT_KOKO_ENGLISH_URL = "http://localhost:8000"
$env:AGENT_FITNESS_URL = "http://localhost:8000"
$env:AGENT_NUTRITION_URL = "http://localhost:8000"
$env:AGENT_DEFAULT_SECRET = "your-shared-secret"
```

Remote trading agents **require** their own URL and secret:

```powershell
$env:AGENT_NEURALTRADER_URL = "http://neuraltrader-control:8000"
$env:AGENT_NEURALTRADER_SECRET = "..."
$env:AGENT_VOLATILITYHUNTER_URL = "http://volatilityhunter_monitoring:8080"
$env:AGENT_VOLATILITYHUNTER_SECRET = "..."
```

Without these, control-plane telemetry/commands for NeuralTrader and VolatilityHunter will fail.

## Agent Mesh and Platform Agents

The platform now uses an **Agent Mesh** (`agent_mesh.py`) for in-process agent collaboration:

- **Publish/subscribe events** (fire-and-forget): e.g. `fitness.workout_completed` triggers the nutrition agent.
- **Request/response asks** (blocking with audit trail): any agent can `await agent_mesh.ask(to_agent, intent, ...)` and get a result.

Built-in platform agents are registered in `registered_agents` on startup and loaded dynamically from `agent_loader.py`:

- `orchestrator` — meta-agent that runs scheduled intents (`daily_summary`) and coordinates other agents.
- `messenger` — cross-channel messaging (Telegram today, email/WhatsApp stubs for the future).
- `data` — read-only data/memory query agent for other agents to ask questions about family data.
- `nutrition` — full agent with its own endpoints and mesh handlers.
- `fitness` — existing Coach agent.
- `koko_math` / `koko_english` — existing tutors.

To add a new built-in agent:

1. Create its module under `agents/` (or at the root) and expose a FastAPI `router`.
2. Add it to `database.BUILTIN_AGENTS` with `is_builtin=1`, `module_path`, and `router_attr="router"`.
3. Register any mesh handlers with `agent_mesh.on_event(...)` or `agent_mesh.on_ask(...)`.
4. Run the mock test suite to ensure no regressions.

## Child Simulation (pre-deploy gate)

`scripts/simulate_children.py` role-plays all 4 kids (ליבי/לביא/ינאי/נויה) against the REAL KOKO via `/api/tutor/english` — a second LLM generates age-appropriate child utterances, then deterministic checks run: Hebrew-first opening, gender-consistent address, immersion ratio vs. demonstrated level, no colors-by-default, task-ended turns, and Lavi's garbled-'קר'(car) acceptance. Requires OPENAI_API_KEY; costs a few cents.

```powershell
python scripts/simulate_children.py            # all kids, 10 turns
python scripts/simulate_children.py --kids ליבי --turns 6 --verbose
```

Run before every deploy alongside the mock suite. It exits non-zero on failures — do not deploy if it fails.
