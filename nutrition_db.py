"""Storage layer for the meal_coach agent (docs/DESIGN.md section 5).

Separate engine/schema from the main platform DB:
  - NUTRITION_DATABASE_URL        runtime connection (non-owner role on PG)
  - NUTRITION_ADMIN_DATABASE_URL  optional owner connection for init/RLS

Dev/tests default to SQLite. On PostgreSQL, init additionally enables and
forces RLS on every client-scoped table: policies key on the `app.client_id`
transaction-local GUC, so an unset/mismatched actor context returns zero rows
(fail-closed) rather than leaking another client's data.

Every private row carries user_id. A missing value is NULL, never zero.
Published objects (plan/profile/rule versions, proposals, eating snapshots)
are immutable by convention AND by DB permission for the runtime role on PG.
"""
import json
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import (
    Column, DateTime, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

load_dotenv()

NUTRITION_DATABASE_URL = os.getenv(
    "NUTRITION_DATABASE_URL", "sqlite+aiosqlite:///./nutrition.db"
)
NUTRITION_ADMIN_DATABASE_URL = os.getenv("NUTRITION_ADMIN_DATABASE_URL")

if NUTRITION_DATABASE_URL.startswith("sqlite:///"):
    NUTRITION_DATABASE_URL = NUTRITION_DATABASE_URL.replace(
        "sqlite:///", "sqlite+aiosqlite:///", 1
    )

IS_POSTGRES = NUTRITION_DATABASE_URL.startswith("postgresql")

# NullPool in tests: pytest-asyncio runs each test on a fresh event loop and
# pooled asyncpg connections cannot migrate between loops. Production pools.
_engine_kwargs = {}
if os.getenv("NUTRITION_POOL") == "null":
    from sqlalchemy.pool import NullPool
    _engine_kwargs["poolclass"] = NullPool

nutrition_engine = create_async_engine(NUTRITION_DATABASE_URL, **_engine_kwargs)
NutritionSessionLocal = async_sessionmaker(
    autocommit=False, autoflush=False, bind=nutrition_engine, class_=AsyncSession,
    # Immutable versioned rows; post-commit attribute reads must not lazily
    # reload inside async handlers (MissingGreenlet - see AGENTS.md).
    expire_on_commit=False,
)
NutritionBase = declarative_base()


# ---------------------------------------------------------------------------
# Plan pipeline: draft -> approval -> immutable published version
# ---------------------------------------------------------------------------
class MealPlanDraft(NutritionBase):
    __tablename__ = "meal_plan_drafts"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    base_plan_version = Column(Integer, nullable=True)
    draft_revision = Column(Integer, nullable=False, default=1)
    content_json = Column(Text, nullable=False)
    validated_against_json = Column(Text, nullable=True)
    updated_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealPlanApproval(NutritionBase):
    __tablename__ = "meal_plan_approvals"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    draft_id = Column(String, nullable=False, index=True)
    draft_revision = Column(Integer, nullable=False)
    content_hash = Column(String, nullable=False)
    input_vector_json = Column(Text, nullable=False)
    approved_by = Column(String, nullable=False)
    approved_at = Column(DateTime, nullable=False)


class MealPlanVersion(NutritionBase):
    __tablename__ = "meal_plan_versions"
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_meal_plan_version"),)

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    published_content_json = Column(Text, nullable=False)  # immutable
    approval_id = Column(String, nullable=False)
    published_by = Column(String, nullable=False)
    effective_at = Column(DateTime, nullable=False)
    change_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Profile versions + change intents (DESIGN 5.1 profile_drafts/versions)
# ---------------------------------------------------------------------------
class MealProfileVersion(NutritionBase):
    __tablename__ = "meal_profile_versions"
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_meal_profile_version"),)

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    # typed fields: preferences/habits/allergies/kosher/excluded+habitual ids
    profile_json = Column(Text, nullable=False)
    confirmed_by = Column(String, nullable=False)
    confirmation_ref = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealProfileChangeIntent(NutritionBase):
    __tablename__ = "meal_profile_change_intents"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending")  # pending|confirmed|cancelled
    proposed_json = Column(Text, nullable=False)
    base_profile_version = Column(Integer, nullable=False, default=0)
    diff_json = Column(Text, nullable=True)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    confirmed_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Health update intents + clinical rule bundles (fail-closed pathway)
# ---------------------------------------------------------------------------
class MealHealthUpdateIntent(NutritionBase):
    __tablename__ = "meal_health_update_intents"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending")  # pending|confirmed|cancelled
    reported_text = Column(String, nullable=False)  # a report is never a diagnosis
    affected_scope = Column(String, nullable=False, default="recommendations")
    source_context = Column(String, nullable=True)
    resolution_json = Column(Text, nullable=True)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class MealClinicalRuleBundle(NutritionBase):
    __tablename__ = "meal_clinical_rule_bundles"
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_meal_rule_bundle"),)

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="draft")  # draft|approved|superseded|revoked
    rules_json = Column(Text, nullable=False)
    supported_pathway = Column(String, nullable=False, default="wellness")
    # Private label set by the professional - stored on the bundle only,
    # never sent to the AI payload or the shared catalog.
    condition_label = Column(String, nullable=True)
    professional_id = Column(String, nullable=True)
    instruction_date = Column(DateTime, nullable=True)
    approver_capability_ref = Column(String, nullable=True)
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealProfessionalCapability(NutritionBase):
    """Verified professional capabilities - platform-level, empty by default.

    Nothing self-assigns these; a bundle can only be approved when a matching
    unexpired, unrevoked capability row exists (DESIGN 4.1)."""
    __tablename__ = "meal_professional_capabilities"

    id = Column(String, primary_key=True)
    principal = Column(String, nullable=False)          # e.g. role:user or principal id
    name = Column(String, nullable=True)                # professional display name (attestation)
    profession = Column(String, nullable=True)          # dietitian|physician
    capability = Column(String, nullable=False)         # e.g. clinical_rule_approver
    jurisdiction = Column(String, nullable=True)
    population_scope = Column(String, nullable=True)
    verified_by = Column(String, nullable=False)
    evidence_ref = Column(String, nullable=True)
    valid_from = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Client state + validation-vector epochs
