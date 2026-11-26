# DESIGN.md: Coach-led nutrition planning

Revision 1.0 | 22 November 2026 | Engineering proposal, not an implemented system

## 0. Read this first

This document translates the current nutrition product specification into an implementation design for Devin. It is intentionally written in English for repository use. The product UI is Hebrew-first and RTL. It does not describe the internal implementation of any existing commercial app.

Product baseline: https://files.instinct.com/mj9ipqg2rs50-all-in-fit, publication checked on 22 November 2026. The baseline includes the nutrition flow, coach workspace, health boundaries and product priorities. Its competitor screenshots and older cost scenarios are reference material, not instructions to implement every visible feature. The existing product specification remains separate and unchanged.

Status labels used throughout:
- BASELINE: behavior required by the current product specification. This is not a claim that every design detail received separate user approval.
- PROPOSED: an engineering choice that can be changed after review.
- GATE: a product, professional, legal, security or vendor decision that engineering must not invent.

No codebase was inspected because none was supplied. The SQL, TypeScript and HTTP examples below are design contracts and pseudocode, not tested migrations, compiled code or a complete OpenAPI document. No application was built, no infrastructure was provisioned, no model or clinical quality was measured and no production deployment is authorized by this document. The accompanying DEVIN_BRIEF.md starts with a planning-only checkpoint. Later implementation needs the owner's explicit go-ahead.

## 1. Scope and non-negotiable invariants

### 1.1 BASELINE

B01. Coach or nutrition professional and client complete initial setup together. The professional uses a desktop-friendly web workspace; the trainee uses a simple mobile app.
B02. Save draft, review/approve and publish are separate operations. Only a successful authorized publication changes the active plan atomically. Initial publication is also gated on profile confirmation and permitted sharing, not only professional approval.
B03. A trainee may choose within an explicit replacement policy. An exact target is not an implied tolerance. With no replacement policy, only explicitly approved alternatives are eligible after current checks.
B04. A shared validated food catalog is separate from the private profile, health reports, habitual foods and explicitly saved meals. Missing at home is session context, not an inventory record. An unknown catalog food is a private submission for review.
B05. Selection is planning. Eating is a separate explicit action. Selection, publication, AI chat and saving a recipe never create eating events or infer a permanent habit.
B06. Nutrients, quantities, permissions and all hard constraints are checked by server code. Optional AI proposes bounded combinations or explanations; it is not the calculator, approver or source of treatment rules.
B07. Health reports are not diagnoses. Only an appropriately authorized professional can approve therapeutic rules within an approved jurisdiction and scope. Missing, conflicting, expired or unsupported instructions block dependent recommendations, not historical viewing or honest eaten logging.
B08. Confirmed profile/health/rule/plan/catalog/day-ledger changes invalidate old proposal validation. Pending relevant health clarification also blocks affected recommendations without silently creating a diagnosis. Old cards, chat proposals and saved meals cannot bypass this.
B09. Already-eaten records keep their original quantities, food sources and context. Later plan or catalog changes do not silently recalculate history. Explicit corrections are traceable.
B10. Familiar suitable foods rank first, then simplicity and variety. Total time, active time, preparation state, equipment, practical portion bounds and household conversions require identified recipe/data sources. No assumed oil, sauce, hidden cooking or standard can size.
B11. A profile can omit preferences; safety-relevant unknowns are not interpreted as negative answers. Skipped allergy disclosure is not 'no allergies'. Kosher compatibility needs the actual relevant rules and product evidence, not a single boolean.
B12. Core actions work without chat, inventory questions or an availability confirmation. Show one leading alternative and more on request. Proposed usability goals: one tap to log the displayed complete meal; two taps from the visible card to choose an alternative when prerequisites are satisfied. These are targets, not measured results.

### 1.2 Excluded from this build scope

No household stock table, fridge photo, purchase scanning for stock, stock deduction or shopping list. No autonomous treatment, disease-based target generation, medication advice, automatic target changes or compensation by skipping meals. No business CRM, billing, social feed, fitness program engine, wearables, restaurant ordering, location tracking or autonomous messages to a coach. Competitor features do not expand scope. Food-photo logging, barcodes, document import, scheduled plan activation, multi-professional organizations and push notifications require separately scoped decisions; none is needed for the first core slice.

## 2. Proposed stack and tradeoffs

Choose a modular monolith first, not microservices.

- apps/coach-web: React + TypeScript, with Next.js proposed for web routing and delivery. Use server API for all domain mutations. Do not duplicate policy inside web server actions.
- apps/trainee-mobile: React Native + Expo + TypeScript proposed for iOS/Android. Share contracts and formatting helpers, not authorization decisions or a forced identical UI.
- apps/api: TypeScript Node HTTP service, with Fastify proposed. One domain application plus background worker process from the same repository. A container-capable host is a deployment proposal, not a purchased provider.
- PostgreSQL for transactional state. Use versioned SQL migrations and a typed query layer; Kysely is a proposal. Review all generated SQL. PostgreSQL numeric values map to decimal-safe values, not JavaScript floating-point totals.
- packages/contracts: runtime request/response validation and generated client types. Zod is proposed. Generate OpenAPI from reviewed schemas during implementation.
- packages/nutrition-core: pure decimal/unit/rule evaluation and deterministic bounded candidate search. No network, model calls or database access.
- Worker: database outbox polling and leased jobs initially. No Redis or message broker dependency required for correctness.
- Identity: managed OIDC provider via an adapter, selected only after security, hosting and jurisdiction decisions. No custom password database. Native clients use authorization-code flow with PKCE; web uses secure server session cookies. Validate issuer, audience, expiry and subject server-side. Choose the actual provider flows against its current docs.
- Optional AI behind a server-only provider adapter. No provider/model ID selected here. Older price examples do not choose a model. No embeddings/vector store needed for the curated first slice.

Tradeoffs: TypeScript reduces contract duplication, but decimal arithmetic, native testing and SQL constraints still need separate care. A monolith makes atomic version checks and publication simpler, but a long solver/AI call must not block HTTP or a database transaction. Expo is proposed for delivery speed, not evidence of production medical readiness. Revisit stack choices in ADR-001 before creating a repository.

Do not use 'latest' dependencies in a committed build. During implementation, choose mutually supported stable releases, verify exact APIs in official docs, record runtime/package/database versions and commit a lockfile. This design makes no claim about current latest version numbers or SDK method signatures.

### 2.1 Repository layout

```text
apps/
  coach-web/src/{features,api-client,ui}
  trainee-mobile/src/{features,api-client,offline,ui}
  api/src/{http,application,domain,infrastructure}
  worker/src/{outbox,jobs}
packages/
  contracts/src/{requests,responses,errors}
  nutrition-core/src/{units,nutrients,rules,allocator,search,ranking}
  test-fixtures/                 # synthetic, explicitly not clinical guidance
db/{migrations,policies,seeds}
docs/{DESIGN.md,ADRs,openapi.yaml,threat-model.md,runbooks}
tests/{integration,authorization,concurrency,e2e,ai-evals}
```

Dependency rule: clients -> contracts; HTTP -> application -> domain -> interfaces; infrastructure implements interfaces. Nutrition core imports neither HTTP nor provider SDKs. Coach and trainee clients never import server database packages. No production secret is bundled in either client.

## 3. Architecture and trust boundaries

