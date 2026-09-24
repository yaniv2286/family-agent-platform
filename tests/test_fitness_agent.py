"""Focused backend tests for the Fitness Agent (Coach).

Covers: migration / schema, profile CRUD, safety-aware plan generation,
directive-aware plan generation, progression logic, event bus / nutrition
collaboration, habit CRUD + completion + streaks, habit history
(TinyHabitCompletion), analytics endpoint, and orchestrator wellness report.

Every behaviour is exercised through the real HTTP endpoints. An earlier
revision of this file re-implemented plan generation, plan completion and
habit completion with raw DB queries to sidestep MissingGreenlet errors;
those errors were genuine production bugs (ORM attributes read after
`await db.commit()` on an expire-on-commit session) and the workaround hid
three HTTP 500s. Do not reintroduce logic copies here - if a test cannot
reach the endpoint, fix the endpoint.
"""
import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select, text

from database import (
    Exercise,
    MemoryFact,
    PlannedWorkoutExercise,
    SessionLocal,
    TinyHabit,
    TinyHabitCompletion,
    User,
    UserFitnessProfile,
    WorkoutPerformance,
    WorkoutPlan,
    add_parent_feedback,
)

APP_PIN = "test-pin-1234"
CHILD_PIN = "child-secret-11"
H = {"x-app-pin": APP_PIN}


# ── helpers ──────────────────────────────────────────────────────────
@pytest.fixture
def child_headers(monkeypatch):
    """Headers that authenticate as the restricted `child` role."""
    monkeypatch.setenv("CHILD_PIN", CHILD_PIN)
    return {"x-app-pin": CHILD_PIN}


async def _child(name=None):
    async with SessionLocal() as db:
        child = User(name=name or f"Fit-{uuid.uuid4().hex[:8]}", role="child")
        db.add(child)
        await db.commit()
        await db.refresh(child)
        return child


async def _profile_via_http(client, user_id, **overrides):
    body = {
        "equipment": ["none", "resistance_bands"],
        "health_constraints": [],
        "goals": ["strength"],
        "supplements": [],
        "preferred_duration_minutes": 30,
        "training_days": ["sunday", "tuesday"],
        **overrides,
    }
    return await client.put(f"/api/fitness/profile/{user_id}", headers=H, json=body)


async def _create_profile_db(user_id, **overrides):
    """Seed a fitness profile directly. Setup only - never asserted against."""
    defaults = dict(
        equipment=json.dumps(["none", "resistance_bands"]),
        health_constraints=json.dumps([]),
        goal_type=json.dumps(["strength"]),
        supplement_stack=json.dumps([]),
        preferred_duration_minutes=30,
        training_days=json.dumps([]),
    )
    defaults.update(overrides)
    async with SessionLocal() as db:
        existing = await db.get(UserFitnessProfile, user_id)
        if existing:
            for k, v in defaults.items():
                setattr(existing, k, v)
        else:
            db.add(UserFitnessProfile(user_id=user_id, **defaults))
        await db.commit()


async def _generate_plan(client, user_id, regenerate=False, expect=200):
    """Generate a workout plan through the real endpoint."""
    url = f"/api/fitness/plans/generate/{user_id}"
    if regenerate:
        url += "?regenerate=true"
    r = await client.post(url, headers=H)
    assert r.status_code == expect, r.text
    return r.json()


async def _complete_plan(client, plan_id, user_id, exercise_ids, rpe=5, fatigue=3,
                         pain=0, duration=30, notes="", reps=8, expect=200):
    """Complete a workout plan through the real endpoint."""
    body = {
        "user_id": user_id,
        "perceived_exertion": rpe,
        "fatigue": fatigue,
        "pain": pain,
        "duration_minutes": duration,
        "notes": notes,
        "exercises": [
            {"exercise_id": eid, "sets": [{"set_number": 1, "reps": reps},
                                          {"set_number": 2, "reps": reps}]}
            for eid in exercise_ids
        ],
    }
    r = await client.post(f"/api/fitness/plans/{plan_id}/complete", headers=H, json=body)
    assert r.status_code == expect, r.text
    return r.json()


async def _create_habit(client, user_id, name="Test", anchor="After waking",
                        routine="5 stretches", schedule=None):
    r = await client.post("/api/fitness/habits", headers=H, json={
        "user_id": user_id, "name": name, "anchor": anchor,
        "minimum_routine": routine, "schedule": schedule or [],
    })
    assert r.status_code == 200, r.text
    return r.json()


