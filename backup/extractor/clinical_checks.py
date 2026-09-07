#!/usr/bin/env python3
"""Evaluate a set of specs against published reference values.

Validation proves an expression runs; invariants prove a selector is live.
Neither proves the number is right. These cases come from the source
literature or the calculator's own worked example, so a regression in the
extractor shows up here as a wrong clinical result rather than as nothing at
all -- which is how the concentration-derivation bug (a thousand-fold error in
five infusion calculators) went unnoticed while every other check passed.

Each case is (slug, inputs, {output: expected}), with a tolerance. Add a case
whenever a calculator's arithmetic is verified by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.evaluator import ExpressionError, run_compute

C = {"ok": "\033[92m", "bad": "\033[91m", "dim": "\033[90m",
     "b": "\033[1m", "r": "\033[0m"}


def _ms(y: int, m: int, d: int) -> float:
    return datetime(y, m, d, 12).timestamp() * 1000


# slug, inputs, expected outputs, note
CASES: list[tuple[str, dict, dict, str]] = [
    ("creatinine-clearance-by-cockcroft-gault-age-16-years",
     {"age": 70, "weight": 70, "serum_creatinine": 1.0, "sex": 0.85},
     {"estimated_creatinine_clearance": 57.85},
     "70yo 70kg SCr 1.0, female = 0.85 x male"),
    ("creatinine-clearance-by-cockcroft-gault-age-16-years",
     {"age": 70, "weight": 70, "serum_creatinine": 1.0, "sex": 1.0},
     {"estimated_creatinine_clearance": 68.06},
     "same patient, male"),

    ("glomerular-filtration-rate-by-mdrd",
     {"age": 60, "standardized_serum_creat": 1.1, "sex": 0.742, "race": 1.0},
     {"glomerular_filtration_rate": 50.67},
     "MDRD 175 x SCr^-1.154 x age^-0.203 x 0.742 (female)"),

    ("acc-aha-2013-cardiovascular-risk-assessment",
     {"age": 55, "total_cholesterol": 213, "hdl_cholesterol": 50,
      "systolic_blood_pressure": 120, "smoker": 0, "diabetes": 0,
      "on_hypertension_med": 0, "sex": 1, "race": 1},
     {"ten_year_risk": 5.38},
     "Pooled Cohort worked example, white man (published 5.3%)"),
    ("acc-aha-2013-cardiovascular-risk-assessment",
     {"age": 55, "total_cholesterol": 213, "hdl_cholesterol": 50,
      "systolic_blood_pressure": 120, "smoker": 0, "diabetes": 0,
      "on_hypertension_med": 0, "sex": 0, "race": 1},
     {"ten_year_risk": 2.05},
     "same, white woman (published 2.1%)"),
    ("acc-aha-2013-cardiovascular-risk-assessment",
     {"age": 55, "total_cholesterol": 213, "hdl_cholesterol": 50,
      "systolic_blood_pressure": 120, "smoker": 0, "diabetes": 0,
      "on_hypertension_med": 0, "sex": 1, "race": 0},
     {"ten_year_risk": 6.07},
     "same, African American man (published 6.1%)"),
    ("acc-aha-2013-cardiovascular-risk-assessment",
     {"age": 55, "total_cholesterol": 213, "hdl_cholesterol": 50,
      "systolic_blood_pressure": 120, "smoker": 0, "diabetes": 0,
      "on_hypertension_med": 0, "sex": 0, "race": 0},
     {"ten_year_risk": 3.03},
     "same, African American woman (published 3.0%)"),

    ("cardiovascular-risk-assessment-10-year-framingham-2008",
     {"sex": 0, "age": 61, "total_chol": 180, "hdl_chol": 47, "sys_bp": 124,
      "sys_bp_factor": 2.76157, "cig": 0, "dm": 0},
     {"risk_factors": 0.24},
     "D'Agostino 2008 example, 61yo woman, untreated SBP"),

    ("dobutamine",
     {"dose": 5, "weight": 70, "concentration": 1000,
      "drug_amount": 250, "infusate_volume": 250},
     {"infuse_rate": 21.0},
     "5 mcg/kg/min x 70 kg / 1000 mcg/mL = 21 mL/hr"),
    ("dopamine",
     {"dose": 5, "weight": 70, "concentration": 1600,
      "drug_amount": 400, "infusate_volume": 250},
     {"infuse_rate": 13.13},
     "5 mcg/kg/min x 70 kg / 1600 mcg/mL"),

    ("morphine-milligram-equivalents-per-day-mmed",
     {"oxycodone": 30, "methadone": 30, "fentanyl": 25, "morphine": 10},
     {"total_mmed": 355.0, "methadone_mme": 240.0, "oxycodone_mme": 45.0},
     "CDC conversion factors, methadone in the >20-40 mg band (x8)"),

    ("gestational-age",
     {"crown_rump_length": 40, "biparietal_diameter": 50,
      "head_circumference": 200,
      "current_time": _ms(2026, 9, 1), "lmp_time": _ms(2026, 1, 1),
      "us_time": _ms(2026, 9, 1)},
     {"lmpweeks": 34.71, "bpdweeks": 20.6, "hcweeks": 22.04,
      "crlweeks": 10.5},
     "printed formulas: BPD days = 2xBPD+44.2; CRL and HC as stated"),

    ("cdc-growth-percentiles-36-months",
     {"sex": 1.0, "age": 12.5, "length": 76.0, "weight": 9.6,
      "head_circumference": 46.0},
     {"weight_percentile": 47.44},
     "girls 12.5mo 9.6 kg sits just below the median"),
    ("cdc-growth-percentiles-36-months",
     {"sex": 2.0, "age": 12.5, "length": 76.0, "weight": 9.6,
      "head_circumference": 46.0},
     {"weight_percentile": 21.65},
     "same child scored as a boy - the sex selector must move the result"),

    ("who-assessment-of-malnutrition-boys-2-5-years",
     {"age": 36, "height": 96.1, "weight": 14.3},
     {"z_score_height": 0.0, "z_score_weight_for_height": 0.0},
     "WHO boys 36mo medians: 96.1 cm, 14.3 kg at that height"),
    ("who-assessment-of-malnutrition-girls-0-2-years",
     {"age": 12, "length": 74.0, "weight": 8.9},
     {"z_score_length": 0.0},
     "WHO girls 12mo median length 74.0 cm"),

    ("calvert-formula-carboplatin-dosing",
     {"calvert_auc": 5, "calvert_gfr": 60, "gfr_known": "Yes",
      "max_cr_cl_input": 0},
     {"calvert_dosage": 425.0},
     "Calvert: AUC 5 x (GFR 60 + 25) = 425 mg"),
    ("calvert-formula-carboplatin-dosing",
     {"calvert_auc": 5, "calvert_gfr": 0, "gfr_known": "No",
      "max_cr_cl_input": 0, "gfr_equation": "Cockcroft-Gault",
      "gfr_age": 70, "gfr_weight": 70, "gfr_srcr": 1.0, "gfr_sex": 1},
     {"calvert_gfroutput": 68.06, "calvert_dosage": 465.3},
     "GFR unknown: Cockcroft-Gault 70yo/70kg/SCr 1.0 male = 68.06, x AUC 5"),
    ("calvert-formula-carboplatin-dosing",
     {"calvert_auc": 5, "calvert_gfr": 0, "gfr_known": "No",
      "max_cr_cl_input": 125, "gfr_equation": "Cockcroft-Gault",
      "gfr_age": 30, "gfr_weight": 90, "gfr_srcr": 0.6, "gfr_sex": 1},
     {"calvert_gfroutput": 125.0},
     "the form's maximum GFR caps the estimate (229 -> 125)"),

    ("aminoglycosides-traditional-intermittent-empiric-dosing",
     {"cr_cl_method": "Cockcroft-Gault", "gender": "male", "age": 60,
      "height": 170, "weight": 70, "sr_cr": 1.0, "cdp": 8, "cdtr": 1,
      "vd": 0.25, "drug": "Amikacin", "detail": 0, "cr_cl": 0},
     {"cr_cl": 77.8},
     "Cockcroft-Gault applies 0.85 to WOMEN only: 60yo 70kg SCr 1.0 male"),
    ("aminoglycosides-traditional-intermittent-empiric-dosing",
     {"cr_cl_method": "Cockcroft-Gault", "gender": "female", "age": 60,
      "height": 170, "weight": 70, "sr_cr": 1.0, "cdp": 8, "cdtr": 1,
      "vd": 0.25, "drug": "Amikacin", "detail": 0, "cr_cl": 0},
     {"cr_cl": 66.1},
     "same patient as a woman = 0.85 x the male value"),

    ("treprostinil",
     {"field_transitioning": 0, "field_treprostinil_route": "IV",
      "field_treprostinil_dose": 10, "field_weight": 70,
      "field_treprostinil_strength": 1, "field_treprostinil_vol": 20,
      "field_freq_final_change": 24, "field_freq_change": 24,
      "field_epoprostenol_dose": 0, "field_epoprostenol_conc": 0,
      "units_output_final": "", "units_weight": "",
      "units_epoprostenol_conc": ""},
     {"output_iv_rate": 0.833},
     "hours must be read as hours: 20 mL over 24 h, not over 24 days"),

    ("treprostinil",
     {"field_transitioning": 0, "field_treprostinil_route": "IV",
      "field_treprostinil_dose": 10, "field_weight": 70,
      "field_treprostinil_strength": 1, "field_treprostinil_vol": 20,
      "field_freq_change": 24, "field_epoprostenol_dose": 0,
      "field_epoprostenol_conc": 0, "field_freq_final_change": 24,
      "units_output_final": "", "units_weight": "", "units_epoprostenol_conc": ""},
     {"output_iv_rate": 0.833},
     "reservoir volume / hours between changes"),
]

TOL = 0.02        # relative


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=Path(__file__).parent / "dist")
    args = ap.parse_args()

    passed = failed = skipped = 0
    print(f"\n{C['b']}══ Clinical reference checks ══{C['r']}")
    for slug, inputs, expected, note in CASES:
        path = args.dist / "calculators" / f"{slug}.json"
        if not path.exists():
            print(f"  {C['dim']}? {slug}: not built{C['r']}")
            skipped += 1
            continue
        spec = json.loads(path.read_text())
        tables = spec.get("tables") or {}
        vec = {i["key"]: 0.0 for i in (spec.get("inputs") or [])}
        vec.update(inputs)
        try:
            got = run_compute(spec["compute"], vec,
                              tables.get("lookups") or [],
                              tables.get("variants") or {})
        except ExpressionError as exc:
            print(f"  {C['bad']}✗{C['r']} {slug[:44]:<44} {exc}")
            failed += 1
            continue
        bad = []
        for key, want in expected.items():
            have = got.get(key)
            if have is None:
                bad.append(f"{key}: absent")
            elif abs(have - want) > max(abs(want) * TOL, 0.02):
                bad.append(f"{key}: {have:.4g} != {want:.4g}")
        if bad:
            print(f"  {C['bad']}✗{C['r']} {slug[:44]:<44} {'; '.join(bad)}")
            print(f"    {C['dim']}{note}{C['r']}")
            failed += 1
        else:
            print(f"  {C['ok']}✓{C['r']} {slug[:44]:<44} {C['dim']}{note}{C['r']}")
            passed += 1

    print(f"\n  {C['ok']}{passed} passed{C['r']}  "
          f"{C['bad'] if failed else C['dim']}{failed} failed{C['r']}  "
          f"{C['dim']}{skipped} skipped{C['r']}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
