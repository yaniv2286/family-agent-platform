"""Deterministic nutrition engine (DESIGN 6.4/7/8).

Pure functions, decimal-safe, catalog-source driven. Unknown propagates;
retracted catalog data cannot enter new plans or proposals; bounded search
exhaustion is reported, never silently treated as infeasible.
"""
import hashlib
import itertools
import json
from decimal import Decimal
from typing import Dict, List, Optional

from .catalog import DEFAULT_SOURCE, CatalogSource
from .rules import detect_rule_conflicts, evaluate_meal_rules, validate_rule
from .units import dec, dstr, scale_per_basis

NUTRIENT_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sodium_mg",
                   "sugar_g", "sat_fat_g", "cholesterol_mg")
SLOT_KEYS = ("breakfast", "snack", "lunch", "snack2", "dinner", "evening_snack")
# Plan slots are finer than recipe-declared slots: any recipe approved for a
# family (e.g. "snack") may serve any plan slot in that family (snack, snack2,
# evening_snack). The coach picks the child's structure; recipes stay coarse.
SLOT_FAMILY = {"snack2": "snack", "evening_snack": "snack"}


def slot_family(slot_key: str) -> str:
    return SLOT_FAMILY.get(slot_key, slot_key)


def recipe_covers_slot(recipe: dict, slot_key: str) -> bool:
    fams = {slot_family(s) for s in recipe.get("slots") or []}
    return not fams or slot_family(slot_key) in fams

# Engineering caps (DESIGN 7.4.4): resource limits, not nutritional policy.
MAX_COMBINATIONS_PER_RECIPE = 2000
MAX_CANDIDATES = 100

# Smart composition (ad-hoc dishes from the slot's approved food basket):
# 1-3 basket foods, each within a quantity grid derived from the ranges the
# catalog's recipes actually use for that food. These caps bound the search;
# they are not nutritional policy.
MAX_COMPOSITION_ITEMS = 3
MAX_COMPOSITION_STEPS = 8
MAX_COMPOSITION_COMBINATIONS = MAX_COMBINATIONS_PER_RECIPE
# Conservative fallback range for a basket food with no usable observed
# recipe range - only bounds the search grid, never invents nutrient data.
COMPOSITION_DEFAULT_MIN_G = Decimal("25")
COMPOSITION_DEFAULT_MAX_G = Decimal("150")
COMPOSITION_DEFAULT_STEP_G = Decimal("25")


def canonical_hash(content: dict) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# totals
# ---------------------------------------------------------------------------
def item_totals(
    food_version_id: str, quantity_g: str,
    food: Optional[dict] = None, src: CatalogSource = DEFAULT_SOURCE,
) -> Dict[str, Optional[Decimal]]:
    food = food or src.get_food_version(food_version_id)
    if not food:
        raise ValueError(f"unknown food_version_id: {food_version_id}")
    out: Dict[str, Optional[Decimal]] = {}
    nutrients = food.get("nutrients") or {}
    for field in NUTRIENT_FIELDS:
        per_basis = nutrients.get(field)
        out[field] = None if per_basis is None else scale_per_basis(
            per_basis, food["basis_g"], quantity_g
        )
    return out


def meal_totals(items: List[dict], src: CatalogSource = DEFAULT_SOURCE) -> Dict[str, Optional[Decimal]]:
    totals: Dict[str, Optional[Decimal]] = {f: Decimal(0) for f in NUTRIENT_FIELDS}
    for item in items:
        per_item = item_totals(item["food_version_id"], item["quantity_g"], item.get("food"), src)
        for field in NUTRIENT_FIELDS:
            if totals[field] is None or per_item[field] is None:
                totals[field] = None
            else:
                totals[field] += per_item[field]
    return totals


def serialize_totals(totals: Dict[str, Optional[Decimal]]) -> Dict[str, Optional[str]]:
    return {k: dstr(v) for k, v in totals.items()}


def snapshot_item(item: dict, food: dict, src: CatalogSource = DEFAULT_SOURCE) -> dict:
    totals = item_totals(item["food_version_id"], item["quantity_g"], food, src)
    return {
        "food_version_id": item["food_version_id"],
        "name_he": food.get("name_he"),
        "quantity_g": str(item["quantity_g"]),
        "nutrients": serialize_totals(totals),
        "source": food.get("evidence", "unknown"),
    }


# ---------------------------------------------------------------------------
# plan content validation
# ---------------------------------------------------------------------------
def _validate_targets(targets: dict, where: str, issues: List[str]) -> None:
    for nutrient, bounds in targets.items():
        if nutrient not in NUTRIENT_FIELDS:
            issues.append(f"{where}: unknown nutrient target {nutrient!r}")
            continue
        if not isinstance(bounds, dict):
            issues.append(f"{where}.{nutrient}: bounds must be an object")
            continue
        try:
            lo = dec(bounds["lower"]) if bounds.get("lower") is not None else None
            hi = dec(bounds["upper"]) if bounds.get("upper") is not None else None
        except ValueError:
            issues.append(f"{where}.{nutrient}: bounds must be decimals")
            continue
        if lo is not None and hi is not None and lo > hi:
            issues.append(f"{where}.{nutrient}: lower bound above upper bound")


def plan_rule_issues(resolved_meals: List[dict], rules: List[dict]) -> List[dict]:
    """Meal-scope and day-scope rule violations for resolved plan meals.

    Returns structured issues ({rule, nutrient, scope, slot_key, status,
    total, lower, upper}) so callers can map them to Hebrew. Medical/profile
    rules are enforced here, never generated (DESIGN 1.2/7). A nutrient a
    bound depends on that is unknown makes the issue fire - unknown never
    passes silently (fail-closed, B07)."""
    issues: List[dict] = []
    meal_rules = [
        r for r in rules
        if r.get("kind") != "nutrient_bounds" or r.get("scope") == "meal"
    ]
    day_bounds = [
        r for r in rules
        if r.get("kind") == "nutrient_bounds" and r.get("scope") == "day"
    ]

    for i, meal in enumerate(resolved_meals):
        ctx = {
            "items": meal["items"],
            "totals": meal["totals"],
            "total_minutes": meal.get("total_minutes"),
            "equipment_used": meal.get("equipment_used") or [],
        }
        for c in evaluate_meal_rules(meal_rules, ctx):
            if c["status"] == "pass":
                continue
            if c["code"].startswith("nutrient_bounds:"):
                n = c["code"].split(":", 1)[1]
                rule = next(
                    (r for r in meal_rules
                     if r.get("kind") == "nutrient_bounds" and r.get("nutrient") == n),
                    {},
                )
                issues.append({
                    "rule": "nutrient_bounds", "nutrient": n, "scope": "meal",
                    "slot_key": meal["slot_key"], "status": c["status"],
                    "total": meal["totals"].get(n),
                    "lower": rule.get("lower"), "upper": rule.get("upper"),
                })
            else:
                issues.append({
                    "rule": c["code"], "slot_key": meal["slot_key"],
                    "status": c["status"], "detail": c.get("detail"),
                })

    if day_bounds:
        sums: Dict[str, Optional[Decimal]] = {f: Decimal(0) for f in NUTRIENT_FIELDS}
        for meal in resolved_meals:
            for f in NUTRIENT_FIELDS:
                v = meal["totals"].get(f)
                if sums[f] is None or v is None:
                    sums[f] = None
                else:
                    sums[f] += dec(v)
        for r in day_bounds:
            n = r["nutrient"]
            total = sums.get(n)
            base = {"rule": "nutrient_bounds", "nutrient": n, "scope": "day",
                    "lower": r.get("lower"), "upper": r.get("upper")}
            if total is None:
                issues.append({**base, "status": "unknown", "total": None})
                continue
            if r.get("upper") is not None and total > dec(r["upper"]):
                issues.append({**base, "status": "fail", "total": dstr(total)})
            if r.get("lower") is not None and total < dec(r["lower"]):
                issues.append({**base, "status": "fail", "total": dstr(total)})
    return issues