```text
Coach web --- authenticated HTTPS ---+
                                    |--> API / object+field authorization
Trainee app - authenticated HTTPS ---+       |
                                            +-- plan/profile/rule services
                                            +-- deterministic nutrition engine
                                            +-- catalog review service
                                            +-- eating ledger service
                                            +-- optional bounded AI adapter --> chosen provider
                                            |
                                         PostgreSQL
                                            |
                                      transactional outbox
                                            |
                                     worker / retries
                                            |
                               optional generic update notification
```

The server is the source of truth. Notification delivery, a cached mobile plan and a client 'validated' flag are not authoritative. The optional model receives only the minimum authorized task data; it has no database credentials or mutation tools. The catalog is application-shared reference data, not a publicly accessible database.

Use ports: IdentityVerifier, AuthorizationRepository, CatalogRepository, PlanRepository, NutritionEvaluator, CandidateGenerator, AiSuggestionProvider, Clock, IdGenerator, AuditWriter and OutboxWriter. Inject a deterministic clock and test catalog into tests. External services are replaceable adapters. Do not build an agent framework for the core flow.

## 4. Identity, authorization and consent

### 4.1 Capabilities, not role names alone

- trainee: read own allowed profile/active plan; propose/confirm own preference and health-report changes; choose within permitted rules; manage own saved meals; log/correct own eating.
- plan_editor: draft non-therapeutic plan fields for actively linked clients within granted scopes.
- plan_publisher: publish after approval, current authorization and capability checks.
- clinical_rule_approver: separate verified capability bounded by jurisdiction, population, rule category and expiry. Never self-assigned from a registration checkbox.
- catalog_reviewer: review food reference data; no access to requester health, plans, chat or eating history by virtue of this role.
- support/operator: no default read access to private health or chat. Any exceptional access requires a separately defined audited process before production.

An actor may hold multiple capabilities. Every request checks actor + object ownership/link + field scope + operation + current grant validity. An active coach link alone does not grant health, diary or chat access. A food reviewer cannot publish a plan. A qualified clinician still needs the client's authorized link and relevant sharing scope.

Proposed sharing scopes: profile.preferences.read/write, profile.habits.read/write, plan.read/draft/publish, health.report.read, clinical.rules.read/approve and eating.read. Do not add chat.read by implication. Owner confirmation of profile data, consent to disclosure and professional plan approval are distinct records. Setup can be a draft while any prerequisite is missing.

All lists, detail reads, nested IDs, old revisions, search, exports and storage objects get the same checks. For forbidden object access return a consistent not-found policy to avoid existence leakage; log the denial without private body content. Server derives actor from verified identity, never from actor_id in JSON.

### 4.2 Database defense in depth

Use a non-owner runtime DB role without BYPASSRLS, narrow table privileges, enabled and forced RLS on private tables, and migration-only elevated credentials. Set actor context transaction-locally from the authenticated server, never from arbitrary SQL or a client header. Pool reuse must not leak actor state. RLS is row-level only: health and diary tables remain separate, with explicit field projection in APIs. Test actual runtime and worker roles, not only a privileged test owner. PostgreSQL documents both owner/BYPASSRLS bypass and default deny after RLS activation [S1].

Revocation takes the same authorization/client-state locks used by protected writes. Writes that serialize after revocation must fail; in-flight reads already delivered cannot be recalled. Increment an authorization epoch, terminate applicable subscriptions/sessions, evict private caches and remove coach offline data on next contact. An offline device cannot be guaranteed to erase immediately. Therefore the proposed first coach release has no persistent client-data offline cache. Retention and downloaded exports are a GATE, not a promise of remote deletion.

## 5. Domain model and storage schema

All identifiers are opaque UUIDs. All timestamps are timestamptz, stored as instants. Use UTC at transport boundaries and an IANA zone for day assignment. Each private child row carries client_id with a composite foreign key to the parent client row, preventing cross-client ID association. A missing value is NULL/unknown, not zero.

### 5.1 Proposed entities

| Entity | Essential columns and invariants |
| --- | --- |
| principals | id, identity_issuer, identity_subject, display_name; unique issuer+subject; no password hashes |
| client_accounts | client_id, owner_principal_id UNIQUE, nutrition_timezone, timezone_revision; ownership enforced server-side |
| professional_capabilities | principal_id, capability, jurisdiction, population_scope, verified_by, evidence_ref, valid_from, expires_at, revoked_at; evidence private |
| client_links | id, client_id, professional_id, status, epoch, starts_at, ends_at; only explicitly accepted links active |
| sharing_grants | id, link_id, client_id, scope, granted_by, granted_at, revoked_at, purpose_version; deny absent scope |
| profile_drafts | client_id, author_id, base_profile_version, draft_revision, proposed_preferences/habits; not live |
| profile_versions | id, client_id, version, typed preferences/habits/allergy/kosher/preparation fields, confirmation_ref, created_at; immutable |
| health_reports | id, client_id, reported_code_or_text, source_kind, status, confirmed_at, supersedes_id; reports not diagnoses |
| health_update_intents | id, client_id, status, affected_scope, source_ref, confirmed_report_id; explicit pending clarification, never a diagnosis |
| clinical_rule_bundles | id, client_id, version, status, supported_pathway, approver_id, capability_ref, valid_from/until, source_ref, rules; immutable approved bundle |
| plan_drafts | id, client_id, base_plan_version nullable, draft_revision, validated_against vector, content, updated_by; optimistic writes |
| plan_approvals | id, draft_id, draft_revision, content_hash, input_vector, approved_by, capability_ref, approved_at; approval of one exact draft |
| plan_versions | id, client_id, version, published_content, profile_version_at_approval, rule_bundle_id, approval_id, published_by, effective_at, change_summary; immutable |
| plan_meals / meal_items | plan_version_id, client_id, stable_slot_key, ordinal, recipe_version_id/food_version_id, quantity_g, policy_ref; immutable per publication |
| replacement_policies | plan_version_id, slot_key, allowed_mode, nutrient_bounds, portion bounds, future-slot reservation policy, approved alternative IDs, approval_ref; no inferred tolerance |
| client_state | client_id PK, active_plan_id, active_profile_id, active_rule_bundle_id, eligibility, safety_epoch, profile_epoch, plan_epoch, rule_epoch, ledger_epoch, authorization_epoch |
| catalog_state | singleton id, catalog_epoch; global conservative invalidation counter |
| foods / food_versions | food_id; immutable version id, kind generic/branded, preparation_state, basis_g, nutrients, evidence, allergens, kosher evidence, status; approved/retracted/current pointer |
| food_aliases / unit_conversions | locale+normalized alias; food/version reference and state; unit -> grams with evidence and uncertainty; ambiguous aliases not auto-matched |
| recipe_versions | id, ingredients with pinned food versions, quantity steps/bounds, active/total time estimator, equipment, steps, preparation assumptions, evidence; immutable |
| catalog_submissions | id, requester_id, private request text, proposed match, status, reviewer_ref; private until approved reference fields copied separately |
| suggestion_sessions | id, client_id, slot_key, date, missing_food_ids, preparation overrides, revision, expires_at; ephemeral context, no stock quantities |
| proposal_jobs | id, client_id, input_vector, state, attempts, deadline, failure_code; no approval by job success alone |
| proposals | id, client_id, session_id, recipe/food snapshots, quantity vector, totals, validation reasons, input_vector, valid_until, engine_version; server-created |
| selections | id, client_id, nutrition_date, slot_key, proposal_id nullable, exact snapshot, version vector, status; one current planned choice per slot |
| saved_meals | id, client_id, immutable recipe/snapshot reference, saved_explicitly_at; a reusable recipe, never permanent validation |
| eating_events | id, client_id, operation_id, occurred_at, recorded_at, nutrition_date, timezone_snapshot, event_type, corrects_event_id, source_context, item snapshots, completeness; append-oriented |
| day_state | client_id+nutrition_date PK, revision, completeness, totals projection; rebuildable from ledger, not from plan selections |
| idempotency_records | actor_id, client_id nullable, route, key, request_hash, status, response_ref, expires_at; unique scope+key |
| outbox_events / job_attempts | id, type, aggregate_id, safe payload, created_at, lease_until, attempt_count, next_attempt_at, delivered_at; delivery at least once |
| audit_events | actor, operation, object identifiers, timestamp, outcome, before/after hashes where useful; no copied health/chat bodies |

