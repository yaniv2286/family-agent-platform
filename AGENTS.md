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
- `meal_coach` — the full nutrition coach/child product, accessed via `/api/meals/*` (plan drafts, approve/publish, profiles, sessions, proposals, selections, eating events, health intents, bounded LLM assistant).

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
- `scout_ideas` — collects investment ideas from subscribed Telegram channels, RSS feeds, and Discord, then sends scheduled digests via the messenger agent.
- `nutrition` — full agent with its own endpoints and mesh handlers.
- `fitness` — existing Coach agent.
- `koko_math` / `koko_english` — existing tutors.

To add a new built-in agent:

1. Create its module under `agents/` (or at the root) and expose a FastAPI `router`.
2. Add it to `database.BUILTIN_AGENTS` with `is_builtin=1`, `module_path`, and `router_attr="router"`.
3. Register any mesh handlers with `agent_mesh.on_event(...)` or `agent_mesh.on_ask(...)`.
4. Run the mock test suite to ensure no regressions.

## Scout Ideas (social collection agent)

`agents/scout_ideas_agent.py` collects investment-related posts/articles on a
schedule (default twice daily, `SCOUT_DIGEST_HOURS`), dedupes them in
`scout_items`/`scout_source_state`, summarizes with the LLM, and delivers the
digest through the messenger agent. Endpoints: `POST /api/scout/run-now`,
`GET /api/scout/items`, `GET /api/scout/status`.

Configuration is entirely env-driven; a source that is not configured is
skipped silently:

```powershell
# Telegram user client - a bot cannot read the user's subscriptions, so this
# requires api_id/api_hash from https://my.telegram.org plus a StringSession
# generated once via: python scripts/scout_telegram_auth.py
$env:SCOUT_TELEGRAM_API_ID = "..."
$env:SCOUT_TELEGRAM_API_HASH = "..."
$env:SCOUT_TELEGRAM_SESSION = "..."   # keep secret - it is a login credential
$env:SCOUT_TELEGRAM_CHANNELS = "@somechannel,@anotherchannel"

# RSS / newsletters (no auth needed)
$env:SCOUT_RSS_FEEDS = "https://feed1.example.com/rss,https://feed2.example.com/atom"

# Discord - only works with a regular guild bot invited by a server admin.
# There is no ToS-compliant way to read a personal Discord account (self-bots
# get accounts banned).
$env:SCOUT_DISCORD_BOT_TOKEN = "..."
$env:SCOUT_DISCORD_CHANNEL_IDS = "123456789012345678,234567890123456789"

# Schedule (defaults shown)
$env:SCOUT_DIGEST_ENABLED = "1"
$env:SCOUT_DIGEST_HOURS = "8,20"
$env:SCOUT_DIGEST_MINUTE = "15"

# Deep-research pipeline (optional but recommended). When GEMINI_API_KEY is
# set, the digest switches from a plain summary to a 2-stage pipeline:
# (1) a cheap LLM extracts concrete asset candidates + their source channels;
# (2) each top candidate (default 8, SCOUT_RESEARCH_MAX_IDEAS) is researched by
# Gemini with Google Search grounding - fundamentals, technicals, analyst
# ratings, recent reports - and the digest shows a verdict (מומלץ/זהירות/לא
# מומלץ) + numeric reasoning + the originating Telegram channel.
# Get a key at https://aistudio.google.com (free tier is enough).
$env:GEMINI_API_KEY = "..."
$env:SCOUT_GEMINI_MODEL = "gemini-2.5-flash"
$env:SCOUT_RESEARCH_MAX_IDEAS = "8"
```

Items stay `digested=0` when the Telegram send fails, so the next run retries
them instead of losing them silently.

The digest format is Hebrew and includes per idea: asset name, verdict with
emoji, the source's direction, the research reasoning with concrete numbers,
confidence level, and `📌 ערוץ:` with every channel that mentioned the asset.
It is informational only, not investment advice.

## Full QA Bot (pre-deploy gate)