def validate_plan_content(content: dict, src: CatalogSource = DEFAULT_SOURCE,
                          extra_rules: Optional[List[dict]] = None) -> dict:
    """Full deterministic validation of a draft plan (DESIGN 6.4 step 4).

    extra_rules = profile + active clinical bundle rules: every planned meal
    is checked at meal scope and the sum of planned meals at day scope."""
    issues: List[str] = []
    meals = content.get("meals")
    if not isinstance(meals, list) or not meals:
        return {"valid": False, "issues": ["plan must contain at least one meal"], "meals": []}

    _validate_targets(content.get("day_targets") or {}, "day_targets", issues)

    # Optional coach-chosen meal structure (DESIGN: children can have 3-6
    # meals). Absent = legacy plan; present = every planned/alternative slot
    # must belong to the declared structure.
    declared_slots = content.get("meal_slots")
    if declared_slots is not None:
        if not isinstance(declared_slots, list) or not declared_slots:
            issues.append("meal_slots must be a non-empty list of slot keys")
            declared_slots = None
        else:
            if any(s not in SLOT_KEYS for s in declared_slots):
                issues.append(f"meal_slots: every slot must be one of {SLOT_KEYS}")
            elif len(set(declared_slots)) != len(declared_slots):
                issues.append("meal_slots: duplicate slot keys")
            else:
                declared_slots = set(declared_slots)

    alternatives = content.get("alternatives") or {}
    if not isinstance(alternatives, dict):
        issues.append("alternatives must be {slot_key: [recipe_version_id, ...]}")
        alternatives = {}
    for slot, rids in alternatives.items():
        if slot not in SLOT_KEYS:
            issues.append(f"alternatives: unknown slot {slot!r}")
            continue
        if declared_slots and slot not in declared_slots:
            issues.append(f"alternatives: slot {slot!r} not in declared meal_slots")
            continue
        if not isinstance(rids, list):
            issues.append(f"alternatives.{slot}: must be a list")
            continue
        for rid in rids:
            recipe = src.get_recipe_version(rid)
            if not recipe:
                issues.append(f"alternatives.{slot}: unknown recipe_version_id {rid!r}")
            elif not recipe_covers_slot(recipe, slot):
                issues.append(f"alternatives.{slot}: recipe {rid} not approved for slot {slot!r}")

    seen_slots = set()
    resolved_meals = []
    for idx, meal in enumerate(meals):
        where = f"meals[{idx}]"
        if not isinstance(meal, dict):
            issues.append(f"{where}: not an object")
            continue
        slot = meal.get("slot_key")
        if slot not in SLOT_KEYS:
            issues.append(f"{where}: slot_key must be one of {SLOT_KEYS}")
            continue
        if declared_slots and slot not in declared_slots:
            issues.append(f"{where}: slot {slot!r} not in declared meal_slots")
            continue
        if slot in seen_slots:
            issues.append(f"{where}: duplicate slot_key {slot!r}")
        seen_slots.add(slot)

        items = meal.get("items")
        if not isinstance(items, list) or not items:
            issues.append(f"{where}: must contain at least one item")
            continue

        recipe = None
        rid = meal.get("recipe_version_id")
        if rid:
            recipe = src.get_recipe_version(rid)
            if not recipe:
                issues.append(f"{where}: unknown recipe_version_id {rid!r}")
            elif not recipe_covers_slot(recipe, slot):
                issues.append(f"{where}: recipe {rid} is not approved for slot {slot!r}")

        recipe_items = {i["food_version_id"]: i for i in recipe["items"]} if recipe else {}
        resolved_items = []
        for j, item in enumerate(items):
            iwhere = f"{where}.items[{j}]"
            fid = item.get("food_version_id")
            food = src.get_food_version(fid) if fid else None
            if not food:
                issues.append(f"{iwhere}: unknown food_version_id {fid!r}")
                continue
            if food["status"] != "approved":
                issues.append(f"{iwhere}: food version {fid} is {food['status']}")
                continue
            try:
                qty = dec(item.get("quantity_g"))
            except ValueError:
                issues.append(f"{iwhere}: quantity_g is not a valid decimal")
                continue
            if qty < 0:
                issues.append(f"{iwhere}: quantity_g must be non-negative")
                continue
            if recipe_items:
                spec = recipe_items.get(fid)
                if not spec:
                    issues.append(f"{iwhere}: {fid} is not an ingredient of {rid}")
                    continue
                lo, hi, step = dec(spec["min_g"]), dec(spec["max_g"]), dec(spec["step_g"])
                if step <= 0 or qty < lo or qty > hi or (qty - lo) % step != 0:
                    issues.append(
                        f"{iwhere}: quantity_g {qty} outside [{lo},{hi}] step {step} for {rid}"
                    )
                    continue
            resolved_items.append({"food_version_id": fid, "quantity_g": format(qty, "f"), "food": food})

        totals = meal_totals(resolved_items, src) if resolved_items else {f: None for f in NUTRIENT_FIELDS}
        resolved_meals.append({
            "slot_key": slot,
            "recipe_version_id": rid,
            "items": resolved_items,
            "totals": serialize_totals(totals),
            "total_minutes": recipe["total_minutes"] if recipe else None,
            "equipment_used": recipe["equipment"] if recipe else [],
        })

    rules = content.get("rules") or []
    for r_idx, rule in enumerate(rules):
        err = validate_rule(rule)
        if err:
            issues.append(f"rules[{r_idx}]: {err}")
    issues.extend(detect_rule_conflicts(rules))

    # Profile + clinical bundle rules: enforced on the plan, never generated.
    if extra_rules:
        issues.extend(plan_rule_issues(resolved_meals, extra_rules))

    return {
        "valid": not issues,
        "issues": issues,
        "meals": resolved_meals,
        "rules": rules,
        "day_targets": content.get("day_targets") or {},
        "alternatives": alternatives,
        "meal_slots": list(declared_slots) if declared_slots else None,
    }


# ---------------------------------------------------------------------------
# coach auto-compose: deterministic first-draft day builder.
# The coach picks the structure + kosher split; we fill slots with foods whose
# kind/kosher class fit and size portions to a share of the day targets. This
# is a draft helper only - validate/approve/publish stays authoritative.
# ---------------------------------------------------------------------------
_AUTO_PROTEIN_KINDS = {
    "meat":  ("poultry", "meat"),
    "dairy": ("dairy", "egg"),
    "parve": ("fish", "egg", "legume", "nut_seed"),
    "any":   ("poultry", "meat", "fish", "seafood", "egg", "dairy", "legume", "nut_seed"),
}
_AUTO_KOSHER_ALLOW = {
    "meat": {"meat", "parve"},
    "dairy": {"dairy", "parve"},
    "parve": {"parve"},
    "any": {"meat", "dairy", "parve"},
}
_AUTO_CARB_KINDS = ("bread", "grain", "legume")
_AUTO_VEG_KINDS = ("vegetable",)
_AUTO_FRUIT_KINDS = ("fruit",)
_AUTO_FAT_KINDS = ("fat_oil", "nut_seed", "sauce")
# Per-slot fallbacks when the draft has no day_targets yet.
_AUTO_DEFAULT_BUDGET = {"kcal_upper": 500, "protein_lower": 25,
                        "carbs_upper": 50, "fat_upper": 15}
_AUTO_SNACK_SHARE = Decimal("0.5")
# Real-meal preference order (exact name_he, matched to the catalog): a
# suggestion should read like food a kid actually eats, not a nutrient
# optimum. Anything not listed still competes via the numeric pool sort.
_AUTO_PREFER_PROTEIN = {
    "meat":  ["חזה עוף צלוי בתיבול", "חזה הודו צלוי", "עוף בגריל", "פסטרמה הודו",
              "בשר בקר מבושל רזה", "בשר טחון בקר 5%", "טחון הודו",
              "פרגית הודו טרייה", "חזה עוף מעושן"],
    "dairy": ["קוטג׳ 5%", "יוגורט יווני 5%", "גבינה לבנה 5%", "ביצה קשה",
              "חביתה", "יוגורט יווני 0%", "גבינה לבנה 0%", "קוטג׳ 0%",
              "ביצה עלומה", "קוטג׳ 3%"],
    "parve": ["טונה במים (משומר)", "סלמון צלוי", "ביצה קשה", "טונה טרייה צלויה",
              "חומוס מוכן (ממרח)", "עדשים כתומות מבושלות",
              "שעועית לבנה מבושלת", "חביתה", "ביצה עלומה"],
    "any":   ["קוטג׳ 5%", "חזה עוף צלוי בתיבול", "ביצה קשה", "יוגורט יווני 5%",
              "טונה במים (משומר)", "חזה הודו צלוי", "גבינה לבנה 5%", "סלמון צלוי",
              "בשר בקר מבושל רזה", "חומוס מוכן (ממרח)"],
}
_AUTO_PREFER_CARB = ["אורז בסמטי מבושל", "לחם פרוס מלא", "פסטה מלאה מבושלת",
                     "בטטה בתנור", "תפוח אדמה בתנור", "קוסקוס מבושל",
                     "לחם שחור / דגנים", "קינואה מבושלת", "אורז מלא מבושל",
                     "שיבולת שועל גולמית", "בורגול מבושל", "פסטה לבנה מבושלת"]
_AUTO_PREFER_VEG = ["מלפפון בשיל", "עגבניית שרי", "חסה", "פלפל אדום", "גזר",
                    "קישוא", "ברוקולי", "פלפל ירוק", "חסה רומי", "פלפל קלוי"]
_AUTO_PREFER_FAT = ["אבוקדו", "טחינה מוכנה (סלט)", "שמן זית", "שקדים",
                    "אגוזי מלך", "גרעיני דלעת", "טחינה מלאה"]
_AUTO_PREFER_FRUIT = ["תפוח עץ", "בננה", "תות שדה", "אשכולית", "מלון",
                      "אבטיח", "תפוז", "ענבים", "אגס"]
# Never a meal component even if the nutrients fit: supplements and
# liquid/powdered products need a human hand, not an auto-composer.
_AUTO_NO_COMPONENT = ("אבקת", "אבקה", "משקה", "חלבון ביצה", "שמנת", "חמאה",
                      "מרגרינה")
_AUTO_NO_SIDE = ("קמח", "עמילן", "סולת", "דגני", "אבקת")
_AUTO_NO_VEG = ("חמוץ", "כבוש", "מיובש", "מיץ")
# Realistic caps: nobody serves 350g of egg whites or half a kilo of rice.
_AUTO_CAP = {"protein": Decimal(250), "side": Decimal(200), "veg": Decimal(200),
             "fat": Decimal(30), "fruit": Decimal(200)}
_AUTO_VEG_QTY = Decimal(150)
_AUTO_MIN_FAT_QTY = Decimal(10)


