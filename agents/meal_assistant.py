"""Bounded LLM assistant for the meal coach (DESIGN section 10).

Everything works with the assistant disabled (AI_DISABLED). The model only
sees a bounded candidate set - never the child's name, profile, allergies,
health data or history - and its output is strictly validated in code. The
model can never approve, publish, write targets/quantities, select a meal or
create an eating event; all mutations go through the same session PATCH
rules and the shared deterministic proposal helper.

Safety boundaries:
  - healthReport=true only returns health_preview so the UI can ask the
    child to confirm; no health intent is created here.
  - Output keys, ids and list sizes are allowlisted; anything else (extra
    keys, unknown ids, invented quantities, non-JSON) is an AI failure and
    falls back to the plain deterministic proposal.
  - Per-user daily cap (MEAL_AI_DAILY_CAP) enforced server-side.
  - Logs carry model id, latency and validator outcome - never the raw
    message or model output.
"""
import asyncio
import json
import os
import re
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decimal import Decimal

import tutors
from tutors import call_llm

from nutrition_core import engine
from nutrition_core.food_library import derive_attributes
from nutrition_core.lookup import alias_terms_in_text, lookup_food, builtin_terms_in_text
from nutrition_db import (
    bind_rls_actor,
    MealFoodVersion,
    MealPlanVersion,
    MealProposal,
    MealSuggestionSession,
    get_nutrition_db,
)
from agents.nutrition_coach_agent import (
    _catalog_source,
    _current_profile,
    _day_projection,
    _err,
    _get_state,
    _load_open_session,
    _loads,
    _lock_for,
    _require_member,
    run_proposal_job,
)

router = APIRouter(prefix="/api/meals", tags=["Meal Assistant"])

_AI_TIMEOUT_SECONDS = 8
_AI_TOTAL_BUDGET_SECONDS = 15
_AI_MAX_LIST = 10

_EXPLANATION_KEYS = {
    "missing_ingredient",
    "wants_different",
    "preference_match",
    "no_change",
    "unavailable",
    "health_question",
    "adapted",
    "auto_added",
    "unclear",
}
_ALLOWED_KEYS = {
    "candidateRecipeIds",
    "missingFoodIds",
    "rejectRecipeIds",
    "requestedFoodIds",
    "requestedFoodName",
    "explanationKey",
    "healthReport",
}
_EXPLANATIONS_HE = {
    "missing_ingredient": "הבנתי שחסר מצרך — הוצאתי אותו והצעתי אפשרות אחרת.",
    "wants_different": "בסדר! ננסה אפשרות אחרת.",
    "unavailable": "אין היום ארוחה כזאת באפשרויות — רק מה שהמאמן אישר מראש.",
    "adapted": "אין את המנה הזו במתכונים, אבל התאמתי לך מנה ממה שמותר",
    "auto_added": "הוספתי את זה למאגר והתאמתי לך מנה — המאמן יאשר את הנתונים",
    "partial_fit": "התאמתי לך מנה — חלק מהמבוקש חורג מהיעדים, ראי את ההצעות למטה.",
    "blocked": "יש עדכון בריאותי שממתין לבדיקת המאמן — הצעות מושהות בינתיים.",
    "last_option": "זו האפשרות היחידה שנשארה לארוחה הזאת היום.",
    "preference_match": "מצאתי משהו שמתאים למה שביקשת.",
    "no_change": "לא שיניתי כלום — זו עדיין האפשרות הכי מתאימה.",
    "health_question": "זה נשמע כמו עדכון בריאותי — צריך לאשר לפני שממשיכים.",
    "unclear": "לא בטוח שהבנתי — הנה האפשרות הכי מתאימה בכל זאת.",
}

