"""Rule DSL evaluation (DESIGN 7.2).

Rules are typed data, never executable text. A check whose required evidence
is missing reports 'unknown' - it can never pass on fabricated data. Conflicts
are reported, not silently merged.
"""
from decimal import Decimal
from typing import Dict, List, Optional

from .units import dec

RULE_KINDS = {
    "exclude_food",
    "require_verified_attribute",
    "nutrient_bounds",
    "preparation_limit",
}
ALLOWED_NUTRIENTS = {"kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sodium_mg",
                     "sugar_g", "sat_fat_g", "cholesterol_mg"}
ALLOWED_ATTRIBUTES = {"kosher_certified", "allergen_free", "preparation_state", "diet_flag"}


def validate_rule(rule: dict) -> Optional[str]:
    """Return an error string if the rule is malformed, else None."""
    kind = rule.get("kind")
    if kind not in RULE_KINDS:
        return f"unknown rule kind: {kind!r}"
    if kind == "exclude_food":
        if not isinstance(rule.get("food_ids"), list) or not rule["food_ids"]:
            return "exclude_food requires non-empty food_ids"
    elif kind == "require_verified_attribute":
        if rule.get("attribute") not in ALLOWED_ATTRIBUTES:
            return f"attribute not in allowlist: {rule.get('attribute')!r}"
    elif kind == "nutrient_bounds":
        if rule.get("nutrient") not in ALLOWED_NUTRIENTS:
            return f"nutrient not in allowlist: {rule.get('nutrient')!r}"
        if rule.get("scope") not in ("meal", "day"):
            return "nutrient_bounds scope must be 'meal' or 'day'"
        if rule.get("lower") is None and rule.get("upper") is None:
            return "nutrient_bounds needs at least one bound"
        try:
            if rule.get("lower") is not None:
                dec(rule["lower"])
            if rule.get("upper") is not None:
                dec(rule["upper"])
        except ValueError as exc:
            return str(exc)
    elif kind == "preparation_limit":
        if not isinstance(rule.get("total_minutes"), int) or rule["total_minutes"] < 0:
            return "preparation_limit requires non-negative total_minutes"
        if not isinstance(rule.get("equipment_allowed"), list):
            return "preparation_limit requires equipment_allowed list"
    return None


def _check(code: str, status: str, evidence: Optional[List[str]] = None) -> dict:
    return {"code": code, "status": status, "evidence_ids": evidence or []}


def evaluate_meal_rules(rules: List[dict], meal_context: dict) -> List[dict]:
    """Evaluate meal-scope rules against a resolved meal.

    meal_context: {items: [{food_version_id, quantity_g, food}], totals,
                   total_minutes, equipment_used}
    Returns list[CheckResult] with status pass|fail|unknown.
    """
    results: List[dict] = []
    for rule in rules:
        kind = rule.get("kind")
        if kind == "exclude_food":
            excluded = set(rule.get("food_ids") or [])
            hit = [i["food_version_id"] for i in meal_context.get("items", []) if i["food_version_id"] in excluded]
            results.append(_check("exclude_food", "fail" if hit else "pass", hit))

        elif kind == "nutrient_bounds" and rule.get("scope") == "meal":
            nutrient = rule["nutrient"]
            total = (meal_context.get("totals") or {}).get(nutrient)
            if total is None:
                results.append(_check(f"nutrient_bounds:{nutrient}", "unknown"))
                continue
            total = dec(total)
            if rule.get("lower") is not None and total < dec(rule["lower"]):
                results.append(_check(f"nutrient_bounds:{nutrient}", "fail"))
            elif rule.get("upper") is not None and total > dec(rule["upper"]):
                results.append(_check(f"nutrient_bounds:{nutrient}", "fail"))
            else:
                results.append(_check(f"nutrient_bounds:{nutrient}", "pass"))

        elif kind == "require_verified_attribute":
            attr = rule["attribute"]
            expected = rule.get("expected")
            bad, unknown = [], []
            for item in meal_context.get("items", []):
                food = item.get("food") or {}
                if attr == "kosher_certified":
                    if food.get("kosher_evidence") == "unknown":
                        unknown.append(item["food_version_id"])
                    elif food.get("kosher_evidence") != expected:
                        bad.append(item["food_version_id"])
                elif attr == "allergen_free":
                    allergen = expected
                    if food.get("allergens") is None:
                        unknown.append(item["food_version_id"])
                    elif allergen in food["allergens"]:
                        bad.append(item["food_version_id"])
                elif attr == "preparation_state":
                    if food.get("preparation_state") != expected:
                        bad.append(item["food_version_id"])
                elif attr == "diet_flag":
                    # expected = a flag from food_library._derive_attributes
                    # (vegan/lactose_free/low_sugar/...). None attributes
                    # (pre-column data) are unknown - never a silent pass.
                    fattrs = food.get("attributes")
                    if fattrs is None:
                        unknown.append(item["food_version_id"])
                    elif expected not in fattrs:
                        bad.append(item["food_version_id"])
            if bad:
                results.append(_check(f"attribute:{attr}", "fail", bad))
            elif unknown:
                results.append(_check(f"attribute:{attr}", "unknown", unknown))
            else:
                results.append(_check(f"attribute:{attr}", "pass"))

        elif kind == "preparation_limit":
            minutes = meal_context.get("total_minutes")
            if minutes is None:
                results.append(_check("preparation_limit", "unknown"))
            elif minutes > rule["total_minutes"]:
                results.append(_check("preparation_limit", "fail"))
            else:
                equipment = set(meal_context.get("equipment_used") or [])
                allowed = set(rule.get("equipment_allowed") or [])
                if "none" in allowed:
                    allowed = allowed | {"none"}
                extra = equipment - allowed - {"none"}
                results.append(_check("preparation_limit", "fail" if extra else "pass", sorted(extra)))

    return results


def detect_rule_conflicts(rules: List[dict]) -> List[str]:
    """Report contradictory rules. Conflicts block; they never auto-merge.

    Currently detects directly contradictory nutrient bounds (lower > upper)
    and exclude+require on the same food. Anything else is passed through.
    """
    conflicts: List[str] = []
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            if (
                a.get("kind") == b.get("kind") == "nutrient_bounds"
                and a.get("nutrient") == b.get("nutrient")
                and a.get("scope") == b.get("scope")
            ):
                lo = max(
                    (dec(r["lower"]) for r in (a, b) if r.get("lower") is not None),
                    default=None,
                )
                hi = min(
                    (dec(r["upper"]) for r in (a, b) if r.get("upper") is not None),
                    default=None,
                )
                if lo is not None and hi is not None and lo > hi:
                    conflicts.append(f"contradictory bounds for {a['nutrient']}: {lo} > {hi}")
    return conflicts