def _per100(food: dict, nutrient: str) -> Optional[Decimal]:
    v = (food.get("nutrients") or {}).get(nutrient)
    return dec(v) if v is not None else None


def _round_qty(qty: Decimal, step: Decimal = Decimal(10)) -> Decimal:
    return max(step, (qty / step).to_integral_value() * step)


def _auto_totals(items) -> dict:
    tot = {}
    for f, qty in items:
        for n in ("kcal", "protein_g", "carbs_g", "fat_g"):
            p = _per100(f, n)
            tot[n] = (tot.get(n) or Decimal(0)) + (p * qty / 100 if p is not None else Decimal(0))
    return tot


def _auto_fits(items, p_lo, k_up, c_up, f_up) -> bool:
    t = _auto_totals(items)
    return (t["protein_g"] >= p_lo and t["kcal"] <= k_up
            and t["fat_g"] <= f_up and t["carbs_g"] <= c_up)


def _auto_over(items, p_lo, k_up, c_up, f_up) -> Decimal:
    t = _auto_totals(items)
    return (max(Decimal(0), t["kcal"] - k_up) + max(Decimal(0), t["fat_g"] - f_up)
            + max(Decimal(0), t["carbs_g"] - c_up)
            + max(Decimal(0), p_lo - t["protein_g"]))


def _auto_bound(day_targets: dict, name: str, direction: str) -> Optional[Decimal]:
    v = ((day_targets or {}).get(name) or {}).get(direction)
    return dec(v) if v is not None else None


def _auto_slot_budgets(slots: List[dict], day_targets: dict) -> List[dict]:
    """Each slot's share of the day budget: snack-family slots get half a
    main meal's share."""
    weights = [_AUTO_SNACK_SHARE if slot_family(s["slot_key"]) == "snack"
               else Decimal(1) for s in slots]
    total_w = sum(weights) or Decimal(1)
    budgets = []
    for w in weights:
        share = w / total_w
        budgets.append({
            "p_lo": (_auto_bound(day_targets, "protein_g", "lower") or
                     Decimal(_AUTO_DEFAULT_BUDGET["protein_lower"]) * total_w) * share,
            "k_up": (_auto_bound(day_targets, "kcal", "upper") or
                     Decimal(_AUTO_DEFAULT_BUDGET["kcal_upper"]) * total_w) * share,
            "c_up": (_auto_bound(day_targets, "carbs_g", "upper") or
                     Decimal(_AUTO_DEFAULT_BUDGET["carbs_upper"]) * total_w) * share,
            "f_up": (_auto_bound(day_targets, "fat_g", "upper") or
                     Decimal(_AUTO_DEFAULT_BUDGET["fat_upper"]) * total_w) * share,
            "is_snack": w < 1,
        })
    return budgets


def _auto_pool(usable: List[dict], kosher: str, kinds, need_nutrient: str,
               sort_mode: str = "lean", prefer=None, skip=()) -> List[dict]:
    """Candidate pool: kosher/kind/name filters, preference names first,
    then the numeric sort."""
    allow = _AUTO_KOSHER_ALLOW.get(kosher, _AUTO_KOSHER_ALLOW["any"])
    pool = [f for f in usable
            if f.get("kind") in kinds
            and f.get("kosher_evidence") in allow
            and _per100(f, need_nutrient) is not None
            and not any(w in (f.get("name_he") or "") for w in skip)]
    pref = [f for n in (prefer or ())
            for f in pool if f.get("name_he") == n]
    seen = {f["food_version_id"] for f in pref}
    rest = [f for f in pool if f["food_version_id"] not in seen]
    if sort_mode == "lean":   # lowest fat, then densest in need_nutrient
        rest.sort(key=lambda f: (_per100(f, "fat_g") or Decimal(999),
                                 -(_per100(f, need_nutrient) or Decimal(0))))
    elif sort_mode == "min":  # lightest first (veg, fruit)
        rest.sort(key=lambda f: _per100(f, need_nutrient) or Decimal(999))
    else:                     # densest first (carbs)
        rest.sort(key=lambda f: -(_per100(f, need_nutrient) or Decimal(0)))
    return pref + rest


def _auto_infer_role(food: dict) -> str:
    kind = food.get("kind")
    if kind in _AUTO_FAT_KINDS:
        return "fat"
    if kind in _AUTO_VEG_KINDS:
        return "veg"
    if kind in _AUTO_FRUIT_KINDS:
        return "fruit"
    if kind in _AUTO_CARB_KINDS:
        return "carb"
    return "protein"


def auto_compose_day(slots: List[dict], day_targets: dict, src: CatalogSource,
                     *, excluded_food_ids: Optional[set] = None) -> dict:
    """Greedy deterministic draft: one protein + carb + veg per main slot,
    protein + fruit per snack slot, portions sized to the slot's share of
    day_targets. Returns meals + per-slot unmet notes."""
    excluded = set(excluded_food_ids or set())
    usable = [f for f in src.approved_foods()
              if f["status"] == "approved"
              and f["food_version_id"] not in excluded
              and f.get("kosher_evidence") != "non_kosher"]
    _pool = lambda *a, **kw: _auto_pool(usable, *a, **kw)

    budgets = _auto_slot_budgets(slots, day_targets)

    meals, unmet = [], []
    used_ids: set = set()
    for idx, spec in enumerate(slots):
        slot, kosher = spec["slot_key"], spec.get("kosher") or "any"
        b = budgets[idx]
        p_lo, k_up, c_up, f_up = b["p_lo"], b["k_up"], b["c_up"], b["f_up"]
        is_snack = b["is_snack"]
        proteins = _pool(kosher, _AUTO_PROTEIN_KINDS.get(kosher) or _AUTO_PROTEIN_KINDS["any"],
                         "protein_g", "lean",
                         prefer=_AUTO_PREFER_PROTEIN.get(kosher) or _AUTO_PREFER_PROTEIN["any"],
                         skip=_AUTO_NO_COMPONENT)
        sides = (_pool(kosher, _AUTO_FRUIT_KINDS, "kcal", "min",
                       prefer=_AUTO_PREFER_FRUIT, skip=_AUTO_NO_COMPONENT) if is_snack
                 else _pool(kosher, _AUTO_CARB_KINDS, "carbs_g", "max",
                            prefer=_AUTO_PREFER_CARB,
                            skip=_AUTO_NO_COMPONENT + _AUTO_NO_SIDE))
        vegs = [] if is_snack else _pool(kosher, _AUTO_VEG_KINDS, "kcal", "min",
                                         prefer=_AUTO_PREFER_VEG,
                                         skip=_AUTO_NO_COMPONENT + _AUTO_NO_VEG)
        fats = [] if is_snack else _pool(kosher, _AUTO_FAT_KINDS, "fat_g", "min",
                                         prefer=_AUTO_PREFER_FAT,
                                         skip=_AUTO_NO_COMPONENT)
        # Variety: a protein already served today drops to the end of the
        # candidate list rather than repeating in every meal.
        proteins = ([p for p in proteins if p["food_version_id"] not in used_ids]
                    + [p for p in proteins if p["food_version_id"] in used_ids])

        _fits = lambda items: _auto_fits(items, p_lo, k_up, c_up, f_up)
        best, best_items = None, None
        for pi in range(len(proteins)):
            prot = proteins[(idx + pi) % len(proteins)]
            p100 = _per100(prot, "protein_g")
            if not p100 or p100 <= 0:
                continue
            p_qty = min(_AUTO_CAP["protein"], _round_qty(p_lo * 100 / p100, Decimal(25)))
            for si in range(min(len(sides), 8)):
                side = sides[(idx + si) % len(sides)]
                s100 = _per100(side, "kcal" if is_snack else "carbs_g")
                s_target = (k_up * Decimal("0.35")) if is_snack else (c_up * Decimal("0.9"))
                side_cap = _AUTO_CAP["fruit"] if is_snack else _AUTO_CAP["side"]
                s_qty = min(side_cap, _round_qty(s_target * 100 / s100, Decimal(25))
                            if s100 and s100 > 0 else Decimal(150))
                items = [(prot, p_qty), (side, s_qty)]
                if vegs:
                    items.append((vegs[idx % len(vegs)], _AUTO_VEG_QTY))
                # A primary protein alone sometimes can't reach the target
                # within a realistic portion (250g cottage ≈ 27g protein) -
                # a coach adds a second protein, e.g. cottage + eggs.
                prot_deficit = p_lo - _auto_totals(items)["protein_g"]
                if prot_deficit > Decimal(5) and len(proteins) > 1:
                    prot2 = proteins[(idx + pi + 1) % len(proteins)]
                    p2100 = _per100(prot2, "protein_g")
                    if p2100 and p2100 > 0:
                        p2_qty = min(Decimal(150),
                                     _round_qty(prot_deficit * 100 / p2100, Decimal(25)))
                        items.append((prot2, p2_qty))
                # A real meal carries a fat source (olive oil/tahini/avocado/
                # nuts) - sized to the fat headroom the protein left.
                has_fat = False
                if fats:
                    fat = fats[idx % len(fats)]
                    fa100 = _per100(fat, "fat_g")
                    head = f_up - _auto_totals(items)["fat_g"]
                    fa_qty = (_round_qty(head * 100 / fa100, Decimal(5))
                              if fa100 and fa100 > 0 else Decimal(0))
                    fa_qty = min(_AUTO_CAP["fat"], fa_qty)
                    if fa_qty >= _AUTO_MIN_FAT_QTY:
                        items.append((fat, fa_qty))
                        has_fat = True
                # Shrink to fit the ceilings: fat first, then side, then protein.
                for _ in range(16):
                    if _fits(items):
                        break
                    t = _auto_totals(items)
                    if t["fat_g"] > f_up and has_fat:
                        items.pop()          # drop the fat topping first
                        has_fat = False
                    elif t["carbs_g"] > c_up or t["kcal"] > k_up:
                        items[1] = (side, max(Decimal(0), items[1][1] - 25))
                    elif t["fat_g"] > f_up and items[0][1] > Decimal(80):
                        items[0] = (prot, items[0][1] - 25)
                    else:
                        break
                if _fits(items):
                    best_items = items
                    break                    # first fitting combo wins the slot
                score = _auto_over(items, p_lo, k_up, c_up, f_up)
                if best is None or score < best:
                    best, best_items = score, items
            if best_items and _fits(best_items):
                break
        if best_items is None:
            unmet.append({"slot_key": slot, "reason": "אין מצרכים מתאימים באילוצי הכשרות/החרגות"})
            continue
        if not _fits(best_items):
            unmet.append({"slot_key": slot, "reason": "הכמויות לא עמדו במלוא התקציב — כדאי לכוון ידנית"})
        meals.append({
            "slot_key": slot,
            "items": [{"food_version_id": f["food_version_id"], "quantity_g": format(q, "f")}
                      for f, q in best_items],
        })
        used_ids.update(f["food_version_id"] for f, _ in best_items)
    return {"meals": meals, "unmet": unmet}