_SYSTEM_PROMPT = """You assist a trainee's meal-choice session inside a coach-approved meal plan.
You receive ONLY a bounded JSON payload: the candidate recipes for this meal
slot (each with id, Hebrew name and ingredient food ids), basketFoods (the
approved ingredient foods the engine may compose a dish from, id and Hebrew
name only), the session's missing food ids and rejected recipe ids, and the
trainee's free-text message.
The payload's currentRecipeId is the recipe currently proposed in the
session; when the trainee asks for something different, put it (or the
recipes they reject) in rejectRecipeIds.
Reply with ONE strict JSON object, no other text, using ONLY these optional keys:
  candidateRecipeIds: up to 10 recipe ids from the candidates, best first
  missingFoodIds:     up to 10 food ids (from candidate ingredients or the
                      session list) the trainee says are missing
  rejectRecipeIds:    up to 10 recipe ids from the candidates the trainee
                      rejects for this session
  requestedFoodIds:   up to 3 food ids from basketFoods the trainee asks
                      for (e.g. "בא לי עוף") - the engine composes a dish
                      around them when no recipe fits
  requestedFoodName:  the food/dish NAME (<=60 chars) the trainee asks for
                      when it is NOT among the candidates or basketFoods
                      (e.g. "סטייק") - the system looks it up in a real
                      nutrient database and may auto-add it; never invent
                      nutrients yourself
  explanationKey:     one of missing_ingredient|wants_different|
                      preference_match|no_change|unavailable|adapted|
                      health_question|unclear
  healthReport:       true whenever the message reports or asks about a
                      health change (allergy, illness, doctor instruction)
Use explanationKey=adapted when you set requestedFoodIds - the engine then
composes a dish from approved foods instead of a catalog recipe.
Use requestedFoodName (not requestedFoodIds) for a food that is not in the
payload at all, and use explanationKey=adapted with it - the platform
enriches it from a real nutrient source when possible and only answers
unavailable when enrichment truly fails.
Never invent ids, quantities, nutrients or any other keys."""


class AssistantMessage(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# enablement + daily cap
# ---------------------------------------------------------------------------
def _llm_configured() -> bool:
    """Key-configured check, isolated so tests can patch it."""
    return tutors._get_async_client() is not None


def assistant_enabled() -> bool:
    return os.getenv("MEAL_AI_ENABLED") == "1" and _llm_configured()


_daily_counts: dict = {}


def _cap_limit() -> int:
    try:
        return int(os.getenv("MEAL_AI_DAILY_CAP", "30"))
    except ValueError:
        return 30


def _cap_key(user_id: int) -> tuple:
    return (user_id, time.strftime("%Y-%m-%d"))


def _cap_reached(user_id: int) -> bool:
    return _daily_counts.get(_cap_key(user_id), 0) >= _cap_limit()


def _cap_bump(user_id: int) -> None:
    key = _cap_key(user_id)
    _daily_counts[key] = _daily_counts.get(key, 0) + 1
    for old in [k for k in _daily_counts if k[1] != key[1]]:
        _daily_counts.pop(old, None)


# ---------------------------------------------------------------------------
# strict output validation (DESIGN 10.1): unknown keys/ids = AI failure
# ---------------------------------------------------------------------------
def _validate_ai_output(raw: str, allowed_recipe_ids: set, allowed_food_ids: set,
                        name_to_recipe_id: Optional[dict] = None,
                        name_to_food_id: Optional[dict] = None,
                        allowed_requested_food_ids: Optional[set] = None) -> Optional[dict]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        # Models often wrap the object in a markdown fence; strip it and
        # retry once before declaring the output invalid.
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(),
                          flags=re.MULTILINE)
        try:
            data = json.loads(stripped)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict) or not set(data.keys()) <= _ALLOWED_KEYS:
        return None

    def _id_list(value, allowed, names):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > _AI_MAX_LIST:
            return None
        resolved = []
        for v in value:
            if not isinstance(v, str):
                return None
            vid = v if v in allowed else (names or {}).get(v)
            if vid is None:
                return None
            resolved.append(vid)
        return list(dict.fromkeys(resolved))

    out = {
        "candidateRecipeIds": _id_list(data.get("candidateRecipeIds"), allowed_recipe_ids,
                                       name_to_recipe_id),
        "missingFoodIds": _id_list(data.get("missingFoodIds"), allowed_food_ids,
                                   name_to_food_id),
        "rejectRecipeIds": _id_list(data.get("rejectRecipeIds"), allowed_recipe_ids,
                                    name_to_recipe_id),
        # Coach gate: requested foods must come from the approved basket
        # only - a food outside it stays 'unavailable', never composed.
        "requestedFoodIds": _id_list(
            data.get("requestedFoodIds"),
            allowed_requested_food_ids if allowed_requested_food_ids is not None
            else allowed_food_ids,
            name_to_food_id,
        ),
    }
    if any(v is None for v in out.values()):
        return None
    if len(out["requestedFoodIds"]) > 3:
        return None
    requested_name = data.get("requestedFoodName")
    if requested_name is not None:
        if not isinstance(requested_name, str) or not 1 <= len(requested_name.strip()) <= 60:
            return None
        requested_name = requested_name.strip()
    out["requestedFoodName"] = requested_name
    key = data.get("explanationKey")
    if key is not None and key not in _EXPLANATION_KEYS:
        return None
    health = data.get("healthReport")
    if health is not None and not isinstance(health, bool):
        return None
    out["explanationKey"] = key or "unclear"
    out["healthReport"] = bool(health)
    return out


