#!/usr/bin/env python3
"""Exercise every renderer through the HTTP surface.

One calculator of each shape, driven the way a client would drive it, with the
clinical answers checked against the same reference values `clinical_checks.py`
uses. If the API and the extractor ever disagree, that shows up here.
"""

from __future__ import annotations

import sys
from datetime import datetime
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.config import CALCULATORS
from app.main import app

C = {"ok": "\033[92m", "bad": "\033[91m", "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}
client = TestClient(app)
failures: list[str] = []


# However many the build published. Stated once, read from the folder the
# API serves, so a calculator that quietly stops being built fails here.
PUBLISHED = len([f for f in CALCULATORS.glob('*.json')
                 if f.name not in ('index.json', 'categories.json')])


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {C['ok']}✓{C['r']} {name} {C['dim']}{detail}{C['r']}")
    else:
        print(f"  {C['bad']}✗{C['r']} {name} {C['bad']}{detail}{C['r']}")
        failures.append(name)


def near(a, b, tol=0.02) -> bool:
    try:
        return abs(float(a) - float(b)) <= max(abs(float(b)) * tol, 0.02)
    except (TypeError, ValueError):
        return False


def calc(slug: str, **body):
    return client.post(f"/api/calculators/{slug}/calculate", json=body).json()


print(f"\n{C['b']}══ API smoke test ══{C['r']}")

# -- catalogue -------------------------------------------------------------
health = client.get("/api/health").json()
check("health", health["status"] == "ok" and health["calculators"] == PUBLISHED,
      f"{health['calculators']} calculators")

listing = client.get("/api/calculators", params={"limit": 500}).json()
check("list all", listing["count"] == PUBLISHED, f"count={listing['count']}")

search = client.get("/api/calculators", params={"q": "creatinine clearance"}).json()
check("search ranks title matches first", search["count"] >= 3,
      search["calculators"][0]["title"] if search["count"] else "none")

cat = client.get("/api/catalog").json()
check("catalog meta", len(cat["categories"]) == 35 and len(cat["renderers"]) == 8)

# The surface is small on purpose: one endpoint per job, no aliases.
api_routes = {
    (sorted(r.methods - {"HEAD", "OPTIONS"})[0], r.path)
    for r in app.routes
    if getattr(r, "methods", None) and r.path.startswith("/api")
}
check("no duplicate or unused endpoints", len(api_routes) == 8,
      f"{len(api_routes)} endpoints")

check("404 for unknown slug",
      client.get("/api/calculators/not-a-real-calculator").status_code == 404)

# -- schema / section plan -------------------------------------------------
covered = set()
for entry in listing["calculators"]:
    r = client.get(f"/api/calculators/{entry['slug']}/schema")
    if r.status_code != 200:
        failures.append(f"schema {entry['slug']}")
        continue
    plan = r.json()["sections"]
    covered.update(s["kind"] for s in plan)
    if not plan:
        failures.append(f"empty section plan: {entry['slug']}")
check("every calculator has a section plan",
      not any(f.startswith("empty section plan") for f in failures))
check("section kinds in use", len(covered) >= 8, ", ".join(sorted(covered)))

# -- formula ---------------------------------------------------------------
r = calc("creatinine-clearance-by-cockcroft-gault-age-16-years",
         inputs={"age": 70, "weight": 70, "serum_creatinine": 1.0, "sex": 0.85})
crcl = next((o for o in r["outputs"] if o["key"] == "estimated_creatinine_clearance"), {})
check("formula: Cockcroft-Gault female", near(crcl.get("value"), 57.85),
      f"{crcl.get('value')} mL/min")

r = calc("dobutamine", inputs={"dose": 5, "weight": 154.324, "concentration": 1000,
                               "drug_amount": 250, "infusate_volume": 250},
         units={"weight": "lbs"})
rate_lb = next((o for o in r["outputs"] if o["key"] == "infuse_rate"), {})
check("units: weight accepted in pounds", near(rate_lb.get("value"), 21.0),
      f"154.3 lb -> {rate_lb.get('value')} mL/hr")

# -- validation ------------------------------------------------------------
r = calc("creatinine-clearance-by-cockcroft-gault-age-16-years",
         inputs={"age": 4, "weight": 70, "serum_creatinine": 1.0, "sex": 0.85})
check("bounds rejected", not r["ok"] and any(e["field"] == "age" for e in r["errors"]),
      r["errors"][0]["message"] if r["errors"] else "")

r = calc("creatinine-clearance-by-cockcroft-gault-age-16-years",
         inputs={"age": 70, "weight": 70, "serum_creatinine": "abc", "sex": 0.85})
check("non-numeric rejected", not r["ok"], r["errors"][0]["message"] if r["errors"] else "")

r = calc("dobutamine", inputs={"dose": 5, "weight": 154.324, "concentration": 1000,
                               "drug_amount": 250, "infusate_volume": 250},
         units={"weight": "lbs"})
rate_kg = next((o for o in r["outputs"] if o["key"] == "infuse_rate"), {})
check("pounds and kilograms give the same answer", near(rate_kg.get("value"), 21.0),
      f"154.324 lb == 70 kg -> {rate_kg.get('value')} mL/hr")

# -- score -----------------------------------------------------------------
schema = client.get("/api/calculators/4t-score/schema").json()
groups = schema["scoring"]["groups"]
top = {g["key"]: g["options"][0]["key"] for g in groups}
r = calc("4t-score", selections=top)
check("score: all top options", r["score"]["total"] == 8,
      f"total {r['score']['total']}, band {r['score']['band']['label']}")

low = {g["key"]: g["options"][-1]["key"] for g in groups}
r = calc("4t-score", selections=low)
check("score: all lowest options", r["score"]["total"] == 0
      and "low" in (r["score"]["band"]["label"] or "").lower(),
      f"total {r['score']['total']}, band {r['score']['band']['label']}")

r = calc("4t-score", selections={})
check("score: missing criteria reported", not r["ok"] and len(r["errors"]) == len(groups))

# -- variant constants (sex-dependent coefficient sets) --------------------
base = {"age": 55, "total_cholesterol": 213, "hdl_cholesterol": 50,
        "systolic_blood_pressure": 120, "smoker": 0, "diabetes": 0,
        "on_hypertension_med": 0}
got = {}
for label, sex, race in (("man", 1, 1), ("woman", 0, 1)):
    r = calc("acc-aha-2013-cardiovascular-risk-assessment", inputs={**base, "sex": sex, "race": race})
    got[label] = next(o["value"] for o in r["outputs"] if o["key"] == "ten_year_risk")
check("ACC/AHA matches the published example",
      near(got["man"], 5.3, 0.05) and near(got["woman"], 2.1, 0.05),
      f"man {got['man']:.2f}%, woman {got['woman']:.2f}%")

# -- lookup ladders (growth charts) ----------------------------------------
r = calc("cdc-growth-percentiles-36-months",
         inputs={"sex": 1.0, "age": 12.5, "length": 76.0, "weight": 9.6,
                 "head_circumference": 46.0})
girl = next(o["value"] for o in r["outputs"] if o["key"] == "weight_percentile")
r = calc("cdc-growth-percentiles-36-months",
         inputs={"sex": 2.0, "age": 12.5, "length": 76.0, "weight": 9.6,
                 "head_circumference": 46.0})
boy = next(o["value"] for o in r["outputs"] if o["key"] == "weight_percentile")
check("lms: the sex selector changes the table",
      near(girl, 47.4) and near(boy, 21.6), f"girl {girl}%ile, boy {boy}%ile")

# -- titration table -------------------------------------------------------
r = calc("dobutamine", inputs={"dose": 5, "weight": 70, "concentration": 1000,
                               "drug_amount": 250, "infusate_volume": 250})
rate = next(o["value"] for o in r["outputs"] if o["key"] == "infuse_rate")
check("titration: scalar rate", near(rate, 21.0), f"{rate} mL/hr")
check("titration: ladder returned",
      r["table"] and len(r["table"]["rows"]) > 5,
      f"{len(r['table']['rows']) if r['table'] else 0} rows")
row5 = next((x for x in (r["table"]["rows"] if r["table"] else []) if x["dose"] == 5.0), None)
check("titration: the 5 mcg/kg/min row matches the scalar answer",
      row5 is not None and near(list(row5.values())[1], 21.0),
      str(row5))

# -- converter -------------------------------------------------------------
schema = client.get("/api/calculators/unit-conversions-weight/schema").json()
pairs = next(s["pairs"] for s in schema["sections"] if s["kind"] == "converter")
idx = next(i for i, p in enumerate(pairs) if p["from"] == "kg" and p["to"] == "lb")
r = calc("unit-conversions-weight", pair_index=idx, value=70)
check("convert: 70 kg -> lb", near(r["conversion"]["result"], 154.32),
      f"{r['conversion']['result']} lb")
r = calc("unit-conversions-weight", pair_index=idx, value=154.32, reverse=True)
check("convert: reversed", near(r["conversion"]["result"], 70.0),
      f"{r['conversion']['result']} kg")

# -- decision tree ---------------------------------------------------------
r = calc("rabies-post-exposure-prophylaxis-treecalc", answers=[])
check("tree: opens on the first question", bool(r["tree"]["question"]),
      (r["tree"]["question"] or "")[:60])
r = calc("rabies-post-exposure-prophylaxis-treecalc", answers=["no"])
check("tree: a 'no' at the root ends it", r["tree"]["done"] and r["tree"]["outcome"],
      (r["tree"]["outcome"] or {}).get("text"))

# -- weight-based drug table ----------------------------------------------
r = calc("advanced-life-support-adult", inputs={"weight": 70})
check("dose table: drugs dosed for the weight",
      r["drugs"] and len(r["drugs"]["drugs"]) > 5,
      f"{len(r['drugs']['drugs']) if r['drugs'] else 0} drugs")

r = calc("advanced-life-support-adult", inputs={"weight": 70})
def _dose(name, phase=None):
    for d in (r["drugs"] or {}).get("drugs", []):
        if name.lower() in d["name"].lower():
            for rt in d["routes"]:
                for x in rt["doses"]:
                    if phase is None or phase.lower() in (x["phase"] or "").lower():
                        return x
    return {}
ne = _dose("Norepinephrine")
check("dose table: a ranged infusion is a range, not a dash",
      ne.get("value") == [7.0, 35.0],
      f"0.1-0.5 mcg/kg/min at 70 kg -> {ne.get('value')} {ne.get('unit')}")
ad = _dose("Adenosine", "Initial")
check("dose table: a fixed dose does not scale with weight",
      ad.get("value") == 6.0 and not ad.get("per_kg"), f"{ad.get('value')} mg")
check("dose table: no dose is left unstated",
      not [x for d in r["drugs"]["drugs"] for rt in d["routes"]
           for x in rt["doses"] if x["value"] is None],
      f"{sum(len(rt['doses']) for d in r['drugs']['drugs'] for rt in d['routes'])} doses")

# -- MMED ------------------------------------------------------------------
r = calc("morphine-milligram-equivalents-per-day-mmed",
         inputs={"oxycodone": 30, "methadone": 30, "fentanyl": 25, "morphine": 10})
total = next(o["value"] for o in r["outputs"] if o["key"] == "total_mmed")
check("mmed: CDC factors with the methadone band", near(total, 355.0), f"{total} MMED")

# -- dates -----------------------------------------------------------------
ms = lambda y, m, d: datetime(y, m, d, 12).timestamp() * 1000  # noqa: E731
r = calc("gestational-age",
         inputs={"crown_rump_length": 40, "biparietal_diameter": 50,
                 "head_circumference": 200, "current_time": ms(2026, 9, 1),
                 "lmp_time": ms(2026, 1, 1), "us_time": ms(2026, 9, 1)})
weeks = next(o["value"] for o in r["outputs"] if o["key"] == "lmpweeks")
check("dates: gestation from LMP", near(weeks, 34.71), f"{weeks} weeks")

# -- names list and the one-shot /run endpoint -----------------------------
names = client.get("/api/calculators/names").json()
check("names list", names["count"] == PUBLISHED and "slug" in names["names"][0],
      f"{names['count']} names, first {names['names'][0]['name']!r}")

r = client.post("/api/calculators/Cockcroft Gault/run", json={}).json()
check("run: a partial name resolves", r["slug"].startswith("creatinine-clearance-by-cockcroft"),
      r["name"])
check("run: asks for values when given none",
      r["status"] == "needs_values" and len(r["needs"]["fields"]) == 4,
      f"{len(r['needs']['fields'])} fields")

r = client.post(
    "/api/calculators/Cockcroft Gault/run",
    json={"Age": 70, "Weight†": 70, "Serum creatinine": 1.0, "Sex": 0.85},
).json()
crcl = next((o for o in r["result"]["outputs"]
             if o["key"] == "estimated_creatinine_clearance"), {})
check("run: computes from labels, not just keys",
      r["status"] == "answered" and near(crcl.get("value"), 57.85) and not r["unknown_keys"],
      f"{crcl.get('value')} mL/min")

r = client.post("/api/calculators/dobutamine/run", json={"Dose": 5, "Weight": 70,
                "Concentration": 1000, "Drug Amount": 250, "Infusate Volume": 250}).json()
rate = next((o for o in r["result"]["outputs"] if o["key"] == "infuse_rate"), {})
check("run: flat body works for an infusion", near(rate.get("value"), 21.0),
      f"{rate.get('value')} mL/hr")

r = client.post("/api/calculators/4T score/run", json={}).json()
sel = {g["key"]: g["options"][0]["key"] for g in r["needs"]["criteria"]}
r = client.post("/api/calculators/4T score/run", json={"selections": sel}).json()
check("run: a score answers from its criteria",
      r["status"] == "answered" and r["result"]["score"]["total"] == 8,
      f"total {r['result']['score']['total']}")

r = client.post("/api/calculators/4T score/run",
                json={"selections": dict(list(sel.items())[:2])}).json()
check("run: an incomplete score says which criteria are missing",
      r["status"] == "needs_values" and len(r["errors"]) == 2,
      "; ".join(e["message"] for e in r["errors"])[:70])

bad = client.post("/api/calculators/not-a-calculator/run", json={})
check("run: an unknown name is a 404", bad.status_code == 404)

# -- every calculator answers something ------------------------------------
stalled = []
for entry in listing["calculators"]:
    slug, renderer = entry["slug"], entry["renderer"]
    spec = client.get(f"/api/calculators/{slug}").json()
    body: dict = {}
    if renderer == "score":
        body["selections"] = {
            g["key"]: (g["options"][0]["key"] if g["options"] else None)
            for g in (spec.get("scoring") or {}).get("groups") or []
        }
    elif renderer == "convert":
        body.update(pair_index=0, value=1)
    elif renderer == "tree":
        body["answers"] = []
    else:
        inputs = {}
        fields = spec.get("inputs") or []
        for i, f in enumerate(fields):
            c = f.get("constraints") or {}
            if f.get("options"):
                inputs[f["key"]] = f["options"][0].get("value")
                continue
            lo, hi = c.get("min"), c.get("max")
            lo = 1.0 if lo is None else float(lo)
            hi = float(hi) if hi is not None else max(lo * 2, lo + 10, 10.0)
            # Spread the fields across their ranges rather than putting every
            # one at its midpoint: a pharmacokinetic model given an identical
            # peak and trough divides by ln(1), which is a degenerate patient
            # rather than a broken calculator.
            frac = 0.35 + 0.3 * (i / max(len(fields) - 1, 1))
            inputs[f["key"]] = lo + (hi - lo) * frac or 1.0
        body["inputs"] = inputs
    res = client.post(f"/api/calculators/{slug}/calculate", json=body).json()
    produced = (
        res.get("outputs") or res.get("score") or res.get("conversion")
        or res.get("tree") or res.get("drugs") or res.get("table")
    )
    if not produced:
        stalled.append(f"{slug} ({renderer})")
# ---- how a calculator is addressed ---------------------------------------
# Three titles in the corpus contain a "/". The browser sends %2F, uvicorn
# decodes it before routing, and a `{slug}` parameter then sees two segments --
# so these three could not be opened at all.
for _title in ("ACC/AHA 2013 Cardiovascular Risk Assessment",
               "Pediatric Dosing: Oral Liquid/Parenteral",
               "TIMI Risk Score (UA/NSTEMI)"):
    _r = client.get(f"/api/calculators/{quote(_title, safe='')}/schema")
    check(f"name with a slash: {_title[:28]}", _r.status_code == 200,
          f"HTTP {_r.status_code}")

# The same calculator by slug, by title and by fragment is the same calculator.
_by = [client.get(f"/api/calculators/{quote(s, safe='')}/schema").json().get("slug")
       for s in ("acc-aha-2013-cardiovascular-risk-assessment",
                 "ACC/AHA 2013 Cardiovascular Risk Assessment")]
check("slug and title resolve to one calculator",
      len(set(_by)) == 1 and _by[0] == "acc-aha-2013-cardiovascular-risk-assessment",
      str(_by))

# `{slug:path}` is greedy, so every fixed suffix has to be declared above the
# bare route. Assert the order rather than trusting the next edit to remember.
_paths = [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/calculators")]
_bare = _paths.index("/api/calculators/{slug:path}")
_suffixed = [i for i, p in enumerate(_paths)
             if p.startswith("/api/calculators/{") and p != "/api/calculators/{slug:path}"]
_literal = [i for i, p in enumerate(_paths) if p == "/api/calculators/names"]
check("the greedy route is declared last",
      all(i < _bare for i in _suffixed + _literal),
      f"bare at {_bare}, others at {sorted(_suffixed + _literal)}")


check("every calculator returns a result", not stalled,
      "; ".join(stalled[:4]) if stalled else f"{PUBLISHED}/{PUBLISHED}")

print()
if failures:
    print(f"  {C['bad']}{len(failures)} failed{C['r']}: " + ", ".join(failures[:8]))
    sys.exit(1)
print(f"  {C['ok']}all checks passed{C['r']}\n")
