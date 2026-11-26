"""Catalog access for the meal_coach agent.

`CatalogSource` wraps food/recipe version dictionaries so the deterministic
engine can run against either the synthetic fixtures (below) or rows loaded
from the DB catalog tables. All nutrient values are per `basis_g` edible
grams (DESIGN 7.1); None = unknown, never zero.

The fixture data is explicitly synthetic - gate G04 (licensed food sources)
is open, so these entries exist to exercise the pipeline, including the edge
cases the DESIGN requires: unknown nutrients, a retracted version, raw vs
cooked identities, and ambiguous aliases.
"""
from typing import Dict, List, Optional

from .food_library import derive_attributes

FIXTURE_EPOCH = "fixture-catalog/1"


class CatalogSource:
    """Lookup facade over food/recipe version dicts + epoch."""

    def __init__(self, foods=None, recipes=None, epoch: str = FIXTURE_EPOCH, aliases=None):
        self.foods: Dict[str, dict] = foods if foods is not None else FOODS
        self.recipes: Dict[str, dict] = recipes if recipes is not None else RECIPES
        self.epoch = epoch
        if aliases is None:
            aliases = {}
            for f in self.foods.values():
                for a in f.get("aliases") or []:
                    aliases.setdefault(a.strip(), []).append(f["food_version_id"])
        self.aliases = aliases

    def get_food_version(self, food_version_id: str) -> Optional[dict]:
        return self.foods.get(food_version_id)

    def get_recipe_version(self, recipe_version_id: str) -> Optional[dict]:
        return self.recipes.get(recipe_version_id)

    def approved_foods(self) -> List[dict]:
        return [f for f in self.foods.values() if f.get("status") == "approved"]

    def approved_recipes(self) -> List[dict]:
        return [r for r in self.recipes.values() if r.get("status", "approved") == "approved"]

    def match_food_text(self, text: str) -> dict:
        """Exact-alias lookup only. Ambiguous matches are returned, never guessed."""
        ids = list(dict.fromkeys(self.aliases.get(text.strip(), [])))
        if not ids:
            return {"status": "none", "food_version_ids": []}
        return {"status": "exact" if len(ids) == 1 else "ambiguous", "food_version_ids": ids}


def _food(
    fid: str,
    name_he: str,
    kind: str = "generic",
    preparation_state: str = "raw",
    basis_g: str = "100",
    nutrients: Optional[Dict[str, Optional[str]]] = None,
    allergens: Optional[List[str]] = None,
    kosher: str = "unknown",
    status: str = "approved",
    aliases: Optional[List[str]] = None,
) -> dict:
    return {
        "food_version_id": fid,
        "name_he": name_he,
        "kind": kind,
        "preparation_state": preparation_state,
        "basis_g": basis_g,
        "nutrients": nutrients or {},
        "allergens": allergens or [],
        # same deterministic flags the library uses - computed from the
        # row's own data so fixture rows behave like reference rows.
        "attributes": derive_attributes(name_he, kind, allergens or [], nutrients or {}),
        "kosher_evidence": kosher,
        "status": status,
        "aliases": aliases or [name_he],
        "evidence": "synthetic-fixture",
    }


FOODS: Dict[str, dict] = {
    f["food_version_id"]: f
    for f in [
        _food(
            "f_chicken_cooked_v1", "חזה עוף מבושל", preparation_state="cooked",
            nutrients={"kcal": "165", "protein_g": "31", "carbs_g": "0", "fat_g": "3.6",
                       "sodium_mg": "74", "fiber_g": "0", "sugar_g": "0", "sat_fat_g": "1", "cholesterol_mg": "85"},
            kosher="unknown", aliases=["חזה עוף מבושל", "עוף מבושל"],
        ),
        _food(
            "f_rice_cooked_v1", "אורז לבן מבושל", preparation_state="cooked",
            nutrients={"kcal": "130", "protein_g": "2.7", "carbs_g": "28", "fat_g": "0.3",
                       "sodium_mg": "1", "fiber_g": "0.4", "sugar_g": "0.1", "sat_fat_g": "0.1", "cholesterol_mg": "0"},
            aliases=["אורז מבושל", "אורז לבן מבושל"],
        ),
        _food(
            "f_rice_dry_v1", "אורז לבן יבש", preparation_state="raw",
            nutrients={"kcal": "365", "protein_g": "7.1", "carbs_g": "80", "fat_g": "0.7",
                       "sodium_mg": "5", "fiber_g": "1.3", "sugar_g": "0.2", "sat_fat_g": "0.2", "cholesterol_mg": "0"},
            aliases=["אורז יבש", "אורז לבן יבש"],
        ),
        _food(
            "f_cucumber_v1", "מלפפון",
            nutrients={"kcal": "15", "protein_g": "0.7", "carbs_g": "3.6", "fat_g": "0.1",
                       "sodium_mg": "2", "fiber_g": "0.5", "sugar_g": "1.7", "sat_fat_g": "0", "cholesterol_mg": "0"},
        ),
        _food(
            "f_tomato_v1", "עגבנייה",
            nutrients={"kcal": "18", "protein_g": "0.9", "carbs_g": "3.9", "fat_g": "0.2",
                       "sodium_mg": "5", "fiber_g": "1.2", "sugar_g": "2.6", "sat_fat_g": "0", "cholesterol_mg": None},
        ),
        _food(
            "f_bread_v1", "לחם מחמצת", allergens=["gluten"],
            nutrients={"kcal": "265", "protein_g": "9", "carbs_g": "49", "fat_g": "3.2",
                       "sodium_mg": "491", "fiber_g": "2.7", "sugar_g": "3", "sat_fat_g": "0.7", "cholesterol_mg": "0"},
        ),
        _food(
            "f_tahini_v1", "טחינה גולמית", allergens=["sesame"],
            # sugar/sat_fat/cholesterol deliberately unknown on a used food:
            # exercises fail-closed 'unknown is never a pass' bound handling.
            nutrients={"kcal": "595", "protein_g": "17", "carbs_g": "21", "fat_g": "54",
                       "sodium_mg": "115", "fiber_g": "9"},
        ),
        _food(
            "f_egg_boiled_v1", "ביצה קשה", preparation_state="cooked", allergens=["egg"],
            nutrients={"kcal": "155", "protein_g": "13", "carbs_g": "1.1", "fat_g": "11",
                       "sodium_mg": "124", "fiber_g": "0", "sugar_g": "1.1", "sat_fat_g": "3.3", "cholesterol_mg": "373"},
        ),
        _food(
            "f_yogurt_v1", "יוגורט יווני", allergens=["milk"],
            nutrients={"kcal": "59", "protein_g": "10", "carbs_g": "3.6", "fat_g": "0.4",
                       "sodium_mg": "36", "fiber_g": "0", "sugar_g": "3.2", "sat_fat_g": "0.1", "cholesterol_mg": "10"},
        ),
        _food(
            "f_mystery_spread_v1", "ממרח ביתי (fixture)",
            nutrients={"kcal": "220", "protein_g": None, "carbs_g": "14", "fat_g": "18",
                       "sodium_mg": None, "fiber_g": None, "sugar_g": None, "sat_fat_g": None, "cholesterol_mg": None},
        ),
        _food(
            "f_cereal_old_v1", "דגני בוקר (גרסה מבוטלת)", status="retracted",
            nutrients={"kcal": "379", "protein_g": "7", "carbs_g": "84", "fat_g": "2",
                       "sodium_mg": "500", "fiber_g": "6", "sugar_g": "24", "sat_fat_g": "0.5", "cholesterol_mg": "0"},
        ),
    ]
}


