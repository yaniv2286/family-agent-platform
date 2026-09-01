# Deployment Map - Koko / Family Agent Platform

## Live URLs

- Main UI: `https://lugassy-agents.duckdns.org/`
- Parent dashboard: `https://lugassy-agents.duckdns.org/dashboard`
- Telegram webhook: `https://lugassy-agents.duckdns.org/api/telegram/webhook`
- Core API endpoints:
  - `GET /api/ping`
  - `GET /api/users`
  - `POST /api/tutor/chat`
  - `POST /api/tutor/english`
  - `POST /api/tutor/speech`
  - `POST /api/tutor/end-session`
  - `GET /api/history/{child_name}?subject=math|english`

## One-Click Local Deployment

Double-click `deploy.bat` in the repo root on Windows.

What it does:

1. Runs `pytest` locally (`33 tests`).
2. Creates a `koko-deploy-{random}.tar.gz` archive of the project.
3. Uploads it to the VPS (`root@207.154.218.23:/root/koko-deploy.tar.gz`).
4. Extracts it on the server into `/root`, preserving directory structure.
5. Cleans stale `venv/.venv`, `__pycache__`, `.pytest_cache`, `logs`, `*.py`, `*.db`, and `*.sqlite3` files before each build.
6. Runs `docker compose down` and `docker compose up -d --build`.
7. Streams live logs with `docker compose logs -f`.

## Repository Structure

Local working copy: `D:\GitHub\family-agent-platform`

Key files:

- `main.py` — FastAPI app.
- `tutors.py` — Math/English tutor logic and LLM parsing.
- `database.py` — SQLAlchemy models, migration helpers, chat persistence.
- `scheduler.py` — Daily orchestrator job.
- `orchestrator.py` — Telegram summary orchestration.
- `seed_db.py` — Seed command for student profiles.
- `static/index.html` — Kids-facing tutor UI.
- `static/dashboard.html` — Parent dashboard.
- `Dockerfile` — Python 3.11-slim image.
- `docker-compose.yml` — Caddy + `koko-backend` services.
- `Caddyfile` — HTTPS gateway config for `lugassy-agents.duckdns.org`.
- `deploy.bat` — Windows one-click deploy.
- `deploy.sh` — Optional manual deploy script.
- `.dockerignore` — Keeps venv, .git, .env, DB files, and local artifacts out of the image.
- `DEPLOYMENT_MAP.md` — This file.
- `AGENTS.md` — Project rules (test command, etc.).

## Server Layout

VPS working directory: `/root`

- `/root/.env` — All secrets. Never committed.
- `/root/Caddyfile` — Caddy HTTPS config.
- `/root/docker-compose.yml` — Compose orchestration.
- `/root/data/koko/` — Persistent SQLite data (`family_platform.db`, `tutor_history.db`).
- `/root/Dockerfile`, `main.py`, `tutors.py`, `database.py`, etc. — Application code.

Inside the `koko-backend` container:

- `/app/main.py` — FastAPI entry point.
- `/app/database.py` — DB setup.
- `/app/tutors.py` — Tutor logic.
- `/app/scheduler.py` — Daily job.
- `/app/data/` — Mounted to `/root/data/koko` on the host.

## Secrets

All secrets are defined in `/root/.env` only:

- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_SECRET_TOKEN`
- `TELEGRAM_CHAT_ID`
- `APP_PIN`

Never commit these. They are also excluded from the deploy archive and from the Docker build context.

## Common Maintenance Commands

Run on the server:

```bash
# Start / recreate
docker compose up -d --build

# Stop
docker compose down

# View logs
docker compose logs -f koko-backend
docker compose logs -f caddy

# Restart one service
docker compose restart koko-backend

# Verify env vars inside the container
docker compose exec koko-backend env | grep -E 'TELEGRAM|OPENAI|APP_PIN'
```

## Verification

After `deploy.bat` completes:

- `docker compose ps` shows `caddy` and `koko-backend` as `Up`.
- `koko-backend` logs say `Application startup complete`.
- `https://lugassy-agents.duckdns.org/` loads the UI.
- `GET /api/ping` with `x-app-pin` returns `200`.
- `GET /api/users` with `x-app-pin` returns the student list.

## Notes

- Telegram integration is **webhook-only**. No long polling is used.
- The daily summary runs at 21:30 server time via `scheduler.py`.
- Updating `.env` requires `docker compose up -d --force-recreate` to reload.