JSONB is suitable for versioned typed documents only with schema_version, runtime validation and tested migrations. It is not permission to store arbitrary model output. Normalize child entities queried for authorization or joins. Add SQL foreign keys, required constraints, status checks and numeric nonnegative checks. Add indexes for link/professional/status; plan/client/version; events/client/date; submissions/status; outbox/next_attempt_at; aliases/locale/normalized_name. Bound search/query sizes.

### 5.2 Constraint sketches, not a runnable migration

```sql
-- Every version table has UNIQUE(client_id, id) for tenant-safe references.
-- Prefer explicit column names in migrations; these are representative constraints.
UNIQUE (client_id, version)                         -- plan/profile versions
FOREIGN KEY (client_id, active_plan_id)
  REFERENCES plan_versions(client_id, id)          -- client_state
UNIQUE (actor_id, route, idempotency_key)           -- include client in request hash
UNIQUE (client_id, operation_id)                   -- eating operation identity
CHECK (quantity_g IS NULL OR quantity_g >= 0)
CHECK (expires_at IS NULL OR expires_at > valid_from)
CREATE UNIQUE INDEX one_current_selection_per_slot
  ON selections(client_id, nutrition_date, slot_key)
  WHERE status = 'current';
```

Do not expose a generic endpoint allowing writes to client_state, approval_id, source evidence or validated totals. Published objects cannot be edited through ordinary UPDATE endpoints. Make immutable writes a database permission/trigger concern as well as an application convention. Deletion obligations are addressed in section 14; immutable does not mean exempt from lawful deletion.

## 6. Versioning, state machines and transactions

### 6.1 Validation vector

```ts
type ValidationVector = {
  planVersion: string;
  profileVersion: string;
  ruleBundleVersion: string | null;
  eligibilityEpoch: string;
  authorizationEpoch: string;
  catalogEpoch: string;
  ledgerEpoch: string;
  dayRevision: string;
  sessionRevision: string;
  engineVersion: string;
  nutritionDate: string;       // YYYY-MM-DD in recorded IANA zone
  timezoneRevision: string;
};
```

Global catalogEpoch and per-client ledgerEpoch are deliberately conservative: unrelated changes may force extra revalidation, but cannot leave an old proposal apparently current. Later dependency-level optimization needs equivalent tests. Also check actual wall-clock expiry of credentials, rules and proposal lifetime on every validation; an epoch alone does not capture time passing.

### 6.2 State transitions

Plan: draft -> approved exact draft revision -> published immutable version. An edit after approval invalidates that approval. A draft with changed profile/rules/base plan is stale. Publication supersedes the active pointer; an old version is not deleted. Reverting publishes a newly reviewed version.

Health eligibility: unassessed | pending_clarification | ready_with_explicit_rules | unsupported | conflicting | expired. 'Ready' describes eligibility under configured checks, not comprehensive medical safety. No therapeutic pathway is enabled until its GATE is cleared. Do not silently enroll an unsupported person in a generic wellness replacement flow.

Proposal: queued -> generating -> validated_candidate | no_match | blocked | failed. Then validated_candidate -> selected | stale | expired. Client displays a temporary computing state; do not stream actionable portions of an unvalidated proposal.

Eating: recorded -> corrected/voided by another linked event. Unknown quantities remain recorded with incomplete totals. Ordinary logging does not require plan compliance.

### 6.3 Locking protocol

Use short database transactions. Never hold a transaction open during model calls or candidate search. Proposed lock order for state-changing domain operations: catalog_state (shared for validation; exclusive for catalog publication/retraction), then relevant authorization/link records in stable ID order, then client_state, then day_state rows ordered by date, then draft/session/selection rows. All code paths that mutate these authorities must follow the same order. Catalog writes increment the epoch while holding the exclusive lock. Confirmed or pending safety updates, link revocation, rule changes, plan publication and eating writes increment the relevant client epochs while holding client_state. A test must prove each invalidation path participates.

Use row locks plus exact compare-and-swap checks. For operations whose reads cannot be covered by that protocol, use SERIALIZABLE with bounded full-transaction retry on serialization failure. PostgreSQL requires handling retryable failures; a retry is not a failed transaction's partial continuation [S2]. Return results and emit notifications only after commit. The idempotency uniqueness constraint is part of the transaction, including the concurrent-first-request case.

### 6.4 Publication algorithm

1. Authenticate, validate body/If-Match and claim operation key; same key with a different request hash returns 409 IDEMPOTENCY_KEY_REUSED. A concurrent identical request waits for the transaction or gets an explicit in-progress status; never execute a second logical publication.
2. Acquire locks in the documented order. Re-check active link, scopes, capability expiry and clinical authority where relevant, not only earlier screen-load permission.
3. Compare current draft revision, base active plan and profile/rule/catalog versions to the approval's content hash and input vector. Missing profile confirmation or required sharing blocks publication.
4. Run deterministic publication validation: complete units, pinned source versions, complete required fields, recipe feasibility, allowed rules, day/meal consistency and required approvals. A permitted difference between plan items and daily targets must itself be explicitly defined; do not invent an epsilon.
5. Insert one immutable plan version, meals and policy snapshot; update active pointer and epoch; insert audit/outbox and stored operation result in the same transaction. Proposed initial effective_at is commit-time publication, no retroactive change or future schedule.
6. Commit and return version, effective_at and change summary. Outbox delivery may happen later. Mobile foreground/current-plan reads recover even when every notification is lost.

### 6.5 Generation and selection algorithm

1. Read one coherent authorized snapshot, active vector, date and session context. Fail closed on unsupported/missing/conflicting safety rules or prerequisites. Create a bounded job, then release database locks.
2. Generate candidates and search quantities outside the transaction. Pin all reference versions. Re-evaluate each final rounded candidate deterministically.
3. Reacquire locks, compare vector and all expiries, and persist only still-current validation. If changed, discard computation; retry once against a fresh snapshot or return STALE_INPUT. Never attach a new vector to old results without rerunning validation.
4. At POST selection, authenticate owner, verify session/client/slot binding, take locks, compare vector and expiry again, recompute dependent checks. If stale, return 409 with stale fields and a refresh path. If a refreshed amount differs, show it and require a new explicit selection, not a silent swap.
5. Supersede the prior selection for that slot and store the exact new snapshot atomically. This is not an eating event and does not decrement daily consumed totals. Selections for other unconsumed slots can affect the planned reservation model only as explicit planned allocations.

## 7. Deterministic nutrition engine

### 7.1 Units, sources and unknowns

Canonical food basis: edible grams of the specified food/preparation state. Nutrients: kcal, protein_g, carbs_g, fat_g and additional fields required by an approved rule such as sodium_mg, saturated_fat_g or fiber_g. These examples are data fields, not treatment recommendations. Each value has source, source date/version and status known | unknown | estimated. Unknown is not 0. Keep source energy; do not overwrite kcal with 4/4/9. That calculation may be a discrepancy diagnostic with explicit source methodology, not a reason to fabricate an authoritative value.