def _recipe(
    rid: str, name_he: str, slots: List[str], items: List[dict],
    active_minutes: int, total_minutes: int, equipment: List[str], steps_he: List[str],
) -> dict:
    return {
        "recipe_version_id": rid,
        "name_he": name_he,
        "slots": slots,
        "items": items,
        "active_minutes": active_minutes,
        "total_minutes": total_minutes,
        "equipment": equipment,
        "steps_he": steps_he,
        "status": "approved",
        "evidence": "synthetic-fixture",
    }


RECIPES: Dict[str, dict] = {
    r["recipe_version_id"]: r
    for r in [
        _recipe(
            "r_chicken_plate_v1", "חזה עוף עם אורז וסלט", ["lunch", "dinner"],
            [
                {"food_version_id": "f_chicken_cooked_v1", "default_g": "150", "min_g": "100", "max_g": "300", "step_g": "25"},
                {"food_version_id": "f_rice_cooked_v1", "default_g": "200", "min_g": "100", "max_g": "400", "step_g": "25"},
                {"food_version_id": "f_cucumber_v1", "default_g": "100", "min_g": "0", "max_g": "300", "step_g": "25"},
                {"food_version_id": "f_tomato_v1", "default_g": "100", "min_g": "0", "max_g": "300", "step_g": "25"},
            ],
            active_minutes=10, total_minutes=10, equipment=["plate"],
            steps_he=["מניחים על הצלחת ומגישים"],
        ),
        _recipe(
            "r_egg_bread_v1", "ביצה קשה עם לחם ועגבנייה", ["breakfast", "snack"],
            [
                {"food_version_id": "f_egg_boiled_v1", "default_g": "100", "min_g": "50", "max_g": "200", "step_g": "50"},
                {"food_version_id": "f_bread_v1", "default_g": "60", "min_g": "30", "max_g": "180", "step_g": "30"},
                {"food_version_id": "f_tomato_v1", "default_g": "100", "min_g": "0", "max_g": "300", "step_g": "25"},
            ],
            active_minutes=5, total_minutes=5, equipment=["none"],
            steps_he=["פורסים, מניחים ליד הלחם"],
        ),
        _recipe(
            "r_tahini_plate_v1", "צלחת טחינה עם לחם", ["lunch", "dinner"],
            [
                {"food_version_id": "f_tahini_v1", "default_g": "50", "min_g": "25", "max_g": "100", "step_g": "25"},
                {"food_version_id": "f_bread_v1", "default_g": "60", "min_g": "30", "max_g": "180", "step_g": "30"},
                {"food_version_id": "f_tomato_v1", "default_g": "100", "min_g": "0", "max_g": "300", "step_g": "25"},
            ],
            active_minutes=5, total_minutes=5, equipment=["plate"],
            steps_he=["מורחים את הטחינה ומגישים עם הלחם"],
        ),
        _recipe(
            "r_yogurt_bowl_v1", "קערת יוגורט", ["breakfast", "snack"],
            [
                {"food_version_id": "f_yogurt_v1", "default_g": "200", "min_g": "100", "max_g": "400", "step_g": "50"},
            ],
            active_minutes=2, total_minutes=2, equipment=["none"],
            steps_he=["יוצאים לקערה"],
        ),
    ]
}

DEFAULT_SOURCE = CatalogSource()
