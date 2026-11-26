"""Meal Coach agent - coach-led family meal planning (docs/DESIGN.md).

Non-negotiable invariants (ADR-001 amendment):
  - draft != approve != publish; only an authorized publish changes the
    active plan atomically, in one transaction with its idempotency record
  - selection != eating; only POST /eating-events creates ledger entries
  - unknown is never zero; stale vectors reject rather than re-stamp
  - pending health clarification blocks recommendations, not viewing/logging
  - clinical pathway is fail-closed: bundle approval requires a verified,
    unexpired capability row; none exist in the family deployment
  - per-user mutations serialize through a process-local lock and commit
    as one transaction

Confirmed profile shape (profile_json, whole-profile replace):
  allergy_disclosure: "declared" | "none_declared" | "skipped"
      (a missing field == skipped: not 'no allergies', B11)
  allergies:            list of allergen ids (meaningful only when declared)
  kosher_mode:          e.g. "none" | "strict"
  habitual_food_ids:    food_version_ids the family eats routinely
  excluded_food_ids:    hard profile exclusions
  disliked_recipe_ids:  "never suggest this" - removes alternatives only,
                        never the coach's planned meal
  preparation_defaults: optional {max_total_minutes, equipment}
"""
import asyncio
from decimal import Decimal
import json
import re
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nutrition_core import ENGINE_VERSION, engine
from nutrition_core.catalog import CatalogSource
from nutrition_core.food_library import KNOWN_DIET_FLAGS, derive_attributes
from nutrition_core.lookup import lookup_food
from nutrition_db import (
    MealCatalogState,
    MealCatalogSubmission,
    MealClientState,
    MealClinicalRuleBundle,
    MealDayState,
    MealEatingEvent,
    MealFoodVersion,
    MealHealthUpdateIntent,
    MealIdempotencyRecord,
    MealOutboxEvent,
    MealAuditEvent,
    MealPlanApproval,
    MealPlanDraft,
    MealPlanVersion,
    MealProfessionalCapability,
    MealProfileChangeIntent,
    MealProfileVersion,
    MealProposal,
    MealProposalJob,
    MealRecipeVersion,
    MealSavedMeal,
    MealSelection,
    MealSuggestionSession,
    get_nutrition_db,
    get_nutrition_db_coach,
)
from rbac import Role, get_current_role

router = APIRouter(prefix="/api/meals", tags=["Meal Coach"])

_APP_TZ = os.getenv("APP_TIMEZONE", "Asia/Jerusalem")
_PROPOSAL_TTL_SECONDS = 900
_SESSION_TTL_SECONDS = 1800
_user_locks: dict = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def _require_coach(request: Request) -> str:
    role = get_current_role(request)
    if role not in (Role.PARENT, Role.ADMIN):
        raise HTTPException(403, detail="meal planning requires a parent or admin")
    return role


def _require_member(request: Request) -> str:
    role = get_current_role(request)
    if role not in (Role.CHILD, Role.PARENT, Role.ADMIN):
        raise HTTPException(403, detail="unauthorized")
    return role