`scripts/simulate_children.py` is a complete QA bot: it role-plays all 4 kids (ליבי/לביא/ינאי/נויה, with age-appropriate LLM personas) against the REAL system and checks everything end-to-end. Sections:

- `smoke` — endpoint health, PIN auth (positive+negative), latency budgets. No LLM cost, run it always.
- `chat` — English free-chat per kid via /api/tutor/english: Hebrew-first opening, gender-consistent address, immersion ratio vs demonstrated level, no colors-by-default, task-ended turns, garbled kid-speech acceptance (Lavi's קר/car).
- `math` — math chat per kid via /api/tutor/chat: same checks + no English leaking into math.
- `lessons` — structured lesson flow: fetch slides, answer every slide (real answers from the lesson JSON), complete, verify stars/completion, rewards endpoint sanity.
- `perf` — latency report (median/p95/max) for every request; budgets: 5s non-LLM, 45s chat.

```powershell
python scripts/simulate_children.py                  # full run (~2-4 min, a few cents)
python scripts/simulate_children.py --section smoke  # fast pre-check, free
python scripts/simulate_children.py --kids ליבי --turns 6 --verbose
```

Run before every deploy alongside the mock suite. Non-zero exit = do not deploy.

## Meal Coach QA bot (pre-deploy gate)

`scripts/simulate_meal_flow.py` role-plays the whole nutrition flow: the ADMIN
coach's one-time setup (profile, draft, validate, approve, publish) and the
child's daily use (planned meal, alternatives, session rejects/missing
ingredients, dislikes, saved meals, honest eating log, corrections/voids,
health blocks) plus a Playwright UI walk and a seeded `persona` section
(P-checks: CoachPersona + ChildPersona replay a realistic week while an
invariant oracle re-checks state after every mutating action). Assertions
C1-C8 (coach), K1-K12 (child) and L1-L5 (live assistant) map to DESIGN
B01-B12/§10/§12.

```powershell
python scripts/simulate_meal_flow.py               # coach + child + ui + persona (no LLM cost)
python scripts/simulate_meal_flow.py --section coach
python scripts/simulate_meal_flow.py --section persona --seed 7
python scripts/simulate_meal_flow.py --section llm # live LLM, costs a little
python scripts/simulate_meal_flow.py --base-url http://localhost:8000
```

### Postgres lane (mandatory before deploy - production runs Postgres)

The suite and QA bot default to SQLite, which cannot catch PG-only defects
(DDL type names, transaction-local RLS `app.client_id` lost after a
commit/rollback). Both shipped broken once and were caught only here. Run
`tests/test_meal_coach.py` against a throwaway database in the local
`koko-pg-local` container (port 55432), never the dev `nutrition` DB:

```bash
docker exec koko-pg-local sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE nutrition_qa OWNER koko_owner" && psql -U "$POSTGRES_USER" -d nutrition_qa -c "GRANT USAGE ON SCHEMA public TO koko_app; ALTER DEFAULT PRIVILEGES FOR ROLE koko_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO koko_app; ALTER DEFAULT PRIVILEGES FOR ROLE koko_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO koko_app;"'
NUTRITION_DATABASE_URL="postgresql+asyncpg://koko_app:<NUTRITION_APP_PASSWORD>@localhost:55432/nutrition_qa" NUTRITION_ADMIN_DATABASE_URL="postgresql+asyncpg://koko_owner:<NUTRITION_OWNER_PASSWORD>@localhost:55432/nutrition_qa" python -m pytest tests/test_meal_coach.py -q
docker exec koko-pg-local sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE nutrition_qa"'
```

Recreate the database rather than `DROP SCHEMA` - dropping the schema also
drops the default privileges and every test fails with permission denied.
Code that commits or rolls back and then keeps querying must call
`nutrition_db.bind_rls_actor(db, user_id)` again.

Default mode uses a fresh temporary nutrition SQLite DB in-process;
`--section llm` / `--live` makes real LLM calls. Run before every deploy
alongside the mock suite and `simulate_children.py`. Non-zero exit = do not
deploy. Never loosen an assertion to go green — fix the defect and add a
regression in `tests/test_meal_coach.py`.