Use decimal-safe arithmetic server-side and decimal strings in transport. Proposed persistence: quantity_g NUMERIC(12,3), nutrient source values and totals NUMERIC(16,6). Reject unsupported scale/overflow; round for display only. Portion steps come from recipe/conversion evidence, not numeric precision. No default 'one can = X grams'. Raw/cooked and drained/undrained are different identities or evidence-backed conversions. A recipe yield conversion needs an identified recipe and preparation method.

For nutrient n:

```text
meal[n] = sum_i((edibleGrams[i] / foodBasisGrams[i]) * nutrientPerBasis[i,n])
```

If any included item lacks a required nutrient or conversion, the relevant total is unknown and the required check cannot pass. Reject a missing allergen/product certification fact when it is required for this user's constraint. Do not infer kosher certification, absence of cross-contact or allergen safety from a name.

### 7.2 Rule DSL proposal

Rules are typed data, never arbitrary JavaScript, SQL or natural-language instructions to execute.

```ts
type DecimalString = string;
type Rule =
 | { kind: 'exclude_food'; foodIds: string[]; provenanceId: string }
 | { kind: 'require_verified_attribute'; attribute: string; expected: string; provenanceId: string }
 | { kind: 'nutrient_bounds'; nutrient: string; unit: 'g'|'mg'|'kcal';
     scope: 'meal'|'day'; lower: DecimalString|null; upper: DecimalString|null;
     provenanceId: string }
 | { kind: 'preparation_limit'; totalMinutes: number; equipmentAllowed: string[];
     provenanceId: string };
```

The production schema must replace free attribute/nutrient strings with a reviewed allowlist and define unknown behavior. Rule bundle stores authority, scope, version, expiry and approval. Extending to timing interactions, medication-dependent or combined-disease rules requires a separately validated DSL and clinical pathway. Do not encode those by guessing. Conflicts produce a blocking reason, not an automatic 'strictest rule wins' merge. Preferences can be relaxed only by an explicit current-context user choice where the specification permits it; safety and professional rules cannot.

### 7.3 Budget allocation

Consumed totals come only from the current effective eating ledger, with completeness tracked. Proposed allocator receives the active plan and explicit approved replacement policy. It does not infer that an untouched log means the user ate nothing; require the configured completeness information when a decision depends on it.

For each nutrient n, calculate remaining target/bound from the current plan and consumed totals. For a candidate slot, exclude the original/selected meal for that slot from planned allocations, and exclude already-consumed slots from future reservation. Reserve the approved minimum allocations for other unconsumed slots. Constrain the current candidate by its approved slot bounds and the residual allowed after reservations. Lower bounds and cross-slot/day constraints must be solved jointly, not only each nutrient's upper bound. If the approved policy defines exact per-meal alternatives instead, use that policy rather than invent a residual algorithm.

If remaining budget is negative, unknown or infeasible, report the reason. Do not pack the entire day's remainder into one meal, change targets, recommend skipping food or manufacture a tolerance. A mid-day publication uses the new current target plus actual already-eaten history without resetting the day.

### 7.4 Candidate generation and bounded search

Proposed deterministic v1:
1. Retrieve approved recipe templates and coach alternatives. Use suitable explicitly confirmed habitual foods/saved meals first. Session missing-food IDs stay excluded throughout the same sequence; they do not change the permanent profile.
2. Try recipe-declared replacements for only missing ingredients. If none works, try another complete template. Every replacement has to carry preparation time/equipment and source evidence.
3. Filter hard exclusions, eligibility, current source status, raw/cooked identity, required attributes, equipment and total-time limit. If required time/equipment is unknown, ask or reject the dependent candidate rather than pretend the preference is met.
4. Enumerate allowed recipe portion steps and substitution groups in stable order. Proposed engineering caps: at most 100 templates, 2,000 combinations per template and a two-second worker CPU budget. These are configurable initial resource limits, not nutritional policy or measured performance. Prune by proven bounds only. Timeout/search exhaustion is not proof that no feasible meal exists.
5. Compute totals, satisfy approved bounds including future allocations, round to actual permitted portion steps, then validate again. Practical portion bounds and time scaling come from recipe data. Any quantity-based preparation change must recompute total time.
6. Among feasible candidates rank lexicographically: approved familiar-food fit, normalized approved target distance, preparation complexity/ingredient count, then variety. Use a deterministic ID tie-break. Rank never overrides a failed hard rule. Weight/scale values are engineering configuration reviewed against examples, not therapeutic thresholds.
7. Return one leading candidate with optional more, exact ingredients/quantities, source-backed household measures, estimated active/total time, equipment, short reviewed preparation steps and check-specific explanation. No current-home-availability claim.

The initial deterministic route is intentionally limited to curated recipes and portion grids. It may miss a feasible continuous solution. If search quality is insufficient, propose a solver in a later ADR with deterministic fixtures, resource limits and final independent validation. Do not insert an unreviewed optimizer dependency or an AI-written recipe with unverified cooking times into the production path.

### 7.5 Validation result

```ts
type CheckResult = { code: string; status: 'pass'|'fail'|'unknown'; evidenceIds: string[] };
type ValidationResult = {
  state: 'eligible_candidate'|'blocked'|'no_match'|'search_incomplete';
  totals: Record<string, DecimalString | null>;
  checks: CheckResult[];
  vector: ValidationVector;
  engineVersion: string;
};
```

The UI must distinguish 'coach-approved alternative' from 'system-calculated within specified rules'. Neither is a blanket claim of medical safety, availability or certified kosher preparation. Explain the specific checked constraints without exposing private clinical reasons to unauthorized viewers.

## 8. Eating ledger, day summaries and offline behavior

### 8.1 Honest logging independent of recommendation

POST eating accepts planned, selected or unplanned food. Never reject a factual eating report solely because a plan changed or a meal violates a rule. Still validate identity, reasonable field shape, units and request limits. Unknown food/quantity can be stored as incomplete with a private description and no fabricated totals.

Snapshot fields: exact food/version or user description, quantity and unit/conversion evidence, nutrient values at logging time when known, plan/selection context if present, occurred_at, recorded_at, original offset and nutrition timezone/date. Client input totals are never trusted. References to a selection must belong to that owner. If the context is stale, record it as such; it cannot claim current compliance.

A tap on 'I ate this displayed meal' logs the displayed full quantity and returns an undo action. No extra confirmation for the ordinary complete-meal path. 'Change amount' happens before logging; correcting afterwards creates a linked correction. One intended action keeps the same operation_id across retries. Do not prevent intentionally eating the same food twice: duplicate protection is operation identity, not equality of food or time. Tapping twice while a request is pending reuses the operation key.

Corrections and voids acquire client/day locks, reject incompatible concurrent corrections using If-Match on the event projection, and increment ledger/day revisions. For a date-changing correction, lock both old and new day rows in stable order. The projection folds effective events exactly once. Undo is another authorized ledger operation, not a deletion of the plan or stock.

### 8.2 Dates and unknowns

Use account nutrition timezone, not device clock alone, to assign a new event's day. Explicit backdated entry shows the chosen date/zone. Preserve its snapshot across timezone changes and daylight-saving boundaries. Timezone changes version future day evaluation and do not rebucket old events automatically. Travel/day-boundary policy needs review before a public release.

Display consumed known totals with completeness state. If there is an unknown amount that affects remaining budget, remaining exact totals are null/not available, not a misleading number with a tiny disclaimer. Block only recommendations that depend on unknown values; reading the plan and correcting the diary still work. Do not silently assume a complete day from absence of events.