async def _complete_habit(client, habit_id, user_id, completed_date=None, expect=200):
    body = {"user_id": user_id}
    if completed_date is not None:
        body["completed_date"] = str(completed_date)
    r = await client.post(f"/api/fitness/habits/{habit_id}/complete", headers=H, json=body)
    assert r.status_code == expect, r.text
    return r.json()


# ── 1. Migration / Schema ───────────────────────────────────────────
class TestMigrationSchema:
    @pytest.mark.asyncio
    async def test_tiny_habit_completions_table_exists(self):
        async with SessionLocal() as db:
            result = await db.execute(text("PRAGMA table_info(tiny_habit_completions)"))
            columns = {row[1] for row in result.fetchall()}
        assert "id" in columns
        assert "habit_id" in columns
        assert "user_id" in columns
        assert "completed_date" in columns

    @pytest.mark.asyncio
    async def test_exercises_table_has_expanded_seed(self):
        async with SessionLocal() as db:
            rows = (await db.execute(select(Exercise))).scalars().all()
        assert len(rows) >= 20
        slugs = {r.slug for r in rows}
        assert "trx-row" in slugs
        assert "balance-board-stand" in slugs

    @pytest.mark.asyncio
    async def test_exercises_table_has_video_url_column(self):
        async with SessionLocal() as db:
            result = await db.execute(text("PRAGMA table_info(exercises)"))
            columns = {row[1] for row in result.fetchall()}
        assert "video_url" in columns

    @pytest.mark.asyncio
    async def test_workout_plans_table_exists(self):
        async with SessionLocal() as db:
            result = await db.execute(text("PRAGMA table_info(workout_plans)"))
            columns = {row[1] for row in result.fetchall()}
        assert "safety_notes_json" in columns
        assert "completed_at" in columns


# ── 2. Profile CRUD ─────────────────────────────────────────────────
class TestProfile:
    @pytest.mark.asyncio
    async def test_profile_defaults_when_not_created(self, client):
        child = await _child()
        r = await client.get(f"/api/fitness/profile/{child.id}", headers=H)
        assert r.status_code == 200
        d = r.json()
        assert d["profile_exists"] is False
        assert "tilt_test_sensitivity" in d["health_constraints"]
        assert "creatine" in d["supplements"]
        assert "not prescribed" in d["supplement_notice"]

    @pytest.mark.asyncio
    async def test_create_and_get_profile(self, client):
        child = await _child()
        r = await _profile_via_http(client, child.id)
        assert r.status_code == 200
        r2 = await client.get(f"/api/fitness/profile/{child.id}", headers=H)
        d = r2.json()
        assert d["profile_exists"] is True
        assert d["equipment"] == ["none", "resistance_bands"]
        assert d["training_days"] == ["sunday", "tuesday"]

    @pytest.mark.asyncio
    async def test_update_profile_changes_values(self, client):
        child = await _child()
        await _profile_via_http(client, child.id)
        r = await _profile_via_http(client, child.id, equipment=["trx"],
                                    preferred_duration_minutes=45)
        assert r.status_code == 200
        d = r.json()
        assert d["equipment"] == ["trx"]
        assert d["preferred_duration_minutes"] == 45

    @pytest.mark.asyncio
    async def test_profile_patch_works(self, client):
        child = await _child()
        await _profile_via_http(client, child.id)
        r = await client.patch(f"/api/fitness/profile/{child.id}", headers=H, json={
            "equipment": ["fitball"], "health_constraints": [], "goals": ["mobility"],
            "supplements": [], "preferred_duration_minutes": 20, "training_days": [],
        })
        assert r.status_code == 200
        assert r.json()["goals"] == ["mobility"]

    @pytest.mark.asyncio
    async def test_profile_stores_memory_facts(self, client):
        child = await _child()
        await _profile_via_http(client, child.id, health_constraints=["knee_pain"])
        async with SessionLocal() as db:
            facts = (await db.execute(
                select(MemoryFact).where(MemoryFact.child_id == child.id,
                                         MemoryFact.domain == "fitness")
            )).scalars().all()
        assert any("knee_pain" in (f.value or "") for f in facts)

    @pytest.mark.asyncio
    async def test_profile_invalid_day_returns_422(self, client):
        child = await _child()
        r = await _profile_via_http(client, child.id, training_days=["notaday"])
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_profile_nonexistent_user_returns_404(self, client):
        r = await client.get("/api/fitness/profile/99999", headers=H)
        assert r.status_code == 404


