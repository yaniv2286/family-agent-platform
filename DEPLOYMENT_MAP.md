# Deployment Map - Koko / Family Agent Platform

## URLs

- Main UI: `https://lugassy-agents.duckdns.org/`
- Telegram webhook: `https://lugassy-agents.duckdns.org/api/telegram/webhook`
- Core API endpoints: `/api/ping`, `/api/tutor/chat`, `/api/users`

## Folder and File Structure

On the host (project root):

- `.env` — Next to `docker-compose.yml` (do not commit to Git).
- `docker-compose.yml` — Caddy + koko-backend service definitions.
- `data/koko/` — Persisted SQLite data directory.

Inside the `koko-backend` container:

- `/app/` — Application source.
- `/app/data/` — Bound to `./data/koko` on the host. Holds `family_platform.db` and `tutor_history.db`.
- `/app/main.py` — FastAPI entry point.
- `/app/database.py` — SQLAlchemy models and DB setup.

## Secrets and Keys

All secrets are defined only in `.env`:

- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_SECRET_TOKEN`
- `TELEGRAM_CHAT_ID`
- `APP_PIN`

Never store these in code or Git.

## Common Maintenance Commands

```bash
# Start / apply
sudo docker compose up -d

# Rebuild after code or Dockerfile change
sudo docker compose up -d --build

# View logs
sudo docker compose logs -f koko-backend
sudo docker compose logs -f caddy

# Restart Koko
sudo docker compose restart koko-backend

# Verify env vars inside the container
sudo docker compose exec koko-backend env | grep TELEGRAM
```

## Notes

- Updating `.env` requires `sudo docker compose up -d --force-recreate` to reload.
- The daily summary runs at 21:30 server time via `scheduler.py`.
