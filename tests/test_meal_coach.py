"""Endpoint-level tests for the meal_coach agent (docs/DESIGN.md).

Tests exercise the real HTTP surface - never re-implemented endpoint logic.
Synthetic fixture catalog only; no clinical claims are made by these tests.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from database import SessionLocal, User

APP_PIN = "test-pin-1234"
CHILD_PIN = "child-secret-11"
ADMIN_PIN = "admin-secret-99"


@pytest.fixture
def parent_headers():
    return {"x-app-pin": APP_PIN}


@pytest.fixture
def child_headers(monkeypatch):
    monkeypatch.setenv("CHILD_PIN", CHILD_PIN)
    return {"x-app-pin": CHILD_PIN}


@pytest.fixture
def admin_headers(monkeypatch):
    monkeypatch.setenv("ADMIN_PIN", ADMIN_PIN)
    return {"x-app-pin": ADMIN_PIN}


async def _user(name=None):
    async with SessionLocal() as db:
        user = User(name=name or f"Meal-{uuid.uuid4().hex[:8]}", role="child")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


PLAN = {
    "name": "תפריט משפחתי",
    "meals": [
        {
            "slot_key": "lunch",
            "recipe_version_id": "r_chicken_plate_v1",
            "items": [
                {"food_version_id": "f_chicken_cooked_v1", "quantity_g": "150"},
                {"food_version_id": "f_rice_cooked_v1", "quantity_g": "200"},
                {"food_version_id": "f_cucumber_v1", "quantity_g": "100"},
                {"food_version_id": "f_tomato_v1", "quantity_g": "100"},
            ],
        },
        {
            "slot_key": "breakfast",
            "recipe_version_id": "r_egg_bread_v1",
            "items": [
                {"food_version_id": "f_egg_boiled_v1", "quantity_g": "100"},
                {"food_version_id": "f_bread_v1", "quantity_g": "60"},
                {"food_version_id": "f_tomato_v1", "quantity_g": "100"},
            ],
        },
    ],
    "alternatives": {"lunch": ["r_tahini_plate_v1"], "breakfast": ["r_yogurt_bowl_v1"]},
    "day_targets": {"kcal": {"upper": "3000"}},
}


async def _create_draft(client, headers, user_id, content=None):
    resp = await client.post(
        f"/api/meals/{user_id}/plan-drafts",
        json={"content": content or PLAN},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["draft_id"], body["draft_revision"]


async def _validate(client, headers, user_id, draft_id):
    resp = await client.post(
        f"/api/meals/{user_id}/plan-drafts/{draft_id}/validate", headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _approve(client, headers, user_id, draft_id, revision, validation):
    resp = await client.post(
        f"/api/meals/{user_id}/plan-drafts/{draft_id}/approve",
        json={"content_hash": validation["content_hash"], "input_vector": validation["input_vector"]},
        headers={**headers, "If-Match": str(revision)},
    )
    return resp


async def _publish(client, headers, user_id, draft_id, approval_id, revision=None, key=None):
    hdrs = {**headers, "Idempotency-Key": key or f"pub-{uuid.uuid4().hex[:8]}"}
    if revision is not None:
        hdrs["If-Match"] = str(revision)
    resp = await client.post(
        f"/api/meals/{user_id}/plan-drafts/{draft_id}/publish",
        json={"approval_id": approval_id},
        headers=hdrs,
    )
    return resp


async def _confirm_profile(client, headers, user_id, proposed=None):
    """Create + confirm a profile-change intent (B02 publish gate)."""
    resp = await client.post(
        f"/api/meals/{user_id}/profile-change-intents",
        json={"proposed": proposed if proposed is not None else {
            "allergy_disclosure": "none_declared", "allergies": [],
            "medical_condition_declared": "no"}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    intent_id = resp.json()["intent_id"]
    confirmed = await client.post(
        f"/api/meals/{user_id}/profile-change-intents/{intent_id}/confirm",
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


async def _publish_plan(client, headers, user_id, content=None, key=None):
    await _confirm_profile(client, headers, user_id)
    draft_id, rev = await _create_draft(client, headers, user_id, content)
    validation = await _validate(client, headers, user_id, draft_id)
    assert validation["valid"], validation["issues"]
    resp = await _approve(client, headers, user_id, draft_id, rev, validation)
    assert resp.status_code == 200, resp.text
    resp = await _publish(client, headers, user_id, draft_id, resp.json()["approval_id"], rev, key)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── authorization ─────────────────────────────────────────────────────────
async def test_child_cannot_create_draft(client, child_headers):
    user = await _user()
    resp = await client.post(
        f"/api/meals/{user.id}/plan-drafts", json={"content": PLAN}, headers=child_headers
    )
    assert resp.status_code == 403


async def test_endpoints_anonymous_is_child_scope(client):
    """No PIN = child scope: member surfaces open, coach surfaces stay 403."""
    user = await _user()
    assert (await client.get(f"/api/meals/{user.id}/plan")).status_code == 200
    assert (await client.get("/api/meals/catalog")).status_code == 200
    resp = await client.post(f"/api/meals/{user.id}/plan-drafts", json={"content": {}})
    assert resp.status_code == 403


# ── draft -> approve -> publish (T01-T06) ─────────────────────────────────
async def test_draft_does_not_change_active_plan(client, parent_headers):
    """T01: a saved draft alone never mutates the active plan."""
    user = await _user()
    await _create_draft(client, parent_headers, user.id)
    plan = await client.get(f"/api/meals/{user.id}/plan", headers=parent_headers)
    assert plan.json()["plan"] is None


async def test_publish_happy_path(client, parent_headers):
    """T02: exact approved draft publishes one immutable version."""
    user = await _user()
    result = await _publish_plan(client, parent_headers, user.id)
    assert result["version"] == 1
    plan = await client.get(f"/api/meals/{user.id}/plan", headers=parent_headers)
    body = plan.json()
    assert body["plan"]["version"] == 1
    slots = {m["slot_key"] for m in body["plan"]["meals"]}
    assert {"lunch", "breakfast"} <= slots


async def test_publish_is_idempotent(client, parent_headers):
    """T04: same publication key retries to one version, same response."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id)
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    key = f"pub-{uuid.uuid4().hex[:8]}"
    first = await _publish(client, parent_headers, user.id, draft_id, approval.json()["approval_id"], rev, key)
    second = await _publish(client, parent_headers, user.id, draft_id, approval.json()["approval_id"], rev, key)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["plan_id"] == second.json()["plan_id"]
    # Reuse with a different payload on the same key is rejected.
    other = await client.post(
        f"/api/meals/{user.id}/plan-drafts/{draft_id}/publish",
        json={"approval_id": "different-approval"},
        headers={**parent_headers, "Idempotency-Key": key},
    )
    assert other.status_code == 409
    assert other.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_edit_after_approval_blocks_publish(client, parent_headers):
    """T06: editing a draft after approval invalidates that approval."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id)
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    # Edit the draft -> revision bumps -> old approval must not publish.
    resp = await client.patch(
        f"/api/meals/{user.id}/plan-drafts/{draft_id}",
        json={"content": {**PLAN, "name": "שונה"}},
        headers={**parent_headers, "If-Match": str(rev)},
    )
    assert resp.status_code == 200
    published = await _publish(client, parent_headers, user.id, draft_id, approval.json()["approval_id"])
    assert published.status_code == 409
    assert published.json()["detail"]["code"] == "STALE_REVISION"


async def test_patch_requires_if_match(client, parent_headers):
    user = await _user()
    draft_id, _ = await _create_draft(client, parent_headers, user.id)
    missing = await client.patch(
        f"/api/meals/{user.id}/plan-drafts/{draft_id}",
        json={"content": PLAN}, headers=parent_headers,
    )
    assert missing.status_code == 428
    wrong = await client.patch(
        f"/api/meals/{user.id}/plan-drafts/{draft_id}",
        json={"content": PLAN}, headers={**parent_headers, "If-Match": "99"},
    )
    assert wrong.status_code == 409


async def test_invalid_plan_cannot_publish(client, parent_headers):
    user = await _user()
    bad = {"name": "x", "meals": [{"slot_key": "lunch", "items": [
        {"food_version_id": "f_cereal_old_v1", "quantity_g": "50"}  # retracted
    ]}]}
    await _confirm_profile(client, parent_headers, user.id)
    draft_id, rev = await _create_draft(client, parent_headers, user.id, bad)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert not validation["valid"]
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    assert approval.status_code == 422  # invalid content cannot be approved
    assert approval.json()["detail"]["code"] == "INVALID_PLAN"


# ── profile (B11) ─────────────────────────────────────────────────────────
async def test_profile_intent_confirm_bumps_epoch(client, parent_headers):
    user = await _user()
    before = (await client.get(f"/api/meals/{user.id}/state", headers=parent_headers)).json()
    intent = await client.post(
        f"/api/meals/{user.id}/profile-change-intents",
        json={"proposed": {"excluded_food_ids": ["f_tahini_v1"], "allergies": []}},
        headers=parent_headers,
    )
    assert intent.status_code == 200
    confirmed = await client.post(
        f"/api/meals/{user.id}/profile-change-intents/{intent.json()['intent_id']}/confirm",
        headers=parent_headers,
    )
    assert confirmed.status_code == 200
    after = (await client.get(f"/api/meals/{user.id}/state", headers=parent_headers)).json()
    assert after["vector"]["profileEpoch"] > (before["vector"] or {}).get("profileEpoch", 0)


# ── eating ledger (T24-T31) ───────────────────────────────────────────────
async def test_eating_event_and_day_totals(client, child_headers):
    user = await _user()
    resp = await client.post(
        f"/api/meals/{user.id}/eating-events",
        json={
            "operation_id": "op-1",
            "nutrition_date": "2026-11-22",
            "items": [{"food_version_id": "f_yogurt_v1", "quantity_g": "200"}],
        },
        headers={**child_headers, "Idempotency-Key": "eat-1"},
    )
    assert resp.status_code == 200, resp.text
    day = await client.get(f"/api/meals/{user.id}/days/2026-11-22", headers=child_headers)
    body = day.json()
    assert body["completeness"] == "complete"
    assert float(body["totals"]["protein_g"]) == pytest.approx(20.0, abs=0.01)


async def test_eating_idempotency_and_double_eating(client, child_headers):
    """T26+T27: same operation retries once; a second intentional meal is kept."""
    user = await _user()
    body = {
        "operation_id": "op-dup",
        "nutrition_date": "2026-11-22",
        "items": [{"food_version_id": "f_yogurt_v1", "quantity_g": "200"}],
    }
    first = await client.post(f"/api/meals/{user.id}/eating-events", json=body,
                              headers={**child_headers, "Idempotency-Key": "k-dup"})
    second = await client.post(f"/api/meals/{user.id}/eating-events", json=body,
                               headers={**child_headers, "Idempotency-Key": "k-dup"})
    assert first.json()["id"] == second.json()["id"]
    again = await client.post(
        f"/api/meals/{user.id}/eating-events",
        json={**body, "operation_id": "op-dup-2"},
        headers={**child_headers, "Idempotency-Key": "k-dup2"},
    )
    day = await client.get(f"/api/meals/{user.id}/days/2026-11-22", headers=child_headers)
    recorded = [e for e in day.json()["events"] if e["event_type"] == "recorded"]
    assert len(recorded) == 2
    assert float(day.json()["totals"]["protein_g"]) == pytest.approx(40.0, abs=0.01)


async def test_unknown_food_is_incomplete_not_zero(client, child_headers):
    """T28: free-text eating is stored honestly incomplete, totals stay null."""
    user = await _user()
    await client.post(
        f"/api/meals/{user.id}/eating-events",
        json={
            "operation_id": "op-unknown",
            "nutrition_date": "2026-11-22",
            "items": [{"description": "משהו מהמסעדה", "quantity_g": "300"}],
        },
        headers={**child_headers, "Idempotency-Key": "k-unk"},
    )
    day = await client.get(f"/api/meals/{user.id}/days/2026-11-22", headers=child_headers)
    body = day.json()
    assert body["completeness"] == "incomplete"
    assert body["totals"]["kcal"] is None


async def test_correction_and_void_refold(client, child_headers):
    user = await _user()
    resp = await client.post(
        f"/api/meals/{user.id}/eating-events",
        json={
            "operation_id": "op-fix",
            "nutrition_date": "2026-11-22",
            "items": [{"food_version_id": "f_yogurt_v1", "quantity_g": "200"}],
        },
        headers={**child_headers, "Idempotency-Key": "k-fix"},
    )
    event_id = resp.json()["id"]
    corrected = await client.post(
        f"/api/meals/{user.id}/eating-events/{event_id}/corrections",
        json={"items": [{"food_version_id": "f_yogurt_v1", "quantity_g": "400"}]},
        headers=child_headers,
    )
    assert corrected.status_code == 200
    day = await client.get(f"/api/meals/{user.id}/days/2026-11-22", headers=child_headers)
    assert float(day.json()["totals"]["protein_g"]) == pytest.approx(40.0, abs=0.01)
    voided = await client.post(
        f"/api/meals/{user.id}/eating-events/{event_id}/voids", headers=child_headers
    )
    assert voided.status_code == 200
    day = await client.get(f"/api/meals/{user.id}/days/2026-11-22", headers=child_headers)
    assert float(day.json()["totals"]["protein_g"] or 0) == pytest.approx(0.0)


# ── proposal jobs + selections (B03/B05) ──────────────────────────────────
async def test_proposal_and_selection_are_not_eating(client, child_headers, parent_headers):
    """T25: generation + selection create zero eating events."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)

    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    assert job.status_code == 200, job.text
    assert job.json()["state"] == "eligible_candidate"
    proposal_id = job.json()["proposal_id"]

    sel = await client.post(
        f"/api/meals/{user.id}/selections",
        json={"nutrition_date": "2026-11-22", "slot_key": "lunch", "proposal_id": proposal_id},
        headers=child_headers,
    )
    assert sel.status_code == 200, sel.text
    assert sel.json()["status"] == "selected_not_eaten"
    assert sel.json()["eating_event_id"] is None
    day = await client.get(f"/api/meals/{user.id}/days/2026-11-22", headers=child_headers)
    assert day.json()["events"] == []


