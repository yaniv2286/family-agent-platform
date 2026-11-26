"""Real-source food lookup for the meal coach (catalog auto-enrichment).

When the trainee asks for a food the shared catalog does not have, this
module resolves it against a REAL nutrient source - never an LLM guess:

  1. Open Food Facts product search (6s timeout, most-complete-nutriments
     product wins).
  2. A small built-in fallback table of synthetic per-100g estimates, used
     ONLY when the network lookup errors or finds nothing AND the name
     fuzzy-matches the table. Entries are marked source="builtin-synthetic".

A missing nutrient is None - never zero. Absent allergen data is unknown
(None), never an empty safe list. Results are cached in-process for 24h.

lookup_food returns:
    {
        "name_he": str,                    # requested term if no product name
        "nutrients": {field: str | None},  # per-100g, all NUTRIENT_FIELDS keys
        "allergens": list[str] | None,     # None = unknown allergen set
        "source": "openfoodfacts" | "builtin-synthetic",
        "evidence": str,
    }
or None when nothing usable was found.
"""
import re
import time
from typing import Dict, List, Optional

import httpx

from .engine import NUTRIENT_FIELDS
from .units import dec

OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
LOOKUP_TIMEOUT_SECONDS = 6
_CACHE_TTL_SECONDS = 24 * 3600

# OFF nutriment key -> (internal field, multiplier). OFF sodium/cholesterol
# are grams per 100g; our catalog stores milligrams.
_OFF_NUTRIMENT_MAP = {
    "energy-kcal_100g": ("kcal", 1),
    "proteins_100g": ("protein_g", 1),
    "carbohydrates_100g": ("carbs_g", 1),
    "fat_100g": ("fat_g", 1),
    "fiber_100g": ("fiber_g", 1),
    "sodium_100g": ("sodium_mg", 1000),
    "sugars_100g": ("sugar_g", 1),
    "saturated-fat_100g": ("sat_fat_g", 1),
    "cholesterol_100g": ("cholesterol_mg", 1000),
}

_OFF_ALLERGEN_MAP = {
    "en:milk": "milk",
    "en:gluten": "gluten",
    "en:eggs": "egg",
    "en:peanuts": "peanut",
    "en:nuts": "nuts",
    "en:soybeans": "soy",
    "en:fish": "fish",
    "en:sesame-seeds": "sesame",
}