def _auto_size_chosen(kosher: str, roles: dict, b: dict,
                      usable: List[dict]) -> list:
    """Size quantities for caller-picked foods. Roles: protein (sized to the
    slot's protein share, topped up by a pool protein when short), carb
    (carbs share), veg (fixed), fat (headroom). Returns items list."""
    p_lo, k_up, c_up, f_up = b["p_lo"], b["k_up"], b["c_up"], b["f_up"]
    is_snack = b["is_snack"]
    items = []
    # protein(s): split the slot's protein share across the picked proteins
    prots = [f for f in roles.get("protein") or []
             if (_per100(f, "protein_g") or Decimal(0)) > 0]
    prot_need = p_lo
    for f in prots:
        target = min(prot_need, p_lo / Decimal(len(prots)))
        qty = min(_AUTO_CAP["protein"],
                  _round_qty(target * 100 / _per100(f, "protein_g"), Decimal(25)))
        items.append((f, qty))
        prot_need = max(Decimal(0), p_lo - _auto_totals(items)["protein_g"])
    # a protein shortfall gets a pool top-up, like a coach adds eggs
    if prot_need > Decimal(5):
        pool = _auto_pool(usable, kosher,
                          _AUTO_PROTEIN_KINDS.get(kosher) or _AUTO_PROTEIN_KINDS["any"],
                          "protein_g", "lean",
                          prefer=_AUTO_PREFER_PROTEIN.get(kosher) or _AUTO_PREFER_PROTEIN["any"],
                          skip=_AUTO_NO_COMPONENT)
        picked = {f["food_version_id"] for f, _ in items}
        for f2 in pool:
            p2100 = _per100(f2, "protein_g")
            if f2["food_version_id"] in picked or not p2100 or p2100 <= 0:
                continue
            items.append((f2, min(Decimal(150),
                                  _round_qty(prot_need * 100 / p2100, Decimal(25)))))
            break
    # carb side / fruit: split the carbs (or snack kcal) share
    side_role = "fruit" if is_snack else "carb"
    sides = roles.get(side_role) or []
    if sides:
        each = Decimal(len(sides))
        for f in sides:
            s100 = _per100(f, "kcal" if is_snack else "carbs_g")
            target = (k_up * Decimal("0.35") if is_snack else c_up * Decimal("0.9")) / each
            cap = _AUTO_CAP["fruit"] if is_snack else _AUTO_CAP["side"]
            qty = min(cap, _round_qty(target * 100 / s100, Decimal(25))
                      if s100 and s100 > 0 else Decimal(150))
            items.append((f, qty))
    for f in roles.get("veg") or []:
        items.append((f, _AUTO_VEG_QTY))
    for f in roles.get("fat") or []:
        fa100 = _per100(f, "fat_g")
        head = f_up - _auto_totals(items)["fat_g"]
        qty = min(_AUTO_CAP["fat"], _round_qty(head * 100 / fa100, Decimal(5))
                  if fa100 and fa100 > 0 else Decimal(0))
        if qty >= _AUTO_MIN_FAT_QTY:
            items.append((f, qty))
    return items


def _auto_fit_items(items, b) -> None:
    """In-place shrink. Fat is the binding constraint first: pop the fat
    topping, then shrink the protein (where most fat rides). Only then the
    carb/fruit side for carbs, and last the side then protein for kcal."""
    p_lo, k_up, c_up, f_up = b["p_lo"], b["k_up"], b["c_up"], b["f_up"]
    side_i = next((i for i, (f, _) in enumerate(items)
                   if _auto_infer_role(f) in ("carb", "fruit")), None)

    def _fattiest_protein_i():
        cand = [(i, (_per100(f, "fat_g") or Decimal(0)) * q)
                for i, (f, q) in enumerate(items)
                if _auto_infer_role(f) == "protein" and q > Decimal(80)]
        return max(cand, key=lambda x: x[1])[0] if cand else None

    for _ in range(24):
        if _auto_fits(items, p_lo, k_up, c_up, f_up):
            return
        t = _auto_totals(items)
        if (t["fat_g"] > f_up and len(items) > 1
                and _auto_infer_role(items[-1][0]) == "fat"):
            items.pop()
        elif t["fat_g"] > f_up and _fattiest_protein_i() is not None:
            i = _fattiest_protein_i()
            items[i] = (items[i][0], items[i][1] - 25)
        elif t["carbs_g"] > c_up and side_i is not None and items[side_i][1] > 0:
            items[side_i] = (items[side_i][0], max(Decimal(0), items[side_i][1] - 25))
        elif t["kcal"] > k_up:
            if side_i is not None and items[side_i][1] > 0:
                items[side_i] = (items[side_i][0], max(Decimal(0), items[side_i][1] - 25))
            elif _fattiest_protein_i() is not None:
                i = _fattiest_protein_i()
                items[i] = (items[i][0], items[i][1] - 25)
            else:
                return
        else:
            return


def auto_compose_from_choices(slots: List[dict], day_targets: dict,
                              src: CatalogSource,
                              *, excluded_food_ids: Optional[set] = None) -> dict:
    """Caller-picked foods (coach draft or LLM suggestion), engine-sized
    portions. The caller decides WHAT goes into each slot; the engine
    decides HOW MUCH under the same budget math and fail-closed filters as
    auto_compose_day. Unknown / excluded / kosher-mismatched ids are
    dropped; a slot left with no usable protein falls back to the
    deterministic pools for its missing structure."""
    excluded = set(excluded_food_ids or set())
    usable = [f for f in src.approved_foods()
              if f["status"] == "approved"
              and f["food_version_id"] not in excluded
              and f.get("kosher_evidence") != "non_kosher"]
    by_id = {f["food_version_id"]: f for f in usable}
    budgets = _auto_slot_budgets(slots, day_targets)

    meals, unmet = [], []
    used_ids: set = set()
    for idx, spec in enumerate(slots):
        slot, kosher = spec["slot_key"], spec.get("kosher") or "any"
        b = budgets[idx]
        allow = _AUTO_KOSHER_ALLOW.get(kosher, _AUTO_KOSHER_ALLOW["any"])
        roles: Dict[str, list] = {"protein": [], "carb": [], "veg": [],
                                  "fat": [], "fruit": []}
        for it in spec.get("items") or []:
            f = by_id.get(it.get("food_version_id"))
            if (not f or f.get("kosher_evidence") not in allow
                    or f["food_version_id"] in used_ids
                    or any(w in (f.get("name_he") or "") for w in _AUTO_NO_COMPONENT)):
                continue
            role = it.get("role")
            roles[role if role in roles else _auto_infer_role(f)].append(f)
        def _fresh(pool):
            """Prefer candidates not already served today."""
            rest = [f for f in pool if f["food_version_id"] not in used_ids]
            return rest or pool

        if not roles["protein"]:
            # no usable protein pick -> deterministic pool, same as other gaps
            pool = _fresh(_auto_pool(
                usable, kosher,
                _AUTO_PROTEIN_KINDS.get(kosher) or _AUTO_PROTEIN_KINDS["any"],
                "protein_g", "lean",
                prefer=_AUTO_PREFER_PROTEIN.get(kosher) or _AUTO_PREFER_PROTEIN["any"],
                skip=_AUTO_NO_COMPONENT))
            if pool:
                roles["protein"] = [pool[idx % len(pool)]]
        if not roles["protein"]:
            unmet.append({"slot_key": slot,
                          "reason": "אין מצרכים מתאימים באילוצי הכשרות/החרגות"})
            continue
        if not b["is_snack"]:
            for role, kinds, pref in (("carb", _AUTO_CARB_KINDS, _AUTO_PREFER_CARB),
                                      ("veg", _AUTO_VEG_KINDS, _AUTO_PREFER_VEG),
                                      ("fat", _AUTO_FAT_KINDS, _AUTO_PREFER_FAT)):
                if not roles[role]:
                    pool = _fresh(_auto_pool(
                        usable, kosher, kinds, "kcal", "min",
                        prefer=pref, skip=_AUTO_NO_COMPONENT))
                    if pool:
                        roles[role] = [pool[idx % len(pool)]]
        elif not roles["fruit"]:
            pool = _fresh(_auto_pool(
                usable, kosher, _AUTO_FRUIT_KINDS, "kcal", "min",
                prefer=_AUTO_PREFER_FRUIT, skip=_AUTO_NO_COMPONENT))
            if pool:
                roles["fruit"] = [pool[idx % len(pool)]]

        items = _auto_size_chosen(kosher, roles, b, usable)
        _auto_fit_items(items, b)
        items = [(f, q) for f, q in items if q > 0]
        if items:
            # lean protein top-up when the pick can't reach the share
            gap = b["p_lo"] - _auto_totals(items)["protein_g"]
            if gap > Decimal(5):
                picked = {f["food_version_id"] for f, _ in items}
                for f2 in _fresh(_auto_pool(
                        usable, kosher,
                        _AUTO_PROTEIN_KINDS.get(kosher) or _AUTO_PROTEIN_KINDS["any"],
                        "protein_g", "lean",
                        prefer=_AUTO_PREFER_PROTEIN.get(kosher) or _AUTO_PREFER_PROTEIN["any"],
                        skip=_AUTO_NO_COMPONENT)):
                    if f2["food_version_id"] in picked:
                        continue
                    p100 = _per100(f2, "protein_g")
                    if not p100 or p100 <= 0:
                        continue
                    qty = min(Decimal(150),
                              _round_qty(gap * 100 / p100, Decimal(25)))
                    trial = items + [(f2, qty)]
                    t = _auto_totals(trial)
                    if (t["fat_g"] <= b["f_up"] and t["kcal"] <= b["k_up"]
                            and t["carbs_g"] <= b["c_up"]):
                        items = trial
                        break
        if not items:
            unmet.append({"slot_key": slot,
                          "reason": "אין מצרכים מתאימים באילוצי הכשרות/החרגות"})
            continue
        if not _auto_fits(items, b["p_lo"], b["k_up"], b["c_up"], b["f_up"]):
            unmet.append({"slot_key": slot,
                          "reason": "הכמויות לא עמדו במלוא התקציב — כדאי לכוון ידנית"})
        meals.append({
            "slot_key": slot,
            "items": [{"food_version_id": f["food_version_id"], "quantity_g": format(q, "f")}
                      for f, q in items],
        })
        used_ids.update(f["food_version_id"] for f, _ in items)
    return {"meals": meals, "unmet": unmet}