# ---------------------------------------------------------------------------
class MealClientState(NutritionBase):
    __tablename__ = "meal_client_state"

    user_id = Column(Integer, primary_key=True)
    active_plan_id = Column(String, nullable=True)
    active_profile_id = Column(String, nullable=True)
    active_rule_bundle_id = Column(String, nullable=True)
    eligibility = Column(String, nullable=False, default="unassessed")
    plan_epoch = Column(Integer, nullable=False, default=1)
    ledger_epoch = Column(Integer, nullable=False, default=1)
    safety_epoch = Column(Integer, nullable=False, default=1)
    profile_epoch = Column(Integer, nullable=False, default=1)
    authorization_epoch = Column(Integer, nullable=False, default=1)
    timezone = Column(String, nullable=False, default="Asia/Jerusalem")
    timezone_revision = Column(Integer, nullable=False, default=1)


# ---------------------------------------------------------------------------
# Suggestion sessions, proposal jobs, proposals, selections, saved meals
# ---------------------------------------------------------------------------
class MealSuggestionSession(NutritionBase):
    __tablename__ = "meal_suggestion_sessions"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    slot_key = Column(String, nullable=False)
    nutrition_date = Column(String, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    missing_food_ids_json = Column(Text, nullable=False, default="[]")
    # Session-only "not in the mood" recipe rejections - never permanent.
    rejected_recipe_ids_json = Column(Text, nullable=False, default="[]")
    preparation_json = Column(Text, nullable=False, default="{}")
    status = Column(String, nullable=False, default="open")  # open|closed|expired
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealProposalJob(NutritionBase):
    __tablename__ = "meal_proposal_jobs"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String, nullable=True)
    slot_key = Column(String, nullable=False)
    nutrition_date = Column(String, nullable=False)
    input_vector_json = Column(Text, nullable=False)
    state = Column(String, nullable=False)  # completed|blocked|no_match|search_incomplete|failed
    proposal_id = Column(String, nullable=True)
    failure_code = Column(String, nullable=True)
    reasons_json = Column(Text, nullable=False, default="[]")
    attempts = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class MealProposal(NutritionBase):
    __tablename__ = "meal_proposals"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String, nullable=True)
    slot_key = Column(String, nullable=False)
    nutrition_date = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)
    input_vector_json = Column(Text, nullable=False)
    valid_until = Column(DateTime, nullable=False)
    engine_version = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealSelection(NutritionBase):
    __tablename__ = "meal_selections"
    __table_args__ = (
        Index(
            "uq_meal_selection_current", "user_id", "nutrition_date", "slot_key",
            unique=True,
            sqlite_where=text("status = 'current'"),
            postgresql_where=text("status = 'current'"),
        ),
    )

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    nutrition_date = Column(String, nullable=False)
    slot_key = Column(String, nullable=False)
    proposal_id = Column(String, nullable=True)
    alternative_recipe_id = Column(String, nullable=True)
    snapshot_json = Column(Text, nullable=False)  # exact planned choice
    vector_json = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="current")  # current|superseded
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealSavedMeal(NutritionBase):
    __tablename__ = "meal_saved_meals"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String, nullable=False)
    snapshot_json = Column(Text, nullable=False)  # reusable recipe, never permanent validation
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Eating ledger + day projection + idempotency
# ---------------------------------------------------------------------------
class MealEatingEvent(NutritionBase):
    __tablename__ = "meal_eating_events"
    __table_args__ = (
        UniqueConstraint("user_id", "operation_id", name="uq_meal_event_operation"),
    )

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    operation_id = Column(String, nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    recorded_at = Column(DateTime, nullable=False)
    nutrition_date = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)  # recorded | correction | void
    corrects_event_id = Column(String, nullable=True)
    items_json = Column(Text, nullable=False)
    completeness = Column(String, nullable=False, default="complete")
    plan_version_id = Column(String, nullable=True)
    source_context = Column(Text, nullable=True)