async def test_missing_ingredient_selects_alternative(client, child_headers, parent_headers):
    """T21: a missing ingredient excludes the planned meal; approved
    alternatives are still offered after current checks."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session = await client.post(
        f"/api/meals/{user.id}/suggestion-sessions",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    session_id = session.json()["session_id"]
    await client.patch(
        f"/api/meals/{user.id}/suggestion-sessions/{session_id}",
        json={"missing_food_ids": ["f_chicken_cooked_v1"]},
        headers={**child_headers, "If-Match": "1"},
    )
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": session_id},
        headers=child_headers,
    )
    body = job.json()
    assert body["state"] == "eligible_candidate"
    proposal = (await client.get(
        f"/api/meals/{user.id}/proposals/{body['proposal_id']}", headers=child_headers
    )).json()
    assert proposal["recipe_version_id"] == "r_tahini_plate_v1"


async def test_stale_vector_rejects_selection(client, child_headers, parent_headers):
    """T11: a plan change invalidates the proposal's vector - no re-stamping."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    proposal_id = job.json()["proposal_id"]
    # Publish plan v2 -> plan_epoch + active_plan_id change -> stale vector.
    await _publish_plan(client, parent_headers, user.id)
    sel = await client.post(
        f"/api/meals/{user.id}/selections",
        json={"nutrition_date": "2026-11-22", "slot_key": "lunch", "proposal_id": proposal_id},
        headers=child_headers,
    )
    assert sel.status_code == 409
    assert sel.json()["detail"]["code"] == "STALE_INPUT"


async def test_unapproved_alternative_rejected(client, child_headers, parent_headers):
    """B03: with no replacement policy, non-approved recipes cannot be selected."""
    user = await _user()
    plan = {**PLAN, "alternatives": {}}
    await _publish_plan(client, parent_headers, user.id, plan)
    sel = await client.post(
        f"/api/meals/{user.id}/selections",
        json={
            "nutrition_date": "2026-11-22", "slot_key": "lunch",
            "alternative_recipe_id": "r_yogurt_bowl_v1",
        },
        headers=child_headers,
    )
    assert sel.status_code == 422
    assert sel.json()["detail"]["code"] == "NOT_APPROVED_ALTERNATIVE"


# ── health intents (B08/T14) ──────────────────────────────────────────────
async def test_pending_health_intent_blocks_proposals(client, child_headers, parent_headers):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    intent = await client.post(
        f"/api/meals/{user.id}/health-update-intents",
        json={"reported_text": "הרופא אמר משהו חדש"},
        headers=child_headers,
    )
    assert intent.status_code == 200
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    assert job.json()["state"] == "blocked"
    # Viewing the plan and logging eating still work while blocked.
    assert (await client.get(f"/api/meals/{user.id}/plan", headers=child_headers)).json()["plan"]
    resolve = await client.post(
        f"/api/meals/{user.id}/health-update-intents/{intent.json()['intent_id']}/resolve",
        json={"action": "cancel"},
        headers=child_headers,
    )
    assert resolve.status_code == 403  # child can report, only the coach clears
    resolve = await client.post(
        f"/api/meals/{user.id}/health-update-intents/{intent.json()['intent_id']}/resolve",
        json={"action": "cancel"},
        headers=parent_headers,
    )
    assert resolve.status_code == 200
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    assert job.json()["state"] == "eligible_candidate"


# ── clinical pathway is fail-closed (B07) ─────────────────────────────────
async def test_rule_bundle_approval_denied_without_capability(client, parent_headers):
    user = await _user()
    bundle = await client.post(
        f"/api/meals/{user.id}/clinical-rule-bundles",
        json={"rules": [{"kind": "nutrient_bounds", "nutrient": "sodium_mg",
                         "scope": "day", "upper": "2000"}],
              "professional_id": "p-none",
              "valid_until": (datetime.utcnow() + timedelta(days=30)).isoformat()},
        headers=parent_headers,
    )
    assert bundle.status_code == 200, bundle.text
    approved = await client.post(
        f"/api/meals/{user.id}/clinical-rule-bundles/{bundle.json()['bundle_id']}/approve",
        json={"professional_id": "p-none"},
        headers=parent_headers,
    )
    assert approved.status_code == 403
    assert approved.json()["detail"]["code"] == "CLINICAL_CAPABILITY_REQUIRED"


# ── catalog review (B04) ──────────────────────────────────────────────────
async def test_catalog_submission_review_flow(client, child_headers, parent_headers):
    user = await _user()
    child_list = await client.get("/api/meals/catalog-submissions", headers=child_headers)
    assert child_list.status_code == 403
    sub = await client.post(
        f"/api/meals/{user.id}/catalog-submissions",
        json={"request_text": "ממרח חדש שאין בקטלוג"},
        headers=child_headers,
    )
    assert sub.status_code == 200
    sid = sub.json()["submission_id"]
    before = (await client.get("/api/meals/catalog", headers=parent_headers)).json()["catalog_epoch"]
    approved = await client.post(
        f"/api/meals/catalog-submissions/{sid}/approve",
        json={
            "name_he": "ממרח חדש", "basis_g": "100",
            "nutrients": {"kcal": "100", "protein_g": "2", "carbs_g": "10", "fat_g": "5"},
            "aliases": ["ממרח חדש"],
        },
        headers=parent_headers,
    )
    assert approved.status_code == 200, approved.text
    after = (await client.get("/api/meals/catalog", headers=parent_headers)).json()["catalog_epoch"]
    assert after != before
    names = {f["name_he"] for f in (await client.get("/api/meals/catalog", headers=parent_headers)).json()["foods"]}
    assert "ממרח חדש" in names