### 8.3 Offline and sync

- Mobile may read an encrypted cached published plan with version and last-checked timestamp. It is not guaranteed current.
- Unverified offline selection stays a draft. No new validated badge, selection success or compliance claim while offline.
- Offline factual eating queues with stable operation_id and original event time. Sync keeps those facts even if the plan changed and never double counts.
- Before foreground use, regeneration or selection, fetch server state and resolve pending logs; if the relevant day remains incomplete/pending, don't show verified remaining budget.
- Revocation/logout clears device caches and queued confidential data under a reviewed policy; warn before discarding an unsynced factual log where appropriate. Account switching cannot expose prior-account data.
- Proposed local storage: tokens/small keys in secure platform storage; encrypted local database for permitted plan and queue data with key handling tested in native builds. SecureStore is for small secrets, not whole health profiles [S5]. Expo SQLite persistence is not by itself encryption; SQLCipher support needs a suitable custom binary, verification and platform testing, not Expo Go [S6]. Never claim storage encrypted merely because a configuration flag exists.

No coach offline editing in v1. Save failures retain unsaved in-memory edits and show an honest status. Two browser tabs use draft revision checks instead of last-write-wins.

## 9. HTTP API contract

Proposed REST JSON under /v1. OpenAPI is to be generated and reviewed during implementation. All endpoints require verified authentication except explicit health/readiness checks, which disclose no private state. IDs are opaque strings; decimal values are strings. Timestamps use RFC3339 instants; date is YYYY-MM-DD with zone context. Reject unknown privileged properties. Enforce payload, page and generation limits. Return opaque cursor pagination with a maximum page size of 50 for lists.

Web mutations use CSRF protection with secure HttpOnly SameSite cookies. Native bearer tokens stay in protected storage and are sent only to the intended API origin. CORS allowlists are not authorization. Do not include secrets or health data in URLs.

Writes require Idempotency-Key; optimistic edits additionally require If-Match. Missing required precondition returns 428. Wrong revision returns 409; malformed body 400; unauthenticated 401; unauthorized object 404 consistently; semantically blocked action 422; rate limit 429 with Retry-After; transient dependency unavailable 503. No-match is a completed domain result, not an HTTP 500. Standard error: {code, messageKey, requestId, fieldErrors?, staleFields?, retryable}; no stack traces or cross-client values.

### 9.1 Endpoint surface

| Method/path | Request essentials | Response / permission |
| --- | --- | --- |
| GET /me | none | principal, allowed capabilities, account context; no global client enumeration |
| GET /coach/clients?cursor=... | bounded search | linked clients with permitted minimal fields |
| POST /client-links/invitations | intended account identifier + requested scopes | pending invitation; no active link or private client lookup disclosure; actual send channel requires separate authorization |
| POST /client-links/{id}/accept | scopes displayed to owner + purpose version | active link/grants only after owner acceptance |
| POST /client-links/{id}/revoke | reason code optional | revoked epoch; owner or designated authorized administrator |
| GET /clients/{id}/state | none | current authorized vector, eligibility summary, active plan reference |
| GET /clients/{id}/plan | none | published allowed plan, version, effective_at, change summary |
| POST /clients/{id}/plan-drafts | basePlanVersion nullable | draft id/revision; linked plan_editor |
| PATCH /clients/{id}/plan-drafts/{draftId} | allowed draft fields + If-Match | new revision; no active-plan mutation |
| POST .../plan-drafts/{draftId}/validate | expected input vector + If-Match | structured issues; not approval/publication |
| POST .../plan-drafts/{draftId}/approve | exact content hash/vector + If-Match | approval bound to draft revision; required authority checked |
| POST .../plan-drafts/{draftId}/publish | approvalId, expectedVector + If-Match | active plan version/effective_at/change summary |
| POST /clients/{id}/profile-change-intents | typed proposed fields | preview diff; no direct edit of clinical rules |
| POST /clients/{id}/profile-change-intents/{intentId}/confirm | expected profile revision + displayed diff hash | immutable new profile/version + invalidation |
| POST /clients/{id}/health-update-intents | reported change, source context | pending clarification + safety epoch; no diagnosis |
| POST /clients/{id}/health-update-intents/{intentId}/resolve | confirm/correct/cancel + explicit displayed change | report status, eligibility; cannot remove professional rule |
| POST /clients/{id}/clinical-rule-bundles | typed proposed rules + evidence ref | draft bundle; restricted capable professional |
| POST .../clinical-rule-bundles/{bundleId}/approve | hash + scope/expiry | approved version only with independently verified capability and owner sharing |
| GET /catalog/foods?query=... | locale/state, bounded query | approved reference data only, no submission owner |
| POST /catalog/submissions | private unknown food request | pending candidate, no automatic public availability |
| POST /catalog/reviews/{id}/approve | verified reference fields/source/license + If-Match | approved new version + catalog epoch; reviewer only |
| POST /catalog/food-versions/{id}/retract | reason and reviewer authority | reference unavailable to new recommendations + epoch; history preserved |
| POST /clients/{id}/suggestion-sessions | slotKey, date, optional preparation override | session id, revision; no inventory |
| PATCH /clients/{id}/suggestion-sessions/{sessionId} | missingFoodIds + If-Match | context revision; missing at home is not catalog submission |
| POST /clients/{id}/proposal-jobs | sessionId, expectedVector, origin | 202 jobId/status path; bounded rate/queue limit |
| GET /clients/{id}/proposal-jobs/{jobId} | none | pending / result with validated proposal IDs / blocked/no_match |
| POST /clients/{id}/selections | proposalId, expectedVector, sessionRevision | current selection snapshot or stale error; owner only |
| POST /clients/{id}/saved-meals | selection/recipe reference, explicit save | private saved recipe, no permanent validation |
| POST /clients/{id}/saved-meals/{savedId}/proposal-jobs | current session | same current validation pipeline |
| POST /clients/{id}/eating-events | operationId, occurredAt, zone/date, items or snapshot ref | ledger event + current day projection + undo reference |
| POST /clients/{id}/eating-events/{eventId}/corrections | new factual content + If-Match | linked correction, recalculated effective projection |
| POST /clients/{id}/eating-events/{eventId}/voids | If-Match, optional reason | linked void event; authorized actor, never chat alone |
| GET /clients/{id}/days/{date} | zone context | effective eaten totals + completeness + current plan context |
| POST /clients/{id}/assistant/messages | text, session/meal refs, no actor scope | response or pending job; proposals only through validator |

Link invitations are a proposed setup design. In the first synthetic development environment use explicit fixture bindings and no real invitations. For production, design verification of the actual intended recipient, expiry, single use and anti-enumeration before any send. Do not auto-discover or link a client merely by matching a name.

### 9.2 Example selection response (illustrative IDs, no nutrition prescription)

```json
{
  "selectionId": "opaque-selection-id",
  "status": "selected_not_eaten",
  "proposalId": "opaque-proposal-id",
  "planVersion": "4",
  "nutritionDate": "2026-11-22",
  "complianceLabel": "checked_against_specified_rules",
  "eatingEventId": null
}
```

### 9.3 Idempotency and jobs

Keep durable uniqueness for publication and eating logical operation IDs. Proposed short-lived response-cache retention for other idempotency keys is seven days, pending retention policy review; expiring a cached response must not allow the same eating operation or publication operation to run twice. Scope keys by actor+route, bind client and canonical request hash, and never return another actor's result. Reuse with a changed payload returns 409.