class MealDayState(NutritionBase):
    __tablename__ = "meal_day_state"
    __table_args__ = (UniqueConstraint("user_id", "nutrition_date", name="uq_meal_day"),)

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    nutrition_date = Column(String, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    totals_json = Column(Text, nullable=False)
    completeness = Column(String, nullable=False, default="complete")


class MealIdempotencyRecord(NutritionBase):
    __tablename__ = "meal_idempotency_records"
    __table_args__ = (UniqueConstraint("actor", "route", "key", name="uq_meal_idem"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)  # RLS scope: stored response is client data
    actor = Column(String, nullable=False)
    route = Column(String, nullable=False)
    key = Column(String, nullable=False)
    request_hash = Column(String, nullable=False)
    status = Column(String, nullable=False, default="completed")
    response_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Shared catalog (DB-backed; seeded from synthetic fixtures)
# ---------------------------------------------------------------------------
class MealCatalogState(NutritionBase):
    __tablename__ = "meal_catalog_state"

    id = Column(Integer, primary_key=True)  # singleton: always 1
    catalog_epoch = Column(Integer, nullable=False, default=1)


class MealFoodVersion(NutritionBase):
    __tablename__ = "meal_food_versions"

    id = Column(String, primary_key=True)          # food_version_id
    food_id = Column(String, nullable=False, index=True)
    name_he = Column(String, nullable=False)
    kind = Column(String, nullable=False, default="generic")
    preparation_state = Column(String, nullable=False, default="raw")
    basis_g = Column(String, nullable=False, default="100")
    nutrients_json = Column(Text, nullable=False)
    allergens_json = Column(Text, nullable=False, default="[]")
    kosher_evidence = Column(String, nullable=False, default="unknown")
    status = Column(String, nullable=False, default="approved")  # approved|retracted
    aliases_json = Column(Text, nullable=False, default="[]")
    # deterministic dietary flags (vegan/lactose_free/low_sugar/...) computed
    # from the row's own nutrients+allergens+kind by food_library.
    attributes_json = Column(Text, nullable=False, default="[]")
    evidence = Column(String, nullable=False, default="synthetic-fixture")
    # catalog | auto_lookup | builtin-synthetic. Auto-enriched rows are
    # served unverified until the coach confirms their nutrient data.
    # server_default: raw-SQL seed/migration inserts bypass Python defaults.
    source = Column(String, nullable=False, server_default="catalog")
    verified = Column(Integer, nullable=False, server_default="1")  # 0 = unverified
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealRecipeVersion(NutritionBase):
    __tablename__ = "meal_recipe_versions"

    id = Column(String, primary_key=True)          # recipe_version_id
    name_he = Column(String, nullable=False)
    slots_json = Column(Text, nullable=False)
    items_json = Column(Text, nullable=False)
    active_minutes = Column(Integer, nullable=False, default=0)
    total_minutes = Column(Integer, nullable=False, default=0)
    equipment_json = Column(Text, nullable=False, default="[]")
    steps_json = Column(Text, nullable=False, default="[]")
    status = Column(String, nullable=False, default="approved")
    evidence = Column(String, nullable=False, default="synthetic-fixture")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MealCatalogSubmission(NutritionBase):
    __tablename__ = "meal_catalog_submissions"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)  # requester - private
    request_text = Column(String, nullable=False)
    proposed_match = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")  # pending|approved|rejected
    reviewer_role = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Outbox + audit (append-oriented; no private bodies in audit)
# ---------------------------------------------------------------------------
class MealOutboxEvent(NutritionBase):
    __tablename__ = "meal_outbox_events"

    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)
    aggregate_id = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    delivered_at = Column(DateTime, nullable=True)


