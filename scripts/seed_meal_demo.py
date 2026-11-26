# scripts/seed_meal_demo.py
"""Seed realistic nutrition demo data for נויה through the HTTP API only.

Every write goes through the public endpoints, so all invariants, validation,
idempotency and rule evaluation run exactly as in the real UI.

Usage:
    python scripts/seed_meal_demo.py                          # default, no medical bundle
    python scripts/seed_meal_demo.py --base-url http://localhost:8000
    python scripts/seed_meal_demo.py --reset                  # proceed when a plan exists
    python scripts/seed_meal_demo.py --with-medical           # also seed a MOCK medical bundle

Idempotency: the script refuses to run when נויה already has a published plan;
pass --reset to continue. --reset does NOT delete anything (the app role has no
DELETE and the ledger is immutable) — it publishes a fresh plan version built
from the demo content and re-creates only the additional demo artifacts that
are missing for today.

PIN: read from ADMIN_PIN, then PARENT_PIN, then APP_PIN (env or .env). The PIN
is sent as x-app-pin and is never printed. Child calls carry no PIN, exactly
like the real UI.
"""
import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

ARGS = None
FAILS = []

APP_TZ = "Asia/Jerusalem"
TODAY = datetime.now(ZoneInfo(APP_TZ)).date()


def load_dotenv():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


load_dotenv()
PIN = os.environ.get("ADMIN_PIN") or os.environ.get("PARENT_PIN") or os.environ.get("APP_PIN") or ""
ADMIN = {"x-app-pin": PIN} if PIN else {}
CHILD = {}  # PIN-less family model, same as the real UI


def fail(msg):
    print(f"  FAIL {msg}")
    FAILS.append(msg)


async def call(client, method, path, body=None, headers=None, expect=(200,), tag=""):
    r = await client.request(method, path, json=body, headers=headers or CHILD)
    if r.status_code not in expect:
        fail(f"{tag or path} -> {r.status_code}: {r.text[:400]}")
        return None
    try:
        return r.json()
    except Exception:
        return {}


async def get(client, path, headers=None, tag=""):
    return await call(client, "GET", path, headers=headers, tag=tag)


def meal_items(recipe):
    return [{"food_version_id": i["food_version_id"], "quantity_g": str(i["default_g"])}
            for i in recipe["items"]]


async def publish_plan(client, uid, content, summary="MOCK demo plan"):
    d = await call(client, "POST", f"/api/meals/{uid}/plan-drafts",
                   {"content": content}, ADMIN, tag="draft")
    if not d:
        return None
    draft_id, rev = d["draft_id"], d["draft_revision"]
    v = await call(client, "POST", f"/api/meals/{uid}/plan-drafts/{draft_id}/validate",
                   headers=ADMIN, tag="validate")
    if not v or not v.get("valid"):
        fail(f"validate: {v}")
        return None
    a = await call(client, "POST", f"/api/meals/{uid}/plan-drafts/{draft_id}/approve",
                   {"content_hash": v["content_hash"], "input_vector": v["input_vector"]},
                   {**ADMIN, "If-Match": str(rev)}, tag="approve")
    if not a:
        return None
    p = await call(client, "POST", f"/api/meals/{uid}/plan-drafts/{draft_id}/publish",
                   {"approval_id": a["approval_id"], "change_summary": summary},
                   {**ADMIN, "If-Match": str(rev), "Idempotency-Key": f"seed-{uuid.uuid4().hex[:12]}"},
                   tag="publish")
    return p


async def eat(client, uid, day, items, slot=None, plan_id=None, occurred=None):
    body = {"operation_id": str(uuid.uuid4()), "nutrition_date": day,
            "items": items, "plan_version_id": plan_id,
            "source_context": f"slot:{slot}" if slot else "manual"}
    if occurred:
        body["occurred_at"] = occurred
    return await call(client, "POST", f"/api/meals/{uid}/eating-events", body,
                      {"Idempotency-Key": f"seed-eat-{uuid.uuid4().hex[:16]}"},
                      tag=f"eat {day} {slot or 'free'}")


