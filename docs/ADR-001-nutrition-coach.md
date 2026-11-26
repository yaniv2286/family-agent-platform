# ADR-001 (amended): Nutrition coach as a built-in Koko agent

Status: accepted for the family deployment | 22 November 2026

## Context

`docs/DESIGN.md` proposes a TypeScript monorepo (Next.js coach web, Expo trainee
app, Fastify API, Kysely, PostgreSQL). That stack is marked PROPOSED in the
design, not BASELINE — the invariants are binding, the implementation language
is not.

The Koko family platform already provides: FastAPI + async SQLAlchemy, PIN
role auth (child/parent/admin), the agent mesh + `registered_agents` registry,
Hebrew RTL UI verified on phone viewports, Whisper transcription + TTS in
Hebrew, Telegram messaging, and `deploy.bat` → docker-compose on the VPS.

## Decision

- Implement the product as a built-in agent `meal_coach` in this repository
  (`agents/nutrition_coach_agent.py`, prefix `/api/meals`) instead of a
  separate TypeScript stack.
- Pure domain logic lives in `nutrition_core/` (pure Python, `Decimal`
  arithmetic — no FastAPI/SQLAlchemy imports).
- The nutrition schema lives in its own database: `NUTRITION_DATABASE_URL`.
  SQLite for local dev/tests; a dedicated PostgreSQL service in
  docker-compose for production, with a non-owner runtime role and
  FORCE ROW LEVEL SECURITY keyed on the `app.client_id` transaction GUC
  (`db/nutrition-init/01-provision.sh` + runtime `ALTER TABLE` policies).
- Parent = `plan_editor` + `plan_publisher`. Family member = trainee/client
  (`client_id` = existing `users.id`). `clinical_rule_approver` is unassigned:
  the clinical pathway is fail-closed until gates G02/G03 are resolved.
- First slice serves only parent-approved exact meals (G05 open — no
  independent substitution engine yet). Alternatives engine is a later
  package (DESIGN P05).
- Voice logging reuses `tutors.transcribe_audio` (Whisper). Transcript →
  candidate items → explicit user tap creates the eating event. A transcript
  is never itself an eating event.

## Deviations from DESIGN.md (recorded honestly)

| DESIGN proposal | Amended decision | Why |
| --- | --- | --- |
| TS monorepo / Next / Expo / Fastify / Kysely | Python in-repo agent | Reuse auth, mesh, voice, deploy; one language |
| Coach desktop web app | New screen in existing `static/index.html` | Same Hebrew RTL, mobile-verified UI |
| Expo trainee app | Existing mobile-friendly web UI | Family already uses the browser app |
| Client identity linking + invitations | Existing family `users` rows | Family deployment has no external clients |
| OIDC provider | Existing PIN auth | Already the platform auth model; revisit if product goes external |

Not compromised: draft != approve != publish, atomic versioned publication,
selection != eating, immutable snapshots, idempotency, unknown != zero,
fail-closed gates, server-side validation of all quantities and constraints.

## Versions verified 22 Nov 2026

PostgreSQL 18.7 (NOT 19.0 — released 19 Nov, too new per vetting rule),
asyncpg 0.30.x, Python 3.10+ runtime of this repo.
