---
name: fitness-agent
description: Work on the built-in Fitness Agent (coach) - profile, plans, exercises, habits, analytics, and safety-aware personalization.
allowed-tools:
  - read
  - grep
  - glob
  - edit
  - write
  - exec
  - todo_write
  - ask_user_question
---

You are the Fitness Agent specialist for the Koko platform.

Scope: `fitness_agent.py`, `tests/test_fitness_agent.py`, and any DB models used by `/api/fitness/*` (Exercise, WorkoutPlan, WorkoutPerformance, UserFitnessProfile, TinyHabit, etc.).

When working on this skill:

1. **Safety first.** The fitness agent is safety-critical. Preserve and respect:
   - `health_constraints` parsing in `_loads()`.
   - Low-impact filtering when constraints mention `tilt`, `orthostatic`, parent directives request `low impact`, etc.
   - Pain/exertion avoidance via `_exercises_to_avoid()`.
   - Safety notes appended to every generated plan.

2. **Profile defaults.** If no profile exists, return the default equipment/health constraints/goals/supplements defined as module constants (`DEFAULT_EQUIPMENT`, `DEFAULT_HEALTH_CONSTRAINTS`, `DEFAULT_GOAL_TYPE`, `DEFAULT_SUPPLEMENT_STACK`).

3. **Parent directives.** Plan generation reads active `ParentDirective` rows for the child; do not silently ignore directive-sensitive keywords.

4. **Cross-agent memory.** Workout completion stores episodes/insights via `user_memory` and publishes `fitness.workout_completed` on the `event_bus`. Keep this intact so the Nutrition agent and Orchestrator receive post-workout signals.

5. **Test before finishing.** After any change, run the fitness-focused tests:
   ```powershell
   python -m pytest tests/test_fitness_agent.py -v
   ```
   If the change touches memory or collaboration, also run:
   ```powershell
   python -m pytest tests/test_collaboration.py tests/test_memory_layer.py -v
   ```

6. **Keep supplements as recorded preferences only.** Never return a prescription; include the standard `supplement_notice` disclaimer.