class MealAuditEvent(NutritionBase):
    __tablename__ = "meal_audit_events"

    id = Column(String, primary_key=True)
    actor = Column(String, nullable=False)
    operation = Column(String, nullable=False)
    object_id = Column(String, nullable=False)
    outcome_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# RLS + init + catalog seed
# ---------------------------------------------------------------------------
_RLS_TABLES = (
    "meal_plan_drafts",
    "meal_plan_approvals",
    "meal_plan_versions",
    "meal_profile_versions",
    "meal_profile_change_intents",
    "meal_health_update_intents",
    "meal_clinical_rule_bundles",
    "meal_client_state",
    "meal_suggestion_sessions",
    "meal_proposal_jobs",
    "meal_proposals",
    "meal_selections",
    "meal_saved_meals",
    "meal_eating_events",
    "meal_day_state",
    "meal_idempotency_records",
    "meal_catalog_submissions",
)

_RLS_STATEMENTS = (
    [f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY" for t in _RLS_TABLES]
    + [f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY" for t in _RLS_TABLES]
    + [f"DROP POLICY IF EXISTS meal_client_scope ON {t}" for t in _RLS_TABLES]
    + [
        (
            f"CREATE POLICY meal_client_scope ON {t} "
            f"USING (user_id::text = current_setting('app.client_id', true)) "
            f"WITH CHECK (user_id::text = current_setting('app.client_id', true))"
        )
        for t in _RLS_TABLES
    ]
    + [
        # The catalog review queue is a coach surface: the parent reviews
        # submissions from any family member. Scope comes from a server-set
        # GUC applied only after the app-level _require_coach check.
        "DROP POLICY IF EXISTS meal_coach_scope ON meal_catalog_submissions",
        "CREATE POLICY meal_coach_scope ON meal_catalog_submissions "
        "USING (current_setting('app.coach', true) = '1') "
        "WITH CHECK (current_setting('app.coach', true) = '1')",
    ]
)


async def _apply_rls(conn) -> None:
    for stmt in _RLS_STATEMENTS:
        await conn.execute(text(stmt))


async def _seed_catalog(conn) -> None:
    """Seed shared catalog tables from the synthetic fixtures once."""
    from nutrition_core import catalog as fixture_catalog

    existing = await conn.execute(text("SELECT COUNT(*) FROM meal_catalog_state"))
    if (existing.scalar() or 0) > 0:
        return
    await conn.execute(text("INSERT INTO meal_catalog_state (id, catalog_epoch) VALUES (1, 1)"))
    now = datetime.utcnow()
    for food in fixture_catalog.FOODS.values():
        await conn.execute(
            text(
                "INSERT INTO meal_food_versions (id, food_id, name_he, kind, preparation_state,"
                " basis_g, nutrients_json, allergens_json, attributes_json, kosher_evidence,"
                " status, aliases_json, evidence, created_at) VALUES (:id, :food_id, :name_he,"
                " :kind, :prep, :basis, :nutrients, :allergens, :attributes, :kosher, :status,"
                " :aliases, :evidence, :created_at)"
            ),
            {
                "id": food["food_version_id"], "food_id": food["food_version_id"],
                "name_he": food["name_he"], "kind": food["kind"],
                "prep": food["preparation_state"], "basis": food["basis_g"],
                "nutrients": json.dumps(food["nutrients"], ensure_ascii=False),
                "allergens": json.dumps(food["allergens"], ensure_ascii=False),
                "attributes": json.dumps(food.get("attributes") or [], ensure_ascii=False),
                "kosher": food["kosher_evidence"], "status": food["status"],
                "aliases": json.dumps(food["aliases"], ensure_ascii=False),
                "evidence": food["evidence"], "created_at": now,
            },
        )
    for recipe in fixture_catalog.RECIPES.values():
        await conn.execute(
            text(
                "INSERT INTO meal_recipe_versions (id, name_he, slots_json, items_json,"
                " active_minutes, total_minutes, equipment_json, steps_json, status, evidence,"
                " created_at) VALUES (:id, :name_he, :slots, :items, :active, :total, :equipment,"
                " :steps, :status, :evidence, :created_at)"
            ),
            {
                "id": recipe["recipe_version_id"], "name_he": recipe["name_he"],
                "slots": json.dumps(recipe["slots"], ensure_ascii=False),
                "items": json.dumps(recipe["items"], ensure_ascii=False),
                "active": recipe["active_minutes"], "total": recipe["total_minutes"],
                "equipment": json.dumps(recipe["equipment"], ensure_ascii=False),
                "steps": json.dumps(recipe["steps_he"], ensure_ascii=False),
                "status": recipe["status"], "evidence": recipe["evidence"], "created_at": now,
            },
        )


async def _seed_library(conn) -> None:
    """Additively seed the curated reference library (food_library.LIBRARY_FOODS).

    Unlike _seed_catalog (one-shot fixture seed), this runs on EVERY init:
    rows whose id is already present are left untouched (a coach may have
    retracted one), and new ids are inserted as verified reference data.
    Bumps catalog_epoch only when rows were actually added."""
    from nutrition_core import food_library

    existing = {
        r[0] for r in (
            await conn.execute(text("SELECT id FROM meal_food_versions"))
        ).fetchall()
    }
    missing = [f for f in food_library.LIBRARY_FOODS.values()
               if f["food_version_id"] not in existing]
    if not missing:
        return
    now = datetime.utcnow()
    for food in missing:
        await conn.execute(
            text(
                "INSERT INTO meal_food_versions (id, food_id, name_he, kind, preparation_state,"
                " basis_g, nutrients_json, allergens_json, attributes_json, kosher_evidence,"
                " status, aliases_json, evidence, source, verified, created_at)"
                " VALUES (:id, :food_id, :name_he, :kind, :prep, :basis, :nutrients,"
                " :allergens, :attributes, :kosher, :status, :aliases, :evidence,"
                " :source, :verified, :created_at)"
            ),
            {
                "id": food["food_version_id"], "food_id": food["food_version_id"],
                "name_he": food["name_he"], "kind": food["kind"],
                "prep": food["preparation_state"], "basis": food["basis_g"],
                "nutrients": json.dumps(food["nutrients"], ensure_ascii=False),
                "allergens": json.dumps(food["allergens"], ensure_ascii=False),
                "attributes": json.dumps(food.get("attributes") or [], ensure_ascii=False),
                "kosher": food["kosher_evidence"], "status": food["status"],
                "aliases": json.dumps(food["aliases"], ensure_ascii=False),
                "evidence": food["evidence"], "source": "library",
                "verified": 1, "created_at": now,
            },
        )
    await conn.execute(
        text("UPDATE meal_catalog_state SET catalog_epoch = catalog_epoch + 1 WHERE id = 1")
    )


async def _ensure_columns(conn) -> None:
    """Additive column migrations for DBs created before a column existed.

    create_all never alters existing tables, so new nullable-with-default
    columns are added here on both SQLite and PostgreSQL."""
    new_columns = [
        ("meal_suggestion_sessions", "rejected_recipe_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("meal_clinical_rule_bundles", "condition_label", "TEXT"),
        ("meal_clinical_rule_bundles", "professional_id", "VARCHAR"),
        ("meal_clinical_rule_bundles", "instruction_date", "TIMESTAMP"),
        ("meal_professional_capabilities", "name", "VARCHAR"),
        ("meal_professional_capabilities", "profession", "VARCHAR"),
        ("meal_food_versions", "source", "VARCHAR NOT NULL DEFAULT 'catalog'"),
        ("meal_food_versions", "verified", "INTEGER NOT NULL DEFAULT 1"),
        ("meal_food_versions", "attributes_json", "TEXT NOT NULL DEFAULT '[]'"),
    ]
    if IS_POSTGRES:
        for table, col, coltype in new_columns:
            await conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"
            ))
    else:
        for table, col, coltype in new_columns:
            rows = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {r[1] for r in rows.fetchall()}
            if col not in existing:
                await conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"
                ))