# ---------------------------------------------------------------------------
# allocator (DESIGN 7.3): remaining day budget from consumed ledger totals
# ---------------------------------------------------------------------------
def remaining_budget(day_targets: dict, consumed_totals: Dict[str, Optional[str]]) -> dict:
    """Residual per nutrient. Unknown consumed values -> residual unknown,
    which must block any decision that depends on it (never zero)."""
    residual = {}
    for nutrient, bounds in (day_targets or {}).items():
        consumed = consumed_totals.get(nutrient)
        if consumed is None:
            residual[nutrient] = {"lower": bounds.get("lower"), "upper": None, "unknown": True}
            continue
        upper = bounds.get("upper")
        try:
            rem = dec(upper) - dec(consumed) if upper is not None else None
        except ValueError:
            rem = None
        residual[nutrient] = {"lower": bounds.get("lower"), "upper": dstr(rem), "unknown": False}
    return residual


# ---------------------------------------------------------------------------
# deterministic proposal generation (DESIGN 7.4/7.5)
# ---------------------------------------------------------------------------
def _spec_quantity_steps(specs: List[dict]) -> List[List[Decimal]]:
    """Allowed quantity_g values per item spec (bounded enumeration)."""
    steps = []
    for spec in specs:
        lo, hi, step = dec(spec["min_g"]), dec(spec["max_g"]), dec(spec["step_g"])
        values = []
        v = lo
        while v <= hi and len(values) < 50:
            values.append(v)
            v += step
        steps.append(values or [dec(spec["default_g"])])
    return steps


def _recipe_quantity_steps(recipe: dict) -> List[List[Decimal]]:
    """Allowed quantity_g values per recipe item (bounded enumeration)."""
    return _spec_quantity_steps(recipe["items"])


def _context_for(src, recipe, items, rules):
    totals = meal_totals(items, src)
    ctx = {
        "items": items,
        "totals": serialize_totals(totals),
        "total_minutes": recipe["total_minutes"],
        "equipment_used": recipe["equipment"],
    }
    return ctx, evaluate_meal_rules(rules, ctx)


def _day_check_records(totals: dict, day_bounds: List[dict]) -> List[dict]:
    """Day-scope residual bound records for a meal's totals (fail-closed:
    an unknown residual or total is 'unknown', never a pass)."""
    records = []
    for b in day_bounds:
        n = b["nutrient"]
        t = totals.get(n)
        rem = b.get("residual")
        if rem is None or t is None:
            status = "unknown"
        elif dec(t) > rem:
            status = "fail"
        else:
            status = "pass"
        records.append({
            "code": f"day_bounds:{n}", "rule": "nutrient_bounds",
            "nutrient": n, "scope": "day", "status": status,
            "total": t, "residual": (dstr(rem) if rem is not None else None),
            "lower": b.get("lower"), "upper": b.get("upper"),
        })
    return records


def _day_check(totals: dict, day_bounds: List[dict]) -> bool:
    return all(r["status"] == "pass" for r in _day_check_records(totals, day_bounds))


def _bounded_fit_items(
    src: CatalogSource,
    specs: List[dict],
    meal_ctx: dict,
    rules: List[dict],
    day_bounds: List[dict],
    *,
    quantity_steps=None,
    counter: Optional[List[int]] = None,
    limit: int = MAX_COMBINATIONS_PER_RECIPE,
) -> tuple:
    """Shared bounded portion search over declared item specs - the same
    machinery recipe alternatives use: default quantities first, then an
    exhaustive search over the declared step grid when bounds exist.

    specs: [{food_version_id, default_g, min_g, max_g, step_g}]
    meal_ctx: anything _context_for accepts ({total_minutes, equipment}).
    counter/limit: callers that run several searches (smart composition)
    share one budget so exhaustion is reported, never silently treated as
    infeasible.
    Returns ("fit", items, ctx) | ("no_match", None, None) |
    ("search_incomplete", None, None)."""
    hard_bounds = [
        r for r in rules
        if r.get("kind") == "nutrient_bounds" and r.get("scope") == "meal"
    ]
    steps = (quantity_steps or _spec_quantity_steps)(specs)
    default_items = [
        {"food_version_id": s["food_version_id"], "quantity_g": s["default_g"],
         "food": src.get_food_version(s["food_version_id"])}
        for s in specs
    ]
    ctx, checks = _context_for(src, meal_ctx, default_items, rules)
    found = None
    if not any(c["status"] in ("fail", "unknown") for c in checks) \
            and _day_check(ctx["totals"], day_bounds):
        found = default_items
    examined = counter if counter is not None else [0]
    if found is None and (hard_bounds or day_bounds):
        for combo in itertools.product(*steps):
            if examined[0] >= limit:
                return ("search_incomplete", None, None)
            examined[0] += 1
            items = [
                {"food_version_id": s["food_version_id"], "quantity_g": format(q, "f"),
                 "food": src.get_food_version(s["food_version_id"])}
                for s, q in zip(specs, combo)
            ]
            ctx, checks = _context_for(src, meal_ctx, items, rules)
            if not any(c["status"] in ("fail", "unknown") for c in checks) \
                    and _day_check(ctx["totals"], day_bounds):
                found = items
                break
    if found is None:
        return ("no_match", None, None)
    return ("fit", found, ctx)


def _fits_prep(recipe: dict, prep: dict) -> bool:
    """Session preparation overrides: total time and equipment (DESIGN 7.4.3)."""
    if not prep:
        return True
    max_minutes = prep.get("max_total_minutes")
    if max_minutes is not None and recipe["total_minutes"] > int(max_minutes):
        return False
    allowed = prep.get("equipment_allowed")
    if allowed is not None:
        usable = set(allowed) | {"none"}
        if set(recipe["equipment"]) - usable:
            return False
    if prep.get("no_heat") and any(e in recipe["equipment"] for e in ("stove", "oven", "pan")):
        return False
    return True


def _recipe_proposal(
    slot_key: str, src: CatalogSource, recipe: dict, items: List[dict],
    ctx: dict, rules: List[dict], day_bounds: List[dict],
) -> dict:
    """Proposal payload for a catalog recipe candidate (planned, alternative
    or minus-missing)."""
    _, checks = _context_for(src, recipe, items, rules)
    return {
        "slot_key": slot_key,
        "recipe_version_id": recipe["recipe_version_id"],
        "name_he": recipe["name_he"],
        "items": [
            {
                "food_version_id": i["food_version_id"],
                "name_he": (i.get("food") or {}).get("name_he"),
                "quantity_g": i["quantity_g"],
            }
            for i in items
        ],
        "totals": ctx["totals"],
        "checks": checks + _day_check_records(ctx["totals"], day_bounds),
        "total_minutes": recipe["total_minutes"],
        "equipment": recipe["equipment"],
        "steps_he": recipe["steps_he"],
        "compliance_label": "checked_against_specified_rules",
    }