Job queue proposal: one active generation per client/session revision, three active jobs per client maximum, total deadline 15 seconds, cancellation/supersession on changed context. These are resource defaults for testing, not performance promises. Background completion never selects or logs a meal. Worker lease/retry is at-least-once; stable job/result identifiers and unique indexes ensure logical once-only effects. Exhausted/transient errors return explicit failed/search_incomplete results and a safe manual route.

## 10. Optional LLM integration

The core app must work with AI_DISABLED. Do not require a model to save a draft, calculate food totals, publish, choose a curated alternative or log eating.

### 10.1 Narrow input/output

Input proposal: allowed candidate recipe IDs and food IDs; current replacement scope; minimal allowed preference/habit signals; preparation context; missing IDs; task-local context. Prefer code-derived allowed candidates and rules over sending diagnoses, identity, entire chat history, documents or a full profile. Vendor processing needs a privacy GATE even when direct identifiers were removed. Redaction does not establish anonymity.

```ts
interface AiSuggestionProvider {
  suggest(input: BoundedSuggestionInput): Promise<{
    candidateRecipeIds: string[];
    proposedFoodIds: string[];
    explanationKey?: string;
  }>;
}
```

Runtime validation rejects extra keys, unknown IDs, invented nutrients, executable code and oversized output. AI cannot return a trusted valid=true, approval, target, diagnosis or quantity that bypasses deterministic calculation. Explanations shown as check claims are generated from validator results, not free model assertions. Unknown recipes/time/steps are drafts for review, never immediately trusted meal instructions.

### 10.2 Safety and failure boundaries

No direct model tools for database writes, messaging, HTTP browsing, identity changes, rule approval, eating or publication. Treat user chat, food names, uploaded text and model output as untrusted data. Bound permissions in code outside the model; filters and schemas alone do not prevent prompt injection [S3, S4]. Escape rendered text; never render model HTML or evaluate SQL/code.

An explicit profile update from chat creates a preview intent, then user confirmation. A 'what if' question creates no saved diagnosis. A safety-relevant clarification can pause affected proposals while unresolved, but is not a diagnosis. Intent detection can miss natural-language changes; do not promise automatic medical monitoring. Provide a clear manual 'update health information' route and test ambiguous conversations.

Proposed call limits: 8-second provider timeout within the 15-second job deadline; at most one schema repair retry if time/budget remains; circuit breaker after repeated provider faults; no infinite escalation. These are initial engineering defaults. Deterministic templates remain available only when current eligibility and rules permit; an outage is not permission to bypass a clinical block.

Track model identifier/version, prompt-template version, schema version, token usage, latency and validator outcome without raw private text by default. Per-user/model cost caps are enforced server-side. No model/provider selected until Hebrew evaluation, contractual/privacy review, region and real measured cost pass. No SDK/API names or historic price table are assumed current.

## 11. Shared catalog ingestion and correction

1. Normalize search aliases conservatively by locale. An ambiguous match asks for the relevant preparation/product identity rather than merging raw/cooked or branded/generic foods.
2. An unknown-food request is private. It contains only data needed for review and is not visible in shared search.
3. Reviewer records source, license/permission, food identity, preparation state, basis units, nutrients and known/unknown product attributes. Unsupported nutrients stay unknown. Model-generated values are not a source.
4. Approval creates an immutable reference version and aliases in the shared catalog and increments catalogEpoch atomically. Copy only general verified food fields, never the requester's text, identity, private photo, habits or health.
5. New approved food is still subject to each client's rules. Review approval is not coach approval, medical approval, product certification or home availability.
6. Correct/retract reference data with a new version/status and epoch. New/open/saved proposals revalidate. Prior eating snapshots are not silently rewritten. Define a separately reviewed incident/notification process for material data corrections before launch; do not automatically send unapproved messages.

Sources and reviewer responsibility are launch GATES. Development uses synthetic fixtures labeled as such, never medical or real-label claims. The first fixtures should include unknown nutrients, ambiguous aliases, raw/cooked variants, revoked evidence, allergen uncertainty, preparation constraints and a retracted food version.

## 12. UI implementation notes

Coach web: linked clients list -> client overview -> profile/targets/meals/policies -> draft save -> validation issues -> exact change review -> approve -> publish. Keep visible active version vs draft revision. Review diff includes effective time and invalidation implications. Editing after approval removes publish eligibility until reapproved. Refresh conflict shows both versions; no silent merge of clinical or target fields. Publication succeeds only on server confirmation; a lost response is resolved by its stable operation ID.

Mobile: today card with meal, 'I ate this', change amount, 'something else?' -> one full alternative -> selection shown as not eaten. Missing several ingredients stays in the same session. 'I don't feel like it' requests another option, not a permanent dislike. 'Never suggest this' requires an explicit preference action and cannot remove a professional rule. 'My meals' explicitly saves and revalidates recipes. 'I ate something else' supports incomplete facts. Optional context chat returns to the same session without losing choice.

Preparation context can be no heat, portable, equipment and current total-time choice. It does not collect location or imply knowledge of stock. Store session overrides separately from permanent habits. Default unknown time does not become an invented number. Show total and active time, not only assembly time when prior cooking is required.

Hebrew RTL, readable isolated numbers/units, screen-reader names and keyboard order. Proposed minimum targets: 44px touch areas, text scaling, reduced motion and meaning not conveyed only by color. Test 320/390 phone widths and desktop coach layouts. Never copy misleading quantities such as 'unlimited salad' or generic cans from old illustrative screenshots into a calculated production recipe. A screenshot is not a verified recipe source.

## 13. Verification plan and acceptance matrix

No tests in this section have run on an application. Implement automated tests plus professional review for the enabled clinical scope. Synthetic rule fixtures test software behavior, not the correctness of medical thresholds.