# ── 2b. Role-based access control ────────────────────────────────────
class TestAccessControl:
    @pytest.mark.asyncio
    async def test_child_cannot_change_health_constraints(self, client, child_headers):
        child = await _child()
        await _create_profile_db(child.id, health_constraints=json.dumps(["tilt_test_sensitivity"]))
        r = await client.put(f"/api/fitness/profile/{child.id}", headers=child_headers, json={
            "equipment": ["none"], "health_constraints": [], "goals": ["strength"],
            "supplements": [], "preferred_duration_minutes": 30, "training_days": [],
        })
        assert r.status_code == 403
        # The stored safety constraint must survive the rejected write.
        r2 = await client.get(f"/api/fitness/profile/{child.id}", headers=H)
        assert r2.json()["health_constraints"] == ["tilt_test_sensitivity"]

    @pytest.mark.asyncio
    async def test_child_may_edit_non_safety_fields(self, client, child_headers):
        child = await _child()
        await _create_profile_db(child.id, health_constraints=json.dumps(["tilt_test_sensitivity"]))
        r = await client.put(f"/api/fitness/profile/{child.id}", headers=child_headers, json={
            "equipment": ["trx"], "health_constraints": ["tilt_test_sensitivity"],
            "goals": ["mobility"], "supplements": [],
            "preferred_duration_minutes": 20, "training_days": ["monday"],
        })
        assert r.status_code == 200
        assert r.json()["equipment"] == ["trx"]

    @pytest.mark.asyncio
    async def test_parent_can_change_health_constraints(self, client):
        child = await _child()
        await _create_profile_db(child.id, health_constraints=json.dumps(["tilt_test_sensitivity"]))
        r = await _profile_via_http(client, child.id, health_constraints=["knee_pain"])
        assert r.status_code == 200
        assert r.json()["health_constraints"] == ["knee_pain"]

    @pytest.mark.asyncio
    async def test_missing_pin_is_rejected(self, client):
        child = await _child()
        r = await client.get(f"/api/fitness/profile/{child.id}")
        assert r.status_code == 401