async def _call_and_validate(payload: dict, allowed_recipe_ids: set, allowed_food_ids: set,
                             name_to_recipe_id: Optional[dict] = None,
                             name_to_food_id: Optional[dict] = None,
                             allowed_requested_food_ids: Optional[set] = None) -> Optional[dict]:
    """One LLM call + at most one repair retry, inside the total budget."""
    started = time.monotonic()
    user_payload = json.dumps(payload, ensure_ascii=False)
    messages = [{"role": "user", "content": user_payload}]
    for attempt in (1, 2):
        try:
            raw = await asyncio.wait_for(
                call_llm(_SYSTEM_PROMPT, messages), timeout=_AI_TIMEOUT_SECONDS
            )
        except Exception:
            raw = None
        parsed = _validate_ai_output(raw or "", allowed_recipe_ids, allowed_food_ids,
                                     name_to_recipe_id, name_to_food_id,
                                     allowed_requested_food_ids) if raw else None
        latency_ms = int((time.monotonic() - started) * 1000)
        # DESIGN 10.2: no raw private text in logs by default; raw invalid
        # output is opt-in for local debugging only (MEAL_AI_LOG_RAW=1).
        logger.info(
            "meal assistant call model={} attempt={} valid={} latency_ms={} raw={}",
            os.getenv("OPENAI_MODEL", getattr(tutors, "PRIMARY_MODEL", "unknown")),
            attempt, bool(parsed), latency_ms,
            (raw or "")[:200] if (not parsed and os.getenv("MEAL_AI_LOG_RAW") == "1") else None,
        )
        if parsed is not None:
            return parsed
        # Repair retry only if a full timeout budget still fits.
        if time.monotonic() - started > _AI_TOTAL_BUDGET_SECONDS - _AI_TIMEOUT_SECONDS:
            return None
        messages = [
            {"role": "user", "content": user_payload},
            {"role": "assistant", "content": raw or ""},
            {"role": "user", "content": "Invalid. Reply with a single strict JSON object only."},
        ]
    return None


def _assistant_candidates(plan_content: dict, src, profile: dict, slot_key: str) -> list:
    """Bounded candidate set: planned recipe + plan alternatives for the
    slot, minus permanently disliked recipes. No profile/health fields."""
    resolved = engine.validate_plan_content(plan_content, src)
    disliked = set(profile.get("disliked_recipe_ids") or [])
    rids = []
    planned = {m["slot_key"]: m for m in resolved["meals"]}.get(slot_key) or {}
    if planned.get("recipe_version_id"):
        rids.append(planned["recipe_version_id"])
    rids.extend(resolved["alternatives"].get(slot_key) or [])
    candidates = []
    seen = set()
    for rid in rids:
        if rid in disliked or rid in seen:
            continue
        seen.add(rid)
        rec = src.get_recipe_version(rid)
        if not rec:
            continue
        candidates.append({
            "id": rid,
            "name_he": rec["name_he"],
            "ingredients": [
                {
                    "food_id": i["food_version_id"],
                    "name_he": (src.get_food_version(i["food_version_id"]) or {}).get("name_he"),
                }
                for i in rec["items"]
            ],
        })
    return candidates


def _match_catalog_food(src, name: str) -> Optional[str]:
    """Resolve a free-text food name to an approved catalog food id:
    exact alias match first, then a single unambiguous containment match
    over names/aliases. Ambiguous or missing -> None."""
    match = src.match_food_text(name)
    if match["status"] == "exact":
        return match["food_version_ids"][0]
    if match["status"] == "ambiguous":
        return None
    lowered = name.strip().lower()
    hits = []
    for food in src.approved_foods():
        names = [food.get("name_he") or ""] + list(food.get("aliases") or [])
        for n in names:
            n = (n or "").strip().lower()
            if n and (n == lowered or lowered in n or n in lowered):
                hits.append(food["food_version_id"])
                break
    return hits[0] if len(hits) == 1 else None


