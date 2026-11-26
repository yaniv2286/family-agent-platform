# DEVIN_BRIEF.md

## First task: planning/review only

Read DESIGN.md revision 1.0 (22 November 2026) and the companion product specification if accessible. DESIGN.md includes the required product baseline so an authenticated link is not a prerequisite for the first review. No repository exists yet. Do not start application implementation, provision services, purchase anything, deploy, send invitations or transfer private data to vendors in this first task.

Return:
1. A short consistency review of the requirements, schema, API, deterministic nutrition pipeline, locking/version protocol and clinical/privacy boundaries. Identify concrete risks or missing contracts rather than claiming the design is production-ready.
2. A proposed ADR-001 for accepting or changing the proposed TypeScript monorepo, coach web, trainee Expo app, server modular monolith and PostgreSQL. Verify supported stable versions and exact APIs from current official docs; do not assume package versions or model names from prior cost examples.
3. A dependency-ordered implementation plan using P01-P11, with requirement IDs B01-B12 and test IDs T01-T40. For every first slice name files/modules, migrations, APIs, failure modes and tests.
4. One grouped list of G01-G10 decisions needing an owner. Clearly separate questions that block all implementation from those that only block real clinical data, invitations, AI or public release. Do not invent tolerances, treatment thresholds, licensed food data, professional credentials, permissions or vendor selections.
5. Stop and request the owner's approval of the stack and the first bounded implementation slice. Do not interpret this planning brief as permission to implement the entire product.

## After explicit implementation approval

Implement only the approved package(s), using synthetic data until the applicable gates are resolved. Work in small reviewable commits/PRs. Keep AI and real clinical pathways disabled unless specifically approved and validated. Include exact local run/test commands and their actual output. Label unrun tests and any simulated service honestly.

Mandatory behavior:
- Draft != approval != publication. Active publication is atomic and versioned.
- Selection != eating. No chat, saving, publication or repeated delivery may create an eaten event.
- No household inventory, stock deduction, fridge photo, shopping list or assumed ingredients at home.
- Shared reference food data never contains a client's private request, profile, health, habits or chat.
- Server code validates permissions, all quantities and constraints. The model cannot approve, mutate a plan, infer a diagnosis, change targets or bypass a clinical block.
- Relevant missing/unknown/conflicting data cannot become zero or a successful check.
- Every proposal, including saved/chat alternatives, is revalidated on current versions and expiry before selection. Old results cannot be stamped with a new vector without rerunning checks.
- Factual eating logging remains possible when recommendation is blocked. Preserve historical snapshots and use explicit correction/void events.
- Permission and health gates are fail-closed from the first usable slice, not later cleanup.
- Use client-scoped object/field authorization, runtime DB roles/RLS, stable operation IDs, optimistic revisions and tested transaction race handling.
- Core curated meal planning works with AI_DISABLED. No vendor SDK, live credential or paid service is necessary for the first synthetic unit/integration fixtures.

For each later PR return: what changed; requirement/test coverage; actual commands/results; UI screenshots when relevant; data/permission/migration effects; remaining gates and risks; and the next bounded package. Do not call the system clinically safe, legally compliant or production-ready merely because tests pass.