# ---------------------------------------------------------------------------
# Offline fallback table - SYNTHETIC per-100g estimates (not measured data).
# Used only when OFF is unreachable/empty and the name fuzzy-matches. Every
# entry is served unverified and waits for coach verification.
# ---------------------------------------------------------------------------
BUILTIN_FOODS: Dict[str, dict] = {
    "סטייק בקר": {
        "aliases": ["סטייק", "סטייק בקר", "sirloin", "steak", "beef steak"],
        "nutrients": {"kcal": "200", "protein_g": "27", "carbs_g": "0",
                      "fat_g": "9", "fiber_g": "0", "sodium_mg": "60",
                      "sugar_g": "0", "sat_fat_g": "3.5", "cholesterol_mg": "80"},
        "allergens": [],
    },
    "פסטה מבושלת": {
        "aliases": ["פסטה", "פסטה מבושלת", "pasta", "spaghetti"],
        "nutrients": {"kcal": "158", "protein_g": "5.8", "carbs_g": "31",
                      "fat_g": "0.9", "fiber_g": "1.8", "sodium_mg": "1",
                      "sugar_g": "0.6", "sat_fat_g": "0.2", "cholesterol_mg": "0"},
        "allergens": ["gluten"],
    },
    "אורז מבושל": {
        "aliases": ["אורז", "אורז מבושל", "rice", "white rice"],
        "nutrients": {"kcal": "130", "protein_g": "2.7", "carbs_g": "28",
                      "fat_g": "0.3", "fiber_g": "0.4", "sodium_mg": "1",
                      "sugar_g": "0.1", "sat_fat_g": "0.1", "cholesterol_mg": "0"},
        "allergens": [],
    },
    "פרוסת פיצה": {
        "aliases": ["פיצה", "פרוסת פיצה", "pizza", "pizza slice"],
        "nutrients": {"kcal": "266", "protein_g": "11", "carbs_g": "33",
                      "fat_g": "10", "fiber_g": "2.3", "sodium_mg": "598",
                      "sugar_g": "3.8", "sat_fat_g": "4.5", "cholesterol_mg": "17"},
        "allergens": ["gluten", "milk"],
    },
    "שקדי מרקס": {
        "aliases": ["שקדי מרקס", "שקדים למרק", "soup almonds", "croutons"],
        "nutrients": {"kcal": "430", "protein_g": "10", "carbs_g": "70",
                      "fat_g": "12", "fiber_g": "3", "sodium_mg": "800",
                      "sugar_g": "3", "sat_fat_g": "2", "cholesterol_mg": "15"},
        "allergens": ["gluten", "egg"],
    },
    "המבורגר בקר": {
        "aliases": ["המבורגר", "המבורגרית", "המבורגר בקר", "hamburger", "burger", "beef burger"],
        "nutrients": {"kcal": "254", "protein_g": "17", "carbs_g": "20",
                      "fat_g": "12", "fiber_g": "1", "sodium_mg": "380",
                      "sugar_g": "4", "sat_fat_g": "4.5", "cholesterol_mg": "55"},
        "allergens": ["gluten", "egg"],
    },
    "צ'יפס": {
        "aliases": ["צ'יפס", "ציפס", "תפוחי אדמה מטוגנים", "french fries", "fries", "chips"],
        "nutrients": {"kcal": "312", "protein_g": "3.4", "carbs_g": "41",
                      "fat_g": "15", "fiber_g": "3.8", "sodium_mg": "210",
                      "sugar_g": "0.6", "sat_fat_g": "1.5", "cholesterol_mg": "0"},
        "allergens": [],
    },
    "שניצל עוף": {
        "aliases": ["שניצל", "שניצל עוף", "schnitzel", "chicken schnitzel"],
        "nutrients": {"kcal": "260", "protein_g": "20", "carbs_g": "15",
                      "fat_g": "12", "fiber_g": "1", "sodium_mg": "450",
                      "sugar_g": "1", "sat_fat_g": "2.5", "cholesterol_mg": "60"},
        "allergens": ["gluten", "egg"],
    },
    "חומוס מוכן": {
        "aliases": ["חומוס", "חומוס מוכן", "hummus"],
        "nutrients": {"kcal": "166", "protein_g": "8", "carbs_g": "14",
                      "fat_g": "10", "fiber_g": "6", "sodium_mg": "380",
                      "sugar_g": "0.3", "sat_fat_g": "1.4", "cholesterol_mg": "0"},
        "allergens": ["sesame"],
    },
    "בננה": {
        "aliases": ["בננה", "banana"],
        "nutrients": {"kcal": "89", "protein_g": "1.1", "carbs_g": "22.8",
                      "fat_g": "0.3", "fiber_g": "2.6", "sodium_mg": "1",
                      "sugar_g": "12.2", "sat_fat_g": "0.1", "cholesterol_mg": "0"},
        "allergens": [],
    },
    "תפוח": {
        "aliases": ["תפוח", "apple"],
        "nutrients": {"kcal": "52", "protein_g": "0.3", "carbs_g": "13.8",
                      "fat_g": "0.2", "fiber_g": "2.4", "sodium_mg": "1",
                      "sugar_g": "10.4", "sat_fat_g": "0", "cholesterol_mg": "0"},
        "allergens": [],
    },
    "שוקולד מריר": {
        "aliases": ["שוקולד", "שוקולד מריר", "chocolate", "dark chocolate"],
        "nutrients": {"kcal": "550", "protein_g": "5", "carbs_g": "60",
                      "fat_g": "32", "fiber_g": "7", "sodium_mg": "20",
                      "sugar_g": "48", "sat_fat_g": "19", "cholesterol_mg": "5"},
        "allergens": ["milk", "soy"],
    },
    "קורנפלקס": {
        "aliases": ["קורנפלקס", "דגני בוקר", "cornflakes", "cereal"],
        "nutrients": {"kcal": "357", "protein_g": "7", "carbs_g": "84",
                      "fat_g": "1", "fiber_g": "3", "sodium_mg": "700",
                      "sugar_g": "8", "sat_fat_g": "0.3", "cholesterol_mg": "0"},
        "allergens": ["gluten"],
    },
    "גבינה לבנה": {
        "aliases": ["גבינה לבנה", "גבינה", "white cheese", "cream cheese"],
        "nutrients": {"kcal": "100", "protein_g": "12", "carbs_g": "3",
                      "fat_g": "4.5", "fiber_g": "0", "sodium_mg": "350",
                      "sugar_g": "3", "sat_fat_g": "3", "cholesterol_mg": "15"},
        "allergens": ["milk"],
    },
}

# name-normalized index over the table's aliases
_BUILTIN_INDEX: Dict[str, str] = {}
for _canon, _entry in BUILTIN_FOODS.items():
    for _alias in [_canon] + list(_entry["aliases"]):
        _BUILTIN_INDEX[_alias.strip().lower()] = _canon