async def _insert_lookup_food(db, requested_name: str, looked: dict) -> str:
    """Persist a real-source looked-up food as an UNVERIFIED catalog row.

    The row is request-servable under the unverified gate until the coach
    verifies it; the catalog epoch and the plan vector intentionally do
    not change (an additive row cannot invalidate anything)."""
    fid = f"f_{uuid.uuid4().hex[:12]}"
    name_he = (looked.get("name_he") or "").strip() or requested_name.strip()
    aliases = list(dict.fromkeys(
        a for a in [requested_name.strip(), name_he,
                    (looked.get("product_name") or "").strip()]
        if a
    ))
    db.add(MealFoodVersion(
        id=fid, food_id=fid, name_he=name_he,
        kind="generic", preparation_state="raw", basis_g="100",
        nutrients_json=json.dumps(looked.get("nutrients") or {}, ensure_ascii=False),
        # None serializes to "null" -> allergens unknown (never an empty
        # safe list).
        allergens_json=json.dumps(looked.get("allergens"), ensure_ascii=False),
        # dietary flags derive deterministically; unknown allergen set
        # suppresses free-from claims but nutrient-based flags still apply.
        attributes_json=json.dumps(
            derive_attributes(name_he, "generic", looked.get("allergens"),
                              looked.get("nutrients") or {}),
            ensure_ascii=False),
        kosher_evidence=looked.get("kosher") or "unknown", status="approved",
        aliases_json=json.dumps(aliases, ensure_ascii=False),
        evidence=looked.get("evidence") or looked.get("source") or "auto_lookup",
        source="auto_lookup" if looked.get("source") == "openfoodfacts"
               else (looked.get("source") or "builtin-synthetic"),
        verified=0,
    ))
    await db.flush()
    return fid


# ---------------------------------------------------------------------------
# unmet requested foods: why a named food was dropped + fitting substitutes
# ---------------------------------------------------------------------------
_NUTRIENT_HE = {
    "kcal": "הקלוריות", "protein_g": "החלבון", "carbs_g": "הפחמימות",
    "fat_g": "השומן", "fiber_g": "הסיבים", "sodium_mg": "הנתרן",
    "sugar_g": "הסוכר", "sat_fat_g": "השומן הרווי",
    "cholesterol_mg": "הכולסטרול",
}


def _dec(value):
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _min_portion_g(food_version_id: str, src) -> Decimal:
    spec = engine._observed_portion_spec(food_version_id, src)
    if spec:
        try:
            v = Decimal(str(spec["min_g"]))
            if v > 0:
                return v
        except Exception:
            pass
    return engine.COMPOSITION_DEFAULT_MIN_G


def _food_per100(food: dict, nutrient: str) -> Optional[Decimal]:
    v = (food.get("nutrients") or {}).get(nutrient)
    return _dec(v) if v is not None else None