def generate_proposal(
    slot_key: str,
    src: CatalogSource,
    plan_content: dict,
    profile: dict,
    session: dict,
    consumed_totals: Dict[str, Optional[str]],
    day_completeness: str,
    preferred_recipe_ids: Optional[List[str]] = None,
    day_bounds: Optional[List[dict]] = None,
    requested_food_ids: Optional[List[str]] = None,
    extra_basket_food_ids: Optional[List[str]] = None,
    prefer_composition: bool = False,
) -> dict:
    """Generate one validated candidate for a slot, or an honest reason.

    Candidate pool = the planned meal for the slot + its explicitly approved
    alternative recipes. No policy -> planned meal only (B03).
    Returns {state, reasons, proposal?} - DESIGN ValidationResult.

    preferred_recipe_ids is a ranking hint only (DESIGN 7.4.6): it sorts
    feasible candidates but never overrides a failed hard check.

    extra_basket_food_ids are request-servable foods the caller already
    cleared (e.g. an auto-looked-up food); they join the composition basket
    without touching the plan's approved lists. prefer_composition asks the
    engine to try a requested-food composition BEFORE the recipe tiers, so
    an explicit "I want X" is actually served when it fits.
    """
    reasons: List[str] = []
    resolved_plan = validate_plan_content(plan_content, src)
    if not resolved_plan["valid"]:
        return {"state": "blocked", "reasons": ["active plan fails current validation"], "issues": resolved_plan["issues"]}

    # B11: a skipped/missing allergy disclosure is not 'no allergies'. It
    # blocks new proposals (alternatives); viewing the planned meal and
    # honest eating logging stay open in the caller.
    if profile.get("allergy_disclosure") not in ("declared", "none_declared"):
        return {"state": "blocked", "reasons": ["allergy disclosure missing"]}

    rules = list(resolved_plan["rules"]) + list(profile.get("rules") or [])
    conflicts = detect_rule_conflicts(rules)
    if conflicts:
        return {"state": "blocked", "reasons": conflicts}

    # Day-residual gate: a targeted/bound nutrient that is unknown blocks
    # dependent recommendations (unknown is never a pass, DESIGN 8.2).
    # Untargeted unknowns (e.g. cholesterol with only a kcal target) don't.
    day_targets = resolved_plan["day_targets"]
    residual = remaining_budget(day_targets, consumed_totals)
    if day_targets and any(r["unknown"] for r in residual.values()):
        return {"state": "blocked", "reasons": ["day totals incomplete; remaining budget is unknown"]}

    missing = set(session.get("missing_food_ids") or [])
    prep = session.get("preparation") or {}
    habitual = set(profile.get("habitual_food_ids") or [])
    excluded = set(profile.get("excluded_food_ids") or []) | missing
    # Permanent "never suggest this" applies to alternatives only - it can
    # never remove the coach's planned meal (DESIGN 12 / B05).
    disliked = set(profile.get("disliked_recipe_ids") or [])
    # Session-only "not in the mood" rejection - also covers the planned
    # meal for this session, and expires with the session.
    session_rejected = set(session.get("rejected_recipe_ids") or [])

    # Day-scope medical bounds: residual upper limit per nutrient for THIS
    # meal = bound - consumed - other slots' planned/selected allocations.
    # residual None means the bound cannot be verified -> fail-closed.
    day_bounds = day_bounds or []

    planned = {m["slot_key"]: m for m in resolved_plan["meals"]}
    candidate_recipes = []
    planned_meal = planned.get(slot_key)
    if planned_meal and planned_meal.get("recipe_version_id"):
        rid = planned_meal["recipe_version_id"]
        rec = src.get_recipe_version(rid)
        if rec and rid not in session_rejected:
            candidate_recipes.append((rec, planned_meal["items"]))
        elif rec:
            reasons.append(f"{rid}: rejected in this session")
    alt_count = 0
    for rid in resolved_plan["alternatives"].get(slot_key) or []:
        if alt_count >= MAX_CANDIDATES:
            break
        if rid in disliked:
            reasons.append(f"{rid}: excluded by profile (never suggest)")
            continue
        if rid in session_rejected:
            reasons.append(f"{rid}: rejected in this session")
            continue
        rec = src.get_recipe_version(rid)
        if rec:
            candidate_recipes.append((rec, None))
            alt_count += 1
    if not candidate_recipes and not extra_basket_food_ids:
        return {"state": "no_match", "reasons": ["no approved alternatives for this slot"]}

    saw_incomplete = False

    # Explicit food request: try a composition around the requested food
    # before the recipe tiers - "בא לי סטייק" should serve steak, not the
    # planned recipe that doesn't contain it.
    if prefer_composition and requested_food_ids:
        composition = compose_slot_items(
            slot_key, src, [rec for rec, _ in candidate_recipes], rules,
            day_bounds=day_bounds,
            excluded_food_ids=excluded,
            requested_food_ids=requested_food_ids,
            habitual_food_ids=habitual,
            extra_food_ids=extra_basket_food_ids,
        )
        if composition["state"] == "eligible_candidate":
            served = {i["food_version_id"] for i in composition["proposal"]["items"]}
            if set(requested_food_ids) & served:
                return {"state": "eligible_candidate", "reasons": reasons,
                        "proposal": composition["proposal"]}
        if composition["state"] == "search_incomplete":
            # Not fatal: the cheap fixed-quantity check or a feasible
            # recipe below may still yield a valid proposal.
            saw_incomplete = True
            reasons.extend(composition.get("reasons") or [])

    def _primary_food_id(recipe) -> Optional[str]:
        """The item that carries the dish - 'chicken plate minus the chicken'
        is not the same meal. Dominant by protein, kcal as the fallback."""
        best_id, best_score = None, Decimal("0")
        for spec in recipe["items"]:
            food = src.get_food_version(spec["food_version_id"]) or {}
            nutr = food.get("nutrients") or {}
            basis = dec(food.get("basis_g") or "100") or Decimal("100")
            qty = dec(spec.get("default_g") or spec.get("max_g") or "0")
            factor = qty / basis
            protein = dec(nutr.get("protein_g") or "0") * factor
            kcal = dec(nutr.get("kcal") or "0") * factor
            score = protein if protein > 0 else kcal / 10
            if score > best_score:
                best_score, best_id = score, spec["food_version_id"]
        return best_id

    feasible = []
    minus_candidates = []
    for recipe, fixed_items in candidate_recipes:
        rec_foods = {i["food_version_id"] for i in recipe["items"]}
        if rec_foods & missing:
            reasons.append(f"{recipe['recipe_version_id']}: missing ingredient")
            minus_candidates.append((recipe, fixed_items))
            continue
        if not _fits_prep(recipe, prep):
            reasons.append(f"{recipe['recipe_version_id']}: preparation constraints unmet")
            continue

        if fixed_items is not None:
            # Planned meal: quantities already validated by the plan.
            items = [{"food_version_id": i["food_version_id"], "quantity_g": i["quantity_g"],
                      "food": src.get_food_version(i["food_version_id"])} for i in fixed_items]
            ctx, checks = _context_for(src, recipe, items, rules)
            if any(c["status"] in ("fail", "unknown") for c in checks):
                reasons.append(f"{recipe['recipe_version_id']}: rule check not satisfied")
                continue
            if not _day_check(ctx["totals"], day_bounds):
                reasons.append(f"{recipe['recipe_version_id']}: exceeds medical day bound")
                continue
            feasible.append((recipe, items, ctx))
            continue

        # Alternative: bounded portion search over declared steps.
        status, fit_items, ctx = _bounded_fit_items(
            src, recipe["items"], recipe, rules, day_bounds
        )
        if status == "search_incomplete":
            reasons.append(f"{recipe['recipe_version_id']}: combination budget exhausted")
            saw_incomplete = True
            continue
        if status != "fit":
            reasons.append(f"{recipe['recipe_version_id']}: no feasible portion within rules")
            continue
        feasible.append((recipe, fit_items, ctx))

    pref_index = {rid: i for i, rid in enumerate(preferred_recipe_ids or [])}

    def _rank(entry):
        recipe, items, ctx = entry
        familiar = sum(1 for i in items if i["food_version_id"] in habitual)
        complexity = len(items)
        # Ranking hint (DESIGN 7.4.6): preferred candidates sort first among
        # the already-feasible ones - a failed hard rule never reaches here.
        return (pref_index.get(recipe["recipe_version_id"], len(pref_index)),
                -familiar, complexity, recipe["recipe_version_id"])

    if feasible:
        recipe, items, ctx = sorted(feasible, key=_rank)[0]
        return {
            "state": "eligible_candidate",
            "reasons": reasons,
            "proposal": _recipe_proposal(slot_key, src, recipe, items, ctx, rules, day_bounds),
        }

    # Fallback (b): an approved recipe minus its missing ingredient(s) - the
    # remaining items are re-checked in full, never served on trust.
    for recipe, fixed_items in minus_candidates:
        if not _fits_prep(recipe, prep):
            continue
        remaining_specs = [s for s in recipe["items"] if s["food_version_id"] not in missing]
        if not remaining_specs:
            reasons.append(
                f"{recipe['recipe_version_id']}: nothing left after removing missing ingredients"
            )
            continue
        dropped = {i["food_version_id"] for i in recipe["items"]} & missing
        if len(dropped) > 1 or _primary_food_id(recipe) in dropped:
            reasons.append(
                f"{recipe['recipe_version_id']}: missing ingredient is the dish's main item"
            )
            continue
        found = found_ctx = None
        if fixed_items is not None:
            # Planned meal: keep the coach's quantities for what remains.
            reduced = [
                {"food_version_id": i["food_version_id"], "quantity_g": i["quantity_g"],
                 "food": src.get_food_version(i["food_version_id"])}
                for i in fixed_items if i["food_version_id"] not in missing
            ]
            if reduced:
                ctx, checks = _context_for(src, recipe, reduced, rules)
                if not any(c["status"] in ("fail", "unknown") for c in checks) \
                        and _day_check(ctx["totals"], day_bounds):
                    found, found_ctx = reduced, ctx
        if found is None:
            status, fit_items, ctx = _bounded_fit_items(
                src, remaining_specs, recipe, rules, day_bounds
            )
            if status == "search_incomplete":
                reasons.append(
                    f"{recipe['recipe_version_id']}: combination budget exhausted"
                )
                saw_incomplete = True
                continue
            if status == "fit":
                found, found_ctx = fit_items, ctx
        if found is None:
            reasons.append(
                f"{recipe['recipe_version_id']}: remaining items fail checks without the missing ingredient"
            )
            continue
        proposal = _recipe_proposal(slot_key, src, recipe, found, found_ctx, rules, day_bounds)
        proposal["adapted"] = True
        proposal["dropped_food_ids"] = sorted(
            {i["food_version_id"] for i in recipe["items"]} & missing
        )
        return {"state": "eligible_candidate", "reasons": reasons, "proposal": proposal}

    # Fallback (c): ad-hoc smart composition from the slot's approved food
    # basket (the candidate recipes' ingredients, minus session/profile
    # exclusions). Only engine math computes portions and nutrients.
    composition = compose_slot_items(
        slot_key, src, [rec for rec, _ in candidate_recipes], rules,
        day_bounds=day_bounds,
        excluded_food_ids=excluded,
        requested_food_ids=requested_food_ids,
        habitual_food_ids=habitual,
        extra_food_ids=extra_basket_food_ids,
    )
    if composition["state"] == "eligible_candidate":
        return {"state": "eligible_candidate", "reasons": reasons,
                "proposal": composition["proposal"]}
    reasons.extend(composition.get("reasons") or [])
    if composition["state"] == "search_incomplete" or saw_incomplete:
        return {"state": "search_incomplete", "reasons": reasons}

    return {"state": "no_match", "reasons": reasons or ["no feasible candidate"]}