async def main():
    ap = argparse.ArgumentParser(
        description="Seed MOCK nutrition demo data for נויה via the HTTP API. "
                    "--reset never deletes anything: it publishes a new plan "
                    "version from the demo content (the app role has no DELETE; "
                    "history/ledger is immutable).")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--reset", action="store_true",
                    help="continue even if נויה already has a published plan "
                         "(publishes a fresh version; nothing is deleted)")
    ap.add_argument("--with-medical", action="store_true",
                    help="also register a MOCK professional and approve a MOCK "
                         "carbs bound, then republish (needs ADMIN role)")
    global ARGS
    ARGS = ap.parse_args()

    if not PIN:
        print("FAIL: no PIN in ADMIN_PIN/PARENT_PIN/APP_PIN (env/.env)")
        return 1

    async with httpx.AsyncClient(base_url=ARGS.base_url, timeout=30) as client:
        # ---- locate נויה (verify, don't assume id) ----
        users = await get(client, "/api/users", tag="users")
        if not users:
            return 1
        noya = next((u for u in users if u["name"] == "נויה" and u["role"] == "child"), None)
        lavi = next((u for u in users if u["name"] == "לביא" and u["role"] == "child"), None)
        if not noya:
            fail("user נויה not found")
            return 1
        uid = noya["id"]
        print(f"נויה = user {uid}")

        state = await get(client, f"/api/meals/{uid}/state", tag="state")
        already = state and state.get("has_active_plan")
        if already and not ARGS.reset:
            print("נויה already has a published plan. Re-run with --reset "
                  "(publishes a new version; nothing is deleted).")
            return 1
        if already:
            print("  --reset: publishing a fresh version (nothing deleted)")

        # ---- catalog ----
        cat = await get(client, "/api/meals/catalog", ADMIN, tag="catalog")
        if not cat:
            return 1
        recipes = {r["recipe_version_id"]: r for r in cat.get("recipes", [])}
        foods = {f["food_version_id"]: f for f in cat.get("foods", [])}
        SLOTS = ["breakfast", "lunch", "dinner", "snack"]
        for slot in SLOTS:
            allowed = [r for r in recipes.values() if slot in (r.get("slots") or [])]
            if len(allowed) < 2:
                fail(f"catalog has <2 recipes for slot {slot}")
        if FAILS:
            return 1

        plan_rid = {"breakfast": "r_egg_bread_v1", "lunch": "r_chicken_plate_v1",
                    "dinner": "r_tahini_plate_v1", "snack": "r_yogurt_bowl_v1"}
        missing = [r for r in plan_rid.values() if r not in recipes]
        if missing:
            fail(f"expected catalog recipes missing: {missing}")
            return 1
        alt_rid = {s: [r["recipe_version_id"] for r in recipes.values()
                       if s in (r.get("slots") or []) and r["recipe_version_id"] != plan_rid[s]]
                   for s in SLOTS}

        used_foods = {i["food_version_id"] for s in SLOTS for i in recipes[plan_rid[s]]["items"]}
        habitual = sorted(used_foods)[:4]
        excluded = [fid for fid, f in foods.items()
                    if fid not in used_foods and f.get("status", "approved") == "approved"][:1]

        # ---- 1. coach setup ----
        proposed = {
            "allergy_disclosure": "none_declared",
            "medical_condition_declared": "no",
            "allergies": [],
            "kosher_mode": "none",
            "habitual_food_ids": habitual,
            "excluded_food_ids": excluded,
            "disliked_recipe_ids": [],
            "preparation_defaults": {"max_total_minutes": 30},
        }
        intent = await call(client, "POST", f"/api/meals/{uid}/profile-change-intents",
                            {"proposed": proposed}, ADMIN, tag="profile intent")
        if intent:
            await call(client, "POST",
                       f"/api/meals/{uid}/profile-change-intents/{intent['intent_id']}/confirm",
                       headers=ADMIN, tag="profile confirm")

        content = {
            "meals": [{"slot_key": s, "recipe_version_id": plan_rid[s],
                       "items": meal_items(recipes[plan_rid[s]])} for s in SLOTS],
            "alternatives": alt_rid,
            "day_targets": {},  # empty = no target; the system must not invent any
        }
        pub = await publish_plan(client, uid, content)
        if not pub:
            return 1
        plan_id = pub.get("plan_id") or pub.get("id")
        print(f"  published plan v{pub.get('version')} ({plan_id})")

        # ---- 2. six days of history ----
        slot_names = {"breakfast": "בוקר", "lunch": "צהריים", "dinner": "ערב", "snack": "ביניים"}
        noon = lambda d: f"{d.isoformat()}T12:30:00"
        for back in range(6, 0, -1):
            day = TODAY - timedelta(days=back)
            ds = day.isoformat()
            if back == 1:
                # incomplete day: one description-only "אכלתי משהו אחר"
                await eat(client, uid, ds, [{"description": "MOCK: פיצה בבית ספר"}],
                          plan_id=plan_id, occurred=noon(day))
                await eat(client, uid, ds, meal_items(recipes[plan_rid["breakfast"]]),
                          slot="breakfast", plan_id=plan_id, occurred=f"{ds}T08:00:00")
            elif back == 2:
                # one event voided + one correction
                ev = await eat(client, uid, ds, meal_items(recipes[plan_rid["snack"]]),
                               slot="snack", plan_id=plan_id, occurred=f"{ds}T16:00:00")
                if ev:
                    await call(client, "POST",
                               f"/api/meals/{uid}/eating-events/{ev.get('id') or ev.get('event_id')}/voids",
                               tag="void")
                ev2 = await eat(client, uid, ds, meal_items(recipes[plan_rid["lunch"]]),
                                slot="lunch", plan_id=plan_id, occurred=noon(day))
                if ev2:
                    corrected = meal_items(recipes[plan_rid["lunch"]])
                    corrected[0]["quantity_g"] = "200"  # ate more chicken than planned
                    await call(client, "POST",
                               f"/api/meals/{uid}/eating-events/{ev2.get('id') or ev2.get('event_id')}/corrections",
                               {"items": corrected}, tag="correction")
                await eat(client, uid, ds, meal_items(recipes[plan_rid["breakfast"]]),
                          slot="breakfast", plan_id=plan_id, occurred=f"{ds}T08:00:00")
                await eat(client, uid, ds, meal_items(recipes[plan_rid["dinner"]]),
                          slot="dinner", plan_id=plan_id, occurred=f"{ds}T19:00:00")
            elif back == 3:
                # changed amount day: 150g egg instead of the planned 100g
                changed = meal_items(recipes[plan_rid["breakfast"]])
                changed[0]["quantity_g"] = "150"
                await eat(client, uid, ds, changed, slot="breakfast",
                          plan_id=plan_id, occurred=f"{ds}T08:00:00")
                for s in ("lunch", "dinner"):
                    await eat(client, uid, ds, meal_items(recipes[plan_rid[s]]),
                              slot=s, plan_id=plan_id, occurred=noon(day))
            else:
                for s in ("breakfast", "lunch", "dinner"):
                    await eat(client, uid, ds, meal_items(recipes[plan_rid[s]]),
                              slot=s, plan_id=plan_id, occurred=noon(day))
        print("  history: 6 days of eating events (incl. void + correction + incomplete day)")

        # ---- 3. today ----
        today_s = TODAY.isoformat()
        await eat(client, uid, today_s, meal_items(recipes[plan_rid["breakfast"]]),
                  slot="breakfast", plan_id=plan_id,
                  occurred=f"{today_s}T08:00:00")
        sess = await call(client, "POST", f"/api/meals/{uid}/suggestion-sessions",
                          {"slot_key": "lunch", "nutrition_date": today_s}, tag="session")
        sel_recipe = None
        if sess:
            job = await call(client, "POST", f"/api/meals/{uid}/proposal-jobs",
                             {"slot_key": "lunch", "nutrition_date": today_s,
                              "session_id": sess["session_id"]}, tag="proposal")
            prop = (job or {}).get("proposal") or {}
            if prop.get("proposal_id"):
                sel = await call(client, "POST", f"/api/meals/{uid}/selections",
                                 {"nutrition_date": today_s, "slot_key": "lunch",
                                  "proposal_id": prop["proposal_id"],
                                  "session_id": sess["session_id"]}, tag="selection")
                sel_recipe = prop.get("recipe_version_id")
                if sel:
                    # saved meal from this selection
                    snap_items = [{"food_version_id": i["food_version_id"],
                                   "quantity_g": str(i["quantity_g"])}
                                  for i in prop.get("items", []) if i.get("food_version_id")]
                    await call(client, "POST", f"/api/meals/{uid}/saved-meals",
                               {"name": f"MOCK {prop.get('name_he') or 'הארוחה שלי'}",
                                "snapshot": {"recipe_version_id": sel_recipe,
                                             "items": snap_items}},
                               tag="saved meal")
        print(f"  today: breakfast eaten, lunch selected ({sel_recipe}), dinner+snack planned")

        # ---- 4. health report: created by child, cancelled by coach ----
        hi = await call(client, "POST", f"/api/meals/{uid}/health-update-intents",
                        {"reported_text": "MOCK: כאב לי הבטן אחרי ארוחת צהריים",
                         "source_context": "demo_seed"}, tag="health intent")
        if hi:
            await call(client, "POST",
                       f"/api/meals/{uid}/health-update-intents/{hi['intent_id']}/resolve",
                       {"action": "cancel",
                        "resolution": {"note": "MOCK — הדגמה בלבד"}}, ADMIN, tag="health resolve")
        print("  health report created + resolved (cancelled)")

        # ---- 5. optional MOCK medical bundle ----
        if ARGS.with_medical:
            # meal-scope carbs max = max meal carbs rounded up to next 10
            max_carbs = 0.0
            for s in SLOTS:
                tot = 0.0
                for it in meal_items(recipes[plan_rid[s]]):
                    n = (foods.get(it["food_version_id"], {}).get("nutrients") or {})
                    if n.get("carbs_g") is None:
                        continue
                    tot += float(n["carbs_g"]) * float(it["quantity_g"]) / 100.0
                max_carbs = max(max_carbs, tot)
            import math
            bound = int(math.ceil(max_carbs / 10.0) * 10)
            print(f"  MOCK carbs bound: max planned meal carbs {max_carbs:.1f}g -> bound {bound}g")
            prof = await call(client, "POST", "/api/meals/professionals",
                              {"name": "MOCK דיאטנית לדוגמה", "profession": "dietitian",
                               "license_number": "MOCK-0000",
                               "valid_until": (TODAY + timedelta(days=365)).isoformat()},
                              ADMIN, tag="professional")
            if prof:
                proposed["medical_condition_declared"] = "yes"
                intent = await call(client, "POST",
                                    f"/api/meals/{uid}/profile-change-intents",
                                    {"proposed": proposed}, ADMIN, tag="profile intent(med)")
                if intent:
                    await call(client, "POST",
                               f"/api/meals/{uid}/profile-change-intents/{intent['intent_id']}/confirm",
                               headers=ADMIN, tag="profile confirm(med)")
                bundle = await call(client, "POST",
                                    f"/api/meals/{uid}/clinical-rule-bundles",
                                    {"condition_label": "MOCK — הדגמה בלבד",
                                     "professional_id": prof.get("professional_id") or prof.get("id"),
                                     "instruction_date": TODAY.isoformat(),
                                     "valid_until": (TODAY + timedelta(days=365)).isoformat(),
                                     "rules": [{"kind": "nutrient_bounds", "scope": "meal",
                                                "nutrient": "carbs_g", "max": str(bound)}]},
                                    ADMIN, tag="bundle")
                if bundle:
                    bid = bundle.get("bundle_id") or bundle.get("id")
                    await call(client, "POST",
                               f"/api/meals/{uid}/clinical-rule-bundles/{bid}/approve",
                               {"professional_id": prof.get("professional_id") or prof.get("id")},
                               ADMIN, tag="bundle approve")
                    pub2 = await publish_plan(client, uid, content, "MOCK demo plan + medical bound")
                    if pub2:
                        print(f"  republished plan v{pub2.get('version')} with MOCK bundle")

        # ---- summary ----
        st = await get(client, f"/api/meals/{uid}/state", tag="state(final)")
        day = await get(client, f"/api/meals/{uid}/days/{today_s}", tag="day(final)")
        sel = await get(client, f"/api/meals/{uid}/selections?nutrition_date={today_s}",
                        tag="selections(final)")
        print()
        print("=== סיכום ===")
        print(f"נויה (user {uid}): setup={st.get('setup_status') if st else '?'} "
              f"plan v{st.get('active_plan_version') if st else '?'}")
        for s in SLOTS:
            eaten = any(e.get("source_context") == f"slot:{s}"
                        for e in (day or {}).get("events", [])
                        if e.get("event_type") == "recorded")
            selected = any(x.get("slot_key") == s for x in (sel or {}).get("selections", []))
            print(f"  {slot_names[s]}: " +
                  ("נאכל" if eaten else "נבחר — עוד לא נאכל" if selected else "מתוכנן"))
        if lavi:
            st2 = await get(client, f"/api/meals/{lavi['id']}/state", tag="state(lavi)")
            print(f"לביא (user {lavi['id']}): setup={st2.get('setup_status') if st2 else '?'} (צריך להישאר not_started)")
        print("פתח http://localhost:8000 → נויה → 🥗 סוכן התזונה")

    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