def _unmet_request_explanations(job, src, unmet_ids, basket_foods,
                                served_fids, state):
    """For each requested food the engine resolved but could not serve,
    explain which day bound it would break and suggest approved
    substitutes that DO fit the remaining budget. Deterministic only -
    nutrient values come from the catalog, never from the model.

    Headroom per nutrient comes from the proposal's own day-bound check
    records (residual - served total), the same figures the engine used.
    """
    headroom = {}
    for c in ((job.get("proposal") or {}).get("checks") or []):
        if c.get("scope") != "day" or not c.get("nutrient"):
            continue
        rem = _dec(c.get("residual"))
        total = _dec(c.get("total"))
        if rem is not None and total is not None:
            headroom[c["nutrient"]] = rem - total

    # Substitute candidates: this slot's approved basket first, then any
    # other catalog food servable under the same unverified-food gate the
    # engine applies to extra basket items.
    candidates = []
    seen = set()
    for bf in basket_foods:
        fid = bf.get("food_id")
        if fid and fid not in seen:
            seen.add(fid)
            candidates.append(fid)
    for food in src.approved_foods():
        fid = food.get("food_version_id")
        if fid and fid not in seen:
            seen.add(fid)
            candidates.append(fid)

    out = []
    for fid in unmet_ids:
        food = src.get_food_version(fid) or {}
        min_g = _min_portion_g(fid, src)
        worst = None
        for nutrient, room in headroom.items():
            per100 = _food_per100(food, nutrient)
            if per100 is None:
                continue
            excess = per100 * min_g / 100 - room
            if excess > 0 and (worst is None or excess > worst[1]):
                worst = (nutrient, excess)
        reason_nutrient = worst[0] if worst else None

        subs = []
        drop_v = _food_per100(food, reason_nutrient) if reason_nutrient else None
        for cand_id in candidates:
            if cand_id in unmet_ids or cand_id in served_fids or cand_id == fid:
                continue
            cand = src.get_food_version(cand_id) or {}
            if cand.get("status") != "approved":
                continue
            # same servability gate as the engine's extra-basket rule
            if not cand.get("verified", 1) and (
                    state.active_rule_bundle_id or cand.get("allergens") is None):
                continue
            cand_min_g = _min_portion_g(cand_id, src)
            density = Decimal(0)
            if reason_nutrient:
                cv = _food_per100(cand, reason_nutrient)
                if cv is None:
                    continue  # unknown value for the nutrient that matters
                density = cv
                if cv * cand_min_g / 100 > headroom.get(reason_nutrient, Decimal(0)):
                    continue  # does not fit either - not a real substitute
                if drop_v is not None and cv >= drop_v:
                    continue  # must be meaningfully lighter on the blocker
            fits_all = True
            for nutrient, room in headroom.items():
                cv = _food_per100(cand, nutrient)
                if cv is not None and cv * cand_min_g / 100 > room:
                    fits_all = False
                    break
            if not fits_all:
                continue
            in_basket = any(b.get("food_id") == cand_id for b in basket_foods)
            subs.append((0 if in_basket else 1, density, cand_id,
                         cand.get("name_he") or cand_id))
        subs.sort(key=lambda t: (t[0], t[1], t[2]))
        out.append({
            "food_version_id": fid,
            "name_he": food.get("name_he") or fid,
            "reason_nutrient": reason_nutrient,
            "substitutes": [{"food_version_id": c, "name_he": n}
                            for _b, _d, c, n in subs[:2]],
        })
    return out


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@router.get("/assistant/status")
async def assistant_status(request: Request):
    return {"enabled": assistant_enabled()}