# ── 3. Safety-Aware Plan Generation ─────────────────────────────────
class TestPlanGeneration:
    @pytest.mark.asyncio
    async def test_exercise_library_includes_video_urls(self, client):
        response = await client.get("/api/fitness/exercises", headers=H)
        assert response.status_code == 200
        exercises = response.json()
        assert exercises
        assert all(e["video_url"].startswith(("https://www.youtube.com/", "https://youtu.be/")) for e in exercises)

    @pytest.mark.asyncio
    async def test_generate_plan_uses_safe_defaults_without_profile(self, client):
        child = await _child()
        plan = await _generate_plan(client, child.id)
        assert plan["exercises"]
        assert all(e["video_url"].startswith(("https://www.youtube.com/", "https://youtu.be/")) for e in plan["exercises"])

    @pytest.mark.asyncio
    async def test_generate_plan_success(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        assert plan["user_id"] == child.id
        assert plan["status"] == "planned"
        assert len(plan["exercises"]) >= 1
        assert "safety_notes" in plan
        assert "coach_greeting" in plan

    @pytest.mark.asyncio
    async def test_sensitive_plan_excludes_high_impact(self, client):
        child = await _child()
        await _create_profile_db(child.id, health_constraints=json.dumps(["tilt_test_sensitivity"]))
        plan = await _generate_plan(client, child.id)
        assert any("Low-impact" in n for n in plan["safety_notes"])
        assert "Jumping jacks" not in [e["name"] for e in plan["exercises"]]

    @pytest.mark.asyncio
    async def test_sensitive_plan_excludes_floor_transitions(self, client):
        child = await _child()
        await _create_profile_db(
            child.id,
            health_constraints=json.dumps(["avoid_sudden_orthostatic_drops"]),
            equipment=json.dumps(["none", "pilates_mat"]),
        )
        plan = await _generate_plan(client, child.id)
        names = [e["name"] for e in plan["exercises"]]
        assert "Dead bug" not in names
        assert "Pilates bridge" not in names

    @pytest.mark.asyncio
    async def test_plan_exercise_count_scales_with_duration(self, client):
        child = await _child()
        await _create_profile_db(
            child.id, preferred_duration_minutes=60,
            equipment=json.dumps(["none", "resistance_bands", "trx"]),
        )
        plan = await _generate_plan(client, child.id)
        assert len(plan["exercises"]) == 5

    @pytest.mark.asyncio
    async def test_list_plans(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        await _generate_plan(client, child.id)
        r = await client.get(f"/api/fitness/plans/{child.id}", headers=H)
        assert r.status_code == 200
        assert len(r.json()) >= 1

    @pytest.mark.asyncio
    async def test_repeat_generate_reuses_open_plan(self, client):
        """Regenerating must not stack duplicate plans and wreck adherence."""
        child = await _child()
        await _create_profile_db(child.id)
        first = await _generate_plan(client, child.id)
        second = await _generate_plan(client, child.id)
        assert second["id"] == first["id"]
        assert second["reused_existing_plan"] is True
        r = await client.get(f"/api/fitness/plans/{child.id}", headers=H)
        assert len(r.json()) == 1

    @pytest.mark.asyncio
    async def test_explicit_regenerate_creates_new_plan(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        first = await _generate_plan(client, child.id)
        second = await _generate_plan(client, child.id, regenerate=True)
        assert second["id"] != first["id"]

    @pytest.mark.asyncio
    async def test_plan_scheduled_on_next_training_day(self, client):
        child = await _child()
        await _create_profile_db(child.id, training_days=json.dumps(["friday"]))
        plan = await _generate_plan(client, child.id)
        assert date.fromisoformat(plan["date"]).strftime("%A").lower() == "friday"

    @pytest.mark.asyncio
    async def test_plan_defaults_to_today_without_training_days(self, client):
        child = await _child()
        await _create_profile_db(child.id, training_days=json.dumps([]))
        plan = await _generate_plan(client, child.id)
        assert plan["date"] == str(date.today())


# ── 4. Directive-Aware Plan Generation ──────────────────────────────
class TestDirectiveAwarePlan:
    @pytest.mark.asyncio
    async def test_low_impact_directive_forces_sensitive_mode(self, client):
        child = await _child()
        await _create_profile_db(child.id, health_constraints=json.dumps([]))
        await add_parent_feedback(child.name, "fitness", "please use low-impact exercises only")
        plan = await _generate_plan(client, child.id)
        notes = plan["safety_notes"]
        assert any("Low-impact" in n for n in notes)
        assert "Jumping jacks" not in [e["name"] for e in plan["exercises"]]

    @pytest.mark.asyncio
    async def test_directive_appended_to_safety_notes(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        await add_parent_feedback(child.name, "fitness", "focus on upper body")
        plan = await _generate_plan(client, child.id)
        assert any("upper body" in n.lower() for n in plan["safety_notes"])


# ── 5. Progression Logic ────────────────────────────────────────────
class TestProgression:
    @pytest.mark.asyncio
    async def test_pain_blocks_progression(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan(client, plan["id"], child.id, eids, pain=2)
        assert result["progression_allowed"] is False
        assert "do not progress" in result["guidance"].lower()

    @pytest.mark.asyncio
    async def test_comfortable_session_allows_progression(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan(client, plan["id"], child.id, eids, pain=0)
        assert result["progression_allowed"] is True
        assert result["performance_id"] > 0

    @pytest.mark.asyncio
    async def test_high_rpe_blocks_progression(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan(client, plan["id"], child.id, eids, rpe=9)
        assert result["progression_allowed"] is False

    @pytest.mark.asyncio
    async def test_high_fatigue_blocks_progression(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan(client, plan["id"], child.id, eids, fatigue=8)
        assert result["progression_allowed"] is False

    @pytest.mark.asyncio
    async def test_double_complete_returns_409(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan(client, plan["id"], child.id, eids)
        await _complete_plan(client, plan["id"], child.id, eids, expect=409)
        # Exactly one performance must exist for the plan.
        async with SessionLocal() as db:
            perfs = (await db.execute(
                select(WorkoutPerformance).where(WorkoutPerformance.plan_id == plan["id"])
            )).scalars().all()
        assert len(perfs) == 1

    @pytest.mark.asyncio
    async def test_exercise_outside_plan_returns_422(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        await _complete_plan(client, plan["id"], child.id, [999999], expect=422)

    @pytest.mark.asyncio
    async def test_comfortable_session_raises_next_target_reps(self, client):
        """ProgressionHistory must actually change the next prescription."""
        child = await _child()
        # equipment "none" has exactly 5 seeded exercises, so a 30-minute plan
        # selects all of them and the next plan is guaranteed to overlap.
        await _create_profile_db(child.id, equipment=json.dumps(["none"]))
        first = await _generate_plan(client, child.id)
        assert all(e["reps"] == 8 for e in first["exercises"])
        eids = [e["exercise_id"] for e in first["exercises"]]
        await _complete_plan(client, first["id"], child.id, eids, reps=8,
                             rpe=4, fatigue=2, pain=0)
        second = await _generate_plan(client, child.id)
        progressed = {e["exercise_id"]: e["reps"] for e in second["exercises"]}
        assert any(progressed.get(eid) == 9 for eid in eids), progressed
        assert any("small increase applied" in n for n in second["safety_notes"])

    @pytest.mark.asyncio
    async def test_painful_exercises_are_not_represcribed(self, client):
        child = await _child()
        await _create_profile_db(child.id, equipment=json.dumps(["none", "trx", "resistance_bands"]))
        first = await _generate_plan(client, child.id)
        hurt = first["exercises"][0]["exercise_id"]
        await _complete_plan(client, first["id"], child.id, [hurt], pain=4)
        second = await _generate_plan(client, child.id)
        assert hurt not in [e["exercise_id"] for e in second["exercises"]]

    @pytest.mark.asyncio
    async def test_high_fatigue_exercises_are_not_represcribed(self, client):
        """Fatigue blocks progression, so it must also gate re-prescription."""
        child = await _child()
        await _create_profile_db(child.id, equipment=json.dumps(["none", "trx", "resistance_bands"]))
        first = await _generate_plan(client, child.id)
        drained = first["exercises"][0]["exercise_id"]
        await _complete_plan(client, first["id"], child.id, [drained], fatigue=9)
        second = await _generate_plan(client, child.id)
        assert drained not in [e["exercise_id"] for e in second["exercises"]]


# ── 6. Event Bus / Nutrition Collaboration ──────────────────────────
class TestEventNutrition:
    @pytest.mark.asyncio
    async def test_completion_creates_nutrition_fact(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan(client, plan["id"], child.id, eids)
        async with SessionLocal() as db:
            facts = (await db.execute(
                select(MemoryFact).where(MemoryFact.child_id == child.id, MemoryFact.domain == "nutrition")
            )).scalars().all()
        assert len(facts) >= 1
        assert any("balanced meal" in f.value for f in facts)

    @pytest.mark.asyncio
    async def test_completion_publishes_workout_event(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan(client, plan["id"], child.id, eids)
        from database import AgentEvent
        async with SessionLocal() as db:
            events = (await db.execute(
                select(AgentEvent).where(AgentEvent.child_id == child.id)
            )).scalars().all()
        assert any(e.event_type == "fitness.workout_completed" for e in events)

    @pytest.mark.asyncio
    async def test_log_workout_completed_publishes_event(self, client):
        child = await _child()
        r = await client.post("/api/fitness/log", headers=H, json={
            "user_id": child.id, "workout_type": "walk", "exercises": [], "status": "completed", "notes": ""
        })
        assert r.status_code == 200
        from database import AgentEvent
        async with SessionLocal() as db:
            events = (await db.execute(
                select(AgentEvent).where(AgentEvent.child_id == child.id)
            )).scalars().all()
        assert any(e.event_type == "fitness.workout_completed" for e in events)

    @pytest.mark.asyncio
    async def test_incomplete_log_does_not_publish_event(self, client):
        child = await _child()
        r = await client.post("/api/fitness/log", headers=H, json={
            "user_id": child.id, "workout_type": "stretch", "exercises": [], "status": "planned", "notes": ""
        })
        assert r.status_code == 200
        from database import AgentEvent
        async with SessionLocal() as db:
            events = (await db.execute(
                select(AgentEvent).where(AgentEvent.child_id == child.id)
            )).scalars().all()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_log_rejects_unknown_status(self, client):
        child = await _child()
        r = await client.post("/api/fitness/log", headers=H, json={
            "user_id": child.id, "workout_type": "walk", "exercises": [],
            "status": "Z" * 400, "notes": ""
        })
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_log_rejects_oversized_exercise_list(self, client):
        child = await _child()
        r = await client.post("/api/fitness/log", headers=H, json={
            "user_id": child.id, "workout_type": "walk",
            "exercises": [{"junk": "y" * 1000}], "status": "completed", "notes": ""
        })
        assert r.status_code == 422


# ── 7. Habits CRUD + Completion + Streaks ────────────────────────────
class TestHabits:
    @pytest.mark.asyncio
    async def test_create_habit(self, client):
        child = await _child()
        d = await _create_habit(client, child.id, "Morning stretch", "After waking up",
                                "5 arm circles", ["sunday", "monday"])
        assert d["name"] == "Morning stretch"
        assert d["current_streak"] == 0

    @pytest.mark.asyncio
    async def test_list_habits(self, client):
        child = await _child()
        await _create_habit(client, child.id, "H1")
        await _create_habit(client, child.id, "H2")
        r = await client.get(f"/api/fitness/habits/{child.id}", headers=H)
        assert r.status_code == 200
        assert len(r.json()) == 2

    @pytest.mark.asyncio
    async def test_habit_completion_increments_streak(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "Streak test")
        result = await _complete_habit(client, h["id"], child.id, date.today())
        assert result["current_streak"] == 1

    @pytest.mark.asyncio
    async def test_consecutive_days_build_streak(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "Cons test")
        today = date.today()
        await _complete_habit(client, h["id"], child.id, today - timedelta(days=1))
        result = await _complete_habit(client, h["id"], child.id, today)
        assert result["current_streak"] == 2
        assert result["best_streak"] == 2

    @pytest.mark.asyncio
    async def test_gap_resets_streak(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "Gap test")
        today = date.today()
        await _complete_habit(client, h["id"], child.id, today - timedelta(days=3))
        result = await _complete_habit(client, h["id"], child.id, today)
        assert result["current_streak"] == 1

    @pytest.mark.asyncio
    async def test_backdate_before_last_completion_returns_409(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "Backdate test")
        today = date.today()
        await _complete_habit(client, h["id"], child.id, today)
        await _complete_habit(client, h["id"], child.id, today - timedelta(days=1), expect=409)

    @pytest.mark.asyncio
    async def test_future_completion_returns_422(self, client):
        """A future date would permanently lock the habit via the backdate rule."""
        child = await _child()
        h = await _create_habit(client, child.id, "Future test")
        await _complete_habit(client, h["id"], child.id,
                              date.today() + timedelta(days=1), expect=422)
        # The habit must still accept a legitimate completion for today.
        result = await _complete_habit(client, h["id"], child.id, date.today())
        assert result["current_streak"] == 1

    @pytest.mark.asyncio
    async def test_repeat_completion_same_day_is_idempotent(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "Idem test")
        today = date.today()
        await _complete_habit(client, h["id"], child.id, today)
        result = await _complete_habit(client, h["id"], child.id, today)
        assert result["current_streak"] == 1
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(TinyHabitCompletion).where(TinyHabitCompletion.habit_id == h["id"])
            )).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_habit_completion_stores_history(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "History test")
        today = date.today()
        await _complete_habit(client, h["id"], child.id, today)
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(TinyHabitCompletion).where(TinyHabitCompletion.habit_id == h["id"])
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].completed_date == today

    @pytest.mark.asyncio
    async def test_habit_history_endpoint(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "Hist EP test")
        await _complete_habit(client, h["id"], child.id, date.today())
        r = await client.get(f"/api/fitness/habits/{h['id']}/history?user_id={child.id}", headers=H)
        assert r.status_code == 200
        data = r.json()
        assert data["habit_id"] == h["id"]
        assert len(data["completions"]) == 1

    @pytest.mark.asyncio
    async def test_update_habit(self, client):
        child = await _child()
        h = await _create_habit(client, child.id, "Old name")
        r = await client.put(f"/api/fitness/habits/{h['id']}", headers=H, json={
            "user_id": child.id, "name": "New name", "anchor": "New anchor",
            "minimum_routine": "New routine", "schedule": ["friday"], "active": True,
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["name"] == "New name"
        assert d["schedule"] == ["friday"]

    @pytest.mark.asyncio
    async def test_update_habit_wrong_owner_returns_404(self, client):
        owner = await _child()
        other = await _child()
        h = await _create_habit(client, owner.id, "Owned")
        r = await client.put(f"/api/fitness/habits/{h['id']}", headers=H, json={
            "user_id": other.id, "name": "Hijack", "anchor": "A",
            "minimum_routine": "R", "schedule": [], "active": True,
        })
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_minimum_routine_endpoint(self, client):
        child = await _child()
        await _create_habit(client, child.id, "Min routine", "After coffee", "10 squats")
        r = await client.get(f"/api/fitness/habits/{child.id}/minimum-routine", headers=H)
        assert r.status_code == 200
        data = r.json()
        assert len(data["routine"]) >= 1
        assert data["routine"][0]["do"] == "10 squats"


# ── 8. Analytics ─────────────────────────────────────────────────────
class TestAnalytics:
    @pytest.mark.asyncio
    async def test_analytics_empty_user(self, client):
        child = await _child()
        r = await client.get(f"/api/fitness/analytics/{child.id}", headers=H)
        assert r.status_code == 200
        d = r.json()
        assert d["adherence"]["planned"] == 0
        assert d["adherence"]["rate"] == 0
        assert d["goal_status"]["status"] == "not_started"

    @pytest.mark.asyncio
    async def test_analytics_after_completion(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan(client, plan["id"], child.id, eids)
        r = await client.get(f"/api/fitness/analytics/{child.id}", headers=H)
        d = r.json()
        assert d["adherence"]["completed"] == 1
        assert d["adherence"]["rate"] == 1.0
        assert len(d["recent_sessions"]) >= 1
        assert d["recent_sessions"][0]["progression_allowed"] is True
        assert d["progression"]

    @pytest.mark.asyncio
    async def test_upcoming_plan_does_not_count_as_missed(self, client):
        """A plan scheduled for a future day has not been skipped yet."""
        child = await _child()
        await _create_profile_db(child.id, training_days=json.dumps([
            (date.today() + timedelta(days=2)).strftime("%A").lower()
        ]))
        await _generate_plan(client, child.id)
        r = await client.get(f"/api/fitness/analytics/{child.id}", headers=H)
        d = r.json()["adherence"]
        assert d["upcoming"] == 1
        assert d["planned"] == 0
        assert d["missed"] == 0
        assert d["rate"] == 0

    @pytest.mark.asyncio
    async def test_analytics_nonexistent_user_returns_404(self, client):
        r = await client.get("/api/fitness/analytics/99999", headers=H)
        assert r.status_code == 404


# ── 9. Exercises endpoint ───────────────────────────────────────────
class TestExercises:
    @pytest.mark.asyncio
    async def test_list_all_exercises(self, client):
        r = await client.get("/api/fitness/exercises", headers=H)
        assert r.status_code == 200
        assert len(r.json()) >= 8

    @pytest.mark.asyncio
    async def test_filter_by_category(self, client):
        r = await client.get("/api/fitness/exercises?category=cardio", headers=H)
        assert r.status_code == 200
        for ex in r.json():
            assert ex["category"] == "cardio"

    @pytest.mark.asyncio
    async def test_filter_by_low_impact(self, client):
        r = await client.get("/api/fitness/exercises?low_impact=true", headers=H)
        assert r.status_code == 200
        for ex in r.json():
            assert ex["low_impact"] is True

    @pytest.mark.asyncio
    async def test_filter_by_equipment(self, client):
        r = await client.get("/api/fitness/exercises?equipment=trx", headers=H)
        assert r.status_code == 200
        for ex in r.json():
            assert ex["equipment"] == "trx"

    @pytest.mark.asyncio
    async def test_pagination_limits_results(self, client):
        r = await client.get("/api/fitness/exercises?limit=3", headers=H)
        assert r.status_code == 200
        assert len(r.json()) == 3


# ── 10. Report / Orchestrator Wellness ──────────────────────────────
class TestReport:
    @pytest.mark.asyncio
    async def test_orchestrator_includes_wellness_data(self, client):
        from orchestrator import gather_daily_report_data
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan(client, child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan(client, plan["id"], child.id, eids, duration=25)
        report = await gather_daily_report_data()
        child_report = next((r for r in report if r["child_name"] == child.name), None)
        assert child_report is not None
        w = child_report["wellness"]
        assert w["workouts_completed"] >= 1
        assert w["activity_minutes"] >= 25
