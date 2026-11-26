---
name: meal-qa
description: Run the meal coach QA bot that simulates the full coach-to-trainee flow (setup, publish, daily child use, UI walk, optional live LLM) before deploy.
allowed-tools:
  - exec
  - read
  - grep
  - glob
---

You are the meal-coach QA assistant for the Koko platform.

`python scripts/simulate_meal_flow.py` role-plays the whole nutrition flow
end to end: ADMIN coach setup (profile, draft, validate, approve, publish)
and the child's daily use (planned meals, alternatives, sessions, saved
meals, honest eating log, corrections, health blocks). Default mode runs the
app in-process against a fresh temporary nutrition SQLite DB.

```powershell
python scripts/simulate_meal_flow.py                     # coach + child + ui + persona
python scripts/simulate_meal_flow.py --section coach     # one section
python scripts/simulate_meal_flow.py --section child
python scripts/simulate_meal_flow.py --section ui        # Playwright, needs Chrome
python scripts/simulate_meal_flow.py --section persona   # seeded personas + oracle (API + UI)
python scripts/simulate_meal_flow.py --section persona --seed 42
python scripts/simulate_meal_flow.py --section llm       # LIVE LLM - costs money
python scripts/simulate_meal_flow.py --live              # everything incl. llm
python scripts/simulate_meal_flow.py --base-url http://localhost:8000
python scripts/simulate_meal_flow.py --verbose
```

`llm` makes real OpenAI calls and costs money — run it only when the user
explicitly asks (or `--live`). In `--base-url` mode shared-catalog
mutations are skipped and an already-published plan is handled by
publishing v+1.

Run before every deploy alongside the mock suite and `check_mobile.py` /
`check_voice.py`. Non-zero exit = do not deploy.

## Checklist (each maps to a DESIGN invariant)

- **C1** auth: no PIN 403, wrong PIN 401, ADMIN PIN 200 (B01/§7.4 RBAC).
- **C2** publish requires a confirmed profile -> 409 PROFILE_CONFIRMATION_REQUIRED (B02).
- **C3** validation rejects wrong-slot recipes and unknown food ids; approve refused.
- **C4** skipped allergy disclosure: publish ok, alternatives "blocked / allergy disclosure", plan view + eating open (B11).
- **C5** profile update confirms; intent on the old base -> 409 STALE_REVISION.
- **C6** publish after draft edit -> 409 STALE_REVISION; re-approve -> ok (§6.5 vector).
- **C7** same Idempotency-Key publishes exactly one version (B10 idempotency).
- **C8** state: נויה published/has_active_plan, לביא false (setup_status).
- **C9** professional registration (ADMIN attestation) + list (§4.1; registration is attestation, not registry verification).
- **C10** medical_condition_declared "yes" without bundle -> publish 409 MEDICAL_INSTRUCTION_REQUIRED (B07).
- **C11** bundle (meal carbs max + day sodium max) approved by named professional -> publish ok.
- **C12** fit-portions fills a slot inside the bounds; wrong-slot recipe -> no_match; read-only.
- **C13** stricter bundle invalidates the active plan -> validate flags the nutrient_bounds issue, proposals "blocked / active plan violates" until a compliant bundle (B1/B2).
- **C14** expired active bundle -> proposals "blocked / medical instruction expired"; plan view + eating stay open (B1).
- **C15** coach health-report list + resolve; child resolve -> 403.
- **K1** one-tap eat: day totals += exact meal totals; op-id retry dedupes (B12/§8.1).
- **K2** edited quantity stored and reflected in totals.
- **K3** selection is planning only: zero eating events (B03/B05).
- **K4** session reject excludes planned meal for that session only ("עוד אפשרות").
- **K5** missing ingredient removed from the next proposal.
- **K6** disliked recipe never returns in alternatives; planned meal unchanged (B11).
- **K7** saved meal revalidated -> saved_meal_revalidated label (§7.5).
- **K8** description-only eating -> incomplete; unknown totals null, never 0 (B09).
- **K9** void/correction update totals; history never rewritten (§8.1 ledger).
- **K10** selection with a pre-publish proposal -> 409 STALE_INPUT.
- **K11** pending health intent blocks proposals, not eating; cancel unblocks (fail-closed).
- **K12** WARN-only: PIN-less cross-child reads (known isolation gap by design).
- **K13** 12 proposal jobs across slots/days: every proposal respects every active medical bound (Decimal).
- **ui** full coach wizard + medical step (professional add -> instruction rules -> approve -> fit portions -> publish) + child day walk; no console errors, no 390px overflow, publish disabled until approve, no raw JSON. Also: with the parent PIN stored in localStorage, opening a child's 🥗 card must show HER trainee app, not the coach view (entry path decides the view, not the role — regression, prod Nov 23).
- **L1–L5** live assistant: missing ingredient applied, wants-different, health preview (never an intent), prompt-injection no-op with exact response keys, preference + selectable proposal; the assistant never creates eating events, selections or plan changes (DESIGN §10).
- **persona (P-checks)** a seeded `CoachPersona` + `ChildPersona` (default `--seed 7`) replay a realistic week on לביא through the HTTP API (ADMIN PIN for coach, none for child): publish-before-validate refusal, wrong-slot/bad-quantity draft errors, STALE_REVISION edit-after-approve, mid-week medical bundle tighten -> plan invalidation -> compliant renewal, two days of weighted daily actions (alternatives, missing ingredients, dislikes, saved meal, free-text log, void, correction), the reject+missing dead-end until `no_match` (per-recipe reasons) and its reset, pending-health `blocked` while eating stays open, assistant must never conjure unapproved food, and cross-sibling write isolation. A `PersonaOracle` re-checks invariants after EVERY mutating action: selections never write events, unknown totals stay `null` (never 0), session state never leaks to other slots/sessions, dislikes never kill the planned meal, every error body is structured (no bare 5xx), and every proposal job state has its required payload (eligible -> proposal with items, no_match -> reasons). It prints a readable transcript + oracle check count, then a Playwright UI walk drives the same intents (missing ingredient -> no-match sheet names recipes -> 'בטל סירובים ונסה שוב' -> '.nx-sheet-x' closes -> eaten card shows only 'בטל דיווח').

When the bot catches a real defect, fix the defect — never loosen an
assertion to go green — and add a regression in
`tests/test_meal_coach.py`.

## Demo seeder

`python scripts/seed_meal_demo.py` seeds MOCK demo data for נויה on a running
server (default http://localhost:8000) through the HTTP API only — published
plan, 6 days of eating history, today's mixed state, a saved meal and a
resolved health report. Refuses to run when a plan already exists unless
`--reset` (publishes a new version, deletes nothing); `--with-medical` adds a
MOCK professional + carbs bound (needs ADMIN role).
