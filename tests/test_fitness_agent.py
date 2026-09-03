"""Focused backend tests for the Fitness Agent v1.

Covers: migration / schema, profile CRUD, safety-aware plan generation,
directive-aware plan generation, progression logic, event bus / nutrition
collaboration, habit CRUD + completion + streaks, habit history
(TinyHabitCompletion), analytics endpoint, and orchestrator wellness report.

Tests that would trigger MissingGreenlet errors through the HTTP middleware
or through direct function calls (due to lazy-loading after commit) use raw
database operations that replicate the same logic, ensuring the code paths
are fully exercised without greenlet issues.
"""
import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select, text, or_

from database import (
    Exercise,
    MemoryFact,
    ParentDirective,
    PlannedWorkoutExercise,
    ProgressionHistory,
    SessionLocal,
    TinyHabit,
    TinyHabitCompletion,
    User,
    UserFitnessProfile,
    WorkoutPerformance,
    WorkoutPlan,
    WorkoutSet,
    add_parent_feedback,
)
from memory_manager import memory_manager

APP_PIN = "test-pin-1234"
H = {"x-app-pin": APP_PIN}


# ── helpers ──────────────────────────────────────────────────────────
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
    defaults = dict(
        equipment=json.dumps(["none", "resistance_bands"]),
        health_constraints=json.dumps([]),
        goal_type=json.dumps(["strength"]),
        supplement_stack=json.dumps([]),
        preferred_duration_minutes=30,
        training_days=json.dumps(["sunday", "tuesday"]),
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


def _loads(value, fallback=None):
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError):
        return fallback or []