async def _upgrade_fixture_nutrients(conn) -> None:
    """Synthetic-fixture upgrade path (synthetic-only exception): rows whose
    evidence is 'synthetic-fixture' get their nutrients_json updated in
    place when the fixture gains new nutrient keys. Real reference data
    must always get NEW versions, never in-place edits."""
    from nutrition_core import catalog as fixture_catalog

    changed = False
    for food in fixture_catalog.FOODS.values():
        row = await conn.execute(
            text("SELECT nutrients_json, evidence FROM meal_food_versions WHERE id = :id"),
            {"id": food["food_version_id"]},
        )
        row = row.fetchone()
        if not row or row[1] != "synthetic-fixture":
            continue
        try:
            current = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            current = {}
        if current != food["nutrients"]:
            await conn.execute(
                text("UPDATE meal_food_versions SET nutrients_json = :n WHERE id = :id"),
                {"n": json.dumps(food["nutrients"], ensure_ascii=False),
                 "id": food["food_version_id"]},
            )
            changed = True
    if changed:
        await conn.execute(
            text("UPDATE meal_catalog_state SET catalog_epoch = catalog_epoch + 1 WHERE id = 1")
        )


async def init_nutrition_db() -> None:
    """Create schema + seed catalog; on PostgreSQL also apply forced RLS.

    When NUTRITION_ADMIN_DATABASE_URL is set, schema/RLS are applied through
    the owner connection; otherwise the runtime connection is used (dev)."""
    if IS_POSTGRES and NUTRITION_ADMIN_DATABASE_URL:
        admin_engine = create_async_engine(NUTRITION_ADMIN_DATABASE_URL)
        try:
            async with admin_engine.begin() as conn:
                await conn.run_sync(NutritionBase.metadata.create_all)
                await _ensure_columns(conn)
                await _apply_rls(conn)
                await _seed_catalog(conn)
                await _seed_library(conn)
                await _upgrade_fixture_nutrients(conn)
        finally:
            await admin_engine.dispose()
    else:
        async with nutrition_engine.begin() as conn:
            await conn.run_sync(NutritionBase.metadata.create_all)
            await _ensure_columns(conn)
            if IS_POSTGRES:
                await _apply_rls(conn)
            await _seed_catalog(conn)
            await _seed_library(conn)
            await _upgrade_fixture_nutrients(conn)


async def bind_rls_actor(db, user_id: Optional[int]) -> None:
    """(Re)bind the transaction-local RLS actor context. The GUC dies with
    its transaction, so any code that commits/rolls back and keeps querying
    must call this again before the next statement."""
    if IS_POSTGRES and user_id is not None:
        await db.execute(
            text("SELECT set_config('app.client_id', :uid, true)"),
            {"uid": str(user_id)},
        )


async def get_nutrition_db(user_id: Optional[int] = None):
    """Session dependency. On PG, binds the RLS actor context when user_id
    is given (set after the app-level authorization check, never from a
    client-supplied actor field)."""
    async with NutritionSessionLocal() as db:
        await bind_rls_actor(db, user_id)
        yield db


async def get_nutrition_db_coach():
    """Session for coach-only cross-client surfaces (catalog review queue).
    The coach GUC is set server-side and only ever wired into endpoints that
    run _require_coach first - a child can never reach a query through it."""
    async with NutritionSessionLocal() as db:
        if IS_POSTGRES:
            await db.execute(text("SELECT set_config('app.coach', '1', true)"))
        yield db