def validate_selection_items(
    items: List[dict], slot_key: str, rules: List[dict],
    src: CatalogSource, recipe: Optional[dict] = None,
) -> dict:
    """Re-validate a proposed/selected item set against current data + rules.

    Used at selection time - a stale or rule-violating choice is rejected,
    never stamped with a fresh vector (DESIGN 6.5 step 4).
    """
    issues = []
    resolved = []
    for j, item in enumerate(items):
        fid = item.get("food_version_id")
        food = src.get_food_version(fid) if fid else None
        if not food or food["status"] != "approved":
            issues.append(f"items[{j}]: food version unavailable {fid!r}")
            continue
        try:
            qty = dec(item.get("quantity_g"))
            if qty < 0:
                raise ValueError
        except ValueError:
            issues.append(f"items[{j}]: invalid quantity_g")
            continue
        if recipe:
            spec = {i["food_version_id"]: i for i in recipe["items"]}.get(fid)
            if not spec:
                issues.append(f"items[{j}]: {fid} not an ingredient of {recipe['recipe_version_id']}")
                continue
            lo, hi, step = dec(spec["min_g"]), dec(spec["max_g"]), dec(spec["step_g"])
            if qty < lo or qty > hi or (qty - lo) % step != 0:
                issues.append(f"items[{j}]: quantity outside recipe bounds")
                continue
        resolved.append({"food_version_id": fid, "quantity_g": format(qty, "f"), "food": food})
    ctx = {
        "items": resolved,
        "totals": serialize_totals(meal_totals(resolved, src)) if resolved else {},
        "total_minutes": recipe["total_minutes"] if recipe else None,
        "equipment_used": recipe["equipment"] if recipe else [],
    }
    checks = evaluate_meal_rules(rules, ctx)
    failing = [c for c in checks if c["status"] in ("fail", "unknown")]
    return {
        "valid": not issues and not failing,
        "issues": issues,
        "failed_checks": failing,
        "items": resolved,
        "totals": ctx["totals"],
    }


# ---------------------------------------------------------------------------
# eating ledger folding (DESIGN 8.1)
# ---------------------------------------------------------------------------
def fold_effective_events(events: List[dict]) -> List[dict]:
    by_id: Dict[str, dict] = {e["id"]: dict(e) for e in events}
    for e in events:
        if e["event_type"] == "correction" and e.get("corrects_event_id") in by_id:
            target = by_id[e["corrects_event_id"]]
            target["items"] = e["items"]
            target["occurred_at"] = e["occurred_at"]
            target["nutrition_date"] = e["nutrition_date"]
            target["_superseded_by"] = e["id"]
        elif e["event_type"] == "void" and e.get("corrects_event_id") in by_id:
            by_id[e["corrects_event_id"]]["_voided"] = True
    return [
        e for e in by_id.values()
        if e["event_type"] == "recorded" and not e.get("_voided")
    ]


def day_totals(effective_events: List[dict], src: CatalogSource = DEFAULT_SOURCE) -> dict:
    items = []
    incomplete = False
    for event in effective_events:
        for item in event.get("items") or []:
            items.append(item)
            if not item.get("food_version_id"):
                incomplete = True
        if event.get("completeness") == "incomplete":
            incomplete = True
    known_items = [
        {"food_version_id": i["food_version_id"], "quantity_g": i["quantity_g"],
         "food": src.get_food_version(i["food_version_id"])}
        for i in items
        if src.get_food_version(i["food_version_id"])
    ]
    unknown_items = len(items) - len(known_items)
    if items and not known_items:
        # Only unknowns on this day: totals are unknown, not a misleading 0.
        totals = {f: None for f in NUTRIENT_FIELDS}
    else:
        totals = meal_totals(known_items, src) if known_items else {f: Decimal(0) for f in NUTRIENT_FIELDS}
    return {
        "totals": serialize_totals(totals),
        "completeness": "incomplete" if (incomplete or unknown_items or any(v is None for v in totals.values())) else "complete",
        "event_count": len(effective_events),
    }


# ---------------------------------------------------------------------------
# day-scope bound residuals + portion fitting (DESIGN 'build within the values')
# ---------------------------------------------------------------------------
def day_bound_residuals(
    day_bounds: List[dict],
    consumed_totals: Dict[str, Optional[str]],
    allocated_totals: Dict[str, Optional[str]],
) -> List[dict]:
    """Residual upper room per day-scope bound nutrient for the NEXT meal:
    residual = bound - consumed - allocated (other slots' planned/selected
    meals). Any unknown input -> residual None, which must block the
    dependent decision (fail-closed, never treated as unlimited).

    Day-scope LOWER bounds cannot be gated on a single meal (the remaining
    day can still close the gap) - they are enforced only at plan
    validation, where the full-day sum is known. Only upper bounds produce
    residuals here."""
    residuals = []
    for rule in day_bounds:
        if rule.get("upper") is None:
            continue
        nutrient = rule["nutrient"]
        consumed = consumed_totals.get(nutrient)
        allocated = allocated_totals.get(nutrient)
        if consumed is None or allocated is None:
            residuals.append({"nutrient": nutrient, "residual": None})
        else:
            residuals.append({
                "nutrient": nutrient,
                "residual": dec(rule["upper"]) - dec(consumed) - dec(allocated),
            })
    return residuals


def fit_portions(
    recipe_version_id: str,
    slot_key: str,
    src: CatalogSource,
    rules: List[dict],
    day_residuals: Optional[List[dict]] = None,
) -> dict:
    """Read-only bounded portion search for one recipe under all rules -
    the same search alternatives run, exposed so the coach can build a meal
    inside the professional's values without the system inventing any
    number (DESIGN 1.2: values come from the professional only)."""
    recipe = src.get_recipe_version(recipe_version_id)
    if not recipe or recipe["status"] != "approved":
        return {"state": "no_match", "reasons": ["recipe unavailable"]}
    if not recipe_covers_slot(recipe, slot_key):
        return {"state": "no_match",
                "reasons": [f"{recipe_version_id} not approved for slot {slot_key}"]}
    conflicts = detect_rule_conflicts(rules)
    if conflicts:
        return {"state": "blocked", "reasons": conflicts}
    day_residuals = day_residuals or []

    def _day_ok(totals: dict) -> bool:
        for b in day_residuals:
            n = b["nutrient"]
            t = totals.get(n)
            rem = b.get("residual")
            if rem is None or t is None:
                return False
            if dec(t) > rem:
                return False
        return True

    def _mk_items(combo):
        return [
            {"food_version_id": s["food_version_id"], "quantity_g": format(q, "f"),
             "food": src.get_food_version(s["food_version_id"])}
            for s, q in zip(recipe["items"], combo)
        ]

    default_items = [
        {"food_version_id": s["food_version_id"], "quantity_g": s["default_g"],
         "food": src.get_food_version(s["food_version_id"])}
        for s in recipe["items"]
    ]
    ctx, checks = _context_for(src, recipe, default_items, rules)
    found = None
    if not any(c["status"] in ("fail", "unknown") for c in checks) and _day_ok(ctx["totals"]):
        found = (default_items, ctx)
    else:
        examined = 0
        for combo in itertools.product(*_recipe_quantity_steps(recipe)):
            if examined >= MAX_COMBINATIONS_PER_RECIPE:
                return {"state": "search_incomplete",
                        "reasons": ["combination budget exhausted"]}
            examined += 1
            items = _mk_items(combo)
            ctx, checks = _context_for(src, recipe, items, rules)
            if any(c["status"] in ("fail", "unknown") for c in checks):
                continue
            if not _day_ok(ctx["totals"]):
                continue
            found = (items, ctx)
            break
    if found is None:
        return {"state": "no_match",
                "reasons": ["no feasible portion within the professional's bounds"]}
    items, ctx = found
    return {
        "state": "fit",
        "slot_key": slot_key,
        "recipe_version_id": recipe_version_id,
        "name_he": recipe["name_he"],
        "items": [
            {"food_version_id": i["food_version_id"],
             "name_he": (i.get("food") or {}).get("name_he"),
             "quantity_g": i["quantity_g"]}
            for i in items
        ],
        "totals": ctx["totals"],
        "checks": _context_for(src, recipe, items, rules)[1],
    }