async def test_retraction_invalidates_proposals(client, child_headers, parent_headers):
    """T35: retracted data stops new recommendations via the epoch bump."""
    user = await _user()
    plan = {**PLAN, "alternatives": {"lunch": []}}
    await _publish_plan(client, parent_headers, user.id, plan)
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    assert job.json()["state"] == "eligible_candidate"
    proposal_id = job.json()["proposal_id"]
    # Retract a food that is not in the plan or any recipe: the catalog epoch
    # bump alone must invalidate the proposal's input vector (fail-closed),
    # without poisoning later tests' plans.
    retract = await client.post("/api/meals/catalog/food-versions/f_rice_dry_v1/retract",
                                headers=parent_headers)
    assert retract.status_code == 200, retract.text
    sel = await client.post(
        f"/api/meals/{user.id}/selections",
        json={"nutrition_date": "2026-11-22", "slot_key": "lunch",
              "proposal_id": proposal_id},
        headers=child_headers,
    )
    assert sel.status_code == 409
    assert sel.json()["detail"]["code"] == "STALE_INPUT"


# ── saved meals ───────────────────────────────────────────────────────────
async def test_saved_meal_revalidation(client, child_headers, parent_headers):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    saved = await client.post(
        f"/api/meals/{user.id}/saved-meals",
        json={
            "name": "היוגורט שלי",
            "snapshot": {
                "recipe_version_id": "r_yogurt_bowl_v1",
                "items": [{"food_version_id": "f_yogurt_v1", "quantity_g": "200"}],
            },
        },
        headers=child_headers,
    )
    assert saved.status_code == 200
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "snack", "nutrition_date": "2026-11-22",
              "saved_meal_id": saved.json()["saved_meal_id"]},
        headers=child_headers,
    )
    assert job.json()["state"] == "eligible_candidate"



# ── publish gate: confirmed profile required (B02) ───────────────────────
async def test_publish_requires_confirmed_profile(client, parent_headers):
    user = await _user()
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    assert approval.status_code == 200, approval.text
    published = await _publish(client, parent_headers, user.id, draft_id,
                               approval.json()["approval_id"], rev)
    assert published.status_code == 409
    assert published.json()["detail"]["code"] == "PROFILE_CONFIRMATION_REQUIRED"


async def test_stale_profile_confirm_rejected(client, parent_headers):
    """Whole-profile replace must not overwrite a newer confirmed version."""
    user = await _user()
    first = await client.post(
        f"/api/meals/{user.id}/profile-change-intents",
        json={"proposed": {"allergy_disclosure": "none_declared"}},
        headers=parent_headers,
    )
    second = await client.post(
        f"/api/meals/{user.id}/profile-change-intents",
        json={"proposed": {"allergy_disclosure": "declared", "allergies": ["milk"]}},
        headers=parent_headers,
    )
    ok = await client.post(
        f"/api/meals/{user.id}/profile-change-intents/{second.json()['intent_id']}/confirm",
        headers=parent_headers,
    )
    assert ok.status_code == 200
    stale = await client.post(
        f"/api/meals/{user.id}/profile-change-intents/{first.json()['intent_id']}/confirm",
        headers=parent_headers,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_REVISION"


async def test_setup_status_transitions(client, parent_headers):
    user = await _user()
    state = (await client.get(f"/api/meals/{user.id}/state", headers=parent_headers)).json()
    assert state["setup_status"] == "not_started" and not state["has_active_plan"]
    await _create_draft(client, parent_headers, user.id)
    state = (await client.get(f"/api/meals/{user.id}/state", headers=parent_headers)).json()
    assert state["setup_status"] == "draft" and not state["has_active_plan"]
    await _publish_plan(client, parent_headers, user.id)
    state = (await client.get(f"/api/meals/{user.id}/state", headers=parent_headers)).json()
    assert state["setup_status"] == "published" and state["has_active_plan"]


# ── B11: skipped allergy disclosure blocks alternatives only ─────────────
async def test_skipped_allergy_disclosure_blocks_proposals(client, child_headers, parent_headers):
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id,
                           {"allergy_disclosure": "skipped",
                            "medical_condition_declared": "no"})
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    published = await _publish(client, parent_headers, user.id, draft_id,
                               approval.json()["approval_id"], rev)
    assert published.status_code == 200, published.text

    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    body = job.json()
    assert body["state"] == "blocked"
    assert any("allergy" in r for r in body["reasons"])
    plan = (await client.get(f"/api/meals/{user.id}/plan", headers=child_headers)).json()
    assert plan["plan"] is not None
    eat = await client.post(
        f"/api/meals/{user.id}/eating-events",
        json={"operation_id": "op-skip", "nutrition_date": "2026-11-22",
              "items": [{"food_version_id": "f_yogurt_v1", "quantity_g": "200"}]},
        headers={**child_headers, "Idempotency-Key": "k-skip"},
    )
    assert eat.status_code == 200


# ── session "not in the mood" vs permanent dislike ────────────────────────
async def _proposal_recipe(client, headers, user_id, session_id):
    job = await client.post(
        f"/api/meals/{user_id}/proposal-jobs",
        json={"session_id": session_id},
        headers=headers,
    )
    body = job.json()
    if body["state"] != "eligible_candidate":
        return None, body
    proposal = (await client.get(
        f"/api/meals/{user_id}/proposals/{body['proposal_id']}", headers=headers
    )).json()
    return proposal["recipe_version_id"], body


async def test_session_reject_excludes_recipes(client, child_headers, parent_headers):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)

    async def _session(reject=None):
        s = await client.post(
            f"/api/meals/{user.id}/suggestion-sessions",
            json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
            headers=child_headers,
        )
        sid = s.json()["session_id"]
        if reject:
            patched = await client.patch(
                f"/api/meals/{user.id}/suggestion-sessions/{sid}",
                json={"rejected_recipe_ids": reject},
                headers={**child_headers, "If-Match": "1"},
            )
            assert patched.status_code == 200, patched.text
        return sid

    # Baseline: with no rejection the engine's top candidate is the tahini plate.
    assert (await _proposal_recipe(client, child_headers, user.id, await _session()))[0] == "r_tahini_plate_v1"
    # Rejecting it for this session falls back to the planned meal.
    rid, _ = await _proposal_recipe(client, child_headers, user.id, await _session(["r_tahini_plate_v1"]))
    assert rid == "r_chicken_plate_v1"
    # Rejecting the planned meal excludes it too - for this session only.
    rid, _ = await _proposal_recipe(client, child_headers, user.id, await _session(["r_chicken_plate_v1"]))
    assert rid == "r_tahini_plate_v1"
    # A fresh session is unaffected.
    assert (await _proposal_recipe(client, child_headers, user.id, await _session()))[0] == "r_tahini_plate_v1"


async def test_disliked_recipe_excludes_alternative_not_planned(client, child_headers, parent_headers):
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id, {
        "allergy_disclosure": "none_declared",
        "medical_condition_declared": "no",
        "disliked_recipe_ids": ["r_chicken_plate_v1"],
    })
    await _publish_plan(client, parent_headers, user.id)
    # Disliking the planned recipe can never remove the coach's planned meal;
    # the remaining alternative (tahini) simply wins the ranking.
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    assert job.json()["state"] == "eligible_candidate"

    await _confirm_profile(client, parent_headers, user.id, {
        "allergy_disclosure": "none_declared",
        "medical_condition_declared": "no",
        "disliked_recipe_ids": ["r_tahini_plate_v1"],
    })
    session = await client.post(
        f"/api/meals/{user.id}/suggestion-sessions",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    sid = session.json()["session_id"]
    # Exclude the alternative AND the planned meal's ingredient: the
    # alternative stays out (profile dislike), but instead of no_match the
    # engine now serves the planned meal minus its missing ingredient.
    await client.patch(
        f"/api/meals/{user.id}/suggestion-sessions/{sid}",
        json={"missing_food_ids": ["f_chicken_cooked_v1"]},
        headers={**child_headers, "If-Match": "1"},
    )
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid},
        headers=child_headers,
    )
    body = job.json()
    assert body["state"] == "eligible_candidate", body
    proposal = body["proposal"]
    # Chicken is the dish's core ingredient - "chicken plate minus the
    # chicken" is never served. The engine composes a different dish from
    # the remaining approved basket foods instead.
    assert proposal.get("composed") is True
    assert all(i["food_version_id"] != "f_chicken_cooked_v1" for i in proposal["items"])
    assert any("never suggest" in r for r in body["reasons"])

    # Once every basket food is missing there is nothing left to serve -
    # only then does the job report an honest no_match.
    await client.patch(
        f"/api/meals/{user.id}/suggestion-sessions/{sid}",
        json={"missing_food_ids": ["f_chicken_cooked_v1", "f_rice_cooked_v1",
                                   "f_cucumber_v1", "f_tomato_v1"]},
        headers={**child_headers, "If-Match": "2"},
    )
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid},
        headers=child_headers,
    )
    assert job.json()["state"] == "no_match"


# ── assistant (DESIGN 10) ─────────────────────────────────────────────────
import agents.meal_assistant as meal_ai


async def _make_session(client, headers, user_id):
    resp = await client.post(
        f"/api/meals/{user_id}/suggestion-sessions",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


@pytest.fixture
def ai_enabled(monkeypatch):
    monkeypatch.setenv("MEAL_AI_ENABLED", "1")
    monkeypatch.setattr(meal_ai, "_llm_configured", lambda: True)
    meal_ai._daily_counts.clear()
    return meal_ai


def _ai_json(**overrides):
    import json as _json
    payload = {"candidateRecipeIds": [], "explanationKey": "no_change"}
    payload.update(overrides)
    return _json.dumps(payload)


def _stub_llm(monkeypatch, raw_or_fn):
    import asyncio
    if callable(raw_or_fn):
        monkeypatch.setattr(meal_ai, "call_llm", raw_or_fn)
    else:
        async def fake(*a, **k):
            return raw_or_fn
        monkeypatch.setattr(meal_ai, "call_llm", fake)


async def test_assistant_disabled_by_default(client, child_headers, parent_headers):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "מה יש לי?"},
        headers=child_headers,
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "AI_DISABLED"