| ID | Scenario | Required assertion |
| --- | --- | --- |
| T01 | Coach saves incomplete draft | Active mobile plan/version unchanged; draft marked incomplete |
| T02 | Exact approved draft publishes | New complete version, pointer, audit and outbox commit together |
| T03 | Crash between inserts/publication | Transaction rolls back or one committed publication; never half a plan |
| T04 | Same publication key retried/concurrent | One plan version, same logical response; changed payload rejected |
| T05 | Two tabs edit same revision | One wins; stale one gets conflict, no overwrite |
| T06 | Edit after approval | Approval invalid; cannot publish edited content |
| T07 | Different client's nested plan/job/food submission ID | No data leakage or mutation through direct API, list or export |
| T08 | Linked coach lacks health/diary scope | Allowed plan fields only; private fields denied |
| T09 | Revocation races with publish/read | Writes serialized after revoke denied; current caches evicted on reconnect |
| T10 | Runtime RLS/pooled actor switching | Non-owner role cannot access other clients; transaction context never leaks |
| T11 | Plan/profile/rule/catalog/log changes after generation | Old vector selection rejected; no new vector stamped on old math |
| T12 | Rule/credential expires without an epoch write | Wall-clock validation blocks affected action |
| T13 | Hypothetical health chat / explicit update | Hypothetical creates no diagnosis; explicit update preview requires confirmation |
| T14 | Pending/confirmed health update | Dependent new/open/cached/saved/chat recommendations blocked/revalidated |
| T15 | Missing sodium or required attribute | Unknown not zero; dependent rule cannot pass |
| T16 | Macros pass but approved additional rule fails | Candidate rejected, no generic bypass |
| T17 | Conflicting or unsupported rule bundle | Blocking reason; no automatic strictest-rule treatment |
| T18 | Vegan/allergy/kosher incompatibility/unknown | Required incompatible/unknown evidence cannot yield eligible candidate |
| T19 | Household unit/raw-cooked identity ambiguous | Ask/reject dependent calculation; no generic conversion |
| T20 | Portion rounding crosses a bound | Final rounded candidate rejected |
| T21 | Missing multiple ingredients | All excluded throughout session, no catalog submission or habit change |
| T22 | No-heat/total-time/equipment mismatch | Candidate rejected including hidden prior cooking after a substitution |
| T23 | Day reserve/other slots/consumed slot | No double-counted replaced slot; no packing entire day remainder |
| T24 | Partial/unlogged relevant eaten data | No exact remaining number or dependent approval; viewing/log correction works |
| T25 | Select / save / publish / chat action | Zero eating events until explicit eating action |
| T26 | Full meal eating tap then retry/undo | One logical event; explicit void updates totals once |
| T27 | Same meal intentionally eaten twice | Different operation IDs record both; not falsely deduplicated |
| T28 | Unplanned or plan-violating factual log | Stored as fact with honest completeness, not rejected for noncompliance |
| T29 | Plan/catalog changes after eating | Original snapshots/totals unchanged absent explicit correction |
| T30 | Concurrent corrections/date move | Conflict detection; both affected day projections remain correct |
| T31 | Offline log/reconnect/retry/new plan | Original time retained, one event, stale context marked, no lost factual log |
| T32 | Offline cached proposal/account switch | No fresh validation claim; prior account's private cache unavailable |
| T33 | Notification missing/duplicated/outbox retry | Foreground fetch correct; no duplicate publication or eating |
| T34 | Unknown catalog item / alias / approval | Private candidate; aliases deduplicate only when identity verified; published fields have no requester data |
| T35 | Catalog retraction | New recommendations stop using it; history stays traceable |
| T36 | Model timeout/schema fault/invented ID/injection | No unauthorized effects; safe failure or eligible deterministic route only |
| T37 | AI disabled | Complete curated coach-to-trainee core flow still works |
| T38 | Hebrew/RTL/text scale/keyboard/screen reader | No clipped quantities or hidden required actions; plan vs eaten understood |
| T39 | Timezone/DST/midnight/backdated correction | Stored date/zone stable; no auto-rebucketing of history |
| T40 | Logs/analytics/notifications/backups | No raw health/chat/token leakage; restoration and access tested |

Unit tests: decimal arithmetic, unknown propagation, recipe scaling, rules and state transitions. Property tests: same input yields same result; no feasible result violates a hard bound; increasing an excluded set never returns an excluded ingredient; projections are idempotent; NULL never coerces to zero.
Integration tests: real PostgreSQL transaction/RLS behavior under actual app/worker roles; migration up and safe forward fix; idempotency and lock-order tests with synchronized concurrent requests. Do not substitute an in-memory DB for concurrency evidence.
E2E: coach draft through mobile read/select/eat/undo; no-chat route; stale open card; two browser tabs; native offline queue and account switch. Check release native builds for actual storage/PKCE behavior.
AI evaluation: a reviewed Hebrew case set including typos, local food names, ambiguous updates, injection and unknown food attributes; measure schema validity, unauthorized proposal rate, validator acceptance, hallucinated IDs, latency and cost per accepted result. A good mean score cannot waive any critical safety failure. It is not a clinical efficacy study.
Usability: test unfamiliar users with varying technical confidence. Measure successful selection without assistance, mistakes distinguishing chosen/eaten, task time and failure points. Do not claim a tap-count target is achieved until tested.

## 14. Security, privacy and operations

Threats to address in a written threat model: cross-client object IDs and nested references, field escalation/mass assignment, link-invite account takeover, role self-assertion, revoked access, client-supplied totals, stale approvals, prompt injection through names/chat, untrusted rendered output, replay, oversized jobs, cost abuse, logs/analytics leakage and lost devices. OWASP API guidance supports object/property authorization and resource controls; it does not certify this design [S4].

Production baseline proposal: TLS; encrypted managed DB/backups/object storage with reviewed keys/access; secret manager, rotation and least privilege; rate limits by principal and resource; structured redacted logs; append-oriented audit with controlled access; dependency and secret scanning; session revocation; regular restore drills; health endpoints with no private details. No full profile/chat logging in crash tools, URLs, traces or push text. Push, if later enabled, says only a generic update exists. Staff dashboards exclude health by default.

Upload processing is deferred. If enabled later, require private storage, content type/size validation, malware review, metadata removal, signed bounded access and no execution of uploaded instructions. Product images/labels submitted privately are not automatically licensed for shared use.

GATE: lawful basis/consent, exact coach-sharing scopes, region/residency, retention and deletion per data class, processor agreements, export/portability, minors and clinical-market requirements. Do not claim HIPAA, GDPR, Israeli-law or medical-device compliance from using a particular host or encryption. Verify the actual intended use and market with qualified advisers.

Deletion design must include live data, indexes, device cache behavior, logs, provider-retained content and backup lifecycle. Append-oriented eating/audit records still need a reviewed deletion/pseudonymization path and retention limits; 'audit' is not an excuse for forever retention. An account deletion request must disclose any legally required retention rather than falsely report immediate deletion everywhere. Do not implement irreversible retention choices from this proposal alone.

Observability: request/job ID, domain error code, operation status, latency percentiles, queue age/retries, stale-vector rate, generation outcomes, ledger projection reconciliation, outbox lag and provider spend. Access metadata has retention too. No raw private text in metrics labels. Alert to authorized operators, not automatically to a coach.

Proposed pilot performance budgets, to measure not promise: ordinary plan/ledger API p95 under 500ms, deterministic generation p95 under 2s on agreed fixtures/hardware, optional AI job deadline 15s. Record load profile and dataset size. No user-scale claim follows from a managed DB's included authentication quota.

Deployment proposal: separate dev/staging/prod identities, databases, credentials and provider projects; no production private data copied into development. CI: lint/typecheck/unit -> schema/contracts -> real-DB integration/security/concurrency -> web/native E2E as supported -> dependency checks -> reviewed staging deployment. Use expand/contract migrations and tested backups. Avoid destructive rollback after data migration; prefer compatible rollback/forward repair.

Runtime controls: AI and specific pathway kill switches, block new recommendations independently of viewing/logging, retract catalog versions, disable publication if integrity is uncertain. Rollback of a deployed build is not rollback of eating history. Runbooks must cover restore, stuck outbox, model outage, data-source correction, wrong publication, credential leak and privacy incident before production.

## 15. Decision register: what Devin must not guess

| Gate | Owner to designate | Blocks | Safe work while open |
| --- | --- | --- | --- |
| G01 Tech ADR and exact supported versions | product/technical owner | repository implementation choices | review contracts and this design |
| G02 Launch countries, adult/minor scope, clinical supported pathways and accountable professional | product + qualified clinical/legal leads | real-user clinical recommendations/public launch | synthetic capability/unknown-state tests; clinical flag off |
| G03 Credential verification, jurisdiction/scope/expiry and revocation process | product + professional authority | real professional rule approval | fixture capabilities, denied access tests |
| G04 Food/recipe sources, licenses, reviewer and unit/time evidence | product/data owner | real nutritional calculations/recommendations | clearly synthetic catalog with no treatment claims |
| G05 Explicit coach/day/meal tolerances and future-meal budget policy | qualified plan owner | independent generated substitutions | exact approved fixture alternatives; block missing policy |
| G06 Client identity linking, confirmation and exact health/diary sharing | product/privacy/security owner | real invitation/data disclosure | synthetic links, deny absent scope |
| G07 Hosting/auth/storage/vendor region and data processing terms | product/security/privacy owner | provisioning or private uploads | adapters and local-only planning |
| G08 Retention/deletion, transfer/disconnection and device/offline policy | product/privacy/legal owner | real personal data collection | data-class map and synthetic deletion tests |
| G09 Model provider/version, Hebrew quality, budget and processing permission | product/privacy/technical owner | live AI/private data transmission | AI-disabled path and fake-provider fixtures |
| G10 Scope/order and implementation authorization | user/product owner | actual app implementation | planning-only Devin review |