def _err(status: int, code: str, message: str, **extra):
    raise HTTPException(status, detail={"code": code, "messageKey": message, **extra})


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """API payloads arrive tz-aware (ISO 'Z'); ORM columns are naive UTC.
    Normalize aware values before compare/store so naive vs aware never
    meets (Postgres may also return aware values for stored columns)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _audit(db: AsyncSession, actor: str, operation: str, object_id: str, outcome: dict) -> None:
    db.add(MealAuditEvent(
        id=str(uuid.uuid4()), actor=actor, operation=operation,
        object_id=object_id, outcome_json=json.dumps(outcome, ensure_ascii=False),
    ))


def _outbox(db: AsyncSession, event_type: str, aggregate_id: str, payload: dict) -> None:
    db.add(MealOutboxEvent(
        id=str(uuid.uuid4()), type=event_type, aggregate_id=aggregate_id,
        payload_json=json.dumps(payload, ensure_ascii=False),
    ))


# ---------------------------------------------------------------------------
# catalog source + vector
# ---------------------------------------------------------------------------
async def _catalog_source(db: AsyncSession) -> CatalogSource:
    epoch_row = (
        await db.execute(select(MealCatalogState).where(MealCatalogState.id == 1))
    ).scalar_one_or_none()
    foods = {}
    for row in (await db.execute(select(MealFoodVersion))).scalars().all():
        foods[row.id] = {
            "food_version_id": row.id, "name_he": row.name_he, "kind": row.kind,
            "preparation_state": row.preparation_state, "basis_g": row.basis_g,
            "nutrients": _loads(row.nutrients_json, {}),
            "allergens": _loads(row.allergens_json, []),
            "attributes": _loads(getattr(row, "attributes_json", "[]") or "[]", []),
            "kosher_evidence": row.kosher_evidence, "status": row.status,
            "aliases": _loads(row.aliases_json, []), "evidence": row.evidence,
            "source": row.source, "verified": row.verified,
        }
    recipes = {}
    for row in (await db.execute(select(MealRecipeVersion))).scalars().all():
        recipes[row.id] = {
            "recipe_version_id": row.id, "name_he": row.name_he,
            "slots": _loads(row.slots_json, []), "items": _loads(row.items_json, []),
            "active_minutes": row.active_minutes, "total_minutes": row.total_minutes,
            "equipment": _loads(row.equipment_json, []),
            "steps_he": _loads(row.steps_json, []), "status": row.status,
            "evidence": row.evidence,
        }
    epoch = f"db/{epoch_row.catalog_epoch}" if epoch_row else "uninitialized"
    return CatalogSource(foods=foods, recipes=recipes, epoch=epoch)


async def _bump_catalog_epoch(db: AsyncSession) -> int:
    row = (
        await db.execute(select(MealCatalogState).where(MealCatalogState.id == 1))
    ).scalar_one_or_none()
    if not row:
        row = MealCatalogState(id=1, catalog_epoch=1)
        db.add(row)
        await db.flush()
    row.catalog_epoch += 1
    return row.catalog_epoch


def _vector(state: MealClientState, src: CatalogSource) -> dict:
    """Reduced plan-input vector bound to approvals (P00 finding: ledger/day
    epochs must NOT invalidate an approval - eating between approve and
    publish is normal)."""
    return {
        "activePlanVersion": state.active_plan_id,
        "activeProfileId": state.active_profile_id,
        "activeRuleBundleId": state.active_rule_bundle_id,
        "planEpoch": state.plan_epoch,
        "safetyEpoch": state.safety_epoch,
        "profileEpoch": state.profile_epoch,
        "authorizationEpoch": state.authorization_epoch,
        "timezoneRevision": state.timezone_revision,
        "catalogEpoch": src.epoch,
        "engineVersion": ENGINE_VERSION,
    }


async def _get_state(db: AsyncSession, user_id: int, create: bool = False) -> MealClientState:
    state = (
        await db.execute(select(MealClientState).where(MealClientState.user_id == user_id))
    ).scalar_one_or_none()
    if state is None and create:
        state = MealClientState(user_id=user_id, timezone=_APP_TZ)
        db.add(state)
        await db.flush()
    return state


async def _current_profile(db: AsyncSession, user_id: int, state: Optional[MealClientState]) -> dict:
    """Effective profile: current immutable version, or empty defaults.

    Empty profile is valid (B11); safety-relevant unknowns stay unknown -
    an omitted allergy field is not 'no allergies'."""
    if state and state.active_profile_id:
        row = (
            await db.execute(select(MealProfileVersion).where(
                MealProfileVersion.id == state.active_profile_id
            ))
        ).scalar_one_or_none()
        if row:
            return _loads(row.profile_json, {})
    return {}


def _profile_rules(profile: dict) -> List[dict]:
    """Profile-derived hard rules: exclusions, allergies, kosher mode."""
    rules = []
    excluded = profile.get("excluded_food_ids") or []
    if excluded:
        rules.append({"kind": "exclude_food", "food_ids": excluded, "provenance_id": "profile"})
    for allergen in profile.get("allergies") or []:
        rules.append({
            "kind": "require_verified_attribute", "attribute": "allergen_free",
            "expected": allergen, "provenance_id": "profile",
        })
    if profile.get("kosher_mode") == "strict":
        rules.append({
            "kind": "require_verified_attribute", "attribute": "kosher_certified",
            "expected": "certified", "provenance_id": "profile",
        })
    for flag in profile.get("diet_flags") or []:
        if flag in KNOWN_DIET_FLAGS:
            rules.append({
                "kind": "require_verified_attribute", "attribute": "diet_flag",
                "expected": flag, "provenance_id": "profile",
            })
    return rules


async def _plan_extra_rules(db: AsyncSession, state: Optional[MealClientState]) -> List[dict]:
    """Profile + valid active-bundle rules to enforce during plan
    validation/approval/publication."""
    profile = await _current_profile(db, user_id=None if state is None else state.user_id, state=state)
    bundle = await _active_bundle(db, state)
    return _profile_rules(profile) + (
        _loads(bundle.rules_json, []) if _bundle_valid(bundle) else []
    )


async def _active_bundle(db: AsyncSession, state: Optional[MealClientState]) -> Optional[MealClinicalRuleBundle]:
    """The bundle row state points at, whatever its status (fail-closed: an
    unreadable or expired active bundle is a block reason, never [])."""
    if not state or not state.active_rule_bundle_id:
        return None
    return (
        await db.execute(select(MealClinicalRuleBundle).where(
            MealClinicalRuleBundle.id == state.active_rule_bundle_id,
        ))
    ).scalar_one_or_none()


def _bundle_valid(bundle: Optional[MealClinicalRuleBundle]) -> bool:
    return bool(
        bundle
        and bundle.status == "approved"
        and not (bundle.valid_until and _naive_utc(bundle.valid_until) < datetime.utcnow())
    )


def _medical_block_reason(
    state: Optional[MealClientState],
    profile: Optional[dict],
    bundle: Optional[MealClinicalRuleBundle],
) -> Optional[str]:
    """Fail-closed medical-instruction gate (B07): an active bundle that
    expired or became unreadable blocks dependent recommendations; a
    declared/unsure medical condition with no valid bundle does too.
    Viewing the plan and logging eating stay open."""
    if state and state.active_rule_bundle_id and not _bundle_valid(bundle):
        return "medical instruction expired"
    declared = (profile or {}).get("medical_condition_declared") or "unsure"
    if declared != "no" and not _bundle_valid(bundle):
        return "medical instruction missing"
    return None


async def _pending_health_block(db: AsyncSession, user_id: int) -> Optional[str]:
    pending = (
        await db.execute(select(MealHealthUpdateIntent).where(
            MealHealthUpdateIntent.user_id == user_id,
            MealHealthUpdateIntent.status == "pending",
        ))
    ).scalars().all()
    return pending[0].id if pending else None


async def _day_projection(db: AsyncSession, user_id: int, nutrition_date: str, src: CatalogSource) -> dict:
    rows = (
        await db.execute(
            select(MealEatingEvent)
            .where(MealEatingEvent.user_id == user_id, MealEatingEvent.nutrition_date == nutrition_date)
            .order_by(MealEatingEvent.recorded_at)
        )
    ).scalars().all()
    events = [
        {
            "id": e.id, "event_type": e.event_type, "corrects_event_id": e.corrects_event_id,
            "items": _loads(e.items_json, []), "occurred_at": e.occurred_at,
            "nutrition_date": e.nutrition_date, "completeness": e.completeness,
        }
        for e in rows
    ]
    return engine.day_totals(engine.fold_effective_events(events), src)


async def _refold_day(db: AsyncSession, user_id: int, nutrition_date: str, src: CatalogSource) -> None:
    projection = await _day_projection(db, user_id, nutrition_date, src)
    day = (
        await db.execute(
            select(MealDayState).where(
                MealDayState.user_id == user_id, MealDayState.nutrition_date == nutrition_date
            )
        )
    ).scalar_one_or_none()
    if day is None:
        day = MealDayState(
            id=str(uuid.uuid4()), user_id=user_id, nutrition_date=nutrition_date,
            revision=0, totals_json="{}", completeness="complete",
        )
        db.add(day)
    day.revision += 1
    day.totals_json = json.dumps(projection["totals"], ensure_ascii=False)
    day.completeness = projection["completeness"]


def _event_dict(e: MealEatingEvent) -> dict:
    return {
        "id": e.id,
        "operation_id": e.operation_id,
        "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
        "nutrition_date": e.nutrition_date,
        "event_type": e.event_type,
        "corrects_event_id": e.corrects_event_id,
        "items": _loads(e.items_json, []),
        "completeness": e.completeness,
        "plan_version_id": e.plan_version_id,
        "source_context": e.source_context,
    }


def _today(tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo(_APP_TZ)
    return datetime.now(zone).date().isoformat()


def _event_date(occurred: datetime, supplied: Optional[str], tz_name: str) -> str:
    """Explicit backdate wins; otherwise derive from occurred_at in the
    account nutrition timezone (never the device clock alone)."""
    if supplied:
        return supplied
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(_APP_TZ)
    aware = occurred if occurred.tzinfo else occurred.replace(tzinfo=ZoneInfo("UTC"))
    return aware.astimezone(tz).date().isoformat()


async def _check_idempotency(
    db: AsyncSession, actor: str, route: str, key: Optional[str], request_hash: str
) -> Optional[dict]:
    if not key:
        _err(428, "IDEMPOTENCY_KEY_REQUIRED", "missing Idempotency-Key header")
    existing = (
        await db.execute(
            select(MealIdempotencyRecord).where(
                MealIdempotencyRecord.actor == actor,
                MealIdempotencyRecord.route == route,
                MealIdempotencyRecord.key == key,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if existing.request_hash != request_hash:
            _err(409, "IDEMPOTENCY_KEY_REUSED", "same key with a different payload")
        return _loads(existing.response_json, {})
    return None


def _store_idempotent_response(db, actor, route, key, request_hash, response, user_id: int) -> None:
    db.add(MealIdempotencyRecord(
        user_id=user_id, actor=actor, route=route, key=key, request_hash=request_hash,
        status="completed", response_json=json.dumps(response, ensure_ascii=False),
    ))


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
class DraftCreate(BaseModel):
    content: dict
    base_plan_version: Optional[int] = None


class DraftPatch(BaseModel):
    content: dict


class ApproveRequest(BaseModel):
    content_hash: str
    input_vector: dict


class PublishRequest(BaseModel):
    approval_id: str
    change_summary: Optional[str] = None


class ProfileIntentCreate(BaseModel):
    proposed: dict  # preferences/habits/allergies/kosher_mode/habitual+excluded ids


class SessionCreate(BaseModel):
    slot_key: str
    nutrition_date: Optional[str] = None
    preparation: Optional[dict] = None


class SessionPatch(BaseModel):
    missing_food_ids: Optional[List[str]] = None
    rejected_recipe_ids: Optional[List[str]] = None
    preparation: Optional[dict] = None


class ProposalJobCreate(BaseModel):
    session_id: Optional[str] = None
    slot_key: Optional[str] = None
    nutrition_date: Optional[str] = None
    expected_vector: Optional[dict] = None
    saved_meal_id: Optional[str] = None


class SelectionCreate(BaseModel):
    nutrition_date: str
    slot_key: str
    proposal_id: Optional[str] = None
    alternative_recipe_id: Optional[str] = None
    items: Optional[List[dict]] = None  # explicit quantities for alternatives
    session_id: Optional[str] = None
    expected_vector: Optional[dict] = None


class SavedMealCreate(BaseModel):
    name: str
    snapshot: dict  # {recipe_version_id?, items:[{food_version_id, quantity_g}]}


class EatingItem(BaseModel):
    food_version_id: Optional[str] = None
    description: Optional[str] = None
    quantity_g: Optional[str] = None


class EatingEventCreate(BaseModel):
    operation_id: str
    occurred_at: Optional[datetime] = None
    nutrition_date: Optional[str] = None
    items: List[EatingItem]
    plan_version_id: Optional[str] = None
    source_context: Optional[str] = None


class CorrectionCreate(BaseModel):
    items: List[EatingItem]
    nutrition_date: Optional[str] = None


class HealthIntentCreate(BaseModel):
    reported_text: str
    source_context: Optional[str] = None


class HealthIntentResolve(BaseModel):
    action: str  # 'confirm' | 'cancel'
    resolution: Optional[dict] = None


class RuleBundleCreate(BaseModel):
    rules: List[dict]
    supported_pathway: Optional[str] = "wellness"
    condition_label: Optional[str] = None  # private - never sent to the AI
    professional_id: Optional[str] = None
    instruction_date: Optional[datetime] = None
    valid_until: datetime


class RuleBundleApprove(BaseModel):
    professional_id: str


class ProfessionalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    profession: str
    license_number: str = Field(min_length=1, max_length=80)
    valid_until: datetime


class FitPortionsRequest(BaseModel):
    slot_key: str
    recipe_version_id: str
    other_meals: Optional[List[dict]] = None  # [{slot_key, items:[{food_version_id, quantity_g}]}]


class AutoPlanRequest(BaseModel):
    slots: List[dict]          # [{"slot_key": "lunch", "kosher": "meat|dairy|parve|any"}]
    day_targets: Optional[dict] = None  # same shape as plan content day_targets
    use_llm: bool = False      # try LLM food picks first, engine sizes portions


class SubmissionCreate(BaseModel):
    request_text: str
    proposed_match: Optional[str] = None


class ReviewApprove(BaseModel):
    name_he: str
    kind: Optional[str] = "generic"
    preparation_state: Optional[str] = "raw"
    basis_g: Optional[str] = "100"
    nutrients: dict
    allergens: Optional[List[str]] = None  # None = undeclared (unknown, fail-closed)
    kosher_evidence: Optional[str] = "unknown"
    aliases: Optional[List[str]] = []
    attributes: Optional[List[str]] = None  # None = derive from nutrients


class FoodCreate(BaseModel):
    name_he: str = Field(min_length=1, max_length=120)
    kind: Optional[str] = "generic"
    preparation_state: Optional[str] = "raw"
    nutrients: Optional[dict] = None      # None -> internet lookup first
    allergens: Optional[List[str]] = None # None = undeclared (fail-closed)
    kosher_evidence: Optional[str] = None
    aliases: Optional[List[str]] = None
    lookup: bool = True


class FoodPatch(BaseModel):
    name_he: Optional[str] = None
    kind: Optional[str] = None
    nutrients: Optional[dict] = None      # merged over existing values
    allergens: Optional[List[str]] = None
    kosher_evidence: Optional[str] = None
    aliases: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# catalog reads
# ---------------------------------------------------------------------------
@router.get("/catalog")
async def get_catalog(request: Request, db: AsyncSession = Depends(get_nutrition_db)):
    get_current_role(request)
    src = await _catalog_source(db)
    return {
        "catalog_epoch": src.epoch,
        "foods": src.approved_foods(),
        "recipes": src.approved_recipes(),
    }


# ---------------------------------------------------------------------------
# client state / active plan
# ---------------------------------------------------------------------------
@router.get("/{user_id}/state")
async def get_state(request: Request, user_id: int, db: AsyncSession = Depends(get_nutrition_db)):
    get_current_role(request)
    src = await _catalog_source(db)
    state = await _get_state(db, user_id)
    has_plan = bool(state and state.active_plan_id)
    plan_version = None
    if has_plan:
        plan_version = (
            await db.execute(select(MealPlanVersion.version).where(
                MealPlanVersion.id == state.active_plan_id
            ))
        ).scalar_one_or_none()
    if has_plan:
        setup_status = "published"
    else:
        draft_count = (
            await db.execute(
                select(func.count()).select_from(MealPlanDraft).where(
                    MealPlanDraft.user_id == user_id
                )
            )
        ).scalar() or 0
        setup_status = "draft" if draft_count > 0 else "not_started"
    return {
        "user_id": user_id,
        "eligibility": state.eligibility if state else "unassessed",
        "active_plan_id": state.active_plan_id if state else None,
        "active_plan_version": plan_version,
        "has_active_plan": has_plan,
        "setup_status": setup_status,
        "vector": _vector(state, src) if state else None,
    }


def _public_plan(plan: MealPlanVersion, resolved: dict) -> dict:
    content = _loads(plan.published_content_json, {})
    meals = []
    for meal in resolved["meals"]:
        meals.append({
            "slot_key": meal["slot_key"],
            "recipe_version_id": meal.get("recipe_version_id"),
            "items": [
                {
                    "food_version_id": i["food_version_id"],
                    "name_he": (i.get("food") or {}).get("name_he"),
                    "quantity_g": i["quantity_g"],
                }
                for i in meal["items"]
            ],
            "totals": meal["totals"],
        })
    return {
        "id": plan.id,
        "version": plan.version,
        "name": content.get("name"),
        "effective_at": plan.effective_at.isoformat() if plan.effective_at else None,
        "change_summary": plan.change_summary,
        "day_targets": resolved.get("day_targets") or {},
        "alternatives": resolved.get("alternatives") or {},
        "meals": meals,
    }


@router.get("/{user_id}/plan")
async def get_active_plan(request: Request, user_id: int, db: AsyncSession = Depends(get_nutrition_db)):
    get_current_role(request)
    src = await _catalog_source(db)
    state = await _get_state(db, user_id)
    if not state or not state.active_plan_id:
        return {"user_id": user_id, "plan": None, "vector": None}
    plan = (
        await db.execute(select(MealPlanVersion).where(MealPlanVersion.id == state.active_plan_id))
    ).scalar_one_or_none()
    if not plan:
        return {"user_id": user_id, "plan": None, "vector": _vector(state, src)}
    resolved = engine.validate_plan_content(_loads(plan.published_content_json, {}), src)
    return {"user_id": user_id, "plan": _public_plan(plan, resolved), "vector": _vector(state, src)}


# ---------------------------------------------------------------------------
# draft / approve / publish
# ---------------------------------------------------------------------------
@router.post("/{user_id}/plan-drafts")
async def create_draft(request: Request, user_id: int, data: DraftCreate,
                       db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)
    async with _lock_for(user_id):
        await _get_state(db, user_id, create=True)
        draft = MealPlanDraft(
            id=str(uuid.uuid4()), user_id=user_id,
            base_plan_version=data.base_plan_version, draft_revision=1,
            content_json=json.dumps(data.content, ensure_ascii=False), updated_by=role,
        )
        db.add(draft)
        await db.commit()
        return {"draft_id": draft.id, "draft_revision": draft.draft_revision}


@router.get("/{user_id}/plan-drafts")
async def list_drafts(request: Request, user_id: int, db: AsyncSession = Depends(get_nutrition_db)):
    _require_coach(request)
    rows = (
        await db.execute(
            select(MealPlanDraft).where(MealPlanDraft.user_id == user_id)
            .order_by(MealPlanDraft.updated_at.desc())
        )
    ).scalars().all()
    return {"drafts": [
        {"draft_id": d.id, "draft_revision": d.draft_revision,
         "base_plan_version": d.base_plan_version,
         "content": _loads(d.content_json, {}),
         "updated_at": d.updated_at.isoformat() if d.updated_at else None}
        for d in rows
    ]}


async def _load_draft(db: AsyncSession, user_id: int, draft_id: str) -> MealPlanDraft:
    draft = (
        await db.execute(select(MealPlanDraft).where(
            MealPlanDraft.id == draft_id, MealPlanDraft.user_id == user_id
        ))
    ).scalar_one_or_none()
    if not draft:
        _err(404, "NOT_FOUND", "draft not found")
    return draft


@router.patch("/{user_id}/plan-drafts/{draft_id}")
async def patch_draft(request: Request, user_id: int, draft_id: str, data: DraftPatch,
                      if_match: Optional[int] = Header(None, alias="If-Match"),
                      db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)
    if if_match is None:
        _err(428, "PRECONDITION_REQUIRED", "missing If-Match header")
    async with _lock_for(user_id):
        draft = await _load_draft(db, user_id, draft_id)
        if draft.draft_revision != if_match:
            _err(409, "STALE_REVISION", "draft revision mismatch",
                 staleFields={"draft_revision": draft.draft_revision})
        draft.content_json = json.dumps(data.content, ensure_ascii=False)
        draft.draft_revision += 1
        draft.updated_by = role
        draft.updated_at = datetime.utcnow()
        await db.commit()
        return {"draft_id": draft.id, "draft_revision": draft.draft_revision}


@router.post("/{user_id}/plan-drafts/{draft_id}/validate")
async def validate_draft(request: Request, user_id: int, draft_id: str,
                         db: AsyncSession = Depends(get_nutrition_db)):
    _require_coach(request)
    src = await _catalog_source(db)
    draft = await _load_draft(db, user_id, draft_id)
    state = await _get_state(db, user_id)
    content = _loads(draft.content_json, {})
    extra = await _plan_extra_rules(db, state)
    result = engine.validate_plan_content(content, src, extra_rules=extra)
    return {
        "draft_id": draft_id,
        "draft_revision": draft.draft_revision,
        "content_hash": engine.canonical_hash(content),
        "input_vector": _vector(state, src) if state else None,
        "valid": result["valid"],
        "issues": result["issues"],
        "meals": [
            {"slot_key": m["slot_key"], "totals": m["totals"],
             "recipe_version_id": m.get("recipe_version_id")}
            for m in result["meals"]
        ],
    }


@router.post("/{user_id}/plan-drafts/{draft_id}/approve")
async def approve_draft(request: Request, user_id: int, draft_id: str, data: ApproveRequest,
                        if_match: Optional[int] = Header(None, alias="If-Match"),
                        db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)
    if if_match is None:
        _err(428, "PRECONDITION_REQUIRED", "missing If-Match header")
    async with _lock_for(user_id):
        src = await _catalog_source(db)
        draft = await _load_draft(db, user_id, draft_id)
        if draft.draft_revision != if_match:
            _err(409, "STALE_REVISION", "approval must bind the current draft revision",
                 staleFields={"draft_revision": draft.draft_revision})
        content = _loads(draft.content_json, {})
        if engine.canonical_hash(content) != data.content_hash:
            _err(409, "HASH_MISMATCH", "content hash does not match this draft")
        extra = await _plan_extra_rules(db, await _get_state(db, user_id))
        resolved = engine.validate_plan_content(content, src, extra_rules=extra)
        if not resolved["valid"]:
            _err(422, "INVALID_PLAN", "cannot approve a draft that fails validation",
                 fieldErrors=resolved["issues"])
        state = await _get_state(db, user_id, create=True)
        current_vector = _vector(state, src)
        if data.input_vector != current_vector:
            _err(409, "STALE_INPUT", "input vector changed since approval preview",
                 staleFields={"current": current_vector})
        approval = MealPlanApproval(
            id=str(uuid.uuid4()), user_id=user_id, draft_id=draft_id,
            draft_revision=draft.draft_revision, content_hash=data.content_hash,
            input_vector_json=json.dumps(data.input_vector, sort_keys=True),
            approved_by=role, approved_at=datetime.utcnow(),
        )
        db.add(approval)
        await db.commit()
        return {"approval_id": approval.id, "draft_revision": approval.draft_revision}


@router.post("/{user_id}/plan-drafts/{draft_id}/publish")
async def publish_draft(request: Request, user_id: int, draft_id: str, data: PublishRequest,
                        if_match: Optional[int] = Header(None, alias="If-Match"),
                        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
                        db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)
    route = f"publish:{user_id}:{draft_id}"
    request_hash = engine.canonical_hash({"approval_id": data.approval_id, "if_match": if_match})
    stored = await _check_idempotency(db, role, route, idempotency_key, request_hash)
    if stored is not None:
        return stored
    async with _lock_for(user_id):
        src = await _catalog_source(db)
        draft = await _load_draft(db, user_id, draft_id)
        if if_match is not None and draft.draft_revision != if_match:
            _err(409, "STALE_REVISION", "draft changed after approval; re-approve required",
                 staleFields={"draft_revision": draft.draft_revision})
        approval = (
            await db.execute(select(MealPlanApproval).where(
                MealPlanApproval.id == data.approval_id,
                MealPlanApproval.draft_id == draft_id,
                MealPlanApproval.user_id == user_id,
            ))
        ).scalar_one_or_none()
        if not approval:
            _err(404, "NOT_FOUND", "approval not found for this draft")
        if approval.draft_revision != draft.draft_revision:
            _err(409, "STALE_REVISION", "draft edited after approval",
                 staleFields={"draft_revision": draft.draft_revision})
        content = _loads(draft.content_json, {})
        if engine.canonical_hash(content) != approval.content_hash:
            _err(409, "HASH_MISMATCH", "draft content changed after approval")
        state = await _get_state(db, user_id, create=True)
        if _loads(approval.input_vector_json, {}) != _vector(state, src):
            _err(409, "STALE_INPUT", "state changed after approval; re-approve required")
        # B02: publication is gated on a confirmed profile, not only on
        # coach approval.
        if not state.active_profile_id:
            _err(409, "PROFILE_CONFIRMATION_REQUIRED",
                 "confirm the nutrition profile before publishing")
        # B07: a declared/unsure medical condition needs an approved,
        # unexpired professional instruction before publishing.
        profile = await _current_profile(db, user_id, state)
        bundle = await _active_bundle(db, state)
        declared = profile.get("medical_condition_declared") or "unsure"
        if declared != "no" and not _bundle_valid(bundle):
            _err(409, "MEDICAL_INSTRUCTION_REQUIRED",
                 "an approved professional instruction is required before publishing")
        extra = _profile_rules(profile) + (
            _loads(bundle.rules_json, []) if _bundle_valid(bundle) else []
        )
        resolved = engine.validate_plan_content(content, src, extra_rules=extra)
        if not resolved["valid"]:
            _err(422, "INVALID_PLAN", "draft fails validation", fieldErrors=resolved["issues"])

        max_version = (
            await db.execute(
                select(func.max(MealPlanVersion.version)).where(MealPlanVersion.user_id == user_id)
            )
        ).scalar() or 0
        version = MealPlanVersion(
            id=str(uuid.uuid4()), user_id=user_id, version=max_version + 1,
            published_content_json=json.dumps(content, ensure_ascii=False, sort_keys=True),
            approval_id=approval.id, published_by=role,
            effective_at=datetime.utcnow(), change_summary=data.change_summary,
        )
        db.add(version)
        state.active_plan_id = version.id
        state.plan_epoch += 1
        state.eligibility = "wellness_only"
        response = {
            "plan_id": version.id, "version": version.version,
            "effective_at": version.effective_at.isoformat(),
            "change_summary": version.change_summary,
        }
        _audit(db, role, "plan.publish", version.id, {"version": version.version})
        _outbox(db, "meal.plan_published", version.id,
                {"user_id": user_id, "plan_id": version.id, "version": version.version})
        _store_idempotent_response(db, role, route, idempotency_key, request_hash, response, user_id)
        await db.commit()
        return response


# ---------------------------------------------------------------------------
# profile versions + change intents
# ---------------------------------------------------------------------------
@router.get("/{user_id}/profile")
async def get_profile(request: Request, user_id: int, db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    state = await _get_state(db, user_id)
    profile = await _current_profile(db, user_id, state)
    return {"user_id": user_id, "profile": profile,
            "version": state.profile_epoch if state else 0}


@router.post("/{user_id}/profile-change-intents")
async def create_profile_intent(request: Request, user_id: int, data: ProfileIntentCreate,
                                db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    state = await _get_state(db, user_id)
    base_version = 0
    if state and state.active_profile_id:
        row = (
            await db.execute(select(MealProfileVersion.version).where(
                MealProfileVersion.id == state.active_profile_id
            ))
        ).scalar_one_or_none()
        base_version = row or 0
    intent = MealProfileChangeIntent(
        id=str(uuid.uuid4()), user_id=user_id, status="pending",
        proposed_json=json.dumps(data.proposed, ensure_ascii=False),
        base_profile_version=base_version, created_by=role,
    )
    db.add(intent)
    await db.commit()
    return {"intent_id": intent.id, "base_profile_version": intent.base_profile_version,
            "proposed": data.proposed}


@router.post("/{user_id}/profile-change-intents/{intent_id}/confirm")
async def confirm_profile_intent(request: Request, user_id: int, intent_id: str,
                                 db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    async with _lock_for(user_id):
        intent = (
            await db.execute(select(MealProfileChangeIntent).where(
                MealProfileChangeIntent.id == intent_id,
                MealProfileChangeIntent.user_id == user_id,
            ))
        ).scalar_one_or_none()
        if not intent or intent.status != "pending":
            _err(404, "NOT_FOUND", "pending intent not found")
        state = await _get_state(db, user_id, create=True)
        # Whole-profile replace: an intent based on an older version must
        # not silently overwrite a newer confirmed profile.
        current_version = 0
        if state.active_profile_id:
            current_version = (
                await db.execute(select(MealProfileVersion.version).where(
                    MealProfileVersion.id == state.active_profile_id
                ))
            ).scalar_one_or_none() or 0
        if intent.base_profile_version != current_version:
            _err(409, "STALE_REVISION",
                 "profile changed since this intent was created",
                 staleFields={"profile_version": current_version})
        max_version = (
            await db.execute(
                select(func.max(MealProfileVersion.version)).where(
                    MealProfileVersion.user_id == user_id
                )
            )
        ).scalar() or 0
        proposed = _loads(intent.proposed_json, {})
        version = MealProfileVersion(
            id=str(uuid.uuid4()), user_id=user_id, version=max_version + 1,
            profile_json=json.dumps(proposed, ensure_ascii=False, sort_keys=True),
            confirmed_by=role, confirmation_ref=intent.id,
        )
        db.add(version)
        state.active_profile_id = version.id
        state.profile_epoch += 1
        intent.status = "confirmed"
        intent.confirmed_at = datetime.utcnow()
        await db.commit()
        return {"profile_version": version.version, "profile": proposed}


# ---------------------------------------------------------------------------
# health update intents (pending clarification blocks recommendations)
# ---------------------------------------------------------------------------
@router.post("/{user_id}/health-update-intents")
async def create_health_intent(request: Request, user_id: int, data: HealthIntentCreate,
                               db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    async with _lock_for(user_id):
        state = await _get_state(db, user_id, create=True)
        intent = MealHealthUpdateIntent(
            id=str(uuid.uuid4()), user_id=user_id, status="pending",
            reported_text=data.reported_text, source_context=data.source_context,
            created_by=role,
        )
        db.add(intent)
        state.safety_epoch += 1
        await db.commit()
        return {"intent_id": intent.id, "status": "pending"}


@router.get("/{user_id}/health-update-intents")
async def list_health_intents(request: Request, user_id: int,
                              db: AsyncSession = Depends(get_nutrition_db)):
    _require_coach(request)
    rows = (
        await db.execute(
            select(MealHealthUpdateIntent)
            .where(MealHealthUpdateIntent.user_id == user_id)
            .order_by(MealHealthUpdateIntent.created_at.desc())
        )
    ).scalars().all()
    return {"intents": [
        {
            "intent_id": i.id, "status": i.status,
            "reported_text": i.reported_text,
            "created_at": i.created_at.isoformat() if i.created_at else None,
            "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
            "resolution": _loads(i.resolution_json, {}),
        }
        for i in rows
    ]}


@router.post("/{user_id}/health-update-intents/{intent_id}/resolve")
async def resolve_health_intent(request: Request, user_id: int, intent_id: str,
                                data: HealthIntentResolve,
                                db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)  # a child can report; only the coach clears
    if data.action not in ("confirm", "cancel"):
        _err(400, "BAD_REQUEST", "action must be confirm or cancel")
    async with _lock_for(user_id):
        intent = (
            await db.execute(select(MealHealthUpdateIntent).where(
                MealHealthUpdateIntent.id == intent_id,
                MealHealthUpdateIntent.user_id == user_id,
            ))
        ).scalar_one_or_none()
        if not intent or intent.status != "pending":
            _err(404, "NOT_FOUND", "pending intent not found")
        state = await _get_state(db, user_id, create=True)
        intent.status = "confirmed" if data.action == "confirm" else "cancelled"
        intent.resolution_json = json.dumps(data.resolution or {}, ensure_ascii=False)
        intent.resolved_at = datetime.utcnow()
        state.safety_epoch += 1
        await db.commit()
        return {"intent_id": intent.id, "status": intent.status}


# ---------------------------------------------------------------------------
# clinical rule bundles - approval requires a verified capability (fail-closed)
# ---------------------------------------------------------------------------
@router.post("/{user_id}/clinical-rule-bundles")
async def create_rule_bundle(request: Request, user_id: int, data: RuleBundleCreate,
                             db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)
    async with _lock_for(user_id):
        max_version = (
            await db.execute(
                select(func.max(MealClinicalRuleBundle.version)).where(
                    MealClinicalRuleBundle.user_id == user_id
                )
            )
        ).scalar() or 0
        # Allowed rule kinds for professional instructions: nutrient_bounds
        # (scope meal|day, catalog nutrient, min and/or max) and exclude_food.
        # 'min'/'max' accepted as aliases for the internal lower/upper DSL.
        from nutrition_core.rules import validate_rule

        rules = []
        for i, rule in enumerate(data.rules or []):
            rule = dict(rule)
            if rule.get("min") is not None and rule.get("lower") is None:
                rule["lower"] = rule.pop("min")
            if rule.get("max") is not None and rule.get("upper") is None:
                rule["upper"] = rule.pop("max")
            rule.pop("min", None)
            rule.pop("max", None)
            if rule.get("kind") not in ("nutrient_bounds", "exclude_food"):
                _err(422, "INVALID_BUNDLE",
                     f"rules[{i}]: only nutrient_bounds and exclude_food are allowed")
            err = validate_rule(rule)
            if err:
                _err(422, "INVALID_BUNDLE", f"rules[{i}]: {err}")
            rules.append(rule)
        if not rules:
            _err(422, "INVALID_BUNDLE", "a medical instruction needs at least one rule")
        bundle = MealClinicalRuleBundle(
            id=str(uuid.uuid4()), user_id=user_id, version=max_version + 1,
            status="draft", rules_json=json.dumps(rules, ensure_ascii=False),
            supported_pathway=data.supported_pathway or "wellness",
            condition_label=data.condition_label,
            professional_id=data.professional_id,
            instruction_date=_naive_utc(data.instruction_date),
            valid_until=_naive_utc(data.valid_until),
        )
        db.add(bundle)
        await db.commit()
        return {"bundle_id": bundle.id, "version": bundle.version, "status": "draft"}


@router.get("/{user_id}/clinical-rule-bundles")
async def list_rule_bundles(request: Request, user_id: int,
                            db: AsyncSession = Depends(get_nutrition_db)):
    _require_coach(request)
    state = await _get_state(db, user_id)
    rows = (
        await db.execute(
            select(MealClinicalRuleBundle)
            .where(MealClinicalRuleBundle.user_id == user_id)
            .order_by(MealClinicalRuleBundle.version.desc())
        )
    ).scalars().all()
    profs = {
        c.id: c for c in (
            await db.execute(select(MealProfessionalCapability))
        ).scalars().all()
    }
    now = datetime.utcnow()
    return {
        "active_bundle_id": state.active_rule_bundle_id if state else None,
        "bundles": [{
            "bundle_id": b.id, "version": b.version, "status": b.status,
            "rules": _loads(b.rules_json, []),
            "condition_label": b.condition_label,
            "professional_id": b.professional_id,
            "professional_name": (
                profs[b.professional_id].name
                if b.professional_id and b.professional_id in profs else None
            ),
            "instruction_date": b.instruction_date.isoformat() if b.instruction_date else None,
            "valid_until": b.valid_until.isoformat() if b.valid_until else None,
            "expired": bool(b.valid_until and _naive_utc(b.valid_until) < now),
            "is_active": bool(state and state.active_rule_bundle_id == b.id),
        } for b in rows],
    }


@router.post("/{user_id}/clinical-rule-bundles/{bundle_id}/approve")
async def approve_rule_bundle(request: Request, user_id: int, bundle_id: str,
                              data: RuleBundleApprove,
                              db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)
    async with _lock_for(user_id):
        bundle = (
            await db.execute(select(MealClinicalRuleBundle).where(
                MealClinicalRuleBundle.id == bundle_id,
                MealClinicalRuleBundle.user_id == user_id,
            ))
        ).scalar_one_or_none()
        if not bundle or bundle.status != "draft":
            _err(404, "NOT_FOUND", "draft bundle not found")
        # Named, verified capability required - never self-assigned, and the
        # professional the instruction was written by must approve it.
        if data.professional_id != bundle.professional_id:
            _err(403, "CLINICAL_CAPABILITY_REQUIRED",
                 "the approving professional must be the instruction's author")
        capability = (
            await db.execute(select(MealProfessionalCapability).where(
                MealProfessionalCapability.id == data.professional_id,
                MealProfessionalCapability.principal == role,
                MealProfessionalCapability.capability == "clinical_rule_approver",
            ))
        ).scalar_one_or_none()
        now = datetime.utcnow()
        if (
            not capability
            or capability.revoked_at
            or _naive_utc(capability.valid_from) > now
            or (capability.expires_at and _naive_utc(capability.expires_at) <= now)
        ):
            _err(403, "CLINICAL_CAPABILITY_REQUIRED",
                 "rule approval requires a verified clinical capability")
        valid_cap = capability
        # Contradictory bundles cannot be approved (DESIGN 7.2).
        from nutrition_core.rules import detect_rule_conflicts, validate_rule

        rules = _loads(bundle.rules_json, [])
        malformed = [validate_rule(r) for r in rules]
        malformed = [m for m in malformed if m]
        conflicts = detect_rule_conflicts(rules)
        if malformed or conflicts:
            _err(422, "INVALID_BUNDLE", "bundle contains malformed or conflicting rules",
                 fieldErrors=malformed + conflicts)
        state = await _get_state(db, user_id, create=True)
        # Approving a new bundle supersedes the previous active one; history
        # is kept (status 'superseded'), never rewritten.
        if state.active_rule_bundle_id and state.active_rule_bundle_id != bundle.id:
            prev = (
                await db.execute(select(MealClinicalRuleBundle).where(
                    MealClinicalRuleBundle.id == state.active_rule_bundle_id,
                ))
            ).scalar_one_or_none()
            if prev and prev.status == "approved":
                prev.status = "superseded"
        bundle.status = "approved"
        bundle.approved_by = role
        bundle.approved_at = now
        bundle.approver_capability_ref = valid_cap.id
        state.active_rule_bundle_id = bundle.id
        state.safety_epoch += 1
        _audit(db, role, "rule_bundle.approve", bundle.id, {"version": bundle.version})
        await db.commit()
        return {"bundle_id": bundle.id, "status": "approved"}


# ---------------------------------------------------------------------------
# professionals (ADMIN-only registration by attestation - not registry
# verification: a known limitation of this POC)
# ---------------------------------------------------------------------------
def _require_admin(request: Request) -> str:
    role = get_current_role(request)
    if role != Role.ADMIN:
        _err(403, "FORBIDDEN", "requires admin")
    return role


def _professional_json(c: MealProfessionalCapability) -> dict:
    return {
        "professional_id": c.id,
        "name": c.name,
        "profession": c.profession,
        "license_number": c.evidence_ref,
        "valid_until": c.expires_at.isoformat() if c.expires_at else None,
        "revoked": bool(c.revoked_at),
    }


@router.post("/professionals")
async def create_professional(request: Request, data: ProfessionalCreate,
                              db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_admin(request)
    if data.profession not in ("dietitian", "physician"):
        _err(400, "BAD_REQUEST", "profession must be dietitian or physician")
    valid_until = _naive_utc(data.valid_until)
    if valid_until <= datetime.utcnow():
        _err(400, "BAD_REQUEST", "valid_until must be in the future")
    cap = MealProfessionalCapability(
        id=str(uuid.uuid4()), principal="admin", name=data.name,
        profession=data.profession, capability="clinical_rule_approver",
        jurisdiction="IL", verified_by="admin_attestation",
        evidence_ref=data.license_number,
        valid_from=datetime.utcnow(), expires_at=valid_until,
    )
    db.add(cap)
    _audit(db, role, "professional.register", cap.id,
           {"name": data.name, "profession": data.profession})
    await db.commit()
    return _professional_json(cap)


@router.get("/professionals")
async def list_professionals(request: Request,
                             db: AsyncSession = Depends(get_nutrition_db)):
    _require_coach(request)
    rows = (
        await db.execute(
            select(MealProfessionalCapability).where(
                MealProfessionalCapability.capability == "clinical_rule_approver",
            ).order_by(MealProfessionalCapability.valid_from.desc())
        )
    ).scalars().all()
    return {"professionals": [_professional_json(c) for c in rows],
            "attestation_note": "registration is attestation-based, not verified against an official registry"}


@router.post("/professionals/{professional_id}/revoke")
async def revoke_professional(request: Request, professional_id: str,
                              db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_admin(request)
    cap = (
        await db.execute(select(MealProfessionalCapability).where(
            MealProfessionalCapability.id == professional_id,
        ))
    ).scalar_one_or_none()
    if not cap:
        _err(404, "NOT_FOUND", "professional not found")
    if not cap.revoked_at:
        cap.revoked_at = datetime.utcnow()
        _audit(db, role, "professional.revoke", cap.id, {})
        await db.commit()
    return _professional_json(cap)


# ---------------------------------------------------------------------------
# fit-portions - read-only portion fitting inside professional bounds
# ---------------------------------------------------------------------------
@router.post("/{user_id}/fit-portions")
async def fit_portions(request: Request, user_id: int, data: FitPortionsRequest,
                       db: AsyncSession = Depends(get_nutrition_db)):
    _require_coach(request)
    src = await _catalog_source(db)
    state = await _get_state(db, user_id)
    profile = await _current_profile(db, user_id, state)
    bundle = await _active_bundle(db, state)
    med_block = _medical_block_reason(state, profile, bundle)
    if med_block:
        return {"state": "blocked", "reasons": [med_block], "items": [], "checks": []}
    rules = _profile_rules(profile) + (
        _loads(bundle.rules_json, []) if _bundle_valid(bundle) else []
    )
    # Day-bound remainder after the coach's currently drafted other meals.
    day_bounds = []
    day_rules = [
        r for r in rules
        if r.get("kind") == "nutrient_bounds" and r.get("scope") == "day"
    ]
    if day_rules:
        allocated = {f: Decimal(0) for f in engine.NUTRIENT_FIELDS}
        for other in data.other_meals or []:
            if other.get("slot_key") == data.slot_key:
                continue
            totals = engine.meal_totals(other.get("items") or [], src)
            for f in engine.NUTRIENT_FIELDS:
                v = totals.get(f)
                if allocated[f] is None or v is None:
                    allocated[f] = None
                else:
                    allocated[f] += v
        day_bounds = engine.day_bound_residuals(
            day_rules, {f: "0" for f in engine.NUTRIENT_FIELDS},
            {k: (None if v is None else str(v)) for k, v in allocated.items()},
        )
        if any(b["residual"] is None for b in day_bounds):
            return {"state": "blocked", "items": [], "checks": day_bounds,
                    "reasons": ["medical instruction day bound cannot be verified (unknown data)"]}
    return engine.fit_portions(
        data.recipe_version_id, data.slot_key, src, rules, day_residuals=day_bounds
    )


# ---------------------------------------------------------------------------
# auto-plan - deterministic first-draft day builder (draft helper only)
# ---------------------------------------------------------------------------
async def _auto_llm_picks(slots: List[dict], day_targets: dict,
                          usable: List[dict]) -> Optional[dict]:
    """Ask the LLM which foods to put in each slot (JSON only). Returns
    {"slot_key": [{food_version_id, role}]} or None on any failure —
    the engine validates and sizes whatever survives."""
    candidates = []
    for f in usable:
        if any(w in (f.get("name_he") or "") for w in engine._AUTO_NO_COMPONENT):
            continue
        candidates.append({
            "id": f["food_version_id"], "name": f.get("name_he"),
            "role": engine._auto_infer_role(f),
            "k": f.get("kosher_evidence"),
        })
    if len(candidates) < len(slots):
        return None
    system_prompt = (
        "You compose a daily meal plan for an Israeli child. Reply with a "
        "single JSON object, no prose: {\"meals\": [{\"slot_key\": \"...\", "
        "\"items\": [{\"id\": \"<candidate id>\", \"role\": "
        "\"protein|carb|veg|fat|fruit\"}]}]}. Rules: pick candidates ONLY "
        "from the provided list, copy ids exactly; a slot must have at "
        "least one protein; main slots should also get a carb, a veg, and "
        "optionally a fat; snack slots get a protein plus a fruit; choose "
        "candidates whose k value is compatible with the slot's kosher "
        "class (meat slot -> meat/parve, dairy -> dairy/parve, parve -> "
        "parve only, any -> everything); vary the foods across slots; "
        "prefer realistic kid-friendly meals."
    )
    user_msg = json.dumps({
        "slots": [{"slot_key": s["slot_key"], "kosher": s.get("kosher") or "any"}
                  for s in slots],
        "day_targets": day_targets,
        "candidates": candidates,
    }, ensure_ascii=False)
    try:
        from tutors import call_llm  # late import like lookup._llm_lookup
        raw = await asyncio.wait_for(
            call_llm(system_prompt,
                     [{"role": "user", "content": user_msg}],
                     max_tokens=2500),
            timeout=25)
    except Exception:
        return None
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0) if m else raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("meals"), list):
        return None
    picks = {}
    for meal in data["meals"]:
        if not isinstance(meal, dict):
            continue
        sk, its = meal.get("slot_key"), meal.get("items")
        if not isinstance(its, list):
            continue
        picks[sk] = [{"food_version_id": it.get("id"), "role": it.get("role")}
                     for it in its if isinstance(it, dict) and it.get("id")]
    return picks or None


@router.post("/{user_id}/auto-plan")
async def auto_plan(request: Request, user_id: int, data: AutoPlanRequest,
                    db: AsyncSession = Depends(get_nutrition_db)):
    _require_coach(request)
    if not data.slots:
        _err(400, "BAD_REQUEST", "slots must be a non-empty list")
    for s in data.slots:
        if s.get("slot_key") not in engine.SLOT_KEYS:
            _err(400, "BAD_REQUEST", "invalid slot_key")
        if (s.get("kosher") or "any") not in ("meat", "dairy", "parve", "any"):
            _err(400, "BAD_REQUEST", "invalid kosher class")
    src = await _catalog_source(db)
    state = await _get_state(db, user_id)
    profile = await _current_profile(db, user_id, state)
    excluded = set(profile.get("excluded_food_ids") or [])
    # Declared allergens become hard exclusions for the draft builder too
    # (fail-closed already handled downstream at validate time).
    allergies = set(profile.get("allergies") or [])
    if allergies:
        for food in src.approved_foods():
            if allergies & set(food.get("allergens") or []):
                excluded.add(food["food_version_id"])
    if data.use_llm:
        usable = [f for f in src.approved_foods()
                  if f["status"] == "approved"
                  and f["food_version_id"] not in excluded
                  and f.get("kosher_evidence") != "non_kosher"]
        try:
            picks = await _auto_llm_picks(
                data.slots, data.day_targets or {}, usable)
        except Exception:
            picks = None
        if picks:
            slots = [dict(s, items=picks.get(s["slot_key"], []))
                     for s in data.slots]
            out = engine.auto_compose_from_choices(
                slots, data.day_targets or {}, src, excluded_food_ids=excluded)
            if out["meals"]:
                out["source"] = "llm"
                return out
    out = engine.auto_compose_day(
        data.slots, data.day_targets or {}, src, excluded_food_ids=excluded)
    out["source"] = "deterministic"
    return out


# ---------------------------------------------------------------------------
# suggestion sessions (ephemeral context - never an inventory record)
# ---------------------------------------------------------------------------
@router.post("/{user_id}/suggestion-sessions")
async def create_session(request: Request, user_id: int, data: SessionCreate,
                         db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    if data.slot_key not in engine.SLOT_KEYS:
        _err(400, "BAD_REQUEST", "invalid slot_key")
    state = await _get_state(db, user_id, create=True)
    nutrition_date = data.nutrition_date or _today(state.timezone)
    session = MealSuggestionSession(
        id=str(uuid.uuid4()), user_id=user_id, slot_key=data.slot_key,
        nutrition_date=nutrition_date, revision=1,
        missing_food_ids_json="[]", rejected_recipe_ids_json="[]",
        preparation_json=json.dumps(data.preparation or {}, ensure_ascii=False),
        status="open", expires_at=datetime.utcnow() + timedelta(seconds=_SESSION_TTL_SECONDS),
    )
    db.add(session)
    await db.commit()
    return {"session_id": session.id, "revision": session.revision,
            "nutrition_date": nutrition_date}


async def _load_open_session(db, user_id, session_id) -> MealSuggestionSession:
    session = (
        await db.execute(select(MealSuggestionSession).where(
            MealSuggestionSession.id == session_id,
            MealSuggestionSession.user_id == user_id,
        ))
    ).scalar_one_or_none()
    if not session or session.status != "open":
        _err(404, "NOT_FOUND", "open session not found")
    if session.expires_at and session.expires_at < datetime.utcnow():
        session.status = "expired"
        await db.commit()
        _err(409, "STALE_INPUT", "session expired")
    return session


@router.patch("/{user_id}/suggestion-sessions/{session_id}")
async def patch_session(request: Request, user_id: int, session_id: str, data: SessionPatch,
                        if_match: Optional[int] = Header(None, alias="If-Match"),
                        db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    if if_match is None:
        _err(428, "PRECONDITION_REQUIRED", "missing If-Match header")
    async with _lock_for(user_id):
        session = await _load_open_session(db, user_id, session_id)
        if session.revision != if_match:
            _err(409, "STALE_REVISION", "session revision mismatch",
                 staleFields={"revision": session.revision})
        src = await _catalog_source(db)
        if data.missing_food_ids is not None:
            unknown = [f for f in data.missing_food_ids if not src.get_food_version(f)]
            if unknown:
                _err(400, "BAD_REQUEST", f"unknown food ids: {unknown}")
            session.missing_food_ids_json = json.dumps(data.missing_food_ids)
        if data.rejected_recipe_ids is not None:
            unknown = [r for r in data.rejected_recipe_ids if not src.get_recipe_version(r)]
            if unknown:
                _err(400, "BAD_REQUEST", f"unknown recipe ids: {unknown}")
            session.rejected_recipe_ids_json = json.dumps(data.rejected_recipe_ids)
        if data.preparation is not None:
            session.preparation_json = json.dumps(data.preparation, ensure_ascii=False)
        session.revision += 1
        await db.commit()
        return {"session_id": session.id, "revision": session.revision}


# ---------------------------------------------------------------------------
# proposal jobs (bounded deterministic generation)
# ---------------------------------------------------------------------------
async def run_proposal_job(
    db: AsyncSession,
    user_id: int,
    role: str,
    *,
    session_id: Optional[str] = None,
    slot_key: Optional[str] = None,
    nutrition_date: Optional[str] = None,
    expected_vector: Optional[dict] = None,
    saved_meal_id: Optional[str] = None,
    preferred_recipe_ids: Optional[List[str]] = None,
    requested_food_ids: Optional[List[str]] = None,
    extra_basket_food_ids: Optional[List[str]] = None,
    prefer_composition: bool = False,
) -> dict:
    """One bounded deterministic generation, shared by the proposal-jobs
    endpoint and the assistant endpoint (DESIGN 6.5). The caller must hold
    _lock_for(user_id); this helper commits its own transaction."""
    src = await _catalog_source(db)
    state = await _get_state(db, user_id)
    vector = _vector(state, src) if state else None
    if not state or not state.active_plan_id:
        _err(409, "NO_ACTIVE_PLAN", "no published plan to propose against")
    if expected_vector is not None and expected_vector != vector:
        _err(409, "STALE_INPUT", "client vector is stale", staleFields={"current": vector})

    session = None
    if session_id:
        session = await _load_open_session(db, user_id, session_id)
        slot_key = slot_key or session.slot_key
        nutrition_date = nutrition_date or session.nutrition_date
    if not slot_key or slot_key not in engine.SLOT_KEYS:
        _err(400, "BAD_REQUEST", "valid slot_key required (directly or via session)")
    nutrition_date = nutrition_date or _today(state.timezone)

    plan = (
        await db.execute(select(MealPlanVersion).where(
            MealPlanVersion.id == state.active_plan_id
        ))
    ).scalar_one_or_none()
    plan_content = _loads(plan.published_content_json, {}) if plan else {}

    profile = await _current_profile(db, user_id, state)
    bundle = await _active_bundle(db, state)
    bundle_rules = _loads(bundle.rules_json, []) if _bundle_valid(bundle) else []
    profile["rules"] = _profile_rules(profile) + bundle_rules

    blocked_by = await _pending_health_block(db, user_id)
    projection = await _day_projection(db, user_id, nutrition_date, src)
    med_block = _medical_block_reason(state, profile, bundle)

    if blocked_by:
        result = {"state": "blocked",
                  "reasons": ["pending health clarification blocks recommendations"],
                  "blocking_intent": blocked_by}
    elif med_block:
        result = {"state": "blocked", "reasons": [med_block]}
    elif bundle_rules and (
        engine.plan_rule_issues(
            engine.validate_plan_content(plan_content, src)["meals"], bundle_rules
        )
    ):
        # The ACTIVE published plan violates the currently active bundle
        # (e.g. the bundle was approved after publication). Fail closed on
        # recommendations; viewing/logging stay open until republish.
        result = {"state": "blocked",
                  "reasons": ["active plan violates medical instruction"]}
    elif saved_meal_id:
        saved = (
            await db.execute(select(MealSavedMeal).where(
                MealSavedMeal.id == saved_meal_id,
                MealSavedMeal.user_id == user_id,
            ))
        ).scalar_one_or_none()
        if not saved:
            _err(404, "NOT_FOUND", "saved meal not found")
        snapshot = _loads(saved.snapshot_json, {})
        recipe = src.get_recipe_version(snapshot.get("recipe_version_id") or "")
        rules = profile["rules"]
        check = engine.validate_selection_items(
            snapshot.get("items") or [], slot_key, rules, src, recipe
        )
        if check["valid"]:
            result = {"state": "eligible_candidate", "reasons": [], "proposal": {
                "slot_key": slot_key,
                "recipe_version_id": snapshot.get("recipe_version_id"),
                "name_he": saved.name,
                "items": [
                    {"food_version_id": i["food_version_id"],
                     "name_he": (i.get("food") or {}).get("name_he"),
                     "quantity_g": i["quantity_g"]}
                    for i in check["items"]
                ],
                "totals": check["totals"],
                "checks": [],
                "compliance_label": "saved_meal_revalidated",
                "saved_meal_id": saved.id,
            }}
        else:
            result = {"state": "no_match",
                      "reasons": check["issues"] or ["saved meal fails current rules"]}
    else:
        session_ctx = {
            "missing_food_ids": _loads(session.missing_food_ids_json, []) if session else [],
            "rejected_recipe_ids": _loads(session.rejected_recipe_ids_json, []) if session else [],
            "preparation": _loads(session.preparation_json, {}) if session else {},
        }
        # Request-servable extra foods (e.g. an auto-looked-up food the
        # trainee named). An UNVERIFIED food may compose only with no
        # active medical bundle AND a known allergen set (empty is fine -
        # a declared allergy still fails allergen_free on known data).
        # Otherwise it is excluded, fail-closed, until the coach verifies.
        extra_food_ids = []
        unverified_reasons = []
        for fid in extra_basket_food_ids or []:
            food = src.get_food_version(fid)
            if not food or food["status"] != "approved":
                continue
            if food.get("verified", 1):
                extra_food_ids.append(fid)
                continue
            if state.active_rule_bundle_id or food.get("allergens") is None:
                unverified_reasons.append("unverified food data")
            else:
                extra_food_ids.append(fid)
        # Day-scope medical bounds: residual = bound - consumed - other
        # slots' allocations (a slot's selection if present, else its
        # planned meal). Any unknown -> blocked (fail-closed).
        day_bounds = []
        day_rules = [
            r for r in profile["rules"]
            if r.get("kind") == "nutrient_bounds" and r.get("scope") == "day"
        ]
        if day_rules:
            selections = (
                await db.execute(select(MealSelection).where(
                    MealSelection.user_id == user_id,
                    MealSelection.nutrition_date == nutrition_date,
                ))
            ).scalars().all()
            sel_by_slot = {s.slot_key: s for s in selections}
            allocated = {f: Decimal(0) for f in engine.NUTRIENT_FIELDS}
            for m in engine.validate_plan_content(plan_content, src)["meals"]:
                if m["slot_key"] == slot_key:
                    continue
                sel = sel_by_slot.get(m["slot_key"])
                if sel is not None:
                    snap = _loads(sel.snapshot_json, {})
                    tmap = snap.get("totals")
                    if not tmap:
                        tmap = engine.serialize_totals(engine.meal_totals(
                            [{"food_version_id": i["food_version_id"],
                              "quantity_g": i["quantity_g"]}
                             for i in snap.get("items") or []], src))
                else:
                    tmap = m["totals"]
                for f in engine.NUTRIENT_FIELDS:
                    v = tmap.get(f)
                    if allocated[f] is None or v is None:
                        allocated[f] = None
                    else:
                        allocated[f] += Decimal(str(v))
            day_bounds = engine.day_bound_residuals(
                day_rules, projection["totals"],
                {k: (None if v is None else str(v)) for k, v in allocated.items()},
            )
            if any(b["residual"] is None for b in day_bounds):
                result = {"state": "blocked", "reasons": [
                    "medical instruction day bound cannot be verified (unknown data)"]}
            else:
                result = engine.generate_proposal(
                    slot_key, src, plan_content, profile, session_ctx,
                    projection["totals"], projection["completeness"],
                    preferred_recipe_ids=preferred_recipe_ids,
                    day_bounds=day_bounds,
                    requested_food_ids=requested_food_ids,
                    extra_basket_food_ids=extra_food_ids,
                    prefer_composition=prefer_composition,
                )
        else:
            result = engine.generate_proposal(
                slot_key, src, plan_content, profile, session_ctx,
                projection["totals"], projection["completeness"],
                preferred_recipe_ids=preferred_recipe_ids,
                requested_food_ids=requested_food_ids,
                extra_basket_food_ids=extra_food_ids,
                prefer_composition=prefer_composition,
            )
        # Foods excluded by the unverified gate surface as honest reasons,
        # whatever the outcome was.
        for reason in unverified_reasons:
            if reason not in result.setdefault("reasons", []):
                result["reasons"].append(reason)

    job = MealProposalJob(
        id=str(uuid.uuid4()), user_id=user_id,
        session_id=session.id if session else None,
        slot_key=slot_key, nutrition_date=nutrition_date,
        input_vector_json=json.dumps(vector, sort_keys=True),
        state=result["state"], reasons_json=json.dumps(result.get("reasons", [])),
        completed_at=datetime.utcnow(),
    )
    proposal = None
    if result["state"] == "eligible_candidate":
        proposal = MealProposal(
            id=str(uuid.uuid4()), user_id=user_id,
            session_id=session.id if session else None,
            slot_key=slot_key, nutrition_date=nutrition_date,
            payload_json=json.dumps(result["proposal"], ensure_ascii=False),
            input_vector_json=json.dumps(vector, sort_keys=True),
            valid_until=datetime.utcnow() + timedelta(seconds=_PROPOSAL_TTL_SECONDS),
            engine_version=ENGINE_VERSION,
        )
        db.add(proposal)
        await db.flush()
        job.proposal_id = proposal.id
    db.add(job)
    await db.commit()
    response = {
        "job_id": job.id, "state": job.state,
        "proposal_id": job.proposal_id,
        "reasons": _loads(job.reasons_json, []),
        "blocking_intent": result.get("blocking_intent"),
    }
    if proposal is not None:
        response["proposal"] = {
            "proposal_id": proposal.id,
            **_loads(proposal.payload_json, {}),
            "valid_until": proposal.valid_until.isoformat(),
        }
    return response


@router.post("/{user_id}/proposal-jobs")
async def create_proposal_job(request: Request, user_id: int, data: ProposalJobCreate,
                              db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    async with _lock_for(user_id):
        return await run_proposal_job(
            db, user_id, role,
            session_id=data.session_id, slot_key=data.slot_key,
            nutrition_date=data.nutrition_date,
            expected_vector=data.expected_vector,
            saved_meal_id=data.saved_meal_id,
        )


@router.get("/{user_id}/proposal-jobs/{job_id}")
async def get_proposal_job(request: Request, user_id: int, job_id: str,
                           db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    job = (
        await db.execute(select(MealProposalJob).where(
            MealProposalJob.id == job_id, MealProposalJob.user_id == user_id
        ))
    ).scalar_one_or_none()
    if not job:
        _err(404, "NOT_FOUND", "job not found")
    out = {"job_id": job.id, "state": job.state, "reasons": _loads(job.reasons_json, []),
           "slot_key": job.slot_key, "nutrition_date": job.nutrition_date}
    if job.proposal_id:
        proposal = (
            await db.execute(select(MealProposal).where(
                MealProposal.id == job.proposal_id
            ))
        ).scalar_one_or_none()
        if proposal:
            out["proposal"] = {"proposal_id": proposal.id, **_loads(proposal.payload_json, {}),
                               "valid_until": proposal.valid_until.isoformat()}
    return out


@router.get("/{user_id}/proposals/{proposal_id}")
async def get_proposal(request: Request, user_id: int, proposal_id: str,
                       db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    proposal = (
        await db.execute(select(MealProposal).where(
            MealProposal.id == proposal_id, MealProposal.user_id == user_id
        ))
    ).scalar_one_or_none()
    if not proposal:
        _err(404, "NOT_FOUND", "proposal not found")
    stale = proposal.valid_until < datetime.utcnow()
    return {"proposal_id": proposal.id, **_loads(proposal.payload_json, {}),
            "valid_until": proposal.valid_until.isoformat(), "expired": stale,
            "input_vector": _loads(proposal.input_vector_json, {})}


# ---------------------------------------------------------------------------
# selections - planning only, never eating
# ---------------------------------------------------------------------------
@router.post("/{user_id}/selections")
async def create_selection(request: Request, user_id: int, data: SelectionCreate,
                           db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    if data.slot_key not in engine.SLOT_KEYS:
        _err(400, "BAD_REQUEST", "invalid slot_key")
    async with _lock_for(user_id):
        src = await _catalog_source(db)
        state = await _get_state(db, user_id)
        if not state or not state.active_plan_id:
            _err(409, "NO_ACTIVE_PLAN", "no published plan")
        vector = _vector(state, src)
        if data.expected_vector is not None and data.expected_vector != vector:
            _err(409, "STALE_INPUT", "client vector is stale", staleFields={"current": vector})

        session = None
        if data.session_id:
            session = await _load_open_session(db, user_id, data.session_id)
            if session.slot_key != data.slot_key or session.nutrition_date != data.nutrition_date:
                _err(409, "STALE_INPUT", "session does not match selection slot/date")

        profile = await _current_profile(db, user_id, state)
        bundle = await _active_bundle(db, state)
        med_block = _medical_block_reason(state, profile, bundle)
        if med_block:
            _err(409, "MEDICAL_INSTRUCTION_BLOCKED", med_block)
        rules = _profile_rules(profile) + (
            _loads(bundle.rules_json, []) if _bundle_valid(bundle) else []
        )
        plan = (
            await db.execute(select(MealPlanVersion).where(
                MealPlanVersion.id == state.active_plan_id
            ))
        ).scalar_one_or_none()
        plan_content = _loads(plan.published_content_json, {}) if plan else {}
        resolved_plan = engine.validate_plan_content(plan_content, src)

        snapshot = None
        proposal_id = None
        alternative_recipe_id = None

        if data.proposal_id:
            proposal = (
                await db.execute(select(MealProposal).where(
                    MealProposal.id == data.proposal_id,
                    MealProposal.user_id == user_id,
                ))
            ).scalar_one_or_none()
            if not proposal:
                _err(404, "NOT_FOUND", "proposal not found")
            # Wall-clock expiry AND vector match, re-checked now (DESIGN 6.5.4).
            stale_fields = {}
            if proposal.valid_until < datetime.utcnow():
                stale_fields["proposal"] = "expired"
            if _loads(proposal.input_vector_json, {}) != vector:
                stale_fields["vector"] = vector
            if proposal.slot_key != data.slot_key or proposal.nutrition_date != data.nutrition_date:
                stale_fields["binding"] = "slot/date mismatch"
            if stale_fields:
                _err(409, "STALE_INPUT", "proposal is stale; request a fresh one",
                     staleFields=stale_fields)
            payload = _loads(proposal.payload_json, {})
            check = engine.validate_selection_items(
                payload.get("items") or [], data.slot_key, rules, src,
                src.get_recipe_version(payload.get("recipe_version_id") or ""),
            )
            if not check["valid"]:
                _err(409, "STALE_INPUT", "proposal fails current validation",
                     staleFields={"issues": check["issues"], "failed": check["failed_checks"]})
            snapshot = payload
            proposal_id = proposal.id
        else:
            # Explicit approved-alternative path (B03): the recipe must be an
            # approved alternative for this slot in the ACTIVE plan.
            rid = data.alternative_recipe_id
            approved_rids = (resolved_plan.get("alternatives") or {}).get(data.slot_key, [])
            planned = {m["slot_key"]: m for m in resolved_plan["meals"]}
            planned_rid = (planned.get(data.slot_key) or {}).get("recipe_version_id")
            if rid and rid != planned_rid and rid not in approved_rids:
                _err(422, "NOT_APPROVED_ALTERNATIVE",
                     "recipe is not an approved alternative for this slot")
            if not rid and planned_rid:
                rid = planned_rid
            recipe = src.get_recipe_version(rid or "")
            raw_items = data.items
            if raw_items is None and recipe:
                raw_items = [
                    {"food_version_id": i["food_version_id"], "quantity_g": i["default_g"]}
                    for i in recipe["items"]
                ]
            if not raw_items:
                _err(400, "BAD_REQUEST", "items or a valid alternative recipe required")
            missing = set(_loads(session.missing_food_ids_json, [])) if session else set()
            if missing & {i.get("food_version_id") for i in raw_items}:
                _err(422, "MISSING_INGREDIENT", "selection uses a missing ingredient")
            check = engine.validate_selection_items(raw_items, data.slot_key, rules, src, recipe)
            if not check["valid"]:
                _err(422, "INVALID_SELECTION", "selection fails current validation",
                     fieldErrors=check["issues"], failed=check["failed_checks"])
            snapshot = {
                "slot_key": data.slot_key,
                "recipe_version_id": rid,
                "name_he": recipe["name_he"] if recipe else None,
                "items": [
                    {"food_version_id": i["food_version_id"],
                     "name_he": (i.get("food") or {}).get("name_he"),
                     "quantity_g": i["quantity_g"]}
                    for i in check["items"]
                ],
                "totals": check["totals"],
                "compliance_label": "approved_alternative",
            }
            alternative_recipe_id = rid

        prior = (
            await db.execute(select(MealSelection).where(
                MealSelection.user_id == user_id,
                MealSelection.nutrition_date == data.nutrition_date,
                MealSelection.slot_key == data.slot_key,
                MealSelection.status == "current",
            ))
        ).scalar_one_or_none()
        if prior:
            prior.status = "superseded"
        selection = MealSelection(
            id=str(uuid.uuid4()), user_id=user_id,
            nutrition_date=data.nutrition_date, slot_key=data.slot_key,
            proposal_id=proposal_id, alternative_recipe_id=alternative_recipe_id,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            vector_json=json.dumps(vector, sort_keys=True), status="current",
        )
        db.add(selection)
        _audit(db, role, "selection.select", selection.id,
               {"slot": data.slot_key, "date": data.nutrition_date})
        await db.commit()
        return {
            "selection_id": selection.id, "status": "selected_not_eaten",
            "nutrition_date": data.nutrition_date, "slot_key": data.slot_key,
            "snapshot": snapshot, "compliance_label": snapshot.get("compliance_label"),
            "eating_event_id": None,
        }


@router.get("/{user_id}/selections")
async def list_selections(request: Request, user_id: int, nutrition_date: Optional[str] = None,
                          db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    query = select(MealSelection).where(
        MealSelection.user_id == user_id, MealSelection.status == "current"
    )
    if nutrition_date:
        query = query.where(MealSelection.nutrition_date == nutrition_date)
    rows = (await db.execute(query)).scalars().all()
    return {"selections": [
        {"selection_id": s.id, "nutrition_date": s.nutrition_date, "slot_key": s.slot_key,
         "snapshot": _loads(s.snapshot_json, {}), "status": "selected_not_eaten"}
        for s in rows
    ]}


# ---------------------------------------------------------------------------
# saved meals
# ---------------------------------------------------------------------------
@router.post("/{user_id}/saved-meals")
async def save_meal(request: Request, user_id: int, data: SavedMealCreate,
                    db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    if not data.name.strip():
        _err(400, "BAD_REQUEST", "name required")
    if not (data.snapshot or {}).get("items"):
        _err(400, "BAD_REQUEST", "snapshot.items required")
    saved = MealSavedMeal(
        id=str(uuid.uuid4()), user_id=user_id, name=data.name.strip(),
        snapshot_json=json.dumps(data.snapshot, ensure_ascii=False),
    )
    db.add(saved)
    await db.commit()
    return {"saved_meal_id": saved.id, "name": saved.name}


@router.get("/{user_id}/saved-meals")
async def list_saved_meals(request: Request, user_id: int,
                           db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    rows = (
        await db.execute(
            select(MealSavedMeal).where(MealSavedMeal.user_id == user_id)
            .order_by(MealSavedMeal.created_at.desc())
        )
    ).scalars().all()
    return {"saved_meals": [
        {"saved_meal_id": s.id, "name": s.name, "snapshot": _loads(s.snapshot_json, {})}
        for s in rows
    ]}


# ---------------------------------------------------------------------------
# catalog submissions + review (private request -> shared reference data)
# ---------------------------------------------------------------------------
@router.post("/{user_id}/catalog-submissions")
async def submit_food(request: Request, user_id: int, data: SubmissionCreate,
                      db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    if not data.request_text.strip():
        _err(400, "BAD_REQUEST", "request_text required")
    submission = MealCatalogSubmission(
        id=str(uuid.uuid4()), user_id=user_id, request_text=data.request_text.strip(),
        proposed_match=data.proposed_match, status="pending",
    )
    db.add(submission)
    _audit(db, role, "catalog.submit", submission.id, {})
    await db.commit()
    return {"submission_id": submission.id, "status": "pending"}


@router.get("/catalog-submissions")
async def list_submissions(request: Request, status: Optional[str] = None,
                           db: AsyncSession = Depends(get_nutrition_db_coach)):
    _require_coach(request)
    query = select(MealCatalogSubmission)
    if status:
        query = query.where(MealCatalogSubmission.status == status)
    rows = (await db.execute(query.order_by(MealCatalogSubmission.created_at.desc()))).scalars().all()
    return {"submissions": [
        {"submission_id": s.id, "user_id": s.user_id, "request_text": s.request_text,
         "proposed_match": s.proposed_match, "status": s.status,
         "created_at": s.created_at.isoformat() if s.created_at else None}
        for s in rows
    ]}


@router.post("/catalog-submissions/{submission_id}/approve")
async def approve_submission(request: Request, submission_id: str, data: ReviewApprove,
                             db: AsyncSession = Depends(get_nutrition_db_coach)):
    role = _require_coach(request)
    submission = (
        await db.execute(select(MealCatalogSubmission).where(
            MealCatalogSubmission.id == submission_id
        ))
    ).scalar_one_or_none()
    if not submission or submission.status != "pending":
        _err(404, "NOT_FOUND", "pending submission not found")
    if not data.name_he.strip() or not data.nutrients:
        _err(400, "BAD_REQUEST", "verified name and nutrients required")
    food_version_id = f"f_{uuid.uuid4().hex[:12]}"
    db.add(MealFoodVersion(
        id=food_version_id, food_id=food_version_id, name_he=data.name_he.strip(),
        kind=data.kind or "generic", preparation_state=data.preparation_state or "raw",
        basis_g=data.basis_g or "100",
        nutrients_json=json.dumps(data.nutrients, ensure_ascii=False),
        # allergens stay NULL when the coach did not declare them - unknown
        # must never look like "no allergens" (fail-closed in allergen_free).
        allergens_json=json.dumps(data.allergens, ensure_ascii=False),
        attributes_json=json.dumps(
            data.attributes
            if data.attributes is not None
            else derive_attributes(data.name_he.strip(), data.kind or "generic",
                                   data.allergens, data.nutrients),
            ensure_ascii=False),
        kosher_evidence=data.kosher_evidence or "unknown", status="approved",
        aliases_json=json.dumps(data.aliases or [data.name_he.strip()], ensure_ascii=False),
        evidence="reviewed-submission",
    ))
    submission.status = "approved"
    submission.reviewer_role = role
    submission.reviewed_at = datetime.utcnow()
    epoch = await _bump_catalog_epoch(db)
    _audit(db, role, "catalog.approve", food_version_id, {"epoch": epoch})
    await db.commit()
    return {"food_version_id": food_version_id, "catalog_epoch": epoch}


@router.post("/catalog-submissions/{submission_id}/reject")
async def reject_submission(request: Request, submission_id: str,
                            db: AsyncSession = Depends(get_nutrition_db_coach)):
    role = _require_coach(request)
    submission = (
        await db.execute(select(MealCatalogSubmission).where(
            MealCatalogSubmission.id == submission_id
        ))
    ).scalar_one_or_none()
    if not submission or submission.status != "pending":
        _err(404, "NOT_FOUND", "pending submission not found")
    submission.status = "rejected"
    submission.reviewer_role = role
    submission.reviewed_at = datetime.utcnow()
    await db.commit()
    return {"submission_id": submission.id, "status": "rejected"}


@router.post("/catalog/food-versions/{food_version_id}/verify")
async def verify_food(request: Request, food_version_id: str,
                      db: AsyncSession = Depends(get_nutrition_db)):
    """Coach verification of an auto-enriched (unverified) food version.

    Sets verified=1; until then the food is request-servable only under
    the fail-closed unverified rules in run_proposal_job."""
    role = _require_coach(request)
    food = (
        await db.execute(select(MealFoodVersion).where(
            MealFoodVersion.id == food_version_id
        ))
    ).scalar_one_or_none()
    if not food:
        _err(404, "NOT_FOUND", "food version not found")
    if not food.verified:
        food.verified = 1
        epoch = await _bump_catalog_epoch(db)
        _audit(db, role, "catalog.verify", food_version_id, {"epoch": epoch})
        await db.commit()
    return {"food_version_id": food_version_id, "verified": True}


@router.post("/catalog/food-versions/{food_version_id}/retract")
async def retract_food(request: Request, food_version_id: str,
                       db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_coach(request)
    food = (
        await db.execute(select(MealFoodVersion).where(
            MealFoodVersion.id == food_version_id
        ))
    ).scalar_one_or_none()
    if not food:
        _err(404, "NOT_FOUND", "food version not found")
    food.status = "retracted"
    epoch = await _bump_catalog_epoch(db)
    _audit(db, role, "catalog.retract", food_version_id, {"epoch": epoch})
    await db.commit()
    return {"food_version_id": food_version_id, "status": "retracted", "catalog_epoch": epoch}


def _clean_nutrients(raw: dict) -> dict:
    """Keep only known nutrient keys; every value must be a real decimal."""
    out = {}
    for k, v in (raw or {}).items():
        if k not in engine.NUTRIENT_FIELDS or v is None:
            continue
        try:
            out[k] = str(engine.dec(v))
        except (ValueError, TypeError):
            _err(400, "BAD_REQUEST", f"nutrient {k}: not a number")
    return out


@router.post("/{user_id}/catalog/foods")
async def create_food(request: Request, user_id: int, data: FoodCreate,
                      db: AsyncSession = Depends(get_nutrition_db_coach)):
    """Coach adds a food to the shared catalog. Without explicit nutrients the
    internet lookup (Open Food Facts, builtin fallback) fills real values;
    a miss returns lookup_miss so the coach can enter values manually."""
    role = _require_coach(request)
    name = data.name_he.strip()
    nutrients = _clean_nutrients(data.nutrients) if data.nutrients else None
    allergens = data.allergens
    hit = None
    source, evidence, verified = "coach-entered", "coach-entered", 1
    if not nutrients:
        if not data.lookup:
            _err(400, "BAD_REQUEST", "nutrients required when lookup is disabled")
        hit = await lookup_food(name)
        if not hit or not hit.get("nutrients"):
            return {"state": "lookup_miss", "food_version_id": None}
        nutrients = _clean_nutrients(hit["nutrients"])
        if allergens is None:
            allergens = hit.get("allergens")
        source = hit.get("source") or "auto_lookup"
        if source == "openfoodfacts":
            source = "auto_lookup"
        evidence = hit.get("evidence") or hit.get("source") or "auto_lookup"
        verified = 0  # external data stays unverified until the coach checks it
    if not nutrients:
        _err(400, "BAD_REQUEST", "at least one nutrient value is required")
    food_version_id = f"f_{uuid.uuid4().hex[:12]}"
    db.add(MealFoodVersion(
        id=food_version_id, food_id=food_version_id, name_he=name,
        kind=data.kind or "generic", preparation_state=data.preparation_state or "raw",
        basis_g="100",
        nutrients_json=json.dumps(nutrients, ensure_ascii=False),
        allergens_json=json.dumps(allergens, ensure_ascii=False),
        attributes_json=json.dumps(
            derive_attributes(name, data.kind or "generic", allergens, nutrients),
            ensure_ascii=False),
        kosher_evidence=data.kosher_evidence or (hit or {}).get("kosher") or "unknown",
        status="approved",
        aliases_json=json.dumps(data.aliases or [name], ensure_ascii=False),
        evidence=evidence, source=source, verified=verified,
    ))
    epoch = await _bump_catalog_epoch(db)
    _audit(db, role, "catalog.food_create", food_version_id,
           {"source": source, "verified": verified})
    await db.commit()
    return {"state": "created", "food_version_id": food_version_id,
            "verified": bool(verified), "source": source, "nutrients": nutrients,
            "catalog_epoch": epoch}


@router.patch("/catalog/food-versions/{food_version_id}")
async def update_food(request: Request, food_version_id: str, data: FoodPatch,
                      db: AsyncSession = Depends(get_nutrition_db_coach)):
    """Coach edits an existing food - the catalog grows and improves over
    time. Provided fields replace; nutrients merge over current values."""
    role = _require_coach(request)
    food = (
        await db.execute(select(MealFoodVersion).where(
            MealFoodVersion.id == food_version_id
        ))
    ).scalar_one_or_none()
    if not food:
        _err(404, "NOT_FOUND", "food version not found")
    changed = {}
    if data.name_he is not None and data.name_he.strip():
        food.name_he = data.name_he.strip()
        changed["name_he"] = food.name_he
    if data.kind is not None:
        food.kind = data.kind
        changed["kind"] = food.kind
    if data.nutrients:
        merged = {**_loads(food.nutrients_json, {}), **_clean_nutrients(data.nutrients)}
        food.nutrients_json = json.dumps(merged, ensure_ascii=False)
        changed["nutrients"] = merged
    if data.allergens is not None:
        food.allergens_json = json.dumps(data.allergens, ensure_ascii=False)
        changed["allergens"] = data.allergens
    if data.kosher_evidence is not None:
        food.kosher_evidence = data.kosher_evidence
        changed["kosher_evidence"] = data.kosher_evidence
    if data.aliases is not None:
        food.aliases_json = json.dumps(data.aliases, ensure_ascii=False)
        changed["aliases"] = data.aliases
    if not changed:
        _err(400, "BAD_REQUEST", "no fields to update")
    # Dietary flags always re-derive from the row's post-edit state.
    food.attributes_json = json.dumps(
        derive_attributes(food.name_he, food.kind,
                          _loads(food.allergens_json, None),
                          _loads(food.nutrients_json, {})),
        ensure_ascii=False)
    epoch = await _bump_catalog_epoch(db)
    _audit(db, role, "catalog.food_update", food_version_id, {"epoch": epoch})
    await db.commit()
    return {"food_version_id": food_version_id, "updated": list(changed),
            "catalog_epoch": epoch}


# ---------------------------------------------------------------------------
# eating ledger
# ---------------------------------------------------------------------------
def _resolve_items(items: List[EatingItem], src: CatalogSource) -> tuple:
    snapshots, incomplete = [], False
    for item in items:
        fid = item.food_version_id
        if fid:
            food = src.get_food_version(fid)
            if not food or food["status"] != "approved":
                _err(422, "UNKNOWN_FOOD", f"food version unavailable: {fid}")
            if item.quantity_g is None:
                _err(400, "BAD_REQUEST", f"quantity_g required for catalog food {fid}")
            try:
                engine.dec(item.quantity_g)
            except ValueError:
                _err(400, "BAD_REQUEST", "quantity_g is not a valid decimal")
            snapshots.append(engine.snapshot_item(
                {"food_version_id": fid, "quantity_g": item.quantity_g}, food, src
            ))
        elif item.description:
            snapshots.append({
                "food_version_id": None, "name_he": item.description,
                "quantity_g": item.quantity_g,
                "nutrients": {f: None for f in engine.NUTRIENT_FIELDS},
                "source": "user_description",
            })
            incomplete = True
        else:
            _err(400, "BAD_REQUEST", "each item needs food_version_id or description")
    return snapshots, ("incomplete" if incomplete else "complete")


@router.post("/{user_id}/eating-events")
async def log_eating(request: Request, user_id: int, data: EatingEventCreate,
                     idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
                     db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    if not data.items:
        _err(400, "BAD_REQUEST", "items must not be empty")
    route = f"eating:{user_id}"
    request_hash = engine.canonical_hash({"operation_id": data.operation_id})
    stored = await _check_idempotency(db, role, route, idempotency_key, request_hash)
    if stored is not None:
        return stored

    async with _lock_for(user_id):
        # Operation identity dedupes retries; intentionally eating the same
        # meal twice uses a different operation_id (DESIGN 8.1).
        existing = (
            await db.execute(select(MealEatingEvent).where(
                MealEatingEvent.user_id == user_id,
                MealEatingEvent.operation_id == data.operation_id,
                MealEatingEvent.event_type == "recorded",
            ))
        ).scalar_one_or_none()
        if existing:
            return _event_dict(existing)

        src = await _catalog_source(db)
        state = await _get_state(db, user_id, create=True)
        now = datetime.utcnow()
        occurred = (data.occurred_at or now).replace(tzinfo=None)
        nutrition_date = _event_date(data.occurred_at or now, data.nutrition_date, state.timezone)

        snapshots, completeness = _resolve_items(data.items, src)
        event = MealEatingEvent(
            id=str(uuid.uuid4()), user_id=user_id, operation_id=data.operation_id,
            occurred_at=occurred, recorded_at=now,
            nutrition_date=nutrition_date, event_type="recorded",
            items_json=json.dumps(snapshots, ensure_ascii=False),
            completeness=completeness, plan_version_id=data.plan_version_id,
            source_context=data.source_context,
        )
        db.add(event)
        await db.flush()
        await _refold_day(db, user_id, nutrition_date, src)
        state.ledger_epoch += 1
        _audit(db, role, "eating.record", event.id, {"date": nutrition_date})
        response = _event_dict(event)
        _store_idempotent_response(db, role, route, idempotency_key, request_hash, response, user_id)
        await db.commit()
        return response


async def _correction_target(db, user_id, event_id) -> MealEatingEvent:
    event = (
        await db.execute(select(MealEatingEvent).where(
            MealEatingEvent.id == event_id, MealEatingEvent.user_id == user_id,
            MealEatingEvent.event_type == "recorded",
        ))
    ).scalar_one_or_none()
    if not event:
        _err(404, "NOT_FOUND", "eating event not found")
    return event


@router.post("/{user_id}/eating-events/{event_id}/corrections")
async def correct_eating(request: Request, user_id: int, event_id: str, data: CorrectionCreate,
                         db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    if not data.items:
        _err(400, "BAD_REQUEST", "correction requires replacement items")
    async with _lock_for(user_id):
        src = await _catalog_source(db)
        target = await _correction_target(db, user_id, event_id)
        state = await _get_state(db, user_id, create=True)
        snapshots, completeness = _resolve_items(data.items, src)
        now = datetime.utcnow()
        new_date = data.nutrition_date or target.nutrition_date
        correction = MealEatingEvent(
            id=str(uuid.uuid4()), user_id=user_id,
            operation_id=f"corr-{uuid.uuid4().hex[:12]}",
            occurred_at=target.occurred_at, recorded_at=now,
            nutrition_date=new_date, event_type="correction",
            corrects_event_id=event_id,
            items_json=json.dumps(snapshots, ensure_ascii=False),
            completeness=completeness,
        )
        db.add(correction)
        await db.flush()
        # Date-moving correction refolds both days, old first (stable order).
        await _refold_day(db, user_id, target.nutrition_date, src)
        if new_date != target.nutrition_date:
            await _refold_day(db, user_id, new_date, src)
        state.ledger_epoch += 1
        _audit(db, role, "eating.correct", correction.id, {"target": event_id})
        await db.commit()
        return _event_dict(correction)


@router.post("/{user_id}/eating-events/{event_id}/voids")
async def void_eating(request: Request, user_id: int, event_id: str,
                      db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    async with _lock_for(user_id):
        src = await _catalog_source(db)
        target = await _correction_target(db, user_id, event_id)
        state = await _get_state(db, user_id, create=True)
        void = MealEatingEvent(
            id=str(uuid.uuid4()), user_id=user_id,
            operation_id=f"void-{uuid.uuid4().hex[:12]}",
            occurred_at=target.occurred_at, recorded_at=datetime.utcnow(),
            nutrition_date=target.nutrition_date, event_type="void",
            corrects_event_id=event_id, items_json="[]",
        )
        db.add(void)
        await db.flush()
        await _refold_day(db, user_id, target.nutrition_date, src)
        state.ledger_epoch += 1
        _audit(db, role, "eating.void", void.id, {"target": event_id})
        await db.commit()
        return _event_dict(void)


@router.get("/{user_id}/days/{day}")
async def get_day(request: Request, user_id: int, day: str,
                  db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    src = await _catalog_source(db)
    state = await _get_state(db, user_id)
    rows = (
        await db.execute(
            select(MealEatingEvent)
            .where(MealEatingEvent.user_id == user_id, MealEatingEvent.nutrition_date == day)
            .order_by(MealEatingEvent.recorded_at)
        )
    ).scalars().all()
    projection = await _day_projection(db, user_id, day, src)
    return {
        "user_id": user_id, "nutrition_date": day,
        "totals": projection["totals"], "completeness": projection["completeness"],
        "events": [_event_dict(e) for e in rows],
        "active_plan_id": state.active_plan_id if state else None,
    }


@router.get("/{user_id}/events")
async def list_events(request: Request, user_id: int, limit: int = 50,
                      db: AsyncSession = Depends(get_nutrition_db)):
    _require_member(request)
    rows = (
        await db.execute(
            select(MealEatingEvent).where(MealEatingEvent.user_id == user_id)
            .order_by(MealEatingEvent.recorded_at.desc()).limit(min(limit, 200))
        )
    ).scalars().all()
    return {"events": [_event_dict(e) for e in rows]}


# ---------------------------------------------------------------------------
# voice assist: transcript -> suggested items. NEVER an eating event itself.
# ---------------------------------------------------------------------------
@router.post("/transcribe")
async def transcribe_voice(request: Request, file: UploadFile = File(...),
                           db: AsyncSession = Depends(get_nutrition_db)):
    get_current_role(request)
    from tutors import transcribe_audio

    audio_bytes = await file.read()
    if not audio_bytes:
        _err(400, "BAD_REQUEST", "empty audio")
    text = await transcribe_audio(audio_bytes, file.filename or "recording.webm", subject="meals")
    src = await _catalog_source(db)
    suggestions = []
    for segment in [s.strip() for s in (text or "").replace("עם", ",").split(",") if s.strip()]:
        match = src.match_food_text(segment)
        if match["status"] != "none":
            suggestions.append({"text": segment, **match})
    return {"transcript": text, "suggestions": suggestions}