@router.post("/{user_id}/assistant/messages")
async def assistant_message(request: Request, user_id: int, data: AssistantMessage,
                            db: AsyncSession = Depends(get_nutrition_db)):
    role = _require_member(request)
    if not assistant_enabled():
        _err(503, "AI_DISABLED", "the meal assistant is not enabled")
    if _cap_reached(user_id):
        _err(429, "AI_CAP_REACHED", "daily assistant cap reached")

    # Phase 1 - snapshot under the per-user lock. The read transaction is
    # closed before the AI call so neither the lock nor a DB transaction is
    # held across it (DESIGN 2/6.5: a slow AI call must not block them).
    async with _lock_for(user_id):
        session = await _load_open_session(db, user_id, data.session_id)
        src = await _catalog_source(db)
        state = await _get_state(db, user_id)
        if not state or not state.active_plan_id:
            _err(409, "NO_ACTIVE_PLAN", "no published plan to propose against")
        plan = (
            await db.execute(select(MealPlanVersion).where(
                MealPlanVersion.id == state.active_plan_id
            ))
        ).scalar_one_or_none()
        plan_content = _loads(plan.published_content_json, {}) if plan else {}
        profile = await _current_profile(db, user_id, state)
        candidates = _assistant_candidates(plan_content, src, profile, session.slot_key)

        allowed_recipe_ids = {c["id"] for c in candidates}
        candidate_food_ids = {
            i["food_id"] for c in candidates for i in c["ingredients"]
        }
        missing = _loads(session.missing_food_ids_json, [])
        rejected = _loads(session.rejected_recipe_ids_json, [])
        allowed_food_ids = candidate_food_ids | set(missing)
        # Approved food basket for this slot = ingredients of the approved
        # candidate recipes, minus foods the session already rejected by
        # rejecting their whole recipe (mirrors the engine's basket).
        rejected_set = set(rejected)
        basket_foods = []
        seen_basket = set()
        for c in candidates:
            if c["id"] in rejected_set:
                continue
            for i in c["ingredients"]:
                if i["food_id"] in seen_basket:
                    continue
                seen_basket.add(i["food_id"])
                basket_foods.append({"food_id": i["food_id"], "name_he": i["name_he"]})
        basket_food_ids = seen_basket
        last_proposal = (
            await db.execute(select(MealProposal).where(
                MealProposal.session_id == session.id,
            ).order_by(MealProposal.created_at.desc()).limit(1))
        ).scalar_one_or_none()
        snap = {
            "current_recipe_id": (
                _loads(last_proposal.payload_json, {}).get("recipe_version_id")
                if last_proposal else None
            ),
            "slot_key": session.slot_key,
            "revision": session.revision,
            "missing": missing,
            "rejected": rejected,
        }
        await db.rollback()  # end the read transaction before the AI call

    # Phase 2 - bounded AI call, no lock and no open transaction.
    _cap_bump(user_id)
    ai = await _call_and_validate(
        {
            "slot_key": snap["slot_key"],
            "candidates": candidates,
            "basketFoods": basket_foods,
            "missingFoodIds": snap["missing"],
            "rejectedRecipeIds": snap["rejected"],
            "currentRecipeId": snap["current_recipe_id"],
            "message": data.message,
        },
        allowed_recipe_ids,
        allowed_food_ids,
        {c["name_he"]: c["id"] for c in candidates},
        {i["name_he"]: i["food_id"] for c in candidates for i in c["ingredients"]
         if i.get("name_he")},
        basket_food_ids,
    )

    # Phase 3 - reacquire the lock, discard the AI result if the session
    # moved while the model was thinking, then propose via the shared path.
    async with _lock_for(user_id):
        await bind_rls_actor(db, user_id)  # GUC ended with the phase-1 transaction
        fresh = (
            await db.execute(select(MealSuggestionSession).where(
                MealSuggestionSession.id == data.session_id,
                MealSuggestionSession.user_id == user_id,
            ))
        ).scalar_one_or_none()
        stale = (
            fresh is None
            or fresh.status != "open"
            or fresh.revision != snap["revision"]
        )
        if stale and ai is not None:
            _err(409, "STALE_REVISION",
                 "session changed while the assistant was thinking")
        if fresh is None or fresh.status != "open":
            session = await _load_open_session(db, user_id, data.session_id)
        else:
            session = fresh

        preferred = None
        explanation_key = "unclear"
        health_preview = False
        if ai is not None:
            merged_missing = sorted(set(snap["missing"]) | set(ai["missingFoodIds"]))
            merged_rejected = [r for r in snap["rejected"]]
            for r in ai["rejectRecipeIds"]:
                if r not in merged_rejected:
                    merged_rejected.append(r)
            changed = False
            if set(merged_missing) != set(_loads(session.missing_food_ids_json, [])):
                session.missing_food_ids_json = json.dumps(merged_missing)
                changed = True
            if merged_rejected != _loads(session.rejected_recipe_ids_json, []):
                session.rejected_recipe_ids_json = json.dumps(merged_rejected)
                changed = True
            if changed:
                session.revision += 1
            preferred = ai["candidateRecipeIds"] or None
            explanation_key = ai["explanationKey"]
            # Fail-closed: a health_question label previews the health-intent
            # confirmation even if the model forgot healthReport=true.
            health_preview = ai["healthReport"] or explanation_key == "health_question"

        # Explicit food request outside the basket: resolve against the
        # catalog first, then enrich from a real nutrient source. The
        # looked-up row is inserted UNVERIFIED and request-servable only -
        # the published plan and its approved lists never change.
        requested_ids = list(ai["requestedFoodIds"]) if ai is not None else []
        extra_ids = []
        lookup_added_ids = []
        lookup_missed = False
        request_names = []
        scanned_catalog_ids = set()
        if ai is not None and ai.get("requestedFoodName"):
            request_names.append(ai["requestedFoodName"])
        if ai is not None:
            src = await _catalog_source(db)
            # Scan the model's own name for known food terms: it may merge
            # several foods into one string ("המבורגר וצ'יפס"), which a
            # single-name lookup resolves to ambiguous. Sub-terms split it.
            scan_terms = set()
            scanned_catalog_ids = set()
            for canon in builtin_terms_in_text(" ".join(request_names)):
                scan_terms.add(canon)
            # Scan the raw message too - but only when the model concluded
            # the request could not be satisfied (no name / unavailable /
            # unclear). On other keys a mentioned food might be the one the
            # trainee REJECTED, so scanning the message there would invert
            # the intent. Matching is whole-token (Hebrew bound prefixes
            # count) - never substring, so 'חלב' cannot fire inside 'חלבון'.
            if not request_names and ai["explanationKey"] in (
                    "unavailable", "unclear"):
                approved_ids = {f["food_version_id"] for f in src.approved_foods()}
                alias_index = {
                    a.strip().lower(): ids[0]
                    for a, ids in src.aliases.items()
                    if len(ids) == 1 and ids[0] in approved_ids
                }
                for fid in alias_terms_in_text(data.message or "", alias_index):
                    scanned_catalog_ids.add(fid)
                for canon in builtin_terms_in_text(data.message or ""):
                    scan_terms.add(canon)
            for term in scan_terms:
                if term not in request_names:
                    request_names.append(term)
        resolved = 0
        missed = 0
        for fid in (scanned_catalog_ids if ai is not None else ()):
            if fid not in requested_ids:
                requested_ids.append(fid)
            if fid not in extra_ids:
                extra_ids.append(fid)
            resolved += 1
        for name in request_names[:3]:
            fid = _match_catalog_food(src, name)
            if fid is None:
                looked = await lookup_food(name)
                if looked is not None:
                    fid = await _insert_lookup_food(db, name, looked)
                    lookup_added_ids.append(fid)
            if fid:
                resolved += 1
                extra_ids.append(fid)
                if fid not in requested_ids:
                    requested_ids.append(fid)
            else:
                missed += 1
        lookup_missed = missed > 0 and resolved == 0
        if lookup_added_ids:
            # The catalog source was snapshotted before the auto-added rows
            # existed; refresh it so unmet explanations and later matches see
            # the real names instead of raw ids.
            src = await _catalog_source(db)

        session_revision = session.revision
        job = await run_proposal_job(
            db, user_id, role,
            session_id=session.id,
            preferred_recipe_ids=preferred,
            requested_food_ids=requested_ids or None,
            extra_basket_food_ids=extra_ids or None,
            prefer_composition=bool(extra_ids),
        )
        proposal = job.get("proposal") or {}
        served_fids = {i.get("food_version_id") for i in proposal.get("items") or []}
        # Foods the trainee named that did not make the served dish: explain
        # the binding day-target and offer fitting substitutes (deterministic;
        # names/nutrients from the catalog, never model text).
        unmet_ids = [f for f in requested_ids if f not in served_fids]
        if job.get("state") == "eligible_candidate" and unmet_ids:
            unmet = _unmet_request_explanations(
                job, src, unmet_ids, basket_foods, served_fids, state)
            if unmet:
                job["unmet_requests"] = unmet
        if job.get("state") != "eligible_candidate":
            # Nothing was served - the model's optimistic label must never
            # claim otherwise. The job's own reasons explain why.
            explanation_key = "blocked" if job.get("state") == "blocked" else "unavailable"
        elif job.get("unmet_requests"):
            explanation_key = "partial_fit"
        elif lookup_added_ids and any(fid in served_fids for fid in lookup_added_ids):
            # The auto-enriched food made it into the served composition.
            explanation_key = "auto_added"
        elif lookup_missed:
            explanation_key = "unavailable"
        elif proposal.get("composed"):
            # The deterministic engine served an ad-hoc composition - the
            # explanation is decided by what was actually served.
            explanation_key = "adapted"
        elif (job.get("state") == "eligible_candidate"
                and proposal.get("recipe_version_id") == snap["current_recipe_id"]
                and explanation_key == "wants_different"):
            explanation_key = "last_option"
        return {
            "ai_used": ai is not None,
            "explanation_key": explanation_key if ai is not None else None,
            "explanation_he": _EXPLANATIONS_HE.get(explanation_key) if ai is not None else None,
            "health_preview": health_preview,
            "session_revision": session_revision,
            "job": job,
        }