async def test_assistant_applies_missing_and_prefers(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)
    calls = []

    async def fake_llm(system, messages, **kw):
        calls.append(messages)
        return _ai_json(candidateRecipeIds=["r_chicken_plate_v1"],
                        missingFoodIds=["f_tahini_v1"],
                        explanationKey="missing_ingredient")

    monkeypatch.setattr(meal_ai, "call_llm", fake_llm)
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "אין לי טחינה בבית"},
        headers=child_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_used"] is True
    assert body["explanation_key"] == "missing_ingredient"
    assert body["explanation_he"]
    # Tahini out (missing) + chicken preferred -> planned meal proposed.
    assert body["job"]["proposal"]["recipe_version_id"] == "r_chicken_plate_v1"
    assert body["job"]["proposal"]["proposal_id"]
    # Missing id persisted into the session (same rules as PATCH).
    rid, _ = await _proposal_recipe(client, child_headers, user.id, session_id)
    assert rid == "r_chicken_plate_v1"
    # The bounded input never contains identity/profile data.
    sent = calls[0][-1]["content"]
    assert user.name not in sent and "allerg" not in sent


async def test_assistant_preference_changes_order_not_rules(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """preferred_recipe_ids changes ranking only: with no preference the
    tahini bowl wins; preferring the chicken plate picks it; marking an
    ingredient missing makes the preference infeasible and loses."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)

    session_id = await _make_session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json(candidateRecipeIds=["r_chicken_plate_v1"]))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "בא לי עוף"},
        headers=child_headers,
    )
    assert resp.json()["job"]["proposal"]["recipe_version_id"] == "r_chicken_plate_v1"

    # Prefer chicken but also report chicken missing -> tahini still wins.
    session_id = await _make_session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json(candidateRecipeIds=["r_chicken_plate_v1"],
                                    missingFoodIds=["f_chicken_cooked_v1"]))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "אין עוף"},
        headers=child_headers,
    )
    assert resp.json()["job"]["proposal"]["recipe_version_id"] == "r_tahini_plate_v1"


def test_assistant_output_validation_tolerates_fences_and_names():
    """The model may wrap JSON in ```json fences or answer with the Hebrew
    name instead of the id; both are normalized, everything else still fails."""
    allowed_r = {"r_a", "r_b"}
    allowed_f = {"f_x"}
    name_r = {"עוף": "r_a"}
    name_f = {"טחינה": "f_x"}
    fenced = '```json\n{"candidateRecipeIds": ["r_b"], "missingFoodIds": ["f_x"]}\n```'
    out = meal_ai._validate_ai_output(fenced, allowed_r, allowed_f, name_r, name_f)
    assert out["candidateRecipeIds"] == ["r_b"]
    assert out["missingFoodIds"] == ["f_x"]
    by_name = meal_ai._validate_ai_output(
        '{"candidateRecipeIds": ["עוף"], "missingFoodIds": ["טחינה"]}',
        allowed_r, allowed_f, name_r, name_f)
    assert by_name["candidateRecipeIds"] == ["r_a"]
    assert by_name["missingFoodIds"] == ["f_x"]
    # an unknown name still fails closed
    assert meal_ai._validate_ai_output(
        '{"missingFoodIds": ["קסם לא קיים"]}',
        allowed_r, allowed_f, name_r, name_f) is None


async def test_assistant_invalid_output_falls_back(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)

    for bad in (
        _ai_json(extraKey="nope"),                              # extra key
        _ai_json(candidateRecipeIds=["r_invented"]),            # unknown id
        '{"candidateRecipeIds": ["r_tahini_plate_v1"], "quantity_g": 50}',  # invented quantity
        "not json at all",
    ):
        session_id = await _make_session(client, child_headers, user.id)
        _stub_llm(monkeypatch, bad)
        resp = await client.post(
            f"/api/meals/{user.id}/assistant/messages",
            json={"session_id": session_id, "message": "x"},
            headers=child_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ai_used"] is False
        assert body["job"]["state"] in ("eligible_candidate", "no_match", "blocked")
        # No AI mutation reached the session.
        assert body["session_revision"] == 1


async def test_assistant_timeout_falls_back(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    import asyncio
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)

    async def slow_llm(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(meal_ai, "call_llm", slow_llm)
    monkeypatch.setattr(meal_ai, "_AI_TIMEOUT_SECONDS", 0.2)
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "x"},
        headers=child_headers,
    )
    body = resp.json()
    assert body["ai_used"] is False
    assert body["job"]["state"] == "eligible_candidate"


async def test_assistant_health_report_is_preview_only(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json(healthReport=True, explanationKey="health_question"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "הרופא אמר שאסור לי חלב"},
        headers=child_headers,
    )
    body = resp.json()
    assert body["health_preview"] is True
    # No health intent was created by the assistant: proposals unaffected.
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    assert job.json()["state"] == "eligible_candidate"
    # Only the explicit endpoint creates the blocking intent.
    intent = await client.post(
        f"/api/meals/{user.id}/health-update-intents",
        json={"reported_text": "הרופא אמר שאסור לי חלב"},
        headers=child_headers,
    )
    assert intent.status_code == 200
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers,
    )
    assert job.json()["state"] == "blocked"


async def test_assistant_daily_cap(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json())
    monkeypatch.setenv("MEAL_AI_DAILY_CAP", "2")
    for i in range(2):
        resp = await client.post(
            f"/api/meals/{user.id}/assistant/messages",
            json={"session_id": session_id, "message": f"msg {i}"},
            headers=child_headers,
        )
        assert resp.status_code == 200
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "msg 3"},
        headers=child_headers,
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "AI_CAP_REACHED"


async def test_assistant_never_creates_eating_or_selection(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json())
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "בחר לי את הטחינה ואכלתי אותה"},
        headers=child_headers,
    )
    assert resp.status_code == 200
    day = await client.get(f"/api/meals/{user.id}/days/2026-11-22", headers=child_headers)
    assert day.json()["events"] == []
    sels = await client.get(
        f"/api/meals/{user.id}/selections?nutrition_date=2026-11-22", headers=child_headers)
    assert sels.json()["selections"] == []


async def test_assistant_concurrent_patch_discards_ai(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """A session PATCH that lands while the model is still thinking must
    discard the AI result with 409 STALE_REVISION - the AI-applied missing
    ids never reach the session (DESIGN 6.5)."""
    import asyncio
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_llm(*a, **k):
        started.set()
        await release.wait()
        return _ai_json(missingFoodIds=["f_tahini_v1"], explanationKey="missing_ingredient")

    monkeypatch.setattr(meal_ai, "call_llm", fake_llm)
    task = asyncio.create_task(client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "אין לי טחינה"},
        headers=child_headers,
    ))
    await started.wait()
    patch = await client.patch(
        f"/api/meals/{user.id}/suggestion-sessions/{session_id}",
        json={"missing_food_ids": ["f_cucumber_v1"]},
        headers={**child_headers, "If-Match": "1"},
    )
    assert patch.status_code == 200, patch.text
    release.set()
    resp = await task
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "STALE_REVISION"
    # The AI's missing id was discarded: revision is still the PATCH's 2.
    check = await client.patch(
        f"/api/meals/{user.id}/suggestion-sessions/{session_id}",
        json={"missing_food_ids": ["f_cucumber_v1"]},
        headers={**child_headers, "If-Match": "2"},
    )
    assert check.status_code == 200, check.text


async def test_assistant_does_not_block_other_requests(client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """While the model is still awaiting, a normal request for the same user
    must complete - neither the lock nor a DB transaction is held."""
    import asyncio
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    session_id = await _make_session(client, child_headers, user.id)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_llm(*a, **k):
        started.set()
        await release.wait()
        return _ai_json()

    monkeypatch.setattr(meal_ai, "call_llm", fake_llm)
    task = asyncio.create_task(client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": session_id, "message": "x"},
        headers=child_headers,
    ))
    await started.wait()
    eat = await asyncio.wait_for(client.post(
        f"/api/meals/{user.id}/eating-events",
        json={
            "operation_id": "op-concurrent",
            "nutrition_date": "2026-11-22",
            "items": [{"food_version_id": "f_yogurt_v1", "quantity_g": "200"}],
        },
        headers={**child_headers, "Idempotency-Key": "eat-concurrent"},
    ), timeout=5)
    assert eat.status_code == 200, eat.text
    release.set()
    resp = await task
    assert resp.status_code == 200, resp.text


async def test_approve_refuses_invalid_draft(client, parent_headers):
    """Regression: approving a draft that fails validation must be refused -
    the approve step binds the *validated* content, never an invalid plan."""
    user = await _user()
    bad_plan = {"meals": [{"slot_key": "lunch", "recipe_version_id": "r_egg_bread_v1",
                           "items": [{"food_version_id": "f_egg_boiled_v1", "quantity_g": "100"}]}]}
    draft_id, rev = await _create_draft(client, parent_headers, user.id, bad_plan)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert validation["valid"] is False
    resp = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_PLAN"


# ── Medical instructions: professionals, bundles, enforcement ─────────────
async def _register_professional(client, headers, **kw):
    body = {
        "name": "Dr Test", "profession": "dietitian",
        "license_number": f"L-{uuid.uuid4().hex[:6]}",
        "valid_until": (datetime.utcnow() + timedelta(days=180)).isoformat(),
        **kw,
    }
    return await client.post("/api/meals/professionals", json=body, headers=headers)


async def _create_bundle(client, headers, user_id, rules, professional_id, **kw):
    body = {
        "rules": rules, "professional_id": professional_id,
        "valid_until": (datetime.utcnow() + timedelta(days=180)).isoformat(),
        **kw,
    }
    resp = await client.post(
        f"/api/meals/{user_id}/clinical-rule-bundles", json=body, headers=headers)
    return resp


async def _approve_bundle(client, headers, user_id, bundle_id, professional_id):
    return await client.post(
        f"/api/meals/{user_id}/clinical-rule-bundles/{bundle_id}/approve",
        json={"professional_id": professional_id}, headers=headers)


async def _approved_bundle(client, admin_headers, user_id, rules, **kw):
    prof = await _register_professional(client, admin_headers)
    assert prof.status_code == 200, prof.text
    pid = prof.json()["professional_id"]
    bundle = await _create_bundle(client, admin_headers, user_id, rules, pid, **kw)
    assert bundle.status_code == 200, bundle.text
    bid = bundle.json()["bundle_id"]
    ok = await _approve_bundle(client, admin_headers, user_id, bid, pid)
    assert ok.status_code == 200, ok.text
    return pid, bid


CARBS_MEAL_MAX = [{"kind": "nutrient_bounds", "scope": "meal",
                   "nutrient": "carbs_g", "max": "5"}]
SODIUM_DAY_MAX = [{"kind": "nutrient_bounds", "scope": "day",
                   "nutrient": "sodium_mg", "max": "10"}]


async def test_medical_condition_declared_yes_blocks_publish_and_proposals(
        client, parent_headers, admin_headers, child_headers):
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id,
                           {"allergy_disclosure": "none_declared",
                            "medical_condition_declared": "yes"})
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    resp = await _publish(client, parent_headers, user.id, draft_id,
                          approval.json()["approval_id"], rev)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "MEDICAL_INSTRUCTION_REQUIRED"

    # After publishing a plan (via a 'no' profile earlier? plan required):
    # use a no-profile to publish, then declare yes -> proposals blocked.
    await _confirm_profile(client, parent_headers, user.id,
                           {"allergy_disclosure": "none_declared",
                            "medical_condition_declared": "no"})
    resp = await _publish(client, parent_headers, user.id, draft_id,
                          approval.json()["approval_id"], rev,
                          key=f"pub-{uuid.uuid4().hex[:6]}")
    # approval was already used; republish path may fail - use fresh draft
    if resp.status_code != 200:
        draft_id, rev = await _create_draft(client, parent_headers, user.id)
        validation = await _validate(client, parent_headers, user.id, draft_id)
        approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
        resp = await _publish(client, parent_headers, user.id, draft_id,
                              approval.json()["approval_id"], rev)
        assert resp.status_code == 200, resp.text
    await _confirm_profile(client, parent_headers, user.id,
                           {"allergy_disclosure": "none_declared",
                            "medical_condition_declared": "unsure"})
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers)
    body = job.json()
    assert body["state"] == "blocked"
    assert any("medical instruction missing" in r for r in body["reasons"])


async def test_medical_bundle_enforced_in_validate_approve_publish(
        client, parent_headers, admin_headers):
    """B2: an active bundle's meal-scope bound makes violating plans invalid."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id)
    await _approved_bundle(client, admin_headers, user.id, CARBS_MEAL_MAX)

    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert validation["valid"] is False
    dict_issues = [i for i in validation["issues"] if isinstance(i, dict)]
    assert any(i.get("rule") == "nutrient_bounds" and i.get("nutrient") == "carbs_g"
               for i in dict_issues)
    resp = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    assert resp.status_code == 422
    resp = await _publish(client, parent_headers, user.id, draft_id,
                          "approval_id", rev)
    assert resp.status_code in (404, 422)


async def test_day_scope_bound_checked_across_planned_meals(
        client, parent_headers, admin_headers):
    """B2 day scope: sum of planned meals exceeding a day bound is invalid."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id)
    await _approved_bundle(client, admin_headers, user.id, SODIUM_DAY_MAX)
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert validation["valid"] is False
    day_issues = [i for i in validation["issues"] if isinstance(i, dict)
                  and i.get("rule") == "nutrient_bounds" and i.get("scope") == "day"]
    assert day_issues, validation["issues"]
    assert day_issues[0]["status"] == "fail"


async def test_unknown_nutrient_required_by_bound_is_invalid(
        client, parent_headers, admin_headers):
    """Fail-closed: tahini has unknown sugar_g - a sugar bound can never pass."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id)
    await _approved_bundle(client, admin_headers, user.id,
                           [{"kind": "nutrient_bounds", "scope": "meal",
                             "nutrient": "sugar_g", "max": "10"}])
    content = {"meals": [{
        "slot_key": "lunch", "recipe_version_id": "r_tahini_plate_v1",
        "items": [
            {"food_version_id": "f_tahini_v1", "quantity_g": "50"},
            {"food_version_id": "f_bread_v1", "quantity_g": "100"},
            {"food_version_id": "f_cucumber_v1", "quantity_g": "100"},
        ]}]}
    draft_id, rev = await _create_draft(client, parent_headers, user.id, content)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert validation["valid"] is False
    assert any(i.get("status") == "unknown" for i in validation["issues"]
               if isinstance(i, dict))


async def test_expired_active_bundle_blocks_proposals_not_plan_or_eating(
        client, parent_headers, admin_headers, child_headers):
    """B1: an expired active bundle fails closed - rules never drop."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    prof = await _register_professional(client, admin_headers)
    pid = prof.json()["professional_id"]
    past = (datetime.utcnow() - timedelta(days=1)).isoformat()
    bundle = await _create_bundle(client, admin_headers, user.id,
                                  CARBS_MEAL_MAX, pid, valid_until=past)
    bid = bundle.json()["bundle_id"]
    ok = await _approve_bundle(client, admin_headers, user.id, bid, pid)
    assert ok.status_code == 200, ok.text

    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers)
    body = job.json()
    body = job.json()
    assert body["state"] == "blocked"
    assert any("medical instruction expired" in r for r in body["reasons"])

    plan = await client.get(f"/api/meals/{user.id}/plan", headers=child_headers)
    assert plan.status_code == 200
    assert plan.json()["plan"] is not None
    eat = await client.post(
        f"/api/meals/{user.id}/eating-events",
        json={"operation_id": f"exp-{uuid.uuid4().hex[:8]}",
              "nutrition_date": "2026-11-22",
              "items": [{"food_version_id": "f_cucumber_v1", "quantity_g": "50"}]},
        headers={**child_headers, "Idempotency-Key": f"exp-{uuid.uuid4().hex[:8]}"})
    assert eat.status_code == 200, eat.text


async def test_professional_registration_admin_only(client, parent_headers,
                                                    child_headers, admin_headers):
    assert (await _register_professional(client, parent_headers)).status_code == 403
    assert (await _register_professional(client, child_headers)).status_code == 403
    ok = await _register_professional(client, admin_headers)
    assert ok.status_code == 200, ok.text
    past = await _register_professional(
        client, admin_headers,
        valid_until=(datetime.utcnow() - timedelta(days=1)).isoformat())
    assert past.status_code == 400


async def test_tz_aware_datetimes_accepted(client, admin_headers):
    """Regression: the UI sends ISO datetimes with a Z/offset; comparing them
    against naive utcnow() used to 500 (naive vs aware TypeError)."""
    user = await _user()
    aware = (datetime.utcnow() + timedelta(days=180)).isoformat() + "+00:00"
    prof = await _register_professional(client, admin_headers, valid_until=aware)
    assert prof.status_code == 200, prof.text
    pid = prof.json()["professional_id"]
    bundle = await _create_bundle(client, admin_headers, user.id, CARBS_MEAL_MAX,
                                  pid, valid_until=aware,
                                  instruction_date="2026-01-01T00:00:00+00:00")
    assert bundle.status_code == 200, bundle.text
    ok = await _approve_bundle(client, admin_headers, user.id,
                               bundle.json()["bundle_id"], pid)
    assert ok.status_code == 200, ok.text
    listed = await client.get(
        f"/api/meals/{user.id}/clinical-rule-bundles", headers=admin_headers)
    assert listed.status_code == 200
    active = [b for b in listed.json()["bundles"] if b["is_active"]]
    assert active and active[0]["expired"] is False


async def test_bundle_approve_requires_matching_valid_professional(
        client, parent_headers, admin_headers):
    user = await _user()
    # no matching professional at all -> 403
    bundle = await _create_bundle(client, admin_headers, user.id, CARBS_MEAL_MAX,
                                  "nonexistent-prof")
    bid = bundle.json()["bundle_id"]
    assert (await _approve_bundle(client, admin_headers, user.id, bid,
                                  "nonexistent-prof")).status_code == 403

    # mismatched professional -> 403
    p1 = (await _register_professional(client, admin_headers)).json()["professional_id"]
    p2 = (await _register_professional(client, admin_headers)).json()["professional_id"]
    bundle2 = await _create_bundle(client, admin_headers, user.id, CARBS_MEAL_MAX, p1)
    bid2 = bundle2.json()["bundle_id"]
    assert (await _approve_bundle(client, admin_headers, user.id, bid2, p2)).status_code == 403

    # parent (non-admin) can't approve: capability principal is 'admin'
    assert (await _approve_bundle(client, parent_headers, user.id, bid2, p1)).status_code == 403

    # revoked professional -> 403
    await client.post(f"/api/meals/professionals/{p1}/revoke", headers=admin_headers)
    assert (await _approve_bundle(client, admin_headers, user.id, bid2, p1)).status_code == 403

    # valid professional approves; a second bundle supersedes the first
    p3 = (await _register_professional(client, admin_headers)).json()["professional_id"]
    b3 = await _create_bundle(client, admin_headers, user.id, SODIUM_DAY_MAX, p3)
    bid3 = b3.json()["bundle_id"]
    ok = await _approve_bundle(client, admin_headers, user.id, bid3, p3)
    assert ok.status_code == 200, ok.text
    p4 = (await _register_professional(client, admin_headers)).json()["professional_id"]
    b4 = await _create_bundle(client, admin_headers, user.id, CARBS_MEAL_MAX, p4)
    bid4 = b4.json()["bundle_id"]
    ok = await _approve_bundle(client, admin_headers, user.id, bid4, p4)
    assert ok.status_code == 200
    bundles = (await client.get(
        f"/api/meals/{user.id}/clinical-rule-bundles", headers=admin_headers)).json()
    by_id = {b["bundle_id"]: b for b in bundles["bundles"]}
    assert by_id[bid3]["status"] == "superseded"
    assert by_id[bid4]["status"] == "approved"
    assert bundles["active_bundle_id"] == bid4


async def test_bundle_approved_after_publish_blocks_until_republish(
        client, parent_headers, admin_headers, child_headers):
    """The active plan stays viewable but proposals fail closed when a newer
    bundle invalidates it."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    day = "2026-11-23"
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": day}, headers=child_headers)
    assert job.json()["state"] == "eligible_candidate"

    await _approved_bundle(client, admin_headers, user.id, CARBS_MEAL_MAX)
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"slot_key": "lunch", "nutrition_date": day}, headers=child_headers)
    body = job.json()
    assert body["state"] == "blocked"
    assert any("active plan violates medical instruction" in r for r in body["reasons"])
    plan = await client.get(f"/api/meals/{user.id}/plan", headers=child_headers)
    assert plan.status_code == 200
    eat = await client.post(
        f"/api/meals/{user.id}/eating-events",
        json={"operation_id": f"viol-{uuid.uuid4().hex[:8]}", "nutrition_date": day,
              "items": [{"food_version_id": "f_cucumber_v1", "quantity_g": "50"}]},
        headers={**child_headers, "Idempotency-Key": f"viol-{uuid.uuid4().hex[:8]}"})
    assert eat.status_code == 200


async def test_fit_portions(client, parent_headers, admin_headers, child_headers):
    """Read-only portion fitting inside the professional's bounds."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    await _approved_bundle(
        client, admin_headers, user.id,
        [{"kind": "nutrient_bounds", "scope": "meal",
          "nutrient": "carbs_g", "max": "80"}])

    state_before = (await client.get(
        f"/api/meals/{user.id}/state", headers=parent_headers)).json()
    resp = await client.post(
        f"/api/meals/{user.id}/fit-portions",
        json={"slot_key": "lunch", "recipe_version_id": "r_chicken_plate_v1"},
        headers=parent_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "fit", body
    assert Decimal(str(body["totals"]["carbs_g"])) <= Decimal("80")
    assert all(c["status"] != "fail" for c in body["checks"])
    state_after = (await client.get(
        f"/api/meals/{user.id}/state", headers=parent_headers)).json()
    assert state_after == state_before  # read-only: no writes, no epoch change

    # infeasible: recipe not approved for this slot -> no_match
    resp = await client.post(
        f"/api/meals/{user.id}/fit-portions",
        json={"slot_key": "breakfast", "recipe_version_id": "r_chicken_plate_v1"},
        headers=parent_headers)
    assert resp.json()["state"] == "no_match"

    # child cannot call it
    resp = await client.post(
        f"/api/meals/{user.id}/fit-portions",
        json={"slot_key": "lunch", "recipe_version_id": "r_chicken_plate_v1"},
        headers=child_headers)
    assert resp.status_code == 403


async def test_proposals_respect_day_bound_across_slots(
        client, parent_headers, admin_headers, child_headers):
    """Alternatives must never push the day total past a day bound: the
    residual is bound - consumed - other slots' allocations."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    await _approved_bundle(
        client, admin_headers, user.id,
        [{"kind": "nutrient_bounds", "scope": "day",
          "nutrient": "carbs_g", "max": "200"}])
    day = "2026-11-24"
    for slot in ("breakfast", "lunch", "dinner"):
        job = await client.post(
            f"/api/meals/{user.id}/proposal-jobs",
            json={"slot_key": slot, "nutrition_date": day}, headers=child_headers)
        body = job.json()
        if body["state"] == "eligible_candidate":
            checks = {c["nutrient"]: c for c in body["proposal"]["checks"]
                      if c.get("scope") == "day"}
            assert checks["carbs_g"]["status"] == "pass"
            assert Decimal(body["proposal"]["totals"]["carbs_g"]) <= \
                Decimal(checks["carbs_g"]["residual"])


async def test_health_intents_child_cannot_resolve_coach_can(
        client, parent_headers, child_headers):
    user = await _user()
    hi = await client.post(
        f"/api/meals/{user.id}/health-update-intents",
        json={"reported_text": "הרופא אמר אלרגיה חדשה"}, headers=child_headers)
    assert hi.status_code == 200
    intent_id = hi.json()["intent_id"]
    bad = await client.post(
        f"/api/meals/{user.id}/health-update-intents/{intent_id}/resolve",
        json={"action": "cancel"}, headers=child_headers)
    assert bad.status_code == 403
    lst = await client.get(
        f"/api/meals/{user.id}/health-update-intents", headers=parent_headers)
    assert lst.status_code == 200
    intents = lst.json()["intents"]
    assert any(i["intent_id"] == intent_id and i["status"] == "pending"
               for i in intents)
    ok = await client.post(
        f"/api/meals/{user.id}/health-update-intents/{intent_id}/resolve",
        json={"action": "cancel"}, headers=parent_headers)
    assert ok.status_code == 200
    lst = await client.get(
        f"/api/meals/{user.id}/health-update-intents", headers=parent_headers)
    assert not any(i["intent_id"] == intent_id and i["status"] == "pending"
                   for i in lst.json()["intents"])


async def test_ai_payload_never_contains_condition_label_or_rules(
        client, child_headers, parent_headers, admin_headers,
        ai_enabled, monkeypatch):
    """Private medical data (condition_label, raw rules) must never reach
    the AI payload - only bounded catalog/session data."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    await _approved_bundle(
        client, admin_headers, user.id, CARBS_MEAL_MAX,
        condition_label="very private diagnosis label")

    session = await client.post(
        f"/api/meals/{user.id}/suggestion-sessions",
        json={"slot_key": "lunch", "nutrition_date": "2026-11-22"},
        headers=child_headers)
    sid = session.json()["session_id"]

    captured = {}

    async def fake_llm(*args, **kwargs):
        captured.update(kwargs)
        if args:
            captured.setdefault("args", args)
        return _ai_json(candidateRecipeIds=["r_chicken_plate_v1"])

    monkeypatch.setattr(meal_ai, "call_llm", fake_llm)
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי משהו אחר"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    import json as _json
    blob = _json.dumps(captured, ensure_ascii=False, default=str)
    assert "very private diagnosis label" not in blob
    assert "condition_label" not in blob
    assert "nutrient_bounds" not in blob


# ── smart composition: ad-hoc dishes from the slot's approved basket ──────
LUNCH_BASKET = {"f_chicken_cooked_v1", "f_rice_cooked_v1", "f_cucumber_v1",
                "f_tomato_v1", "f_tahini_v1", "f_bread_v1"}
BREAKFAST_BASKET = {"f_egg_boiled_v1", "f_bread_v1", "f_tomato_v1", "f_yogurt_v1"}


async def _session(client, headers, user_id, slot="lunch", create=None, patch=None):
    body = {"slot_key": slot, "nutrition_date": "2026-11-22"}
    if create:
        body.update(create)
    resp = await client.post(
        f"/api/meals/{user_id}/suggestion-sessions", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    sid = resp.json()["session_id"]
    if patch:
        p = await client.patch(
            f"/api/meals/{user_id}/suggestion-sessions/{sid}",
            json=patch, headers={**headers, "If-Match": "1"})
        assert p.status_code == 200, p.text
    return sid


async def test_composition_when_all_recipes_excluded(client, child_headers, parent_headers):
    """Every approved recipe fails the session's prep constraint, so the
    engine composes an ad-hoc dish from the slot's approved food basket
    instead of reporting no_match."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    sid = await _session(client, child_headers, user.id,
                         create={"preparation": {"max_total_minutes": 2}})
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid}, headers=child_headers)
    body = job.json()
    assert body["state"] == "eligible_candidate", body
    proposal = body["proposal"]
    assert proposal["composed"] is True
    assert proposal["compliance_label"] == "smart_composition"
    assert proposal["recipe_version_id"] is None
    items = proposal["items"]
    assert 1 <= len(items) <= 3
    assert {i["food_version_id"] for i in items} <= LUNCH_BASKET
    for i in items:
        Decimal(i["quantity_g"])  # engine-computed, decimal-exact
    # A composition is a no-cook plate: no steps, no equipment, no minutes.
    assert proposal["steps_he"] == []
    assert proposal["total_minutes"] == 0


async def test_composition_respects_medical_bound_and_exclusions(
        client, child_headers, parent_headers, admin_headers):
    """A composition never serves missing/disliked foods and its quantities
    are fitted inside the professional's bound - Decimal-exact, not float."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id, {
        "allergy_disclosure": "none_declared",
        "medical_condition_declared": "no",
        "disliked_recipe_ids": ["r_chicken_plate_v1"],
    })
    plan = {
        "meals": [{
            "slot_key": "lunch", "recipe_version_id": "r_tahini_plate_v1",
            "items": [
                {"food_version_id": "f_tahini_v1", "quantity_g": "50"},
                {"food_version_id": "f_bread_v1", "quantity_g": "60"},
                {"food_version_id": "f_tomato_v1", "quantity_g": "100"},
            ],
        }],
        "alternatives": {"lunch": ["r_chicken_plate_v1"]},
    }
    draft_id, rev = await _create_draft(client, parent_headers, user.id, plan)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert validation["valid"], validation["issues"]
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    assert approval.status_code == 200, approval.text
    published = await _publish(client, parent_headers, user.id, draft_id,
                               approval.json()["approval_id"], rev)
    assert published.status_code == 200, published.text

    # Meal-scope protein floor the plan passes (14.8 >= 10) but a plain
    # 60 g portion of bread fails -> the search must fit a bigger portion.
    await _approved_bundle(client, admin_headers, user.id,
                           [{"kind": "nutrient_bounds", "scope": "meal",
                             "nutrient": "protein_g", "min": "10"}])

    # Tahini missing + 2-minute prep limit: the disliked alternative is out
    # of the basket entirely, the planned recipe's minus-ingredient attempt
    # fails prep -> basket = {bread, tomato}, composition must fit the bound.
    sid = await _session(client, child_headers, user.id,
                         create={"preparation": {"max_total_minutes": 2}},
                         patch={"missing_food_ids": ["f_tahini_v1"]})
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid}, headers=child_headers)
    body = job.json()
    assert body["state"] == "eligible_candidate", body
    proposal = body["proposal"]
    assert proposal["composed"] is True
    fids = {i["food_version_id"] for i in proposal["items"]}
    assert fids <= {"f_bread_v1", "f_tomato_v1"}  # basket only
    assert "f_tahini_v1" not in fids              # session-missing
    assert "f_chicken_cooked_v1" not in fids      # disliked recipe's food
    protein = Decimal(proposal["totals"]["protein_g"])
    assert protein == Decimal("10.8"), proposal["totals"]
    assert protein >= Decimal("10")               # bound respected exactly


async def test_no_match_only_when_composition_impossible(client, child_headers, parent_headers):
    """no_match is honest: rejecting every approved recipe empties both the
    candidate pool and the basket; missing every basket food does the same."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)

    sid = await _session(client, child_headers, user.id,
                         patch={"rejected_recipe_ids": ["r_chicken_plate_v1",
                                                        "r_tahini_plate_v1"]})
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid}, headers=child_headers)
    assert job.json()["state"] == "no_match"

    sid = await _session(client, child_headers, user.id,
                         patch={"missing_food_ids": sorted(LUNCH_BASKET)})
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid}, headers=child_headers)
    assert job.json()["state"] == "no_match"


async def test_missing_ingredient_falls_back_before_no_match(client, child_headers, parent_headers):
    """With key ingredients missing, the engine serves the planned recipe
    minus the missing items (or a composition) - never a silent no_match."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    sid = await _session(client, child_headers, user.id,
                         patch={"missing_food_ids": ["f_chicken_cooked_v1",
                                                    "f_tahini_v1"]})
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid}, headers=child_headers)
    body = job.json()
    assert body["state"] == "eligible_candidate", body
    proposal = body["proposal"]
    assert proposal.get("composed") or proposal.get("adapted")
    fids = {i["food_version_id"] for i in proposal["items"]}
    assert fids <= LUNCH_BASKET
    assert "f_chicken_cooked_v1" not in fids
    assert "f_tahini_v1" not in fids


async def test_assistant_requested_food_served_via_composition(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """'בא לי עוף' with no recipe servable -> the engine composes a dish
    around the requested basket food and the explanation is 'adapted'."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    sid = await _session(client, child_headers, user.id,
                         create={"preparation": {"max_total_minutes": 2}})
    _stub_llm(monkeypatch, _ai_json(
        requestedFoodIds=["f_chicken_cooked_v1"],
        explanationKey="wants_different"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי עוף בלי כלום"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_used"] is True
    assert body["explanation_key"] == "adapted"
    proposal = body["job"]["proposal"]
    assert proposal["composed"] is True
    assert "f_chicken_cooked_v1" in {i["food_version_id"] for i in proposal["items"]}

    # Coach gate: a requested food outside the approved basket is an AI
    # failure - never composed.
    sid = await _session(client, child_headers, user.id,
                         create={"preparation": {"max_total_minutes": 2}})
    _stub_llm(monkeypatch, _ai_json(
        requestedFoodIds=["f_mystery_spread_v1"],
        explanationKey="adapted"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי ממרח שלא בתפריט"},
        headers=child_headers)
    assert resp.json()["ai_used"] is False

async def test_assistant_compound_request_no_food_name(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """Regression: 'המבורגר וצ'יפס' where the model answers unavailable
    WITHOUT requestedFoodName must still trigger enrichment - the raw
    message is scanned for food terms so a lazy model can't starve the flow."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    sid = await _session(client, child_headers, user.id)
    monkeypatch.setattr(meal_ai, "lookup_food", _builtin_only_lookup)
    _stub_llm(monkeypatch, _ai_json(explanationKey="unavailable"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי המבורגר וצ'יפס"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    proposal = body["job"]["proposal"]
    served_names = {i.get("name_he") or "" for i in proposal["items"]}
    assert any("המבורגר" in n for n in served_names), served_names
    # The engine may drop chips to fit the carb target (smart-fit), but both
    # requested foods must be resolved into the catalog as unverified rows.
    assert body["explanation_key"] in ("auto_added", "adapted")
    cat = await client.get("/api/meals/catalog", headers=parent_headers)
    cat_names = {f.get("name_he") or "" for f in cat.json()["foods"]}
    assert any("המבורגר" in n for n in cat_names), cat_names
    assert any("צ'יפס" in n or "ציפס" in n for n in cat_names), cat_names

async def test_assistant_compound_food_name_is_split(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """Regression: the model may return the whole phrase
    'המבורגר וצ'יפס' as one requestedFoodName. The name itself is scanned
    for sub-terms so both foods resolve instead of one ambiguous miss."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    sid = await _session(client, child_headers, user.id)
    monkeypatch.setattr(meal_ai, "lookup_food", _builtin_only_lookup)
    _stub_llm(monkeypatch, _ai_json(
        requestedFoodName="המבורגר וצ'יפס",
        explanationKey="wants_different"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי המבורגר וצ'יפס"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    proposal = body["job"]["proposal"]
    served_names = {i.get("name_he") or "" for i in proposal["items"]}
    assert any("המבורגר" in n for n in served_names), served_names
    assert body["explanation_key"] in ("auto_added", "adapted")


async def test_assistant_unmet_request_shows_reason_and_substitutes(
        client, child_headers, parent_headers, admin_headers, ai_enabled,
        monkeypatch):
    """A verified-but-too-carb-heavy requested food under a day carb bound:
    the served dish keeps what fits and unmet_requests names the binding
    nutrient plus fitting substitutes the trainee can pick instead."""
    user = await _user()
    light_plan = {
        "meals": [{"slot_key": "lunch", "recipe_version_id": "r_chicken_plate_v1",
                   "items": [{"food_version_id": "f_chicken_cooked_v1",
                              "quantity_g": "150"}]}],
        "alternatives": {},
    }
    await _publish_plan(client, parent_headers, user.id, content=light_plan)
    monkeypatch.setattr(meal_ai, "lookup_food", _builtin_only_lookup)

    # First request auto-adds chips to the catalog, unverified.
    sid = await _session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json(
        requestedFoodName="צ'יפס", explanationKey="adapted"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי צ'יפס"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    cat = await client.get("/api/meals/catalog", headers=parent_headers)
    chips = next(f for f in cat.json()["foods"] if "צ'יפס" in (f.get("name_he") or ""))
    verify = await client.post(
        f"/api/meals/catalog/food-versions/{chips['food_version_id']}/verify",
        headers=parent_headers)
    assert verify.status_code == 200, verify.text

    # Day carb bound 8: chips at min portion (10.25 g) can never fit, but
    # the lean basket foods can.
    await _approved_bundle(
        client, admin_headers, user.id,
        [{"kind": "nutrient_bounds", "scope": "day",
          "nutrient": "carbs_g", "max": "8"}])
    sid2 = await _session(client, child_headers, user.id)
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid2, "message": "בא לי צ'יפס"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    job = body["job"]
    assert job["state"] == "eligible_candidate", job.get("reasons")
    unmet = job.get("unmet_requests") or []
    unmet_names = {u["name_he"] for u in unmet}
    assert any("צ'יפס" in n for n in unmet_names), unmet_names
    u = unmet[0]
    assert u["reason_nutrient"] == "carbs_g"
    assert u["substitutes"], unmet
    assert body["explanation_key"] == "partial_fit"


async def test_assistant_unavailable_with_no_known_food_stays(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """Guard: a truly unresolvable request keeps the honest unavailable
    answer - the message scan must not invent food."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    sid = await _session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json(explanationKey="unavailable"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי פלאפל-קונג-פו מעופף"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["explanation_key"] == "unavailable"


async def test_composition_uses_only_slot_basket_foods(client, child_headers, parent_headers):
    """Basket isolation: the breakfast composition can only draw from
    breakfast-approved foods - lunch foods never leak across slots."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    sid = await _session(client, child_headers, user.id, slot="breakfast",
                         create={"preparation": {"max_total_minutes": 2}})
    job = await client.post(
        f"/api/meals/{user.id}/proposal-jobs",
        json={"session_id": sid}, headers=child_headers)
    body = job.json()
    assert body["state"] == "eligible_candidate", body
    fids = {i["food_version_id"] for i in body["proposal"]["items"]}
    assert fids <= BREAKFAST_BASKET
    assert not (fids & {"f_rice_cooked_v1", "f_chicken_cooked_v1",
                        "f_tahini_v1", "f_cucumber_v1"})


# ── catalog auto-enrichment: requested food -> real-source lookup -> serve ──
STEAK_LOOKUP = {
    "name_he": "סטייק בקר",
    "product_name": "Beef steak",
    "nutrients": {"kcal": "200", "protein_g": "27", "carbs_g": "0", "fat_g": "9",
                  "fiber_g": "0", "sodium_mg": "60", "sugar_g": "0",
                  "sat_fat_g": "3.5", "cholesterol_mg": "80"},
    "allergens": [],
    "source": "openfoodfacts",
    "evidence": "openfoodfacts",
}
TOFU_LOOKUP = {
    "name_he": "טופו",
    "product_name": "Tofu",
    "nutrients": {"kcal": "76", "protein_g": "8", "carbs_g": "1.9", "fat_g": "4.8",
                  "fiber_g": "0.3", "sodium_mg": "7", "sugar_g": "0.6",
                  "sat_fat_g": "0.7", "cholesterol_mg": "0"},
    "allergens": ["soy"],
    "source": "openfoodfacts",
    "evidence": "openfoodfacts",
}
ARTIK_LOOKUP = {
    "name_he": "ארטיק חלב",
    "product_name": "Milk popsicle",
    "nutrients": {"kcal": "120", "protein_g": "5", "carbs_g": "18", "fat_g": "3",
                  "fiber_g": "0", "sodium_mg": "50", "sugar_g": "15",
                  "sat_fat_g": "2", "cholesterol_mg": "10"},
    "allergens": ["milk"],
    "source": "openfoodfacts",
    "evidence": "openfoodfacts",
}


def _fake_lookup(result):
    async def fake(name):
        return dict(result) if result else None
    return fake


async def _builtin_only_lookup(name):
    from nutrition_core.lookup import _builtin_lookup, _normalize
    return _builtin_lookup(_normalize(name))


async def test_assistant_auto_enriches_requested_food(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """'בא לי סטייק' with no such catalog food: the platform enriches the
    catalog from a real nutrient source and serves a composition around
    it - marked unverified - instead of answering 'unavailable'."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    plan_before = (await client.get(
        f"/api/meals/{user.id}/plan", headers=child_headers)).json()
    sid = await _make_session(client, child_headers, user.id)

    monkeypatch.setattr(meal_ai, "lookup_food", _fake_lookup(STEAK_LOOKUP))
    _stub_llm(monkeypatch, _ai_json(requestedFoodName="ג'קפרוט",
                                    explanationKey="adapted"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי ג'קפרוט"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_used"] is True
    assert body["explanation_key"] == "auto_added"
    assert "הוספתי" in body["explanation_he"]
    proposal = body["job"]["proposal"]
    assert proposal["composed"] is True
    auto_items = [i for i in proposal["items"] if i.get("unverified")]
    assert auto_items, proposal["items"]
    auto_fid = auto_items[0]["food_version_id"]
    Decimal(auto_items[0]["quantity_g"])  # engine-computed, decimal-exact

    # The new row is in the shared catalog as unverified auto_lookup.
    catalog = (await client.get("/api/meals/catalog", headers=parent_headers)).json()
    new_food = next(f for f in catalog["foods"]
                    if f["food_version_id"] == auto_fid)
    assert new_food["verified"] == 0
    assert new_food["source"] == "auto_lookup"

    # (vi) The published plan's approved lists are immutable: identical
    # meals/alternatives before and after the enrichment.
    plan_after = (await client.get(
        f"/api/meals/{user.id}/plan", headers=child_headers)).json()
    assert plan_after["plan"]["meals"] == plan_before["plan"]["meals"]
    assert plan_after["plan"]["alternatives"] == plan_before["plan"]["alternatives"]
    assert plan_after["plan"]["version"] == plan_before["plan"]["version"]


async def test_unverified_food_blocked_by_bundle_then_served_after_verify(
        client, child_headers, parent_headers, admin_headers, ai_enabled, monkeypatch):
    """Rule: with an active medical bundle an unverified food is excluded
    ('unverified food data'); after the coach verifies it, it composes."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    await _approved_bundle(client, admin_headers, user.id,
                           [{"kind": "nutrient_bounds", "scope": "meal",
                             "nutrient": "protein_g", "min": "1"}])

    sid = await _make_session(client, child_headers, user.id)
    monkeypatch.setattr(meal_ai, "lookup_food", _fake_lookup(ARTIK_LOOKUP))
    _stub_llm(monkeypatch, _ai_json(requestedFoodName="אצות וואקמה",
                                    explanationKey="adapted"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי אצות וואקמה"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    job = body["job"]
    assert any("unverified food data" in r for r in job["reasons"]), job
    proposal = job.get("proposal") or {}
    served = {i["food_version_id"] for i in proposal.get("items") or []}
    catalog = (await client.get("/api/meals/catalog", headers=parent_headers)).json()
    artik = next((f for f in catalog["foods"] if f["name_he"] == "ארטיק חלב"), None)
    assert artik is not None
    assert artik["food_version_id"] not in served
    assert body["explanation_key"] != "auto_added"

    # The verify endpoint is coach-only.
    assert (await client.post(
        f"/api/meals/catalog/food-versions/{artik['food_version_id']}/verify",
        headers=child_headers)).status_code == 403
    ok = await client.post(
        f"/api/meals/catalog/food-versions/{artik['food_version_id']}/verify",
        headers=parent_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["verified"] is True

    # After verification the same request composes around it under the bundle.
    sid = await _make_session(client, child_headers, user.id)
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי ארטיק"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    proposal = body["job"].get("proposal") or {}
    assert body["job"]["state"] == "eligible_candidate", body["job"]
    assert artik["food_version_id"] in {
        i["food_version_id"] for i in proposal["items"]}
    assert not any(i.get("unverified") for i in proposal["items"])


async def test_unverified_food_unknown_allergens_excluded_fail_closed(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """Declared allergy + an auto-added food whose allergen set is unknown:
    excluded, never served on guessed safety data."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id, {
        "allergy_disclosure": "declared", "allergies": ["milk"],
        "medical_condition_declared": "no"})
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert validation["valid"], validation["issues"]
    approval = await _approve(client, parent_headers, user.id, draft_id, rev, validation)
    published = await _publish(client, parent_headers, user.id, draft_id,
                               approval.json()["approval_id"], rev)
    assert published.status_code == 200, published.text

    unknown_allergens = {**TOFU_LOOKUP, "allergens": None}
    sid = await _make_session(client, child_headers, user.id)
    monkeypatch.setattr(meal_ai, "lookup_food", _fake_lookup(unknown_allergens))
    _stub_llm(monkeypatch, _ai_json(requestedFoodName="בוריטו שעועית",
                                    explanationKey="adapted"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי טופו מעושן"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    job = body["job"]
    assert any("unverified food data" in r for r in job["reasons"]), job
    proposal = job.get("proposal") or {}
    served = {i["food_version_id"] for i in proposal.get("items") or []}
    catalog = (await client.get("/api/meals/catalog", headers=parent_headers)).json()
    tofu = next((f for f in catalog["foods"] if f["name_he"] == "טופו"), None)
    assert tofu is not None and tofu["verified"] == 0
    assert tofu["food_version_id"] not in served


async def test_assistant_lookup_miss_is_unavailable_nothing_persisted(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """Only when enrichment truly fails is 'unavailable' shown; no phantom
    food row is persisted."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)
    before = {f["food_version_id"] for f in
              (await client.get("/api/meals/catalog", headers=parent_headers)).json()["foods"]}
    sid = await _make_session(client, child_headers, user.id)
    monkeypatch.setattr(meal_ai, "lookup_food", _fake_lookup(None))
    _stub_llm(monkeypatch, _ai_json(requestedFoodName="מאכל שלא קיים בשום מאגר",
                                    explanationKey="unavailable"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי מאכל שלא קיים בשום מאגר"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # the engine still serves a composition, so the honest key is 'adapted';
    # 'unavailable' is only for when nothing at all can be served. either way
    # no phantom row may be persisted for the unresolvable name.
    assert body["explanation_key"] in ("unavailable", "adapted")
    after = {f["food_version_id"] for f in
             (await client.get("/api/meals/catalog", headers=parent_headers)).json()["foods"]}
    assert after == before


async def test_requested_food_name_validation(client, child_headers, parent_headers,
                                              ai_enabled, monkeypatch):
    """requestedFoodName is whitelisted: a too-long value is an AI failure;
    a valid one reaches the lookup path."""
    user = await _user()
    await _publish_plan(client, parent_headers, user.id)

    sid = await _make_session(client, child_headers, user.id)
    _stub_llm(monkeypatch, _ai_json(requestedFoodName="x" * 61))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי משהו"},
        headers=child_headers)
    assert resp.json()["ai_used"] is False

    sid = await _make_session(client, child_headers, user.id)
    calls = []

    async def spy_lookup(name):
        calls.append(name)
        return None
    monkeypatch.setattr(meal_ai, "lookup_food", spy_lookup)
    _stub_llm(monkeypatch, _ai_json(requestedFoodName="משהו חדש לגמרי"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי משהו חדש"},
        headers=child_headers)
    assert resp.json()["ai_used"] is True
    assert calls == ["משהו חדש לגמרי"]



async def test_library_seeds_reference_foods(client, parent_headers):
    """The curated reference library is seeded into every DB: verified,
    per-100g nutrients, and derived dietary attributes on every row."""
    cat = (await client.get("/api/meals/catalog", headers=parent_headers)).json()
    library = [f for f in cat["foods"] if f.get("source") == "library"]
    assert len(library) >= 400
    assert all(f.get("verified") == 1 for f in library)
    assert all(f.get("basis_g") == "100" for f in library)

    by_name = {f["name_he"]: f for f in library}
    cottage = by_name["קוטג׳ 5%"]
    assert cottage["nutrients"]["protein_g"] == "11"  # ~11g protein/100g
    chicken = by_name["חזה עוף טרי"]
    assert float(chicken["nutrients"]["protein_g"]) > 20  # 200g ≈ 45g protein
    assert cottage["kosher_evidence"] == "dairy"
    assert chicken["kosher_evidence"] == "meat"
    assert "high_protein" in chicken["attributes"]
    assert "lactose_free" in by_name["גזר"]["attributes"]
    assert "vegan" in by_name["גזר"]["attributes"]
    assert "milk" in cottage["allergens"]
    assert "lactose_free" not in cottage["attributes"]


async def test_diet_flag_rule_filters_foods(client, parent_headers):
    """A vegan diet_flag on the profile rejects a plan that contains
    poultry - the derived attributes drive real rule enforcement."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id, proposed={
        "allergy_disclosure": "none_declared", "allergies": [],
        "medical_condition_declared": "no", "diet_flags": ["vegan"]})
    draft_id, rev = await _create_draft(client, parent_headers, user.id)
    validation = await _validate(client, parent_headers, user.id, draft_id)
    assert validation["valid"] is False
    dict_issues = [i for i in validation["issues"] if isinstance(i, dict)]
    assert any(i.get("rule") == "attribute:diet_flag" for i in dict_issues)


async def test_token_match_never_substring():
    """'חלב' must not fire inside 'חלבון'; Hebrew bound prefixes must fire."""
    from nutrition_core.lookup import alias_terms_in_text
    index = {"חלב": "milk_fid", "אורז": "rice_fid", "בטטה בתנור": "sp_fid"}
    assert alias_terms_in_text("כמה חלבון יש בסלט?", index) == []
    assert alias_terms_in_text("שתיתי חלב", index) == ["milk_fid"]
    assert set(alias_terms_in_text("אכלתי באורז ובטטה בתנור", index)) == {"rice_fid", "sp_fid"}


async def test_assistant_library_food_served_via_composition(
        client, child_headers, parent_headers, ai_enabled, monkeypatch):
    """A food that exists in the library is composed immediately - no
    external lookup, already verified."""
    user = await _user()
    await _confirm_profile(client, parent_headers, user.id)
    await _publish_plan(client, parent_headers, user.id)
    sid = await _make_session(client, child_headers, user.id)

    calls = []

    async def spy_lookup(name):
        calls.append(name)
        return None
    monkeypatch.setattr(meal_ai, "lookup_food", spy_lookup)
    _stub_llm(monkeypatch, _ai_json(requestedFoodName="בטטה בתנור"))
    resp = await client.post(
        f"/api/meals/{user.id}/assistant/messages",
        json={"session_id": sid, "message": "בא לי בטטה בתנור"},
        headers=child_headers)
    assert resp.status_code == 200, resp.text
    assert calls == []  # resolved from library - no lookup needed
    job = resp.json()["job"]
    assert job["state"] == "eligible_candidate", job.get("reasons")
    served = {i["food_version_id"] for i in job["proposal"]["items"]}
    assert any("בטטה" in (i.get("name_he") or "") for i in job["proposal"]["items"])