def _normalize(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


_cache: Dict[str, tuple] = {}  # normalized name -> (timestamp, result|None)


def _cached(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    if hit:
        _cache.pop(key, None)
    return None


def _product_nutrients(product: dict) -> dict:
    """Map OFF nutriments to our per-100g fields. Missing -> None, never 0."""
    nutriments = product.get("nutriments") or {}
    out = {f: None for f in NUTRIENT_FIELDS}
    for off_key, (field, multiplier) in _OFF_NUTRIMENT_MAP.items():
        raw = nutriments.get(off_key)
        if raw is None:
            continue
        try:
            out[field] = format(dec(raw) * dec(multiplier).normalize(), "f")
        except ValueError:
            continue
    return out


def _product_allergens(product: dict) -> Optional[List[str]]:
    """allergens_tags -> our allergen keys. Key absent -> unknown (None)."""
    tags = product.get("allergens_tags")
    if tags is None:
        return None
    mapped = []
    for tag in tags:
        ours = _OFF_ALLERGEN_MAP.get(tag)
        if ours and ours not in mapped:
            mapped.append(ours)
    return mapped


async def _off_lookup(name: str) -> Optional[dict]:
    params = {
        "search_terms": name,
        "search_simple": 1,
        "action": "process",
        "json": 1,
        "fields": "product_name,nutriments,allergens_tags",
        "page_size": 5,
    }
    async with httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_SECONDS) as client:
        resp = await client.get(OFF_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    products = data.get("products") or []
    best = None
    best_score = -1
    for product in products:
        if not isinstance(product, dict):
            continue
        nutrients = _product_nutrients(product)
        score = sum(1 for v in nutrients.values() if v is not None)
        if score > best_score:
            best_score = score
            best = (product, nutrients)
    if best is None or best_score <= 0:
        return None
    product, nutrients = best
    return {
        # Hebrew display name = the requested term when OFF has no product
        # name; the product name itself is kept in aliases downstream.
        "name_he": (product.get("product_name") or "").strip() or name.strip(),
        "product_name": (product.get("product_name") or "").strip() or None,
        "nutrients": nutrients,
        "allergens": _product_allergens(product),
        "source": "openfoodfacts",
        "evidence": "openfoodfacts",
    }


# Hebrew bound-prefix letters (והבלכמש) attach to a noun in running text:
# "וצ'יפס" is "צ'יפס" with a leading 'ו'. Match allows alias + one prefix.
_PREFIX_LETTERS = "והבלכמש"
_TOKEN_SPLIT = re.compile(r"[\s,.!?;:()\[\]{}\"“”״]+")


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_SPLIT.split(_normalize(text)) if t]


def _token_matches(alias_tok: str, text_tok: str) -> bool:
    if text_tok == alias_tok:
        return True
    return len(alias_tok) >= 2 and text_tok in {p + alias_tok for p in _PREFIX_LETTERS}


def alias_terms_in_text(text: str, alias_index: Dict[str, str]) -> List[str]:
    """Canonical keys whose alias appears as a whole token sequence inside
    free text (multi-word aliases must match consecutive tokens). Whole-
    token matching - never substring: 'חלב' can never fire inside 'חלבון'.
    Longest aliases first so 'המבורגר בקר' wins over 'המבורגר'."""
    tokens = _tokens(text)
    found: List[str] = []
    for alias, canon in sorted(alias_index.items(),
                               key=lambda kv: -len(_tokens(kv[0]))):
        atoks = _tokens(alias)
        if not atoks or canon in found:
            continue
        for i in range(len(tokens) - len(atoks) + 1):
            if all(_token_matches(a, tokens[i + j])
                   for j, a in enumerate(atoks)):
                found.append(canon)
                break
    return found


def builtin_terms_in_text(text: str) -> List[str]:
    """Canonical builtin names whose aliases appear inside free text.
    Lets the caller recover a food request even when the model omitted
    requestedFoodName."""
    return alias_terms_in_text(text, _BUILTIN_INDEX)


def _builtin_lookup(normalized: str) -> Optional[dict]:
    """Fuzzy match the offline table: exact alias, else single-direction
    containment ('סטייק' matches 'סטייק בקר'). Ambiguous -> None."""
    canon = _BUILTIN_INDEX.get(normalized)
    if canon is None:
        hits = {
            key for key in _BUILTIN_INDEX
            if normalized and (normalized in key or key in normalized)
        }
        canons = {_BUILTIN_INDEX[h] for h in hits}
        if len(canons) == 1:
            canon = canons.pop()
    if canon is None:
        return None
    entry = BUILTIN_FOODS[canon]
    return {
        "name_he": canon,
        "product_name": None,
        "nutrients": {f: entry["nutrients"].get(f) for f in NUTRIENT_FIELDS},
        "allergens": list(entry["allergens"]),  # table allergens are known data
        "source": "builtin-synthetic",
        "evidence": "builtin-synthetic",
    }


async def lookup_food(name: str) -> Optional[dict]:
    """Resolve a free-text food name to real nutrient data.

    Open Food Facts first; the synthetic built-in table only when the
    network lookup errors or finds nothing and the name fuzzy-matches it.
    Same name -> same result within 24h (misses are cached too)."""
    key = _normalize(name)
    if not key:
        return None
    hit = _cached(key)
    if hit is not None:
        return hit
    if key in _cache:  # cached miss within TTL
        return None
    result = None
    try:
        result = await _off_lookup(name)
    except Exception:
        result = None
    if result is None:
        result = _builtin_lookup(key)
    _cache[key] = (time.time(), result)
    return result
