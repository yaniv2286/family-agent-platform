---
name: nutrition-agent
description: Work on the Nutrition agent - an event-only collaborator that provides post-workout recommendations via the memory layer.
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

You are the Nutrition Agent specialist for the Koko platform.

Scope: `nutrition_agent.py` and any tests that exercise `fitness.workout_completed` event handling.

When working on this skill:

1. **Event-driven only.** The Nutrition agent has no HTTP router. It collaborates by registering a handler for the `fitness.workout_completed` event via `agent_registry.register("fitness.workout_completed", handle_workout_completed)`.

2. **Health-aware recommendation.** The handler loads the child's `UserFitnessProfile` to read `health_constraints` and produces a conservative, non-prescriptive post-workout recommendation.

3. **Memory persistence.** The recommendation is stored as a `MemoryFact` via `memory_manager.add_fact()` with domain `nutrition`, category `recommendation`, key `post_workout_recommendation`. Include event/workout evidence and a realistic confidence score.

4. **No medical claims.** The recommendation text must stay general: water, balanced meal/snack with carbohydrate and protein when hungry, and follow known health constraints or clinician guidance.

5. **Keep the import side-effect.** `fitness_agent.py` imports `nutrition_agent` so the event handler is registered at import time. If you move the module, update that import and any registration wiring.

6. **Test before finishing.** Nutrition behavior is exercised through the fitness tests. Run:
   ```powershell
   python -m pytest tests/test_fitness_agent.py -v -k nutrition
   ```
   (If no `-k nutrition` match exists, run the full `tests/test_fitness_agent.py` suite.)
