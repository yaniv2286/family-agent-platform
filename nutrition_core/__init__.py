"""Deterministic nutrition core for the meal_coach agent.

Pure Python: no FastAPI, SQLAlchemy, network or model imports. All nutrient
arithmetic is decimal-safe; a missing nutrient value is UNKNOWN (None), never
silently zero. Implements the invariants of docs/DESIGN.md sections 5-8 for
the family (parent-as-coach, non-clinical) slice described in
docs/ADR-001-nutrition-coach.md.
"""

ENGINE_VERSION = "nutrition-core/1.0.0"