These gates need not stop every engineering preparation. They stop the affected real-world behavior. Clinical functionality disabled is a release configuration, not removal of the requested dynamic-health design. No party should quietly substitute a wellness launch for the requested product without an explicit scope decision.

## 16. Implementation work packages and acceptance

These are proposed later implementation slices, not work already started. Each ends with a small PR and tests. Do not ask Devin to implement all packages in one unreviewed run.

P00, planning checkpoint. Inputs: this document and product baseline. Outputs: requirement-to-test map B01-B12/T01-T40, ADR-001, dependency plan and consolidated open-gate questions. Acceptance: no implementation, provisioning or private data transfer; inconsistencies surfaced before coding.

P01, repository and pure core scaffold, after implementation approval. Inputs: G01 decision and synthetic fixtures. Outputs: monorepo, dependency lockfile, CI, decimal/unit primitives, contracts and docs. Acceptance: documented clean local commands; tests run by Devin with logs; no secrets/network requirement for fixtures. Proposed command names to implement are pnpm install --frozen-lockfile, pnpm lint, pnpm typecheck, pnpm test; they do not exist yet.

P02, persistence and identity boundaries. Inputs: chosen auth adapter, migration plan, G03/G06 policy for enabled environment. Outputs: principals, links/grants, state/version tables, scoped repositories, runtime roles/RLS and authorization middleware. Acceptance: T07-T10 including malicious nested IDs and pooled context; synthetic identity not available in production.

P03, professional setup and publication vertical slice. Depends P02, G04/G05 synthetic or approved data. Outputs: coach profile draft/confirmation, plan draft/edit/validate/approve/publish; trainee published-plan view; audit/outbox. Acceptance: T01-T06, T11-T12, T33; incomplete/unauthorized publication impossible; no notifications needed for correctness.

P04, eating ledger and summary. Depends P02/P03. Outputs: explicit eating, unknown/unplanned logging, corrections/undo, day projections, idempotency and date semantics. Acceptance: T24-T30/T39; selection never logs; historical snapshots survive plan changes.

P05, deterministic alternatives. Depends P03/P04 plus explicit fixture/approved replacement policies. Outputs: eligibility gates, recipes, missing-item sessions, bounded portion search, final validation, stale selection checks. Acceptance: T11-T23/T25/T37; no AI dependency and no inventory entity. Include resource-exhaustion behavior distinct from proven infeasibility.

P06, saved meals and real-day UI. Depends P05. Outputs: explicit saved recipes, no-heat/current-time overrides, one leading proposal, partial amount editing and preserved context. Acceptance: T19-T25/T38, revalidation after profile changes, household conversions evidence-backed. Validate two-tap usability separately from technical tests.

P07, mobile encrypted cache and outbox. Depends P04/P05 and G08. Outputs: cached read-only plan, offline factual log queue, sync/conflict/account switching states. Acceptance: T26-T33/T39 in native builds; no offline fresh compliance claim; encryption verified rather than assumed.

P08, catalog review. Depends P02/P05 plus G04. Outputs: private submission, alias resolution, reviewer queue, approve/correct/retract, catalog epoch invalidation. Acceptance: T15/T19/T21/T34-T35; no private request fields in shared search, no approval without evidence.

P09, health pathway enforcement. Permission/health gates must already be fail-closed in P02/P05. This slice implements the richer report/intent/confirmed update/rule approval UI, not a delayed safety patch. Depends G02/G03/G04/G06/G08 for any real pathway. Acceptance: T08-T18 and professional-reviewed test set; no inferred treatment thresholds, unsupported state blocks dependent suggestions. Core can be tested synthetically while real pathway is disabled.

P10, optional contextual assistant. Depends stable validated pipeline and G09. Outputs: bounded provider adapter, Hebrew chat, confirmed change intents and candidate ID generation through the same validator. Acceptance: T13-T18/T25/T36-T37, provider outage and injection tests, measured latency/cost. No autonomous writes or food-photo scope.

P11, release hardening and pilot. Depends enabled slices and applicable gates. Outputs: end-to-end authorization/concurrency/security review, accessibility/RTL/native usability results, load profile, restore proof, cost measurements, runbooks and release checklist. Acceptance: no untriaged critical failures; relevant professional/security/privacy sign-off; explicit owner release decision. Automated tests do not replace clinical or legal review.

Definition of done for every later PR: requirement/test references; migration/rollback implications; exact commands and actual results; screenshots for changed UI at relevant widths; new permissions/data disclosures called out; no real data in fixtures; no unmeasured claim that the feature is safe, fast or production-ready.

## 17. Review before copying into a repository

Technical owner: approve or replace the proposed stack, API/module boundaries, lock protocol and exact dependency versions. Product/professional owners: resolve tolerances, source quality, clinical scope and permissions. Security/privacy owners: review identity, RLS, provider processing, retention and offline behavior. User: approve implementation scope and start. These are separate decisions.

This document is sufficient to give Devin a concrete planning/review task now and bounded implementation tasks after approval. It is not a one-command guarantee that the entire product can be built without decisions or review.

## 18. Sources and provenance

Product source P1: current private product specification, read in full across nutrition, coach, health, priority, reference and cost sections on 22 November 2026: https://files.instinct.com/mj9ipqg2rs50-all-in-fit . Historical cost/model/version claims were not revalidated or reused as selected vendors here. No personal meal targets from screenshots are used as production defaults.

The following primary technical references were opened for this design on 22 November 2026. They support specific capabilities/cautions, not a certification of the proposed architecture. Current-documentation URLs can change; implementation must pin and verify its chosen versions. No current provider prices are claimed. Only primary documentation/security guidance was used for these technical semantics; independent performance benchmarking was not performed.

S1. PostgreSQL, Row Security Policies. Supports per-row policies, default deny, owner/BYPASSRLS caveats and the need to test policy behavior. https://www.postgresql.org/docs/current/ddl-rowsecurity.html
S2. PostgreSQL, Transaction Isolation. Supports isolation semantics and retrying full transactions after serialization failure. https://www.postgresql.org/docs/current/transaction-iso.html
S3. OWASP Cheat Sheet Series, LLM Prompt Injection Prevention. Supports treating model/output and retrieved content as untrusted, tool-boundary authorization and limits of prompt filtering. https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html
S4. OWASP API Security Project. Supports object/property authorization and resource-consumption threat coverage. https://owasp.org/projects/api-security-project
S5. Expo SecureStore. Supports small protected key-value storage and platform lifecycle cautions; not a guarantee that a whole profile is suitable for SecureStore. https://docs.expo.dev/versions/latest/sdk/securestore/
S6. Expo SQLite. Supports persisted local databases and the need for supported SQLCipher/native builds and verification; ordinary SQLite persistence does not prove encryption. https://docs.expo.dev/versions/latest/sdk/sqlite/

All other architecture choices, schema structure, limits, task breakdown and acceptance criteria are authored proposals derived from P1. No copied internal working records, credentials or implementation of any existing app are part of this deliverable.