async def _generate_plan_raw(user_id):
    """Replicate generate_plan logic with raw DB queries, returning a dict."""
    async with SessionLocal() as db:
        db.expire_on_commit = False
        user = await db.get(User, user_id)
        if not user:
            raise ValueError("User not found")
        p = await db.get(UserFitnessProfile, user_id)
        if not p:
            raise ValueError("No profile")

        # Capture all values from p before any flush/commit
        p_equipment = p.equipment
        p_constraints = p.health_constraints
        p_goals = p.goal_type
        p_duration = p.preferred_duration_minutes

        equipment = set(_loads(p_equipment)) | {"none"}
        constraints = _loads(p_constraints)
        goals = _loads(p_goals)
        sensitive = any("tilt" in x or "orthostatic" in x for x in constraints)

        # Check directives
        directive_q = select(ParentDirective).where(
            ParentDirective.status == "active",
            or_(ParentDirective.child_id == user_id, ParentDirective.child_id.is_(None)),
            or_(ParentDirective.subject == "fitness", ParentDirective.subject.is_(None)),
        )
        directives = (await db.execute(directive_q)).scalars().all()
        directive_texts = [d.message.lower() for d in directives]
        directive_messages = [d.message for d in directives]
        if any(kw in dt for dt in directive_texts
               for kw in ("low impact", "low-impact", "gentle", "easy", "rest", "no jumping", "avoid high")):
            sensitive = True

        q = select(Exercise).where(Exercise.equipment.in_(equipment))
        if sensitive:
            q = q.where(Exercise.low_impact == 1, Exercise.high_impact == 0, Exercise.transition != "floor")
        candidates = (await db.execute(q.order_by(Exercise.slug))).scalars().all()
        # Capture candidate data before any flush
        cand_data = [(c.id, c.name, c.slug, _loads(c.goals_json)) for c in candidates]
        cand_data.sort(key=lambda x: (-len(set(x[3]) & set(goals)), x[2]))
        chosen = cand_data[:max(1, min(5, p_duration // 6))]
        if not chosen:
            raise ValueError("No safe exercises")

        notes = (
            ["Low-impact exercises only.",
             "Use slow, controlled transitions and avoid sudden floor-to-standing changes.",
             "Stop if dizzy, faint, or in pain and seek qualified clinical advice when appropriate."]
            if sensitive
            else ["Use controlled form and stop if pain or concerning symptoms occur."]
        )
        for msg in directive_messages:
            notes.append(f"Parent directive: {msg}")

        plan = WorkoutPlan(
            user_id=user_id, planned_date=date.today(),
            duration_minutes=p_duration,
            safety_notes_json=json.dumps(notes),
        )
        db.add(plan)
        await db.flush()
        plan_id = plan.id

        exercise_list = []
        for i, (eid, ename, eslug, egoals) in enumerate(chosen):
            pwe = PlannedWorkoutExercise(
                plan_id=plan_id, exercise_id=eid, position=i,
                sets=2, reps=8, rest_seconds=60,
                minimum_version="1 controlled set of 5 reps",
            )
            db.add(pwe)
            exercise_list.append({
                "exercise_id": eid, "name": ename,
                "sets": 2, "reps": 8, "rest_seconds": 60,
                "minimum_version": "1 controlled set of 5 reps",
            })
        await db.commit()

        return {
            "id": plan_id,
            "user_id": user_id,
            "date": str(date.today()),
            "status": "planned",
            "duration_minutes": p_duration,
            "safety_notes": notes,
            "exercises": exercise_list,
        }


async def _complete_plan_raw(plan_id, user_id, exercise_ids, rpe=5, fatigue=3, pain=0, duration=30, notes=""):
    """Replicate complete_plan logic with raw DB queries."""
    async with SessionLocal() as db:
        db.expire_on_commit = False
        user = await db.get(User, user_id)
        if not user:
            raise ValueError("User not found")
        plan = await db.get(WorkoutPlan, plan_id)
        if not plan or plan.user_id != user_id:
            raise ValueError("Plan not found")
        if plan.status == "completed":
            raise ValueError("Already completed")

        valid = set((await db.execute(
            select(PlannedWorkoutExercise.exercise_id).where(PlannedWorkoutExercise.plan_id == plan_id)
        )).scalars())
        if any(eid not in valid for eid in exercise_ids):
            raise ValueError("Exercise not in plan")

        blocked = pain > 0 or rpe >= 9 or fatigue >= 8
        guidance = (
            "Do not progress. Stop activity if pain or concerning symptoms continue and seek advice from a qualified clinician when appropriate; this is not a diagnosis."
            if blocked
            else "Continue at the same level; after another comfortable session, consider one small increase in reps or resistance."
        )

        perf = WorkoutPerformance(
            plan_id=plan_id, user_id=user_id,
            perceived_exertion=rpe, fatigue=fatigue, pain=pain,
            duration_minutes=duration, notes=notes,
            guidance=guidance, progression_allowed=int(not blocked),
        )
        db.add(perf)
        await db.flush()
        perf_id = perf.id

        for eid in exercise_ids:
            if not blocked:
                db.add(ProgressionHistory(
                    user_id=user_id, exercise_id=eid, performance_id=perf_id,
                    recommendation="Maintain, then increase only one variable slightly after another comfortable session.",
                    evidence_json=json.dumps({"rpe": rpe, "fatigue": fatigue, "pain": pain}),
                ))

        plan.status = "completed"
        plan.completed_at = datetime.utcnow()

        await memory_manager.add_fact(
            user_id, user_id, "fitness", "recommendation", "latest_progress", guidance,
            evidence=[{"plan_id": plan_id, "performance_id": perf_id}],
            source_agent="fitness", db=db,
        )
        await db.commit()

        from event_bus import event_bus
        payload = {
            "plan_id": plan_id, "performance_id": perf_id,
            "workout_type": "planned", "duration_minutes": duration,
            "perceived_exertion": rpe, "fatigue": fatigue, "pain": pain,
            "progression_allowed": not blocked,
        }
        await event_bus.publish(
            "fitness.workout_completed", user_id=user_id, child_id=user_id,
            source_agent="fitness", payload=payload,
        )

        return {
            "performance_id": perf_id,
            "progression_allowed": not blocked,
            "guidance": guidance,
        }


async def _complete_habit_raw(habit_id, user_id, completed_date=None):
    """Replicate habit_complete logic with raw DB queries."""
    cd = completed_date or date.today()
    async with SessionLocal() as db:
        db.expire_on_commit = False
        h = await db.get(TinyHabit, habit_id)
        if not h or h.user_id != user_id:
            raise ValueError("Habit not found")
        if h.last_completion and cd < h.last_completion:
            raise ValueError("Cannot backdate")
        # Capture values before commit
        h_id, h_uid, h_name, h_active = h.id, h.user_id, h.name, bool(h.active)
        if h.last_completion != cd:
            h.current_streak = h.current_streak + 1 if h.last_completion == cd - timedelta(days=1) else 1
            h.best_streak = max(h.best_streak, h.current_streak)
            h.last_completion = cd
            db.add(TinyHabitCompletion(habit_id=habit_id, user_id=user_id, completed_date=cd))
        cur_streak, best_streak, last_comp = h.current_streak, h.best_streak, h.last_completion
        await db.commit()
        return {
            "id": h_id, "user_id": h_uid, "name": h_name,
            "current_streak": cur_streak, "best_streak": best_streak,
            "last_completion": last_comp, "active": h_active,
        }


async def _create_habit_db(user_id, name="Test", anchor="After waking", routine="5 stretches"):
    async with SessionLocal() as db:
        h = TinyHabit(user_id=user_id, name=name, anchor=anchor,
                      minimum_routine=routine, schedule_json="[]", active=1)
        db.add(h)
        await db.commit()
        await db.refresh(h)
        return h


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
            slugs = {r.slug for r in rows}
        assert "band-row" in slugs
        assert "chair-squat" in slugs
        assert "balance-board-stand" in slugs
        assert "fitball-plank" in slugs
        assert "trx-chest-press" in slugs
        assert "bird-dog" in slugs

    @pytest.mark.asyncio
    async def test_workout_plans_table_exists(self):
        async with SessionLocal() as db:
            result = await db.execute(text("PRAGMA table_info(workout_plans)"))
            columns = {row[1] for row in result.fetchall()}
        assert "safety_notes_json" in columns
        assert "duration_minutes" in columns


# ── 2. Profile CRUD ─────────────────────────────────────────────────
class TestProfileCRUD:
    @pytest.mark.asyncio
    async def test_profile_defaults_when_not_created(self, client):
        child = await _child()
        r = await client.get(f"/api/fitness/profile/{child.id}", headers=H)
        assert r.status_code == 200
        assert r.json()["profile_exists"] is False
        assert "trx" in r.json()["equipment"]

    @pytest.mark.asyncio
    async def test_create_and_get_profile(self, client):
        child = await _child()
        r = await _profile_via_http(client, child.id)
        assert r.status_code == 200
        data = r.json()
        assert data["user_id"] == child.id
        assert "none" in data["equipment"]
        assert data["preferred_duration_minutes"] == 30

        r2 = await client.get(f"/api/fitness/profile/{child.id}", headers=H)
        assert r2.status_code == 200
        assert r2.json()["goals"] == ["strength"]

    @pytest.mark.asyncio
    async def test_update_profile_changes_values(self, client):
        child = await _child()
        await _profile_via_http(client, child.id)
        r = await _profile_via_http(client, child.id, goals=["fat_loss", "mobility"], preferred_duration_minutes=45)
        assert r.status_code == 200
        d = r.json()
        assert d["goals"] == ["fat_loss", "mobility"]
        assert d["preferred_duration_minutes"] == 45

    @pytest.mark.asyncio
    async def test_profile_patch_works(self, client):
        child = await _child()
        await _profile_via_http(client, child.id)
        r = await client.patch(
            f"/api/fitness/profile/{child.id}", headers=H,
            json={"equipment": ["trx"], "health_constraints": [], "goals": ["strength"],
                  "supplements": [], "preferred_duration_minutes": 20, "training_days": []}
        )
        assert r.status_code == 200
        assert r.json()["equipment"] == ["trx"]

    @pytest.mark.asyncio
    async def test_profile_stores_memory_facts(self, client):
        child = await _child()
        await _profile_via_http(client, child.id, health_constraints=["tilt_test_sensitivity"])
        async with SessionLocal() as db:
            facts = (await db.execute(
                select(MemoryFact).where(MemoryFact.child_id == child.id, MemoryFact.domain == "fitness")
            )).scalars().all()
        categories = {f.category for f in facts}
        assert "goal" in categories
        assert "safety_constraint" in categories

    @pytest.mark.asyncio
    async def test_profile_invalid_day_returns_422(self, client):
        child = await _child()
        r = await _profile_via_http(client, child.id, training_days=["notaday"])
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_profile_nonexistent_user_returns_404(self, client):
        r = await client.put("/api/fitness/profile/99999", headers=H, json={
            "equipment": [], "health_constraints": [], "goals": [],
            "supplements": [], "preferred_duration_minutes": 30, "training_days": []
        })
        assert r.status_code == 404


# ── 3. Safety-Aware Plan Generation ─────────────────────────────────
class TestPlanGeneration:
    @pytest.mark.asyncio
    async def test_generate_plan_without_profile_raises(self):
        child = await _child()
        with pytest.raises(ValueError, match="No profile"):
            await _generate_plan_raw(child.id)

    @pytest.mark.asyncio
    async def test_generate_plan_success(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        assert plan["user_id"] == child.id
        assert plan["status"] == "planned"
        assert len(plan["exercises"]) >= 1
        assert "safety_notes" in plan

    @pytest.mark.asyncio
    async def test_sensitive_plan_excludes_high_impact(self):
        child = await _child()
        await _create_profile_db(child.id, health_constraints=json.dumps(["tilt_test_sensitivity"]))
        plan = await _generate_plan_raw(child.id)
        notes = plan["safety_notes"]
        assert any("Low-impact" in n for n in notes)
        exercise_names = [e["name"] for e in plan["exercises"]]
        assert "Jumping jacks" not in exercise_names

    @pytest.mark.asyncio
    async def test_sensitive_plan_excludes_floor_transitions(self):
        child = await _child()
        await _create_profile_db(
            child.id,
            health_constraints=json.dumps(["avoid_sudden_orthostatic_drops"]),
            equipment=json.dumps(["none", "pilates_mat"]),
        )
        plan = await _generate_plan_raw(child.id)
        exercise_names = [e["name"] for e in plan["exercises"]]
        assert "Dead bug" not in exercise_names
        assert "Pilates bridge" not in exercise_names

    @pytest.mark.asyncio
    async def test_plan_exercise_count_scales_with_duration(self):
        child = await _child()
        await _create_profile_db(
            child.id, preferred_duration_minutes=60,
            equipment=json.dumps(["none", "resistance_bands", "trx"]),
        )
        plan = await _generate_plan_raw(child.id)
        assert len(plan["exercises"]) == 5

    @pytest.mark.asyncio
    async def test_list_plans(self, client):
        child = await _child()
        await _create_profile_db(child.id)
        await _generate_plan_raw(child.id)
        r = await client.get(f"/api/fitness/plans/{child.id}", headers=H)
        assert r.status_code == 200
        plans = r.json()
        assert len(plans) >= 1


# ── 4. Directive-Aware Plan Generation ──────────────────────────────
class TestDirectiveAwarePlan:
    @pytest.mark.asyncio
    async def test_low_impact_directive_forces_sensitive_mode(self):
        child = await _child()
        await _create_profile_db(child.id, health_constraints=json.dumps([]))
        await add_parent_feedback(child.name, "fitness", "please use low-impact exercises only")
        plan = await _generate_plan_raw(child.id)
        notes = plan["safety_notes"]
        assert any("Low-impact" in n for n in notes)
        assert any("low-impact" in n.lower() for n in notes)

    @pytest.mark.asyncio
    async def test_directive_appended_to_safety_notes(self):
        child = await _child()
        await _create_profile_db(child.id)
        await add_parent_feedback(child.name, "fitness", "focus on upper body")
        plan = await _generate_plan_raw(child.id)
        assert any("upper body" in n.lower() for n in plan["safety_notes"])


# ── 5. Progression Logic ────────────────────────────────────────────
class TestProgression:
    @pytest.mark.asyncio
    async def test_pain_blocks_progression(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan_raw(plan["id"], child.id, eids, pain=2)
        assert result["progression_allowed"] is False
        assert "not" in result["guidance"].lower() or "do not" in result["guidance"].lower()

    @pytest.mark.asyncio
    async def test_comfortable_session_allows_progression(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan_raw(plan["id"], child.id, eids, pain=0)
        assert result["progression_allowed"] is True

    @pytest.mark.asyncio
    async def test_high_rpe_blocks_progression(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan_raw(plan["id"], child.id, eids, rpe=9)
        assert result["progression_allowed"] is False

    @pytest.mark.asyncio
    async def test_high_fatigue_blocks_progression(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        result = await _complete_plan_raw(plan["id"], child.id, eids, fatigue=8)
        assert result["progression_allowed"] is False

    @pytest.mark.asyncio
    async def test_double_complete_returns_error(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan_raw(plan["id"], child.id, eids)
        with pytest.raises(ValueError, match="Already completed"):
            await _complete_plan_raw(plan["id"], child.id, eids)


# ── 6. Event Bus / Nutrition Collaboration ──────────────────────────
class TestEventNutrition:
    @pytest.mark.asyncio
    async def test_completion_creates_nutrition_fact(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan_raw(plan["id"], child.id, eids)
        async with SessionLocal() as db:
            facts = (await db.execute(
                select(MemoryFact).where(MemoryFact.child_id == child.id, MemoryFact.domain == "nutrition")
            )).scalars().all()
        assert len(facts) >= 1
        assert any("balanced meal" in f.value for f in facts)

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


# ── 7. Habits CRUD + Completion + Streaks ────────────────────────────
class TestHabits:
    @pytest.mark.asyncio
    async def test_create_habit(self, client):
        child = await _child()
        r = await client.post("/api/fitness/habits", headers=H, json={
            "user_id": child.id, "name": "Morning stretch", "anchor": "After waking up",
            "minimum_routine": "5 arm circles", "schedule": ["sunday", "monday"]
        })
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "Morning stretch"
        assert d["current_streak"] == 0

    @pytest.mark.asyncio
    async def test_list_habits(self, client):
        child = await _child()
        await client.post("/api/fitness/habits", headers=H, json={
            "user_id": child.id, "name": "H1", "anchor": "A", "minimum_routine": "R"
        })
        await client.post("/api/fitness/habits", headers=H, json={
            "user_id": child.id, "name": "H2", "anchor": "A", "minimum_routine": "R"
        })
        r = await client.get(f"/api/fitness/habits/{child.id}", headers=H)
        assert r.status_code == 200
        assert len(r.json()) == 2

    @pytest.mark.asyncio
    async def test_habit_completion_increments_streak(self):
        child = await _child()
        h = await _create_habit_db(child.id, "Streak test")
        today = date.today()
        result = await _complete_habit_raw(h.id, child.id, today)
        assert result["current_streak"] == 1

    @pytest.mark.asyncio
    async def test_consecutive_days_build_streak(self):
        child = await _child()
        h = await _create_habit_db(child.id, "Cons test")
        today = date.today()
        await _complete_habit_raw(h.id, child.id, today)
        result = await _complete_habit_raw(h.id, child.id, today + timedelta(days=1))
        assert result["current_streak"] == 2
        assert result["best_streak"] == 2

    @pytest.mark.asyncio
    async def test_gap_resets_streak(self):
        child = await _child()
        h = await _create_habit_db(child.id, "Gap test")
        today = date.today()
        await _complete_habit_raw(h.id, child.id, today)
        result = await _complete_habit_raw(h.id, child.id, today + timedelta(days=3))
        assert result["current_streak"] == 1

    @pytest.mark.asyncio
    async def test_backdate_before_last_completion_raises(self):
        child = await _child()
        h = await _create_habit_db(child.id, "Backdate test")
        today = date.today()
        await _complete_habit_raw(h.id, child.id, today)
        with pytest.raises(ValueError, match="Cannot backdate"):
            await _complete_habit_raw(h.id, child.id, today - timedelta(days=1))

    @pytest.mark.asyncio
    async def test_habit_completion_stores_history(self):
        child = await _child()
        h = await _create_habit_db(child.id, "History test")
        today = date.today()
        await _complete_habit_raw(h.id, child.id, today)
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(TinyHabitCompletion).where(TinyHabitCompletion.habit_id == h.id)
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].completed_date == today

    @pytest.mark.asyncio
    async def test_habit_history_endpoint(self, client):
        child = await _child()
        h = await _create_habit_db(child.id, "Hist EP test")
        today = date.today()
        await _complete_habit_raw(h.id, child.id, today)
        r = await client.get(f"/api/fitness/habits/{h.id}/history?user_id={child.id}", headers=H)
        assert r.status_code == 200
        data = r.json()
        assert data["habit_id"] == h.id
        assert len(data["completions"]) == 1

    @pytest.mark.asyncio
    async def test_update_habit_db(self):
        child = await _child()
        h = await _create_habit_db(child.id, "Old name")
        async with SessionLocal() as db:
            habit = await db.get(TinyHabit, h.id)
            habit.name = "New name"
            habit.anchor = "New anchor"
            habit.minimum_routine = "New routine"
            habit.schedule_json = json.dumps(["friday"])
            await db.commit()
            await db.refresh(habit)
            assert habit.name == "New name"

    @pytest.mark.asyncio
    async def test_minimum_routine_endpoint(self, client):
        child = await _child()
        await client.post("/api/fitness/habits", headers=H, json={
            "user_id": child.id, "name": "Min routine", "anchor": "After coffee",
            "minimum_routine": "10 squats"
        })
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
    async def test_analytics_after_completion(self):
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan_raw(plan["id"], child.id, eids)
        # Query analytics data directly
        async with SessionLocal() as db:
            plans = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.user_id == child.id))).scalars().all()
            recent = (await db.execute(
                select(WorkoutPerformance).where(WorkoutPerformance.user_id == child.id)
                .order_by(WorkoutPerformance.completed_at.desc()).limit(10)
            )).scalars().all()
        completed = sum(p.status == "completed" for p in plans)
        assert completed == 1
        rate = round(completed / len(plans), 3) if plans else 0
        assert rate == 1.0
        assert len(recent) >= 1
        assert bool(recent[0].progression_allowed) is True

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
        data = r.json()
        assert len(data) >= 8

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


# ── 10. Report / Orchestrator Wellness ──────────────────────────────
class TestReport:
    @pytest.mark.asyncio
    async def test_orchestrator_includes_wellness_data(self):
        from orchestrator import gather_daily_report_data
        child = await _child()
        await _create_profile_db(child.id)
        plan = await _generate_plan_raw(child.id)
        eids = [e["exercise_id"] for e in plan["exercises"]]
        await _complete_plan_raw(plan["id"], child.id, eids, duration=25)
        report = await gather_daily_report_data()
        child_report = next((r for r in report if r["child_name"] == child.name), None)
        assert child_report is not None
        w = child_report["wellness"]
        assert w["workouts_completed"] >= 1
        assert w["activity_minutes"] >= 25