# ---------------------------------------------------------------------------
# smart composition (ad-hoc dish from the slot's approved food basket)
# ---------------------------------------------------------------------------
def _observed_portion_spec(food_version_id: str, src: CatalogSource) -> Optional[dict]:
    """Quantity range a food is actually served in, derived from every
    approved catalog recipe that uses it. Returns None when no recipe
    declares a usable range - callers then either skip the food or fall
    back to the conservative composition default. Never invents nutrients."""
    lo = hi = step = default = None
    for recipe in src.approved_recipes():
        for spec in recipe.get("items") or []:
            if spec.get("food_version_id") != food_version_id:
                continue
            try:
                s_lo, s_hi = dec(spec["min_g"]), dec(spec["max_g"])
                s_step = dec(spec["step_g"])
                s_def = dec(spec.get("default_g") if spec.get("default_g") is not None else spec["min_g"])
            except (KeyError, ValueError):
                continue
            if s_step <= 0 or s_hi < s_lo:
                continue
            lo = s_lo if lo is None else min(lo, s_lo)
            hi = s_hi if hi is None else max(hi, s_hi)
            step = s_step if step is None else min(step, s_step)
            default = s_def if default is None else min(default, s_def)
    if lo is None or hi is None:
        return None
    if lo <= 0:
        lo = step  # a 0 g item is not a servable portion
    if default is None or default < lo or default > hi:
        default = lo
    # Specs carry transport strings, exactly like catalog recipe items.
    return {"food_version_id": food_version_id,
            "min_g": format(lo, "f"), "max_g": format(hi, "f"),
            "step_g": format(step, "f"), "default_g": format(default, "f")}


def _composition_quantity_steps(specs: List[dict]) -> List[List[Decimal]]:
    """Coarser quantity grid for ad-hoc composition: the declared step
    grid downsampled (endpoints kept) to MAX_COMPOSITION_STEPS values per
    food so the 1-3-item product stays inside the shared search budget."""
    all_steps = _spec_quantity_steps(specs)
    out = []
    for values in all_steps:
        if len(values) > MAX_COMPOSITION_STEPS:
            last = len(values) - 1
            keep = sorted({
                round(i * last / (MAX_COMPOSITION_STEPS - 1))
                for i in range(MAX_COMPOSITION_STEPS)
            })
            values = [values[i] for i in keep]
        out.append(values)
    return out


def compose_slot_items(
    slot_key: str,
    src: CatalogSource,
    basket_recipes: List[dict],
    rules: List[dict],
    *,
    day_bounds: Optional[List[dict]] = None,
    excluded_food_ids: Optional[set] = None,
    requested_food_ids: Optional[List[str]] = None,
    habitual_food_ids: Optional[set] = None,
    extra_food_ids: Optional[List[str]] = None,
) -> dict:
    """Ad-hoc dish composition for a slot: enumerate small combinations
    (1-3 items) of the approved food basket and fit quantities with the
    same bounded-search machinery recipes use.

    basket_recipes = the slot's approved candidate recipes - the caller has
    already removed session-rejected and disliked recipes, so the basket
    can never serve a recipe the trainee refused. Foods in
    excluded_food_ids (session-missing + profile-excluded) are removed.
    Foods with unknown nutrient data stay out wherever a bound depends on
    them (fail-closed via the shared rule/day checks).

    requested_food_ids ranks subsets containing a food the trainee asked
    for first; it never admits foods outside the basket.

    extra_food_ids are request-servable foods that are not ingredients of
    any approved recipe for this slot (e.g. a catalog food the trainee
    named, or an auto-looked-up food the caller already cleared for this
    user's safety gates). They join the serving basket only - never the
    plan's approved lists.
    """
    day_bounds = day_bounds or []
    excluded = set(excluded_food_ids or set())
    food_ids: List[str] = []
    for recipe in basket_recipes:
        for spec in recipe.get("items") or []:
            fid = spec.get("food_version_id")
            if not fid or fid in excluded or fid in food_ids:
                continue
            food = src.get_food_version(fid)
            if not food or food["status"] != "approved":
                continue
            food_ids.append(fid)
    for fid in extra_food_ids or []:
        if not fid or fid in excluded or fid in food_ids:
            continue
        food = src.get_food_version(fid)
        if not food or food["status"] != "approved":
            continue
        food_ids.append(fid)
    if not food_ids:
        return {"state": "no_match",
                "reasons": ["no approved foods left to compose a dish"]}

    specs = {}
    for fid in food_ids:
        specs[fid] = _observed_portion_spec(fid, src) or {
            "food_version_id": fid,
            "min_g": format(COMPOSITION_DEFAULT_MIN_G, "f"),
            "max_g": format(COMPOSITION_DEFAULT_MAX_G, "f"),
            "step_g": format(COMPOSITION_DEFAULT_STEP_G, "f"),
            "default_g": format(COMPOSITION_DEFAULT_MIN_G, "f"),
        }

    requested = {f for f in (requested_food_ids or []) if f in specs}
    habitual = set(habitual_food_ids or set())
    subsets = []
    for size in range(1, min(MAX_COMPOSITION_ITEMS, len(food_ids)) + 1):
        for combo in itertools.combinations(food_ids, size):
            wanted = sum(1 for f in combo if f in requested)
            familiar = sum(1 for f in combo if f in habitual)
            subsets.append((combo, wanted, familiar))
    # Requested foods first, then familiar, then the smaller plate;
    # deterministic order inside each tier.
    subsets.sort(key=lambda t: (-t[1], -t[2], len(t[0]), t[0]))

    # A composition has no recipe steps - it is a "no-cook" plate
    # (0 minutes, no equipment), which always fits preparation constraints.
    meal_ctx = {"total_minutes": 0, "equipment": ["none"], "items": []}

    def _combo_doomed(combo) -> bool:
        # Cheap deterministic prune: if the combo's minimum possible total
        # already exceeds an upper day residual, or its maximum possible
        # total stays below a lower bound, no quantity in the grid can
        # fit - skip it without spending enumeration budget. Unknown
        # nutrient values can never prove a skip (fail-closed preserved).
        for b in day_bounds:
            n = b.get("nutrient")
            rem = b.get("residual")
            if rem is None:
                continue
            total_min = Decimal(0)
            total_max = Decimal(0)
            unknown = False
            for fid in combo:
                v = (src.get_food_version(fid).get("nutrients") or {}).get(n)
                if v is None:
                    unknown = True
                    break
                min_g = dec(specs[fid]["min_g"])
                max_g = dec(specs[fid]["max_g"])
                total_min += dec(v) * min_g / 100
                total_max += dec(v) * max_g / 100
            if unknown:
                continue
            if total_min > rem:
                return True
            if b.get("lower") is not None and total_max < rem:
                return True
        return False

    counter = [0]
    for combo, _wanted, _familiar in subsets:
        if _combo_doomed(combo):
            continue
        status, items, ctx = _bounded_fit_items(
            src, [specs[f] for f in combo], meal_ctx, rules, day_bounds,
            quantity_steps=_composition_quantity_steps,
            counter=counter, limit=MAX_COMPOSITION_COMBINATIONS,
        )
        if status == "search_incomplete":
            return {"state": "search_incomplete",
                    "reasons": ["combination budget exhausted"]}
        if status != "fit":
            continue
        _, checks = _context_for(src, meal_ctx, items, rules)
        proposal = {
            "slot_key": slot_key,
            "recipe_version_id": None,
            "name_he": "מנה מורכבת",
            "items": [
                {
                    "food_version_id": i["food_version_id"],
                    "name_he": (i.get("food") or {}).get("name_he"),
                    "quantity_g": i["quantity_g"],
                    **({"unverified": True}
                       if not (i.get("food") or {}).get("verified", 1) else {}),
                }
                for i in items
            ],
            "totals": ctx["totals"],
            "checks": checks + _day_check_records(ctx["totals"], day_bounds),
            "total_minutes": 0,
            "equipment": ["none"],
            "steps_he": [],
            "compliance_label": "smart_composition",
            "composed": True,
        }
        return {"state": "eligible_candidate", "proposal": proposal}

    return {"state": "no_match",
            "reasons": ["no composition fits the current rules"]}
