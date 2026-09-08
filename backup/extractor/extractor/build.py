"""Build the final canonical spec for one calculator, from PDF + mockup HTML.

Routes each PDF to the parser its engine needs, then merges everything the
mockup knows that the print view cannot carry (unit option lists, category,
LMS reference tables). Both sources are recorded in `provenance`, and any
disagreement is captured in `provenance.conflicts` rather than silently
resolved -- for clinical content, a conflict is information, not noise.

Authority, per field:
  formulas, bounds, defaults, references, notes  -> PDF   (vendor's own code)
  unit option lists + conversion factors         -> HTML  (print collapses them)
  category, subtitle                             -> HTML  (editorial)
  LMS reference tables                           -> HTML  (too large to print)
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Any, Optional

from . import engine_b, engine_radio, engine_score, enrich, units as unitreg
from .identity import squash
from . import jsblock
from .jsparse import js_name_to_label, rename_vars, to_snake


def camel_to_snake(name: str) -> str:
    """dailyDosage -> daily_dosage.

    Engine B's locals are camelCase JS identifiers, unlike EBMcalc's which are
    already underscore-separated, so they need the split that `to_snake`
    deliberately does not perform.
    """
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"__+", "_", s).lower().strip("_")

SCHEMA_VERSION = "1.0"


def prune_dead_steps(compute: dict, inputs: list[dict], table_cols: set[str]) -> list[str]:
    """Drop steps that cannot resolve and that no output actually needs.

    Recovering conditional assignments is a net win overall, but a minority
    reference something the spec cannot supply (a DOM flag, a value computed in
    a helper this parser does not read). Left in, one such step aborts the whole
    calculation even when every output is independently computable.

    A step is dead if it is unreachable from the inputs AND no output depends on
    it, transitively. Removing it cannot change any result -- it can only turn a
    spec that failed outright into one that computes what it legitimately can.
    """
    from .evaluator import ExpressionError, free_names

    steps = compute.get("steps") or []
    outputs = compute.get("outputs") or []
    if not steps:
        return []

    def names(expr):
        try:
            return free_names(expr or "")
        except ExpressionError:
            return set()

    available = {i["key"] for i in inputs} | set(table_cols)
    step_by_key = {s["key"]: s for s in steps}

    # forward pass: what can actually be reached from the inputs
    resolvable = set(available)
    changed = True
    while changed:
        changed = False
        for st in steps:
            if st["key"] in resolvable:
                continue
            if names(st.get("expr")) - resolvable - {st["key"]} == set():
                resolvable.add(st["key"])
                changed = True

    # backward pass: what the outputs transitively require
    needed: set[str] = set()
    frontier = set()
    for o in outputs:
        frontier |= names(o.get("expr"))
    while frontier:
        n = frontier.pop()
        if n in needed:
            continue
        needed.add(n)
        if n in step_by_key:
            frontier |= names(step_by_key[n].get("expr"))

    from .evaluator import compile_expr
    dead = []
    for st in steps:
        if st["key"] in needed:
            continue
        if st["key"] not in resolvable:
            dead.append(st["key"])
            continue
        try:
            compile_expr(st.get("expr") or "0")
        except ExpressionError:
            dead.append(st["key"])
    if dead:
        compute["steps"] = [st for st in steps if st["key"] not in dead]
    return dead


GLUED = re.compile(r"^(.*?)\s+([A-Za-z_]\w*)\s*=(?!=)\s*(.+)$", re.S)


def _repair_glued_expressions(compute: dict) -> None:
    """Split an expression that is really two statements run together."""
    from .evaluator import ExpressionError, compile_expr

    def ok(e: str) -> bool:
        try:
            compile_expr(e)
            return True
        except ExpressionError:
            return False

    for coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for it in coll:
            e = it.get("expr")
            if not e or ok(e):
                continue
            m = GLUED.match(e)
            if not m:
                continue
            head, name, tail = m.group(1), m.group(2), m.group(3)
            # Prefer the later statement: it is the refinement the vendor
            # applied last, and the earlier half usually survives as its operand.
            if name == it["key"] and ok(tail):
                it["expr"] = tail
            elif ok(head):
                it["expr"] = head


NUM_DECL = re.compile(
    r"(?:var|let|const)\s+([A-Za-z_]\w*)\s*=\s*(-?\d+(?:\.\d+)?)\s*[;,]")
STR_DECL = re.compile(
    r"(?:var|let|const)\s+([A-Za-z_]\w*)\s*=\s*(['\"])([^'\"]{1,40})\2\s*[;,]")


def _resolve_module_constants(compute: dict, inputs: list, tables: dict, js: str,
                              _depth: int = 0) -> None:
    """Supply values for identifiers the expressions reference but nothing defines."""
    if _depth > 5:
        return
    from .evaluator import ExpressionError, free_names

    known = {i["key"] for i in inputs}
    known |= {s["key"] for s in (compute.get("steps") or [])}
    known |= {o["key"] for o in (compute.get("outputs") or [])}
    # Names differing only by case are the SAME thing here. Without this,
    # a recovered constant `Distribution_Factor = 0.35` shadows the select
    # `distribution_factor` and silently pins it to one option.
    protected = {squash(k) for k in known}
    protected |= {squash(i.get("label") or "") for i in inputs}
    protected.discard("")
    for t in (tables.get("lookups") or []):
        known |= set(t.get("columns") or [])
    for spec_v in (tables.get("variants") or {}).values():
        known |= set(spec_v.get("variables") or [])

    def _missing(kn: set) -> set:
        out: set[str] = set()
        for coll in (compute.get("steps") or [], compute.get("outputs") or []):
            for it in coll:
                if not it.get("expr"):
                    continue
                try:
                    out |= free_names(it["expr"]) - kn
                except ExpressionError:
                    continue
        return out

    missing = _missing(known)
    if not missing:
        return

    nums = {k: float(v) for k, v in NUM_DECL.findall(js or "")}
    strs = {k: v for k, _q, v in STR_DECL.findall(js or "")}
    lower_n = {k.lower(): v for k, v in nums.items()}
    lower_s = {k.lower(): v for k, v in strs.items()}

    # Anything not a plain constant may still be a computed local the executor
    # did not reach -- assigned in a helper, or in a branch it declined to
    # translate. Adopt the last assignment the module makes to that name.
    from .jsparse import js_expr_to_canonical, strip_embedded_assignments
    expr_decls: dict[str, str] = {}
    for m in re.finditer(
        r"(?:var|let|const)?\s*\b([A-Za-z_]\w*)\s*=\s*([^;{}]{1,200}?)\s*;", js or ""
    ):
        name, rhs = m.group(1), m.group(2).strip()
        if not rhs or rhs.startswith(("new ", "function", "document")):
            continue
        expr_decls[name.lower()] = rhs

    added: list[dict] = []
    for name in sorted(missing):
        if squash(name) in protected:
            continue                      # an input already owns this name
        low = name.lower()
        if low in lower_n:
            added.append({"key": name, "expr": repr(lower_n[low]),
                          "note": "module constant"})
        elif low in lower_s:
            added.append({"key": name, "expr": f"'{lower_s[low]}'",
                          "note": "module constant"})
        elif low in expr_decls:
            cand = strip_embedded_assignments(
                js_expr_to_canonical(expr_decls[low]))
            # Only adopt it if it is self-contained; a partial recovery that
            # drags in more unknowns is worse than the gap it fills.
            try:
                # Names are compared case- and underscore-insensitively: the
                # module writes `bpdindays = (2 * Biparietal_Diameter) + 44.2`
                # while the input key is `biparietal_diameter`, and an exact
                # match rejected the recovery that Gestational Age depends on.
                # `normalise_expression_names` rewrites the spelling afterwards.
                _kn = {squash(k) for k in known} | {squash(name)}
                if {n for n in free_names(cand) if squash(n) not in _kn}:
                    continue
            except ExpressionError:
                continue
            added.append({"key": name, "expr": cand,
                          "note": "recovered from a module assignment"})
    if added:
        compute.setdefault("steps", [])[:0] = added
        # A recovered value can make the next one resolvable, so repeat until
        # nothing further can be supplied.
        known |= {a["key"] for a in added}
        if _missing(known):
            _resolve_module_constants(compute, inputs, tables, js, _depth + 1)


def normalise_expression_names(compute: dict, inputs: list[dict]) -> list[str]:
    """Rewrite any identifier an expression still carries in its original JS form.

    The per-engine parsers build a rename map from the names they recognise, but
    a calculator that reads a field in an unusual way (radio groups in APACHE II,
    for instance) leaves identifiers like `Aa_Gradient_or_PO2` or `Creat` behind.
    Those resolve to a known key once case and underscores are folded, so this
    pass catches them generically instead of per-calculator.

    Returns the names that still could not be resolved.
    """
    from .evaluator import ExpressionError, free_names

    known: dict[str, str] = {}
    for i in inputs:
        known[squash(i["key"])] = i["key"]
    for st in compute.get("steps") or []:
        known[squash(st["key"])] = st["key"]
    for o in compute.get("outputs") or []:
        known[squash(o["key"])] = o["key"]

    unresolved: set[str] = set()

    def fix(expr: Optional[str]) -> Optional[str]:
        if not expr:
            return expr
        try:
            names = free_names(expr)
        except ExpressionError:
            return expr
        mapping = {}
        for n in names:
            if squash(n) in known and known[squash(n)] != n:
                mapping[n] = known[squash(n)]
            elif squash(n) not in known:
                unresolved.add(n)
        return rename_vars(expr, mapping) if mapping else expr

    for st in compute.get("steps") or []:
        st["expr"] = fix(st.get("expr"))
    for o in compute.get("outputs") or []:
        o["expr"] = fix(o.get("expr"))
    return sorted(unresolved)


# --------------------------------------------------------------------------
# engine detection
# --------------------------------------------------------------------------

def detect_engine(sections: dict[str, str]) -> str:
    js = sections.get("Scripts", "") or ""
    # Order matters: a converter also defines fixDP/eTo helpers, and a TreeCalc
    # also ships radio helpers, so the most specific marker is tested first.
    # Titration / infusion tables also mention unitConvert helpers; prefer the
    # calculate()+dose-ladder shape so they are not misfiled as converters.
    if re.search(r"function\s+calculate\s*\(", js) and (
        "TABLE_VALUES" in js or "ID_RATE" in js or "setNumber" in js
    ):
        return "wk"
    if "unitConvert" in js or re.search(r"\bfrom_unit\b.*\bto_unit\b", js, re.S):
        return "convert"                    # engine E
    # Metric Conversions drives four dimensions at once by calling the shared
    # library's converters directly, so it carries none of the usual markers and
    # was filed as unknown -- a converter with nothing to convert. The marker
    # has to be the file's PURPOSE, though: the neonatal resuscitation tables
    # also convert a weight in passing, and reading that as a converter left it
    # with no drug table and nothing to convert either.
    if re.search(r"\bconvert(?:Length|Temperature|Volume|Weight)\s*\(", js) and not (
        len(js) > 20000 or "TABLE_VALUES" in js
    ):
        return "convert"
    # A crash cart is a drug table whatever else the file defines. Both Rapid
    # Sequence Intubation exports also declare an `_fx` entry point, so the
    # EBMcalc rule claimed them and they came out with no inputs, no outputs and
    # nothing to show -- while their whole content sat in the drug model.
    if "createCrashCart" in js and "DRUG_CONCENTRATIONS" in js:
        return "dose_table"                 # engine D
    if re.search(r"function\s+\w+_fx\s*\(", js):
        return "ebmcalc"                    # engine A
    if re.search(r"function\s+calculate\s*\(", js):
        return "wk"                         # engine B
    if "TreeCalc" in (sections.get("_preamble", "") or "") or "treecalc" in js.lower():
        return "tree"                       # engine F
    if len(js) > 20000:
        return "dose_table"                 # engine D (ALS: one fn per drug)
    if "togCB" in js or "setRB" in js:
        return "score"                      # engine C
    return "unknown"


TITRATION = "titration_table"

RENDERER_FOR_ENGINE = {
    "ebmcalc": "formula",
    "wk": "formula",
    "score": "score",
    "convert": "convert",
    "tree": "tree",
    "dose_table": "dose_table",
    "unknown": "unknown",
}

# Calculators whose result is a table interpolation, not a closed-form formula.
LMS_TITLE = re.compile(
    r"\b(CDC|WHO)\b.*(?:\b(?:for Age|for Length|for Height|Percentiles)\b"
    r"|Assessment of Malnutrition)", re.I)


# --------------------------------------------------------------------------
# unit registry extraction from the mockup
# --------------------------------------------------------------------------

DIMENSION_BY_SUFFIX = {
    "PRESSURE": "pressure", "TEMP": "temperature", "AGE": "time_age",
    "WEIGHT": "mass", "HEIGHT": "length", "LENGTH": "length",
    "ELEV": "length", "VOLUME": "volume", "TIME": "time",
    "DOSE": "mass", "SCR": "creatinine", "CHOL": "cholesterol",
    "BP": "pressure", "FIO2": "fraction", "RQ": "ratio", "PCT": "fraction",
    "HC": "length", "MASS": "mass", "VOL": "volume",
}


def units_from_constants(consts: dict) -> dict[str, dict]:
    """Recover `{unit_list_name: {units:[...], factors:{...}}}` from mockup globals.

    The mockup stores each dropdown as a parallel pair: `PDF2_AGE_UNITS` (order)
    and `PDF2_AGE_TO_YR` (factors). Pairing them by stem recovers a usable unit
    definition; the affine offset is 0 for every scale-only map, and temperature
    is handled separately because it needs one.
    """
    lists = {k: v for k, v in (consts or {}).items()
             if k.endswith("_UNITS") and isinstance(v, list)}
    factor_maps = {k: v for k, v in (consts or {}).items()
                   if re.search(r"_TO_[A-Z0-9_]+$", k) and isinstance(v, dict)}

    out: dict[str, dict] = {}
    for lname, options in lists.items():
        stem = lname[: -len("_UNITS")]
        fmap = None
        for fname, fv in factor_maps.items():
            if fname.startswith(stem + "_TO_"):
                fmap = fv
                break
        if fmap is None:                    # try a looser stem match
            tail = stem.split("_")[-1]
            for fname, fv in factor_maps.items():
                if f"_{tail}_TO_" in fname or fname.startswith(tail + "_TO_"):
                    fmap = fv
                    break
        units = []
        for code in options:
            factor = (fmap or {}).get(code)
            units.append({
                "code": code,
                "label": code,
                "factor": factor,
                "offset": 0.0 if factor is not None else None,
                "resolved": factor is not None,
            })
        base = None
        if fmap:
            base = next((c for c, f in fmap.items() if f == 1), None)
        out[stem] = {
            "source_list": lname,
            "dimension": DIMENSION_BY_SUFFIX.get(stem.split("_")[-1]),
            "base_unit": base,
            "units": units,
        }
    return out


def pick_units_for_field(
    field_key: str, base_unit: Optional[str], registry: dict[str, dict]
) -> Optional[dict]:
    """Choose the mockup unit list whose options actually contain this base unit."""
    if not registry:
        return None
    if base_unit:
        for stem, entry in registry.items():
            codes = {squash(u["code"]) for u in entry["units"]}
            if squash(base_unit) in codes:
                return entry
    k = squash(field_key)
    for stem, entry in registry.items():
        s = squash(stem.split("_")[-1])
        if s and (s in k or k in s) and min(len(s), len(k)) >= 3:
            return entry
    return None


# --------------------------------------------------------------------------
# builders per engine
# --------------------------------------------------------------------------

def build_inputs_wk(parsed: engine_b.EngineBScript, printed: dict) -> list[dict]:
    inputs = []
    others = {squash(engine_b.id_to_key(i, parsed.id_map.get(i)))
              for i in parsed.inputs}
    for idc in parsed.inputs:
        dom = parsed.id_map.get(idc)
        key = engine_b.id_to_key(idc, dom)
        b = engine_b.match_bounds(key, parsed.bounds)
        rb = parsed.range_bounds.get(idc) or {}
        label = _label_for(key, printed, others - {squash(key)})
        # The alert text names the field the way the document does; the key is
        # only what the variable was called.
        if rb.get("label") and squash(label) == squash(key):
            label = rb["label"]
        constraints = {k: v for k, v in b.items() if k in ("min", "max")}
        for k in ("min", "max"):
            if k not in constraints and k in rb:
                constraints[k] = rb[k]
                # `if (weight <= 0)` rejects zero itself.
                if rb.get(f"exclusive_{k}"):
                    constraints[f"exclusive_{k}"] = True
        inputs.append({
            "key": key,
            "label": label,
            "widget": "quantity",
            "required": True,
            "default": (printed.get(label) or {}).get("default"),
            "base_unit": b.get("unit") or (printed.get(label) or {}).get("unit"),
            "units": None,
            "constraints": constraints,
            "messages": None,
            "source": {"engine": "wk", "dom_id": dom, "id_const": idc},
        })
    return inputs


# Clinical initialisms a title-cased key mangles: `calvert_gfr` is "GFR", not
# "Gfr". Only forms that are never an English word in their own right.
_ACRONYMS = {
    "gfr", "auc", "bmi", "bsa", "ibw", "abw", "crcl", "egfr", "ldl", "hdl",
    "inr", "ast", "alt", "bun", "scr", "srcr", "cr", "ml", "mg", "kg", "iv",
    "clcr", "auc", "gfr", "tl", "loi",
    "po", "bp", "hr", "qtc", "ecg", "ekg", "cbc", "wbc", "rbc", "ldh", "cl",
    "na", "id", "mdrd", "ckd", "epi", "mme", "mmed", "tbw", "lbw", "nyha",
}
_LABEL_NOISE = {"input", "field", "value", "val", "list"}
_JOINERS = {"of", "per", "in", "to", "and", "or", "by", "for", "at", "the"}
# Initialisms clinicians write in mixed case, not in capitals.
_ACRONYM_SPELLING = {"crcl": "CrCl", "clcr": "ClCr", "egfr": "eGFR",
                     "scr": "SCr", "srcr": "SrCr", "qtc": "QTc",
                     "ml": "mL", "dl": "dL", "mg": "mg", "kg": "kg"}


def _label_from_key(key: str) -> str:
    """Title-case a key, keeping initialisms upper and dropping plumbing words.

    `max_cr_cl_input` read as "Max Cr Cl Input" -- three words of plumbing and
    an initialism split in half. `bolus_dose_output_inputm_l` read as "Bolus
    Dose Output Inputm L", where the camel-case split cut "InputmL" in the
    middle of the unit; that is "Bolus Dose (mL)".
    """
    raw = []
    for w in key.split("_"):
        m = re.fullmatch(r"(input|output)([a-z]{1,3})", w or "", re.I)
        if m:
            raw.extend([m.group(1), m.group(2)])
            continue
        # `calvert_gfroutput` -- the camel-case split missed the seam because
        # the element id ran the two words together in lower case.
        m = re.fullmatch(r"([a-z]{2,})(input|output|value)", w or "", re.I)
        raw.extend([m.group(1), m.group(2)] if m else [w])
    words = [w for w in raw if w and w.lower() not in _LABEL_NOISE]
    # "Output Dose" is what the form calls the answer, so a leading "output" is
    # part of the name. One in the MIDDLE is plumbing -- `bolus_dose_output_ml`
    # is the bolus dose in mL.
    words = [w for n, w in enumerate(words)
             if n == 0 or w.lower() not in ("output", "out")]
    # The unit arrives one letter per token ("m", "l"); it is one word.
    tail = []
    while len(words) > 1 and len(words[-1]) == 1:
        tail.insert(0, words.pop())
    if tail and "".join(tail).lower() in {u.lower() for u in _LIMIT_UNITS.values()} | {
            k.lower() for k in _LIMIT_UNITS}:
        unit = next(v for k, v in _LIMIT_UNITS.items()
                    if k.lower() == "".join(tail).lower()
                    or v.lower() == "".join(tail).lower())
        return f"{_label_from_key('_'.join(words))} ({unit})" if words else unit
    words += tail
    # A trailing unit is the unit, not another word in the name.
    if len(words) > 1:
        last = words[-1].lower()
        unit = next((v for k, v in _LIMIT_UNITS.items() if k.lower() == last), None)
        if unit:
            return f"{_label_from_key('_'.join(words[:-1]))} ({unit})"
    if not words:
        words = key.split("_")
    out, i = [], 0
    while i < len(words):
        w = words[i].lower()
        # `cr_cl` is one initialism the key spelled in two pieces.
        if i + 1 < len(words) and (w + words[i + 1].lower()) in _ACRONYMS:
            joined = w + words[i + 1].lower()
            out.append(_ACRONYM_SPELLING.get(joined, joined.upper()))
            i += 2
            continue
        if w in _ACRONYMS:
            out.append(_ACRONYM_SPELLING.get(w, w.upper()))
        elif out and w in _JOINERS:
            out.append(w)               # "Timing of Level", not "Timing Of Level"
        else:
            out.append(words[i].title())
        i += 1
    return " ".join(out)


# A constant that names a unit is saying which quantity it limits, not adding a
# word to its name: `BOLUS_DOSE_ML_MIN` is a floor on the bolus dose in mL.
# Only tokens that can mean nothing else. `MIN` is "minimum" as often as it is
# "minutes" -- read as a unit it renamed ethanol's "Initial Dose (min)" and
# shifted every result label after it by one.
_LIMIT_UNITS = {"ML": "mL", "L": "L", "MG": "mg", "MCG": "mcg",
                "KG": "kg", "LBS": "lb"}


def _limit_name(stem: str, inputs: list[dict] | None = None) -> str:
    """Name a stated limit the way the script means it.

    `DOSE_WARN` is not a field called "Dose Warn": it is the usual range for the
    dose, which the calculator warns about rather than refuses. And `TL` is not
    a name at all -- it is the abbreviation the same script uses for the field
    it already calls "Timing of Level".
    """
    words = stem.split("_")
    advisory = "WARN" in words or "USUAL" in words
    words = [w for w in words if w not in ("WARN", "USUAL", "RANGE")]

    unit = _LIMIT_UNITS.get(words[-1]) if len(words) > 1 else None
    if unit:
        words = words[:-1]

    base = None
    for i in inputs or []:
        idc = ((i.get("source") or {}).get("id_const") or "")
        if idc and re.sub(r"^ID_", "", idc) == "_".join(words):
            base = i.get("label")
            break
    if not base or squash(base) == squash("_".join(words)):
        base = _label_from_key("_".join(words).lower()) or stem.title()
    if unit:
        base = f"{base} ({unit})"
    return f"{base} (usual range)" if advisory else base


# `k is as follows:` then, line by line, the case and the value it takes.
_LADDER_HEAD = re.compile(r"^\s*([A-Za-z][\w ]{0,24}?)\s+is\s+as\s+follows\s*:\s*$",
                          re.I | re.M)
_SEX_WORD = re.compile(r"\b(male|female|men|women|boys?|girls?)s?\b", re.I)


def coefficient_ladders(equation: str) -> dict[str, list[dict]]:
    """Cases a printed formula enumerates for one of its own coefficients.

    Valganciclovir asks for the Schwartz `k` through two age dropdowns -- one per
    sex -- and the PDF prints neither list. It does print the whole ladder in the
    formula section:

        k is as follows:
        Patients <1 year AND Birth weight = Low birth weight for gestational age
        k = 0.33
        ...
        Males >=13 to <=16 years
        k = 0.7

    Without it the form asked the clinician to type the coefficient itself.
    Returns `{coefficient: [{"label": case, "value": number}, ...]}`.
    """
    out: dict[str, list[dict]] = {}
    for head in _LADDER_HEAD.finditer(equation or ""):
        name = head.group(1).strip()
        assign = re.compile(r"^\s*" + re.escape(name) + r"\s*=\s*(-?\d*\.?\d+)\s*$",
                            re.I | re.M)
        lines = (equation or "")[head.end():].split("\n")
        rows, case = [], []
        for line in lines:
            m = assign.match(line)
            if m:
                label = " ".join(l.strip() for l in case if l.strip())
                if label:
                    rows.append({"label": label, "value": float(m.group(1))})
                case = []
                continue
            if not line.strip():
                continue
            # A new "X = ..." line that is not this coefficient ends the ladder.
            if re.match(r"^\s*[A-Za-z][\w ()]{0,30}\s*=\s*", line) and not case:
                if rows:
                    break
            case.append(line)
            if len(case) > 3:
                case = case[-3:]
        if rows:
            out[name] = rows
    return out


def _apply_coefficient_ladders(inputs: list[dict], compute: dict,
                               equation: str) -> int:
    """Turn a coefficient the user was asked to type into the list of cases."""
    ladders = coefficient_ladders(equation)
    if not ladders:
        return 0
    read_directly = {
        st["key"]: st.get("expr") or "" for st in (compute.get("steps") or [])
    }
    changed = 0
    for name, rows in ladders.items():
        n = name.lower()
        for inp in inputs:
            if inp.get("options") or inp.get("widget") == "select":
                continue
            parts = [w for w in inp["key"].lower().split("_") if w]
            if n not in parts:
                continue
            # The step that consumes it must actually be a step, not a stray
            # field that happens to share a letter with the coefficient.
            if not any(re.search(r"(?<![\w.])" + re.escape(inp["key"]) + r"(?![\w])", e)
                       for e in read_directly.values()):
                continue
            # A field gated to one sex offers only the cases that mention that
            # sex, plus every case that mentions neither.
            gate = (inp.get("visible_when") or {}).get("equals") or []
            want = _SEX_WORD.search(" ".join(str(g) for g in gate)) if gate else None
            mine = []
            for r in rows:
                said = _SEX_WORD.search(r["label"])
                if want and said and not _same_sex(said.group(1), want.group(1)):
                    continue
                mine.append(r)
            if not mine:
                continue
            seen, opts = set(), []
            for r in mine:
                if (r["label"], r["value"]) in seen:
                    continue
                seen.add((r["label"], r["value"]))
                opts.append({"label": r["label"], "value": r["value"]})
            inp["widget"] = "select"
            inp["options"] = opts
            inp["options_source"] = "printed formula"
            inp["constraints"] = {k: v for k, v in (inp.get("constraints") or {}).items()
                                  if k not in ("min", "max")}
            if squash(inp.get("label") or "") == squash(inp["key"]) or \
                    re.fullmatch(r"(?i)id [a-z ]*", inp.get("label") or ""):
                sex = want.group(1).lower() if want else None
                # A one-letter coefficient names nothing a clinician recognises;
                # what the dropdown actually asks for is the age band.
                stem = "Age band" if len(name) <= 2 else f"{name} by age"
                inp["label"] = f"{stem} ({sex})" if sex else stem
            changed += 1
    return changed


_SEX_SYNONYMS = {"male": "m", "men": "m", "boy": "m", "boys": "m",
                 "female": "f", "women": "f", "girl": "f", "girls": "f"}


def _same_sex(a: str, b: str) -> bool:
    return (_SEX_SYNONYMS.get(a.lower().rstrip("s"), a.lower()[:1])
            == _SEX_SYNONYMS.get(b.lower().rstrip("s"), b.lower()[:1]))


_CASE_PHRASE = ("(?:When|If|and)\\s+{label}\\s+is\\s+"
                "([^:\\n]+?)(?=\\s+and\\s+\\w|\\s*[:\\n]|$)")


def options_from_printed_cases(inputs: list[dict], equation: str) -> int:
    """Recover a dropdown's other choices from the formula's own case list.

    Ethanol's script only ever tests `route == ORAL`, so the option list came
    out with one entry and the field was rendered as a number to type. The
    printed formula enumerates the cases itself:

        When Route is Oral or IV and Output Units is mg; mg/hr:
        ...
        When Route is Oral and Output Units is mL; mL/hr:

    Adopting that list is only safe when the value the code DOES test appears in
    it -- that is what ties the prose to this field rather than to a phrase that
    merely repeats its name.
    """
    if not (equation or "").strip():
        return 0
    changed = 0
    for inp in inputs:
        known = [str(o.get("value")) for o in (inp.get("options") or [])]
        if inp.get("options_known_value"):
            known.append(str(inp["options_known_value"]))
        if not known or len(set(known)) > 2:
            continue
        label = (inp.get("label") or "").strip()
        if len(label) < 3:
            continue
        pat = re.compile(_CASE_PHRASE.format(label=re.escape(label)), re.I)
        found: list[tuple[str, str]] = []
        for m in pat.finditer(equation):
            for alt in re.split(r"\s+or\s+|,\s*", m.group(1)):
                alt = alt.strip(" .\u2022").strip('"\u201c\u201d\'')
                # A choice is a name, not a clause: "GFR is calculated as
                # described" is the sentence continuing past the comma.
                if (not alt or len(alt) > 40 or len(alt.split()) > 3
                        or re.search(r"\b(is|are|was|be|the)\b", alt, re.I)):
                    continue
                value = re.split(r"\s*[;(]", alt, 1)[0].strip().strip('"\'')
                if value and (value, alt) not in found:
                    found.append((value, alt))
        if len(found) < 2:
            continue
        vals = {squash(v) for v, _ in found}
        if not any(squash(k) in vals for k in known):
            continue                    # the prose is about something else
        inp["widget"] = "select"
        inp["options"] = [{"label": lab, "value": val} for val, lab in found]
        inp["options_source"] = "printed formula cases"
        inp.pop("options_incomplete", None)
        inp.pop("options_known_value", None)
        inp["constraints"] = {}
        inp["units"] = None
        inp["base_unit"] = None
        changed += 1
    return changed


# Words that only appear in a sentence, never in a unit.
_NOT_A_UNIT = re.compile(
    r"\b(after|before|from|when|using|select|pull|down|patient|patients|must|"
    r"value|start|end|the|of|is|are|or|and|suggested|available|avail|nearest)\b",
    re.I,
)
# `Heart rate (BPM)`, `Avail. Conc. (mL)` -- a label with the unit in brackets.
# The bracketed part must contain a letter: APACHE prints "70-109 mmHg (0)",
# where the (0) is the criterion's point value, not the unit.
_UNIT_IN_BRACKETS = re.compile(
    r"^[^()]{2,40}\((?=[^)]*[A-Za-zµ%°])([A-Za-z%µ/·°.\d]{1,12})\)\s*$")
# The leading token of a phrase that begins with a real unit: "hrs after the
# start of infusion" is `hrs`, and the rest is guidance.
_UNIT_HEAD = re.compile(r"^([A-Za-zµ%°]{1,12}\d?(?:\s*/\s*[A-Za-z0-9µ%°.]{1,12}){0,3})\b")


def tidy_units(inputs: list[dict], outputs: list[dict] | None = None) -> int:
    """Keep a unit that is a unit; move a sentence to where a sentence belongs.

    The bounds and the printed form both hand back trailing text, and some of it
    is prose: the aminoglycoside interval adjustment carried a unit reading
    "hrs after the start of infusion", which the form then clipped mid-word in
    the little box beside the number. The unit is `hrs`. The rest is guidance,
    so it goes to the field's help rather than being thrown away.

    Anything that is really a label -- an output's name, an option, "Pull down
    to select (0)" -- is not a unit at all and is dropped.
    """
    labels = {squash(i.get("label") or "") for i in inputs}
    labels |= {squash(o.get("label") or "") for o in (outputs or [])}
    labels |= {squash(o.get("label") or "")
               for i in inputs for o in (i.get("options") or [])}
    labels.discard("")
    fixed = 0

    def clean(raw: str) -> tuple[str | None, str | None]:
        """Return (unit, guidance-to-keep)."""
        text = (raw or "").strip().rstrip(":.")
        if not text:
            return None, None
        if len(text) <= 12 and not _NOT_A_UNIT.search(text) and squash(text) not in labels:
            return text, None                      # already a unit
        if squash(text) in labels:
            return None, None                      # a label, not a unit
        m = _UNIT_IN_BRACKETS.match(text)
        if m:
            return m.group(1), None                # "Heart rate (BPM)" -> BPM
        if text[:1].isdigit() or text[:1] in "≤≥<>":
            return None, None                      # "70-109 mmHg (0)" is a range
        # "g, mcg, mEq, mg, mmol, or units" is the list of units this field
        # ACCEPTS, not the unit it is in. Taking its first token said grams.
        if re.search(r",\s*\S", text):
            return None, text
        if _NOT_A_UNIT.search(text):
            head = _UNIT_HEAD.match(text)
            # A unit is a symbol or a short word -- hrs, mL, mcg, mmHg, minute.
            # An English word longer than that is prose that happened to come
            # first: "Nonoperative patients" is not a unit called Nonoperative.
            if (head and not _NOT_A_UNIT.match(head.group(1))
                    and not (head.group(1).isalpha() and len(head.group(1)) > 6)):
                return head.group(1), text         # "hrs after ..." -> hrs + note
            return None, text
        return (text, None) if len(text) <= 20 else (None, text)

    for inp in inputs:
        for slot in ("base_unit", "display_unit"):
            raw = inp.get(slot)
            if not isinstance(raw, str) or not raw.strip():
                continue
            unit, note = clean(raw)
            if unit == raw.strip().rstrip(":."):
                continue
            fixed += 1
            inp[slot] = unit
            if note:
                # Nothing the document said is lost -- it moves to the guidance
                # the field already shows on request.
                help_ = inp.setdefault("help", []) or []
                # Never re-case it: `G` and `g` are different prefixes, and this
                # text is full of unit symbols.
                if not any(note.lower() in (h or "").lower() for h in help_):
                    help_.append(note)
                inp["help"] = help_
        # A unit list is only usable if its codes are units too.
        if inp.get("units"):
            keep = [u for u in inp["units"] if clean(str(u.get("code") or ""))[0]]
            if len(keep) != len(inp["units"]):
                inp["units"] = keep or None
                fixed += 1
    return fixed


def _label_for(key: str, printed: dict, others: set[str] = frozenset()) -> str:
    """Match a snake key to a printed form label, else title-case the key.

    The partial rule has to be the LONGEST overlap, and it has to leave alone a
    line that names some other field outright. The interval-adjust form prints
    "Drug" and "Drug Conc." on consecutive rows; taking the first line that was
    merely contained in `drug_conc` named the concentration field "Drug" -- the
    same name the drug dropdown already had, on the same form.
    """
    k = squash(key)
    for label in printed or {}:
        if squash(label) == k:
            return label
    best = ""
    for label in printed or {}:
        s = squash(label)
        if not s or s in others:
            continue                    # that line is another field's own name
        if (s in k or k in s) and min(len(s), len(k)) >= 4 and len(s) > len(best):
            best = s
            hit = label
    if best:
        return hit
    return _label_from_key(key)


def build_compute_wk(parsed: engine_b.EngineBScript, inputs: list[dict]) -> dict:
    """Assemble engine-B compute; symbolic execution already made keys canonical."""
    steps = [{"key": n, "expr": e} for n, e in parsed.steps]

    outputs = []
    for o in parsed.outputs:
        key = o["key"]
        expr = o["expr"]
        # `setNumber(ID_RATE, rate, 1)` -- the output IS the step named `rate`;
        # keep the step and let the output reference it rather than duplicating.
        outputs.append({
            "key": key,
            "label": _label_from_key(key),
            "expr": expr,
            "decimals": o["decimals"],
            "base_unit": None,
            "kind": "text" if key in parsed.text_outputs else "number",
        })

    # Drop steps nothing reads, so the spec carries only what it computes with.
    referenced: set[str] = set()
    for o in outputs:
        referenced |= free_identifiers(o["expr"])
    changed = True
    while changed:
        changed = False
        for s in steps:
            if s["key"] in referenced:
                before = len(referenced)
                referenced |= free_identifiers(s["expr"])
                changed = changed or len(referenced) != before
    steps = [s for s in steps if s["key"] in referenced]
    return {"engine": "expr", "steps": steps, "outputs": outputs}


def free_identifiers(expr: str) -> set[str]:
    return set(re.findall(r"(?<![\w.'\"])([A-Za-z_]\w*)(?![\w(])", expr or ""))


def _bands_from_html(html_entry: Optional[dict]) -> list[dict]:
    """Interpretation bands as the mockup records them, for score calculators."""
    if not html_entry:
        return []
    spec = html_entry.get("spec") or {}
    bands = spec.get("bands")
    if bands:
        return [{"min": b.get("min"), "max": b.get("max"),
                 "label": b.get("label"), "raw": None} for b in bands]
    info = html_entry.get("info") or {}
    return info.get("bands") or []


def build_scoring(parsed: engine_score.ParsedScore) -> dict:
    groups = []
    for gi, g in enumerate(parsed.groups):
        groups.append({
            "key": to_snake(re.sub(r"[^A-Za-z0-9 ]", "", g.label))[:60] or f"group_{gi}",
            "label": g.label,
            "widget": "radio_group" if g.selection == "single" else "checkbox_group",
            "selection": g.selection,
            "required": g.selection == "single",
            "options": [
                {"key": f"opt_{gi}_{oi}", "label": o["label"], "points": o["points"]}
                for oi, o in enumerate(g.options)
            ],
        })
    return {
        "groups": groups,
        "bands": parsed.bands,
        "total": {
            "key": "total_score",
            "label": "Total Criteria Point Count",
            "unit": "points",
            "min": engine_score.min_total(parsed.groups),
            "max": engine_score.max_total(parsed.groups),
        },
    }


# --------------------------------------------------------------------------
# top-level assembly
# --------------------------------------------------------------------------

# Bespoke renderers whose data lives in a named mockup global rather than in the
# print view. The PDF carries only the drug names and prose; the structured
# dose/route/branch data is the mockup's.
DATA_GLOBAL_FOR_SLUG = {
    "advanced-life-support-adult": ("ALS_ADULT", "drugs"),
    "advanced-life-support-neonatal": ("ALS_NEONATAL", "drugs"),
    "advanced-life-support-pediatric": ("ALS_PEDIATRIC", "drugs"),
    "rabies-post-exposure-prophylaxis-treecalc": ("RABIES_TREE", "nodes"),
    "morphine-milligram-equivalents-per-day-mmed": ("MMED_DRUGS", "drugs"),
    "apache-ii-scoring-system-by-diagnosis": ("ADMIT_DX_STRUCT", "admit_dx"),
    "newborn-hyperbilirubinemia-assessment-35-weeks-gestation":
        ("PDF122_DATA", "thresholds"),
}



# Methadone's conversion factor rises with the daily dose; the printed table
# gives it as a band ladder rather than the single number every other opioid
# has, which is why the harvested drug list carries `null` for it.
METHADONE_BANDS = [(20, 4), (40, 8), (60, 10), (None, 12)]


def build_mmed(drugs: list[dict]) -> tuple[list[dict], dict]:
    """One dose field per opioid, its MME, and the total.

    The calculator is a table the user fills in, so nothing in the print view
    reads as a formula and the generic parsers find no outputs at all. The
    arithmetic is stated in the document: each drug's MMED is its total daily
    dose times its conversion factor, and the total is their sum.
    """
    inputs: list[dict] = []
    steps: list[dict] = []
    outputs: list[dict] = []
    for d in drugs:
        key = d["key"]
        inputs.append({
            "key": key, "label": d["label"], "type": "number",
            "unit": d.get("unit"), "base_unit": d.get("unit"),
            "required": False, "default": 0,
            "constraints": {"min": 0, "max": None},
            "help": f"Total daily dose of {d['label']} ({d.get('unit')})",
        })
        mkey = f"{key}_mme"
        if d.get("factor") is None and key == "methadone":
            expr = "0"
            for hi, f in reversed(METHADONE_BANDS):
                expr = (f"({key} * {f})" if hi is None
                        else f"({key} * {f}) if ({key} <= {hi}) else ({expr})")
            expr = f"(0) if ({key} <= 0) else ({expr})"
        elif d.get("factor") is None:
            continue
        else:
            expr = f"{key} * {d['factor']}"
        steps.append({"key": mkey, "expr": expr,
                      "note": "dose x MME conversion factor"})
        outputs.append({
            "key": mkey, "label": f"{d['label']} MMED", "expr": mkey,
            "decimals": 2, "base_unit": "MMED", "kind": "number",
        })
    outputs.append({
        "key": "total_mmed", "label": "Total MMED",
        "expr": " + ".join(s["key"] for s in steps),
        "decimals": 2, "base_unit": "MMED", "kind": "number",
    })
    return inputs, {"engine": "expr", "steps": steps, "outputs": outputs}



_GET_TIME = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\.\s*getTime\s*\(\s*\)")
_SET_TIME = re.compile(
    r"(?<![\w.])([A-Za-z_]\w*)\s*\.\s*setTime\s*\(\s*(.+?)\s*\)\s*;", re.S)


def resolve_date_expressions(compute: dict, inputs: list[dict], js: str) -> bool:
    """Model JavaScript `Date` values as numbers so the evaluator can use them.

    Gestational Age is the only calculator in the corpus that does arithmetic on
    dates, and it does all of it through `getTime()`/`setTime()` -- milliseconds
    since the epoch. That is already a number, so the dates need no special
    machinery: a date field enters as epoch milliseconds and every formula the
    script writes works unchanged. The alternative was leaving the calculator's
    entire LMP half unevaluable for want of a subtraction.
    """
    touched = False
    for coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for item in coll:
            e = item.get("expr")
            if e and _GET_TIME.search(e):
                item["expr"] = _GET_TIME.sub(r"\1", e)
                touched = True
    if not touched:
        return False

    # `current_time = new Date` is the script allocating a Date to fill in from
    # the form's month/day/year selects. As a step it defines nothing, and it
    # masks the fact that the date is an input the caller has to supply.
    compute["steps"] = [
        s for s in (compute.get("steps") or [])
        if not re.match(r"^\s*new\s+Date\b", s.get("expr") or "")
    ]

    defined = {s["key"] for s in (compute.get("steps") or [])}
    defined |= {o["key"] for o in (compute.get("outputs") or [])}
    defined |= {i["key"] for i in inputs}

    # A `setTime` names a date the script derives rather than reads.
    for m in _SET_TIME.finditer(js or ""):
        key = to_snake(m.group(1))
        if key in defined:
            continue
        expr = _GET_TIME.sub(r"\1", re.sub(r"\s+", " ", m.group(2)))
        expr = to_snake_expr(expr)
        # These ARE the calculator's date results -- "EDC by LMP", "EDC by
        # BPD" and so on are four of the eight rows the Results block prints --
        # so they belong in the outputs, not among the working steps where
        # nothing reads them and dead-step pruning removes them.
        compute.setdefault("outputs", []).append({
            "key": key,
            "label": key.replace("_", " ").upper().replace("EDC", "EDC by"),
            "expr": expr, "decimals": 0,
            "base_unit": "epoch_ms", "kind": "date",
            "note": "date arithmetic, epoch ms",
        })
        defined.add(key)

    missing: set[str] = set()
    for coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for item in coll:
            for n in free_identifiers(item.get("expr") or ""):
                if n not in defined and n.endswith("_time"):
                    missing.add(n)
    for key in sorted(missing):
        inputs.append({
            "key": key,
            # Left as the key spelled out, so the printed form can still
            # claim it: "Last Menstrual Period" beats "Lmp Date".
            "label": key.replace("_", " ").title(),
            "type": "date", "unit": "epoch_ms", "base_unit": "epoch_ms",
            "required": False, "constraints": {},
            "help": "Date value, supplied as milliseconds since the Unix epoch",
        })
        defined.add(key)
    return True


def to_snake_expr(expr: str) -> str:
    """Lower-case bare identifiers in a recovered expression."""
    def repl(m: re.Match) -> str:
        n = m.group(1)
        return n if n.isupper() else to_snake(n)
    return re.sub(r"(?<![\w.'\"])([A-Za-z_]\w*)(?![\w(])", repl, expr)



def _merge_ladder_segments(ladders: list[dict], compute: dict) -> None:
    """Join ladders that are consecutive stretches of one table.

    WHO's weight-for-length table is printed in two pieces, 45.5-96.5 cm and
    96-110.5 cm. Read as two tables they look like a girls/boys pair with no
    selector, and the calculator used only the second -- so every child under
    96 cm fell outside the table it was given.
    """
    by_key: dict[str, list[dict]] = {}
    for t in ladders:
        by_key.setdefault(t.get("key"), []).append(t)

    used: set[str] = set()
    for coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for item in coll:
            used |= free_identifiers(item.get("expr") or "")

    for group in by_key.values():
        if len(group) < 2:
            continue
        def span(t: dict) -> tuple:
            th = [r.get("threshold") for r in (t.get("rows") or [])
                  if r.get("threshold") is not None]
            return (min(th), max(th)) if th else (0.0, 0.0)
        group.sort(key=lambda t: span(t)[0])
        lo, hi = span(group[0])
        merged = [group[0]]
        for nxt in group[1:]:
            nlo, nhi = span(nxt)
            step = (hi - lo) / max(len(group[0].get("rows") or []) - 1, 1)
            if not (lo < nlo <= hi + max(step, 1e-9) * 2 and nhi > hi):
                merged = []
                break                       # not consecutive: leave them alone
            merged.append(nxt)
            hi = nhi
        if len(merged) < 2:
            continue
        target = next((t for t in merged
                       if any(c in used for c in (t.get("columns") or []))),
                      merged[0])
        tcols = target.get("columns") or []
        # Every segment is relabelled onto the columns the formula reads --
        # including the first, which is not necessarily the one it names.
        rows: list[dict] = []
        last = None
        for seg in merged:
            ren = dict(zip(seg.get("columns") or [], tcols))
            for r in (seg.get("rows") or []):
                if (last is not None and r.get("threshold") is not None
                        and r["threshold"] <= last):
                    continue
                rows.append({ren.get(k, k): v for k, v in r.items()})
            last = span(seg)[1]
        target["rows"] = rows
        target["row_count"] = len(rows)
        target["merged_segments"] = len(merged)
        for t in merged:
            if t is not target:
                ladders.remove(t)




# A unit is a symbol, not a sentence: mg/dL, mL/hr, %, mcg/kg/min, kg/m2.
_UNIT_BAD = __import__("re").compile(r"[:<>=]|\b(the|for|of|is|are|input|selected)\b", __import__("re").I)


def _unit_is_plausible(code) -> bool:
    if not code:
        return True
    text = str(code).strip()
    if len(text) > 14 or _UNIT_BAD.search(text):
        return False
    return text.count(" ") <= 1



# A helper that builds a warning is not arithmetic. `selectValue()` returns the
# text of an alert; reporting it as a lost calculation trains the reader to
# ignore a warning that elsewhere means a real branch is missing.
_MESSAGE_HELPER = re.compile(
    r"^(get\w*(Message|Row|Text|Content)|.*[Vv]alidat\w*|selectValue|"
    r"show\w*Help|alert\w*)$"
)

# The shared include every calculator loads. A call to one of these is expected
# and resolvable; a call to anything else is a hole the PDF cannot fill.
_KNOWN_LIBRARY = {
    "get", "set", "setNumber", "setHTML", "setText", "clear", "clearOutput",
    "alert", "Number", "parseFloat", "parseInt", "Math", "isNaN", "String",
    "round", "power", "sqr", "eTo", "fixDP", "OneDecimalPoint",
    "TwoDecimalPoints", "ZtoPercentile", "getRangeMessage",
    "getMustRangeMessage", "getOutputBelowMinimumError",
    "getOutputAboveMaximumError", "getMultipleInputsNotFilledInMessage",
    "getMultipleInputsNotANumberMessage", "getTitrationTable",
    "getMetaContent", "getFailedTableRow", "getSuccessfulTableRow",
    "checkInputsAreValid", "checkInputRanges", "document", "window",
    "setTimeout", "clearTimeout", "function", "return", "alertNaN", "clrValue",
    "clrResults", "minMaxCheck", "togCB", "setRB", "resetInTime", "split",
    "toString", "substring", "push", "Array", "Date", "calculate", "reset",
    "test", "with", "Event", "getElementById", "dispatchEvent", "getHelpText",
    "getHelpTextInputMustBeGreaterThan", "validateInputColors", "resetColors",
}



# Alerts the shared library shows on every page. They describe the app, not the
# calculation, and on a page with no birthdate field they are noise dressed as
# guidance.
_LIBRARY_ALERTS = (
    "data panel values cannot be transferred",
    "negative values are not allowed",
    "birthdate must be no later",
    "only digits 0 to 9",
    "attempted input of other characters",
)


def _usable_general_help(messages, inputs: list[dict]) -> Optional[list[str]]:
    """Keep the guidance that is about THIS calculator.

    A message names the thing it is about. If that thing is not one of this
    calculator's fields, the message came from the shared library and belongs to
    a different page -- and a truncated fragment ("Desired Peak (Cdp) for")
    tells the reader nothing at all.
    """
    if not messages:
        return None
    labels = {squash(i.get("label") or "") for i in inputs}
    labels |= {squash(i.get("key") or "") for i in inputs}
    labels.discard("")

    kept: list[str] = []
    for m in messages:
        text = re.sub(r"\s+", " ", str(m)).strip()
        low = text.lower()
        if any(b in low for b in _LIBRARY_ALERTS):
            continue
        if len(text) < 40 and not text.endswith((".", "!", "?")):
            continue                       # a fragment, cut off mid-sentence
        # The PDF loses the quote that ended the string, so the alert runs on
        # into the code after it. That is not a sentence anyone should read.
        if re.search(r"[{};]|\w+\(\w*\)\s*[;{]", text):
            continue
        # The field test applies only to messages ABOUT a field -- a range or a
        # requirement. "The decision tree must restart when this preference is
        # changed" is about the calculator, and demanding it name a field threw
        # away the only guidance that page had.
        about_a_field = re.search(
            r"\b(value for|must be (?:at |between|greater|less|no )|"
            r"required|cannot be (?:greater|less|negative))",
            text, re.I,
        )
        subject = about_a_field and re.match(
            r"^(?:the\s+)?(.{3,40}?)\s+(?:must|is|are|cannot|for)\b", text, re.I)
        if subject:
            want = squash(subject.group(1))
            want = re.sub(r"^(minimum|maximum)valuefor", "", want)
            if want and not any(want in l or l in want for l in labels if len(l) > 2):
                continue                   # names something this page has not got
        if text not in kept:
            kept.append(text)
    return kept or None


def _normalise_field_units(field: dict) -> None:
    """Re-express a field's units so its own base unit is the identity.

    `base = raw * factor + offset` is only meaningful once every factor shares a
    reference, and the reference has to be the unit the field's bounds and
    formulas are written in. Where no base unit is recorded, the unit that is
    already the identity is the one the calculator was written against.
    """
    units = field.get("units") or []
    if len(units) < 2:
        return
    base_code = (field.get("base_unit") or "").strip().lower()
    base = next(
        (u for u in units if str(u.get("code", "")).strip().lower() == base_code), None)
    if base is None:
        base = next(
            (u for u in units
             if u.get("factor") == 1 and not (u.get("offset") or 0)), None)
        if base is None:
            return
        field["base_unit"] = base.get("code")
    else:
        # Adopt the list's own spelling, so nothing downstream has to guess
        # whether `hours` and `Hours` are the same unit.
        field["base_unit"] = base.get("code")
        field["display_unit"] = base.get("code")
    fb = float(base.get("factor") or 1.0)
    ob = float(base.get("offset") or 0.0)
    if fb == 1.0 and ob == 0.0:
        return
    for u in units:
        f = u.get("factor")
        if not f:
            continue
        u["factor"] = float(f) / fb
        u["offset"] = (float(u.get("offset") or 0.0) - ob) / fb
    field["units_rebased_to"] = base.get("code")


# Names an expression may use that no field supplies.
_EVALUABLE_NAMES = {"e", "pi", "if", "else", "and", "or", "not", "min", "max",
                    "abs", "round", "pow", "sqrt", "ln", "log", "exp"}


def build_spec(
    pdf_spec: dict,
    sections: dict[str, str],
    html_entry: Optional[dict],
    engine: str,
    data_globals: Optional[dict] = None,
) -> dict:
    """Assemble the final canonical spec from every available source."""
    from .pdf_extract import parse_bullets, parse_input_block, parse_references

    title = pdf_spec["title"]
    # The page drawn twice repeats a line verbatim, which unbalances the braces
    # and costs the body a whole branch. Repair it once, for every engine.
    if sections.get("Scripts"):
        sections = dict(sections)
        sections["Scripts"] = jsblock.drop_page_seam(sections["Scripts"])
    js = sections.get("Scripts", "") or ""
    _lib_strings = {
        "KILOGRAMS": "kg", "POUNDS": "lbs", "GRAMS": "g",
        "MILLIGRAMS": "mg", "MICROGRAMS": "mcg",
        "CENTIMETERS": "cm", "INCHES": "in", "METERS": "m",
        "MILLILITERS": "mL", "LITERS": "L",
        "MINUTES": "min", "HOURS": "hr", "DAYS": "day",
        "MALE": "Male", "FEMALE": "Female", "YES": "Yes", "NO": "No",
        "OTHER": "other",
    }
    _lib_strings.update(dict(re.findall(
        r"var\s+([A-Z][A-Z0-9_]*)\s*=\s*['\"]([^'\"]{1,40})['\"]", js)))
    _wk_idmap = engine_b.parse(js).id_map if (js and "get(ID_" in js) else {}
    consts = (html_entry or {}).get("constants") or {}
    registry = units_from_constants(consts)
    conflicts: list[dict] = []

    renderer = RENDERER_FOR_ENGINE.get(engine, "unknown")
    inputs = pdf_spec.get("inputs") or []
    compute = pdf_spec.get("compute") or {"engine": "expr", "steps": [], "outputs": []}
    scoring = None
    tables: dict[str, Any] = {}

    if engine == "wk":
        parsed = engine_b.parse(sections.get("Scripts", ""))
        keys = [engine_b.id_to_key(i, parsed.id_map.get(i)) for i in parsed.inputs]
        printed = parse_input_block(
            sections.get("Calculator", ""), [k.replace("_", " ").title() for k in keys]
        )
        inputs = build_inputs_wk(parsed, printed)
        compute = build_compute_wk(parsed, inputs)

        ladder = engine_b.parse_dose_ladder(sections.get("Scripts", ""))
        if ladder:
            # One row per dose: a table generator, not a scalar calculator. The
            # per-row expression indexes the ladder, so rewrite that subscript
            # to the row's own dose variable.
            renderer = "titration_table"
            tables["dose_ladder"] = ladder
            # Capture the loop body: it holds the per-row formula.
            for _n, _e in engine_b.parse_row_body(
                sections.get("Scripts", ""), ladder["name"]
            ):
                _k = camel_to_snake(_n)
                if any(st["key"] == _k for st in (compute.get("steps") or [])):
                    continue
                _rx = engine_b._clean_expr(_e)
                # The loop body shares the calculate() scope, so a working
                # variable the executor had to rename to avoid colliding with a
                # field of the same name must be renamed here too -- otherwise
                # the row formula reads the raw dropdown where the scalar
                # formula reads the value derived from it.
                for _st in (compute.get("steps") or []):
                    if _st["key"].endswith("_calc"):
                        _base = _st["key"][: -len("_calc")]
                        _rx = re.sub(r"(?<![\w.])" + re.escape(_base) + r"(?![\w])",
                                     _st["key"], _rx)
                compute.setdefault("steps", []).append({
                    "key": _k,
                    "expr": _rx,
                    "note": "per-row formula",
                })
            pat = re.compile(re.escape(ladder["name"]) + r"\s*\[\s*\w+\s*\]")
            for coll in (compute.get("steps") or [], compute.get("outputs") or []):
                for item in coll:
                    if item.get("expr"):
                        item["expr"] = pat.sub("dose", item["expr"])
            compute["row_variable"] = "dose"
            if ladder["truncated_in_pdf"]:
                conflicts.append({
                    "kind": "dose_ladder_truncated", "severity": "high",
                    "detail": f"the printed dose array is cut off after "
                              f"{ladder['count']} values (last {ladder['values'][-1]}"
                              f", step {ladder['step']}); confirm the full ladder "
                              f"before publishing",
                })
        if parsed.notes:
            pdf_spec["info"].setdefault("conditional_notes", parsed.notes)

    elif engine == "score":
        parsed = engine_score.parse(sections, sections.get("Scripts", ""))
        scoring = build_scoring(parsed)
        inputs, compute = [], {"engine": "score", "steps": [], "outputs": []}
        if not scoring["groups"]:
            conflicts.append({"kind": "score_no_groups", "severity": "critical",
                              "detail": "no criterion groups parsed from the printed table"})
        if not scoring["bands"]:
            # Many score PDFs carry no Results section at all, so their
            # interpretation bands exist only in the mockup.
            hb = _bands_from_html(html_entry)
            if hb:
                scoring["bands"] = hb
                scoring["bands_source"] = "html"
            else:
                conflicts.append({"kind": "score_no_bands", "severity": "medium",
                                  "detail": "no interpretation bands in the PDF or the mockup"})

    elif engine == "convert" and "titration" not in title.lower():
        # The print view collapses both <select>s to the literal text
        # "Pull-Down", so the from/to pairs and their factors exist only in the
        # mockup, which stores them as NEW_CALCS.converts[].pairs.
        renderer = "convert"
        pairs = (((html_entry or {}).get("spec")) or {}).get("pairs")
        if pairs:
            tables["pairs"] = pairs
        else:
            conflicts.append({"kind": "convert_pairs_missing", "severity": "high",
                              "detail": "conversion pairs are collapsed in the PDF and "
                                        "absent from the mockup entry"})
        inputs, compute = [], {"engine": "convert", "steps": [], "outputs": []}

    elif engine == "tree":
        renderer = "tree"
        conflicts.append({"kind": "tree_nodes_needed", "severity": "high",
                          "detail": "decision-tree nodes are not in the print view; "
                                    "source them from the mockup implementation"})
        inputs, compute = [], {"engine": "tree", "steps": [], "outputs": []}

    # A formula engine that yielded no outputs is often a score calculator that
    # merely happens to ship an _fx()/calculate() stub (Apgar, Gestational Age).
    # Try the declarative table before declaring it broken.
    if renderer == "formula" and not (compute.get("outputs") or []):
        alt = engine_score.parse(sections, sections.get("Scripts", ""))
        if alt.groups:
            renderer = "score"
            scoring = build_scoring(alt)
            inputs = []
            compute = {"engine": "score", "steps": [], "outputs": []}
            conflicts.append({"kind": "reclassified_as_score", "severity": "low",
                              "detail": f"engine reported {engine} but no outputs; "
                                        f"parsed {len(alt.groups)} criterion groups"})

    # Printed equation is vendor text and often has the formulas the JS
    # write-back never named (ToxLevel/RxLevel, multi-equation QT).
    if renderer == "formula" and not (compute.get("outputs") or []):
        eq = (pdf_spec.get("info") or {}).get("equation") or ""
        known = {i["key"] for i in inputs}
        known |= {i.get("js_name") for i in inputs if i.get("js_name")}
        if any("heart_rate" in (k or "") for k in known):
            known.add("rr_interval")
            known.add("RR_interval")
        recovered = enrich.outputs_from_equation(eq, {n for n in known if n})
        if recovered:
            compute.setdefault("steps", [])
            compute["outputs"] = recovered
            conflicts.append({
                "kind": "outputs_from_printed_equation", "severity": "medium",
                "detail": f"recovered {len(recovered)} output(s) from the "
                          f"printed Equation section because the script "
                          f"did not write them to result fields",
            })

    # QT multi-equation prints RR_interval but the form collects heart rate.
    # The vendor's own relationship is RR (ms) = 60000 / HR.
    if renderer == "formula":
        referenced = set()
        for coll in (compute.get("steps") or []), (compute.get("outputs") or []):
            for item in coll:
                referenced |= free_identifiers(item.get("expr") or "")
        keys = {i["key"] for i in inputs} | {
            s["key"] for s in (compute.get("steps") or [])
        }
        hr = next((k for k in keys if "heart_rate" in k), None)
        if "rr_interval" in referenced and "rr_interval" not in keys and hr:
            compute.setdefault("steps", []).insert(
                0, {"key": "rr_interval", "expr": f"60000 / {hr}"}
            )

    # A recovered lookup ladder means the result is a table interpolation, not a
    # closed-form formula. The table came from the PDF itself, so it is vendor
    # ground truth rather than an external growth reference.
    def _bind_ladders_to_select(ladders_, inputs_):
        """Attach variant ladders to the selector that chooses between them.

        Growth charts ship one ladder per sex, keyed on the same variable over
        the same range. Nothing in the formula distinguishes them -- the radio
        picks the TABLE, not a coefficient -- so without this binding the lookup
        always returns the first ladder and the selector is inert.
        """
        if len(ladders_) < 2:
            return None

        # Group by key: one calculator can carry several measures, each with its
        # own per-sex pair. Girls' and boys' tables rarely have the same row
        # count (they cover slightly different ranges), so requiring identical
        # spans rejected exactly the pairs that most needed binding.
        by_key: dict[str, list[dict]] = {}
        for t in ladders_:
            by_key.setdefault(t.get("key"), []).append(t)

        bound_any = None
        for inp in inputs_:
            opts = inp.get("options") or []
            if len(opts) < 2:
                continue
            if not all(len(g) == len(opts) for g in by_key.values()):
                continue
            for group in by_key.values():
                for idx, t in enumerate(group):
                    t["select_key"] = inp["key"]
                    t["select_value"] = opts[idx].get("value")
                    t["variant_label"] = opts[idx].get("label")
            bound_any = inp["key"]
            break
        if bound_any:
            return bound_any

        # Grouping by lookup key fails when one calculator carries several
        # measures over the SAME key: CDC's under-36-months chart has four
        # measures, six of whose eight ladders are keyed on Age, so the group
        # sizes are 6 and 2 rather than 2 and 2. The script emits them as
        # `if (Sex == 1){...} if (Sex == 2){...}` per measure, so consecutive
        # runs are the variants -- chunk in source order instead.
        used = set()
        for coll in (compute.get("steps") or [], compute.get("outputs") or []):
            for item in coll:
                used |= free_identifiers(item.get("expr") or "")
        for inp in inputs_:
            opts = inp.get("options") or []
            n = len(opts)
            if n < 2 or len(ladders_) % n or len(ladders_) // n < 2:
                continue
            for start in range(0, len(ladders_), n):
                chunk = ladders_[start:start + n]
                # Every variant must expose the column names the formula reads,
                # so the sibling tables are relabelled onto the one it uses.
                target = next(
                    (c for c in chunk
                     if any(col in used for col in (c.get("columns") or []))),
                    chunk[0],
                )
                tcols = target.get("columns") or []
                for idx, tb in enumerate(chunk):
                    cols = tb.get("columns") or []
                    if cols != tcols and len(cols) == len(tcols):
                        ren = dict(zip(cols, tcols))
                        tb["columns"] = list(tcols)
                        tb["rows"] = [
                            {ren.get(k, k): v for k, v in row.items()}
                            for row in (tb.get("rows") or [])
                        ]
                        tb["relabelled_from"] = cols
                    tb["select_key"] = inp["key"]
                    tb["select_value"] = opts[idx].get("value")
                    tb["variant_label"] = opts[idx].get("label")
            return inp["key"]
        return bound_any

    ladders = pdf_spec.get("lookup_tables") or (
        [pdf_spec["lookup_table"]] if pdf_spec.get("lookup_table") else []
    )
    if ladders:
        renderer = "lms"
        tables["lookups"] = ladders
        # kept for single-ladder consumers
        tables["lookup"] = ladders[0] if len(ladders) == 1 else None
        compute["engine"] = "lookup+expr"
    elif LMS_TITLE.search(title) and any(
        not o.get("expr") for o in compute.get("outputs") or []
    ):
        renderer = "lms"
        conflicts.append({"kind": "lms_table_not_recovered", "severity": "high",
                          "detail": "growth-chart calculator but no lookup ladder "
                                    "was recovered from the script"})

    if engine == "dose_table" or (engine == "convert" and "titration" in title.lower()):
        renderer = "dose_table"

    # ---- attach unit option lists (mockup is the only source) --------------
    unresolved_units = 0
    for inp in inputs:
        entry = pick_units_for_field(inp["key"], inp.get("base_unit"), registry)
        if entry:
            inp["units"] = entry["units"]
            inp["dimension"] = entry.get("dimension")
            if entry.get("base_unit") and inp.get("base_unit") and \
               squash(entry["base_unit"]) != squash(inp["base_unit"]):
                conflicts.append({
                    "kind": "base_unit_disagreement", "severity": "medium",
                    "field": inp["key"], "pdf": inp["base_unit"],
                    "html": entry["base_unit"],
                })
            if any(not u["resolved"] for u in entry["units"]):
                unresolved_units += 1
        elif inp.get("base_unit"):
            inp["units"] = [{"code": inp["base_unit"], "label": inp["base_unit"],
                             "factor": 1.0, "offset": 0.0, "resolved": True}]
    # Infer the dimension where the mockup's naming did not reveal it, then fill
    # any factor the sources never stated from the canonical physical table.
    for inp in inputs:
        if not inp.get("dimension") and inp.get("units"):
            inp["dimension"] = unitreg.infer_dimension(
                inp.get("base_unit"), [u["code"] for u in inp["units"]]
            )
    ustats = unitreg.fill_unresolved(inputs)
    unresolved_units = ustats["still_unresolved"]
    if unresolved_units:
        conflicts.append({"kind": "unit_factors_missing", "severity": "medium",
                          "detail": f"{unresolved_units} field(s) have unit options "
                                    f"with no conversion factor in either source"})

    # Attach structured data the print view cannot carry.
    mapping = DATA_GLOBAL_FOR_SLUG.get(pdf_spec["slug"])
    if mapping:
        gname, tkey = mapping
        payload = (data_globals or {}).get(gname)
        if payload:
            tables[tkey] = payload
            if gname == "MMED_DRUGS":
                inputs, compute = build_mmed(payload)
                renderer = "mmed"
            if gname == "RABIES_TREE":
                ends = (data_globals or {}).get("RABIES_ENDS")
                if ends:
                    tables["outcomes"] = ends
        else:
            conflicts.append({
                "kind": "data_table_missing", "severity": "high",
                "detail": f"{renderer} data expected in mockup global {gname!r}, "
                          f"which was not harvested",
            })

    # A radio group is a patient-selectable coefficient, not a constant. Adding
    # it as a required select is what stops the calculator silently computing
    # every patient as the last branch (male, non-Black, and so on).
    groups = pdf_spec.get("radio_groups") or []
    # Some radios never touch the calculation directly: their onclick calls a
    # `varloadN()` that swaps the whole coefficient set. Nothing in the `_fx`
    # body records the choice, so without this the last handler's numbers stand
    # for every patient -- Framingham scored every woman as a man.
    _vgroups = engine_radio.find_varload_groups(js)
    if _vgroups:
        _have = {g.name for g in groups}
        groups = list(groups) + [g for g in _vgroups if g.name not in _have]
    if groups:
        engine_radio.attach_labels(
            groups,
            engine_radio.labels_from_printed(
                sections.get("Calculator", ""),
                [g.name for g in groups],
                expected={g.name: len(g.options) for g in groups},
                other_labels={i.get("label") or i["key"] for i in inputs},
            ),
        )
        existing_keys = {i["key"] for i in inputs}
        for g in groups:
            field_def = engine_radio.build_input(g)
            if field_def["key"] in existing_keys:
                continue
            inputs.insert(0, field_def)
            existing_keys.add(field_def["key"])
            if field_def.get("variant_variables"):
                tables.setdefault("variants", {})[field_def["key"]] = {
                    "variables": field_def["variant_variables"],
                    "options": [
                        {"value": o["value"], "label": o["label"],
                         "constants": o.get("constants")}
                        for o in field_def["options"]
                    ],
                }
            if g.label_mismatch:
                conflicts.append({
                    "kind": "radio_labels_uncertain", "severity": "medium",
                    "field": field_def["key"],
                    "detail": "printed option labels and script branches did not "
                              "line up exactly; confirm the label/value pairing",
                })

    # Bind variant ladders now that the radio selects exist as inputs -- the
    # selector has to be present before a ladder can be attached to it.
    _ladders = tables.get("lookups") or []
    if len(_ladders) > 1:
        _merge_ladder_segments(_ladders, compute)
        tables["lookups"] = _ladders
    # Parallel ladders are ones keyed on the SAME variable -- girls' and boys'
    # weight-for-age. Two ladders keyed on different variables are two
    # measures, both used, and demanding a selector for them reported four
    # correct growth calculators as silently wrong.
    _parallel = Counter(t.get("key") for t in _ladders)
    if _parallel and max(_parallel.values()) > 1:
        if not _bind_ladders_to_select(_ladders, inputs):
            conflicts.append({
                "kind": "ladder_variants_unbound", "severity": "critical",
                "detail": f"{len(_ladders)} parallel lookup ladders but no selector "
                          f"with a matching option count; the lookup would always "
                          f"return the first table",
            })

    # A field whose unit dropdown selects an ENTRY MODE needs that choice as a
    # real input; the formulas branch on it.
    for jsname, nmodes in (pdf_spec.get("unit_modes") or {}).items():
        target = to_snake(jsname)
        mkey = f"{target}_unit_mode"
        if any(i["key"] == mkey for i in inputs):
            continue
        host = next((i for i in inputs if i["key"] == target), None)
        codes = [u.get("code") for u in ((host or {}).get("units") or [])]
        opts = []
        for n in range(nmodes):
            opts.append({"label": codes[n] if n < len(codes) else f"Mode {n + 1}",
                         "value": n})
        inputs.insert(0, {
            "key": mkey, "js_name": f"{jsname}_unit", "widget": "select",
            "label": f"{js_name_to_label(jsname)} entry mode",
            "required": True, "default": 0, "base_unit": None, "units": None,
            "constraints": {}, "options": opts,
            "options_source": "pdf_script_unit_mode",
            "options_incomplete": len(codes) < nmodes,
            "source": {"kind": "unit_mode", "field": target},
        })

    # An override question ("on dialysis?") forces an input to a fixed value.
    # Model it as a yes/no select plus a derived value, and point the formula at
    # the derived value -- otherwise the question has no effect at all.
    for ov in (pdf_spec.get("input_overrides") or []):
        target = to_snake(ov["input"])
        inp = next((i for i in inputs if i["key"] == target), None)
        if inp is None:
            continue
        gkey = to_snake(ov["group"])
        label = ov["group"].replace("_", " ").strip()
        if not any(i["key"] == gkey for i in inputs):
            inputs.insert(0, {
                "key": gkey, "js_name": ov["group"], "label": label,
                "widget": "select", "required": True, "default": 0,
                "base_unit": None, "units": None, "constraints": {},
                "options": [{"label": "No", "value": 0},
                            {"label": "Yes", "value": 1}],
                "options_source": "pdf_script+printed_form",
                "source": {"kind": "input_override", "js_group": ov["group"],
                           "overrides": target, "forced_value": ov["value"]},
            })
        eff = f"{target}_effective"
        if not any(st["key"] == eff for st in (compute.get("steps") or [])):
            compute.setdefault("steps", []).insert(0, {
                "key": eff,
                "expr": f"({ov['value']}) if ({gkey} == 1) else ({target})",
                "note": f"{label} forces {target} to {ov['value']}",
            })
        for coll in (compute.get("steps") or [], compute.get("outputs") or []):
            for item in coll:
                if item.get("key") == eff or not item.get("expr"):
                    continue
                item["expr"] = rename_vars(item["expr"], {target: eff})

    # A comparison against a module string constant (`unit != KILOGRAMS`) leaves
    # a bare identifier the evaluator cannot resolve, which deadlocks everything
    # downstream of it. Substitute the literal the constant holds.
    # Engine B compares unit selections against SCREAMING string constants that
    # live in a shared library the export never prints -- the same reason its
    # rounding helpers had to be supplied natively. Their values are fixed
    # vocabulary, so declaring them here is safe; a file that ships its own
    # definition still overrides these.
    LIBRARY_STRINGS = {
        "KILOGRAMS": "kg", "POUNDS": "lbs", "GRAMS": "g",
        "MILLIGRAMS": "mg", "MICROGRAMS": "mcg",
        "CENTIMETERS": "cm", "INCHES": "in", "METERS": "m",
        "MILLILITERS": "mL", "LITERS": "L",
        "MINUTES": "min", "HOURS": "hr", "DAYS": "day",
        "MALE": "Male", "FEMALE": "Female", "YES": "Yes", "NO": "No",
        "OTHER": "other",
    }
    str_consts = _lib_strings
    # ID_* constants hold DOM element ids, not values. Substituting them turns
    # `(ID_DOSE * 15000) / ID_DRUG_AMOUNT` into arithmetic on the strings
    # 'idDose' and 'idDrugAmount'; the reads are resolved to input keys
    # elsewhere, so these must never enter an expression.
    str_consts = {k: v for k, v in str_consts.items()
                  if not k.startswith("ID_")
                  and k not in engine_b.element_id_constants(js)}
    if str_consts:
        from .evaluator import ExpressionError, free_names
        for coll in (compute.get("steps") or [], compute.get("outputs") or []):
            for item in coll:
                e = item.get("expr")
                if not e:
                    continue
                try:
                    names = free_names(e)
                except ExpressionError:
                    names = set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?![\w(])", e))
                repl = {n: f"'{str_consts[n]}'" for n in names if n in str_consts}
                # the parser lower-cases identifiers, so try that spelling too
                for n in names:
                    if n in repl:
                        continue
                    for cname, cval in str_consts.items():
                        if cname.lower() == n.lower():
                            repl[n] = f"'{cval}'"
                            break
                if repl:
                    item["expr"] = rename_vars(e, repl)

    # Inline the calculator's own helper functions, whichever engine it uses.
    # EBMcalc files borrow the same rounding/unit helpers as the newer engine,
    # so restricting inlining to engine B left those calls unresolvable.
    helpers = engine_b.simple_functions(js)
    if helpers:
        for coll in (compute.get("steps") or [], compute.get("outputs") or []):
            for item in coll:
                if item.get("expr"):
                    item["expr"] = engine_b.inline_calls(
                        engine_b._inline_helpers(item["expr"]), helpers
                    )

    # An expression that still will not parse is often two statements the PDF
    # glued together by dropping a semicolon ("rate = a/b rate = round(rate,2)").
    # Repairing only the expressions that actually fail keeps the whole-body
    # split -- which is too blunt for engine B -- out of the picture.
    # JS lets an assignment stand in for its value inside a condition; as an
    # expression that is a syntax error whichever engine produced it, so the
    # reduction runs for all of them rather than in one engine's pipeline.
    from .jsparse import strip_embedded_assignments
    for _coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for _it in _coll:
            if _it.get("expr"):
                _it["expr"] = strip_embedded_assignments(_it["expr"])

    # A guard that is always satisfied adds noise to every expression it wraps.
    for _coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for _it in _coll:
            e = _it.get("expr")
            if not e:
                continue
            prev = None
            while prev != e:
                prev = e
                e = re.sub(r"\((.+?)\)\s+if\s+\(\(1 == 1\)\)\s+else\s+\([^()]*\)",
                           r"\1", e)
                e = re.sub(r"\([^()]*\)\s+if\s+\(\(1 == 0\)\)\s+else\s+\((.+?)\)",
                           r"\1", e)
            _it["expr"] = e

    # Expressions assembled by the branch executor can lose a delimiter when the
    # source statement was cut by a page seam. Balancing is a last repair before
    # anything tries to parse them.
    from .jsparse import balance_parens
    for _coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for _it in _coll:
            if _it.get("expr"):
                _it["expr"] = balance_parens(_it["expr"])

    _repair_glued_expressions(compute)

    # `var weightWarn = WEIGHT_WARN` becomes the step `weight_warn = weight_warn`
    # once both sides canonicalise to the same key. That can never resolve and
    # deadlocks every expression downstream, so drop it and let the constant
    # resolver below supply the real value.
    steps_ = compute.get("steps") or []
    if steps_:
        compute["steps"] = [
            st for st in steps_
            if not (st.get("expr") or "").strip() == st["key"]
        ]

    # Anything an expression still references but nothing defines is usually a
    # module constant the per-engine scan did not reach -- a threshold declared
    # beside the helpers, or a lookup the branch logic consumed. Resolve those
    # from the script rather than leaving the whole calculator unevaluable for
    # one missing number.
    # `var Height = 0;` is the script declaring the variable it is about to
    # read the field into, not a computation. Kept as a step it shadows the
    # input and pins the patient's height to zero.
    _input_keys = {i["key"] for i in inputs}
    compute["steps"] = [
        s for s in (compute.get("steps") or [])
        if not (s["key"] in _input_keys
                and not free_identifiers(s.get("expr") or ""))
    ]

    # Dates are arithmetic too, once `getTime()` is read as the number it is.
    resolve_date_expressions(compute, inputs, js)

    _resolve_module_constants(compute, inputs, tables, js)

    # Fold any identifier still carrying its original JS spelling.
    table_cols = {c for t in (tables.get("lookups") or [])
                  for c in (t.get("columns") or [])}
    table_cols |= {v for spec_v in (tables.get("variants") or {}).values()
                   for v in (spec_v.get("variables") or [])}
    dead = prune_dead_steps(compute, inputs, table_cols)
    if dead:
        conflicts.append({"kind": "dead_steps_pruned", "severity": "low",
                          "detail": f"unreachable and unused: {dead[:6]}"})
    leftovers = [n for n in normalise_expression_names(compute, inputs)
                 if n not in table_cols]
    if leftovers:
        conflicts.append({
            "kind": "unresolved_identifiers", "severity": "critical",
            "detail": f"expressions reference names nothing defines: {leftovers[:8]}",
        })

    # A coefficient with several values is only a problem while nothing in the
    # spec chooses between them. Once it is a select, a variant constant set or
    # a lookup column, the choice is modelled -- reporting it anyway buried the
    # cases that really are unresolved under forty that are not.
    amb = pdf_spec.get("ambiguous_constants") or {}
    _settled = {squash(i["key"]) for i in inputs}
    _settled |= {squash(i.get("label") or "") for i in inputs}
    _settled |= {squash(v) for spec_v in (tables.get("variants") or {}).values()
                 for v in (spec_v.get("variables") or [])}
    _settled |= {squash(c) for c in table_cols}
    _settled |= {squash(s["key"]) for s in (compute.get("steps") or [])}
    amb = {k: v for k, v in amb.items() if squash(k) not in _settled}
    if amb:
        conflicts.append({
            "kind": "variant_dependent_constants", "severity": "critical",
            "detail": "these coefficients take different values per patient "
                      "variant (e.g. sex) and the variant selector was not "
                      f"recovered: {dict(list(amb.items())[:5])}",
        })

    # ---- enrich: help text, dropdown options, then grade completeness ----
    helps = enrich.extract_help_messages(js)
    # A result the page also exposes as a readable element arrives as both an
    # input and an output. Once the calculation defines it, offering it as
    # something the caller fills in is wrong: `abw` is what the calculator
    # works out, not what it is told.
    _computed = {s["key"] for s in (compute.get("steps") or [])}
    _out_exprs = {o["key"]: o.get("expr") or "" for o in (compute.get("outputs") or [])}
    # ...unless the calculation reads it back. The aminoglycoside dose writes
    # creatinine clearance to its field and then clamps what it reads there, so
    # that one really is both.
    _read_back: set[str] = set()
    for st in (compute.get("steps") or []):
        _read_back |= free_identifiers(st.get("expr") or "")
    for k, e in _out_exprs.items():
        _read_back |= free_identifiers(e) - {k}
    _drop = set()
    for inp in inputs:
        k = inp["key"]
        if k not in _out_exprs or k in _read_back:
            continue
        if k in _computed or k not in free_identifiers(_out_exprs[k]):
            _drop.add(k)
    if _drop:
        inputs = [i for i in inputs if i["key"] not in _drop]

    # A field's unit factors must be relative to that field's OWN base unit,
    # because its bounds are. The mockup states them relative to the dimension
    # instead -- A-a gradient's "%" carries factor 0.01 against a fraction --
    # so a percentage entered as 50 arrived as 0.5 and failed a 1-100 bound.
    for inp in inputs:
        _normalise_field_units(inp)

    # A `<STEM>_MIN/MAX` the script declares but no field carries is a limit on
    # something the calculator WORKS OUT -- amiodarone's 2.1 g/day ceiling, a
    # concentration range checked after it is derived. Dropped, the document's
    # own safety limit disappears from the spec entirely; recorded, it can be
    # shown beside the result it constrains.
    _stated_limits: list[dict] = []
    _stated: dict[str, dict] = {}
    for _m in re.finditer(
        r"(?:var|let|const)\s+([A-Z][A-Z0-9_]*?)_(MIN|MAX)\s*=\s*(-?\d*\.?\d+)\s*;?"
        r"[ \t]*(?://[ \t]*((?:(?!\b(?:var|let|const|function)\b)[^\n])*))?",
        js or "",
    ):
        _note = (_m.group(4) or "").strip()
        # ALS declares `var AGE_MIN = 29; //days -- not used in validation` next
        # to `var AGE_MAX = 18; //years`. Two units, and the script says outright
        # that it enforces neither; printed as a range it read "29 - 18".
        if re.search(r"not\s+used", _note, re.I):
            continue
        _slot = _stated.setdefault(_m.group(1), {})
        _slot[_m.group(2).lower()] = float(_m.group(3))
        _u = re.sub(r"^\s*(?:the\s+)?(?:value\s+)?(?:is\s+)?(?:in\s+)?", "", _note)
        _u = re.split(r"\s*(?:--|,|;|\bnot\b|\bused\b)", _u, 1)[0].strip()
        if (re.fullmatch(r"[A-Za-z][A-Za-z/²%.\d]*(?:\s?[A-Za-z/²%.\d]+)?", _u or "")
                and not _slot.get("unit")):
            _slot["unit"] = _u
    if _stated:
        _held = {squash(i["key"]) for i in inputs}
        _held |= {squash(i.get("label") or "") for i in inputs}
        _derived = {squash(s["key"]): s["key"]
                    for s in (compute.get("steps") or [])}
        _derived.update({squash(o["key"]): o["key"]
                         for o in (compute.get("outputs") or [])})
        # A field enforces the constant only when it is the SAME quantity. The
        # containment test read `BOLUS_DOSE_ML_MIN` as belonging to the bolus
        # dose field -- but that field is mcg/kg and the limit is on the mL
        # volume the script works out, so esmolol's 0.1 mL floor reached no one.
        # The same rule hid phenylephrine's usual-range advisory behind `dose`.
        _held_values = {f"{v:g}" for i in inputs
                        for k, v in (i.get("constraints") or {}).items()
                        if k in ("min", "max") and isinstance(v, (int, float))}
        _limits = []
        for stem, bound in _stated.items():
            key = squash(stem)
            same = any(key == h for h in _held if len(h) > 2)
            if not same and any(key in h or h in key for h in _held if len(h) > 2):
                # A near-name still counts as enforced when the numbers agree.
                same = all(f"{v:g}" in _held_values for v in bound.values()
                           if isinstance(v, (int, float)))
            if same:
                continue
            target = next((v for k, v in _derived.items()
                           if k == key or key in k or k in key), None)
            if ("min" in bound and "max" in bound
                    and bound["min"] > bound["max"]):
                # ALS states AGE_MIN in days and AGE_MAX in years. Printed as a
                # range it reads "29 - 18", which is worse than saying nothing.
                conflicts.append({
                    "kind": "stated_limit_inverted", "severity": "medium",
                    "detail": f"{stem}_MIN ({bound['min']:g}) is above "
                              f"{stem}_MAX ({bound['max']:g}); the two are in "
                              f"different units, so neither is shown",
                })
                continue
            _limits.append({
                "name": _limit_name(stem, inputs),
                "applies_to": target,
                **bound,
            })
        if _limits:
            _stated_limits = _limits

    # `CONCENTRATION_MIN/MAX` guards the concentration the script DERIVES, not
    # the dropdown it derives it from: dobutamine's selector offers 1000
    # mcg/mL and the limit is 5 mg/mL. Left on the input, the bound rejects the
    # calculator's own options, so it moves to the value it actually guards.
    _step_keys = {s["key"] for s in (compute.get("steps") or [])}
    for inp in inputs:
        derived = f"{inp['key']}_calc"
        if derived in _step_keys and (inp.get("constraints") or {}):
            inp["derived_constraints"] = {
                "applies_to": derived,
                **{k: v for k, v in inp["constraints"].items()
                   if k in ("min", "max")},
            }
            inp["constraints"] = {
                k: v for k, v in inp["constraints"].items()
                if k not in ("min", "max")
            }

    # A branch that calls a helper from the shared include -- which the PDFs do
    # not carry -- cannot be translated, and the arithmetic inside it is simply
    # absent. That is survivable only if it is SAID: Calvert estimates the GFR
    # itself when the user answers "not known", and with the estimate missing it
    # was dosing carboplatin against a GFR of zero while looking fine.
    _defined = set(re.findall(r"function\s+(\w+)\s*\(", js or ""))
    _body = ""
    for _fn in ("calculate", r"\w+_fx"):
        _body += engine_b._slice_function(js or "", _fn) or ""
    _called = set(re.findall(r"(?<![\w.$])([A-Za-z_]\w{3,})\s*\(", _body))
    # A library helper is named like one: `mL_to_tsp`, `gfrAdultCalculate...`.
    # A bare English word followed by "(" is prose the PDF lost its comment
    # markers on, and reporting it as a missing helper is noise that teaches
    # the reader to ignore the warning.
    _unresolved = sorted(
        c for c in _called - _defined - _KNOWN_LIBRARY
        if not c.endswith("_fx")
        and c not in engine_b.UNIT_HELPERS
        and ("_" in c or re.search(r"[a-z][A-Z]", c))
        and not _MESSAGE_HELPER.match(c)
    )
    if _unresolved:
        conflicts.append({
            "kind": "library_helper_missing", "severity": "high",
            "detail": "the script calls "
                      + ", ".join(f"{h}()" for h in _unresolved[:3])
                      + " from an include the PDF does not carry, so the branch "
                        "using it was not recovered; confirm this calculator "
                        "against the live app before relying on that path",
        })

    # A field's unit `<select>` is not a second field. `height` already carries
    # its unit list and renders a picker beside the box; `height_unit_list`
    # captured the same control again, so the form asked for the unit twice.
    # It is only dropped when the host can actually offer the choice and no
    # formula reads the selector -- Adjusted Body Weight genuinely branches on
    # `abw_unit_list` to decide the unit of its own result.
    _referenced: set[str] = set()
    for _coll in (compute.get("steps") or [], compute.get("outputs") or []):
        for _it in _coll:
            _referenced |= free_identifiers(_it.get("expr") or "")
    _by_key = {i["key"]: i for i in inputs}
    _drop_units: set[str] = set()
    for inp in inputs:
        m = re.fullmatch(r"(.+?)_unit(?:_list)?", inp["key"])
        if not m:
            continue
        host = _by_key.get(m.group(1))
        if host is None or inp["key"] in _referenced:
            continue
        if not host.get("units") and len(inp.get("options") or []) >= 2:
            continue                    # the host cannot offer it; keep the field
        if len(host.get("units") or []) >= 2:
            _drop_units.add(inp["key"])
    if _drop_units:
        inputs = [i for i in inputs if i["key"] not in _drop_units]

    # A value the calculation WORKS OUT is not a value to ask for. These are the
    # page's result elements, read back by the script; as form fields they
    # invite a clinician to type over the answer.
    _step_keys = {s["key"] for s in (compute.get("steps") or [])}
    inputs = [i for i in inputs if i["key"] not in _step_keys]

    # `gender_list`, `method_list` -- the `_list` is the DOM element's name, not
    # part of what the field is called.
    for inp in inputs:
        if inp["key"].endswith("_list") and squash(inp.get("label") or "").endswith("list"):
            inp["label"] = re.sub(r"\s*List$", "", inp["label"]).strip() or inp["label"]

    # A calculator whose logic lives in the app, not in the PDF's script, still
    # states its fields on the printed form. Without them the spec carries a
    # data table and nothing to look up in it.
    if not inputs and renderer in ("dose_table", "tree", "unknown", "convert"):
        _printed = enrich.inputs_from_printed_form(sections.get("Calculator", ""))
        if _printed:
            inputs = _printed
            conflicts.append({
                "kind": "inputs_from_printed_form_only", "severity": "medium",
                "detail": f"{len(_printed)} field(s) recovered from the printed "
                          f"form; the calculation itself is not in the PDF",
            })

    if scoring is not None:
        _rlabels = enrich.score_result_labels(
            sections.get("Calculator", ""), sections.get("Results", ""),
            band_labels={squash(bnd.get("label") or "")
                         for bnd in (scoring.get("bands") or [])},
        )
        if _rlabels:
            # The header names the table's columns, and the text layer leaves
            # its column rule in as "**". The bands themselves now carry those
            # names, so the header only has to read as a header rather than as
            # a stray "Risk ** Death".
            _rlabels = [engine_score._COL_SEP.sub(" \u00b7 ", r) for r in _rlabels]
            scoring["interpretation"] = {
                "key": "interpretation", "label": _rlabels[0],
                "printed_rows": _rlabels,
            }

    enrich.labels_from_printed_options(inputs, sections.get("Calculator", ""))
    enrich.attach_printed_labels(
        inputs, compute.get("outputs") or [],
        sections.get("Calculator", ""), sections.get("Results", ""))
    helped = enrich.attach_help(inputs, helps)
    info = pdf_spec.get("info") or {}
    printed_out_units = {
        squash(k): v.get("unit")
        for k, v in (pdf_spec.get("_printed_outputs") or {}).items() if v.get("unit")
    }
    # The vendor names every field's unit beneath its formula; use that before
    # falling back to guessing from module constants.
    prose_units = enrich.units_from_formula_prose(
        (sections.get("Additional Information", "") or "")
        + "\n" + (info.get("equation") or "")
    )
    if prose_units:
        enrich.attach_units_from_prose(inputs, prose_units)
        enrich.attach_units_from_prose(compute.get("outputs") or [], prose_units)
    enrich.attach_output_units(compute.get("outputs") or [], js, printed_out_units)
    disp = spec_display = pdf_spec.get("display") or {}
    if disp.get("decimal_precision") is None:
        dp = enrich.decimal_precision(js, compute.get("outputs") or [])
        if dp is not None:
            disp["decimal_precision"] = dp
            disp["precision_source"] = "script"

    if engine == "wk":
        enrich.attach_comparison_options(
            inputs, js, _wk_idmap, _lib_strings
        )

    enrich.resolve_option_codes(inputs, js)

    ranges = enrich.help_from_range_functions(js)
    rstats = enrich.attach_range_help(inputs, ranges)
    # A field's own alert text is often the only statement of its limits.
    enrich.bounds_from_help(inputs, helps.get("_general"))
    helped += rstats["help"]
    opt_stats = enrich.attach_options(
        inputs, js, sections.get("Calculator", ""), html_entry
    )
    # A dropdown the varload handler rebuilds has one option list per variant.
    # Pooling them gave Framingham four blood-pressure options -- both sexes'
    # coefficients in one list, with nothing to say which was which.
    _vopts = engine_radio.options_by_varload(js) if _vgroups else {}
    if _vopts:
        _sel = next((i for i in inputs
                     if (i.get("source") or {}).get("js_group")
                     in {g.name for g in _vgroups}), None)
        for inp in inputs:
            per_variant = _vopts.get(squash(inp["key"]))
            if not per_variant or len(per_variant) < 2:
                continue
            merged = []
            for idx in sorted(per_variant):
                seen_v = set()
                for o in per_variant[idx]:
                    if o["value"] in seen_v:
                        continue        # the page drawn twice
                    seen_v.add(o["value"])
                    entry = dict(o)
                    entry["variant_index"] = idx
                    if _sel is not None:
                        vopt = next((x for x in (_sel.get("options") or [])
                                     if x.get("index") == idx), None)
                        entry["variant_value"] = (vopt or {}).get("value", idx)
                        entry["variant_label"] = (vopt or {}).get("label")
                    merged.append(entry)
            inp["options"] = merged
            if _sel is not None:
                inp["options_depend_on"] = _sel["key"]

    if opt_stats["missing"] or opt_stats["default_only"]:
        conflicts.append({
            "kind": "option_list_incomplete", "severity": "high",
            "detail": f"{opt_stats['missing']} field(s) have no option list and "
                      f"{opt_stats['default_only']} carry only the printed default; "
                      f"the full lists exist only in the live app",
        })

    html_info = ((html_entry or {}).get("info")) or {}

    # Engine-B documents keep formula, notes and instructions in one block.
    addl = enrich.parse_additional_information(sections.get("Additional Information", ""))
    if not info.get("equation") and addl["equation"]:
        info["equation"] = addl["equation"]
    if not info.get("notes") and addl["notes"]:
        info["notes"] = addl["notes"]
    usage = addl["instructions"]

    # A "unit" recovered from the printed results block is sometimes the first
    # words of the interpretation line beside it -- "Percentile <5:", "Z-Score >
    # -2:", "the units selected for Input". A unit is a short symbol; anything
    # carrying a colon, a comparison or a sentence is prose that landed in the
    # wrong field, and printing it after a number states something false.
    for _o in (compute.get("outputs") or []):
        if not _unit_is_plausible(_o.get("base_unit")):
            _o["unit_rejected"] = _o.pop("base_unit")
            _o["base_unit"] = None

    # Fields the page shows or hides as another control changes. A coefficient
    # that only applies to one sex is not a field the form always asks for, and
    # modelling it as one made the sex selector look like it changed nothing.
    _vis = engine_b.visibility_rules(engine_b._strip_comments(js or ""), {}) if js else {}
    if _vis:
        _keys = {i["key"]: i for i in inputs}
        for row, rule in _vis.items():
            # `K_AGE_M_ROW` is the row holding the field `id_k_age_m`.
            stem = squash(re.sub(r"_?ROW$", "", row))
            target = next(
                (i for k, i in _keys.items()
                 if squash(k) == stem or squash(k).endswith(stem) or stem.endswith(squash(k))),
                None,
            )
            control = next(
                (i for k, i in _keys.items()
                 if squash(k) == squash(rule["control_const"])
                 or squash(k).endswith(squash(rule["control_const"]).replace("id", ""))),
                None,
            )
            if target is None or control is None or target is control:
                continue
            values = list(rule.get("equals") or [])
            if not values:
                # Only the hiding branch was recovered; the field is visible for
                # every other option the control offers.
                hidden = {str(v) for v in rule.get("hidden_for") or []}
                values = [o.get("value") for o in (control.get("options") or [])
                          if str(o.get("value")) not in hidden]
            if values:
                target["visible_when"] = {"field": control["key"], "equals": values}

        # Only one arm of the toggle is usually recoverable -- the `else` branch
        # that shows the other row is written differently every time. Where two
        # fields are plainly the pair (`id_k_age_f` and `id_k_age_m`), the one
        # without a rule takes the options the other does not claim.
        _ruled = [i for i in inputs if i.get("visible_when")]
        for ruled in _ruled:
            ctrl_key = ruled["visible_when"]["field"]
            control = _keys.get(ctrl_key)
            if not control:
                continue
            claimed = {str(v) for r in _ruled
                       if r["visible_when"]["field"] == ctrl_key
                       for v in r["visible_when"]["equals"]}
            spare = [o.get("value") for o in (control.get("options") or [])
                     if str(o.get("value")) not in claimed]
            if not spare:
                continue
            stem = ruled["key"].rsplit("_", 1)[0]
            for sib in inputs:
                if (sib is not ruled and not sib.get("visible_when")
                        and sib is not control
                        and sib["key"].rsplit("_", 1)[0] == stem):
                    sib["visible_when"] = {"field": ctrl_key, "equals": spare}

    # `setNumber(ID_CRCL, crcl); ... if (get(ID_CRCL) > 125)` -- the script
    # writes a value to its own results field and reads it straight back. The
    # write became a step and the read stayed an input, so the clamp was applied
    # to whatever was in the box rather than to what had just been worked out:
    # the aminoglycoside dose ignored its creatinine-clearance method entirely.
    _steps = compute.get("steps") or []
    _step_at = {s["key"]: i for i, s in enumerate(_steps)}
    _by_squash: dict[str, list[str]] = {}
    for s in _steps:
        _by_squash.setdefault(squash(s["key"]), []).append(s["key"])
    _input_keys = {i["key"] for i in inputs}
    for i, item in enumerate(_steps):
        expr = item.get("expr") or ""
        for name in sorted(free_identifiers(expr)):
            # Either the read stayed an input, or -- once the results field was
            # correctly classed as an output -- it became a name nothing
            # defines. Both mean the same thing: the step above computed it.
            if name in _step_at or name in _EVALUABLE_NAMES:
                continue
            same = [k for k in _by_squash.get(squash(name), [])
                    if k != name and _step_at.get(k, 1 << 30) < i]
            if not same:
                continue
            item["expr"] = re.sub(
                r"(?<![\w.])" + re.escape(name) + r"(?![\w])", same[-1], expr)
            item.setdefault("note", "reads back the value the step above computed")
            expr = item["expr"]

    # The printed data table is held row by row in `tables.lookups`; leaving the
    # flattened copy in the prose puts 14,000 characters of digits in the page.
    info["notes"], _dumped = enrich.strip_data_dumps(info.get("notes") or [])
    if _dumped:
        info["data_table_in_tables"] = True

    # A display unit has to be one of the field's own units. MELD's INR came out
    # showing "Yes" -- the word on the next row of the printed form -- so the
    # form offered a ratio in a unit that does not exist, and the limits beside
    # it belonged to a different scale.
    for _f in inputs:
        _codes = {squash(u.get("code") or "") for u in (_f.get("units") or [])}
        _codes.discard("")
        _d = squash(_f.get("display_unit") or "")
        if _d and _codes and _d not in _codes:
            _f["display_unit"] = _f.get("base_unit")
        elif _d and not _codes and squash(_f.get("base_unit") or "") != _d:
            _f["display_unit"] = _f.get("base_unit")

    # A value the arithmetic tests for is a choice the form has to offer, or the
    # branch is unreachable: the aminoglycoside empiric calculator computes a
    # Traub-Johnson clearance that no option could ever select.
    _blob = " ".join(
        [(st.get("expr") or "") for st in (compute.get("steps") or [])]
        + [(o.get("expr") or "") for o in (compute.get("outputs") or [])])
    for _f in inputs:
        if _f.get("widget") == "date":
            continue
        # A field with NO list that the arithmetic compares to words is a
        # dropdown the extraction missed, not a number: the aminoglycoside
        # empiric form asked the clinician to type a drug name into a numeric
        # box, and then refused it for not being a number.
        if not _f.get("options") and not re.search(
                r"(?<![\w.])" + re.escape(_f["key"]) + r"\s*==\s*'", _blob):
            continue
        _f.setdefault("options", [])
        _have = {squash(str(o.get("value"))) for o in _f["options"]}
        for _m in re.finditer(
                r"(?<![\w.])" + re.escape(_f["key"]) + r"\s*==\s*'([^']*)'", _blob):
            _v = _m.group(1)
            if _v and squash(_v) not in _have:
                _f["options"].append({"label": _v, "value": _v,
                                      "source": "script comparison"})
                _have.add(squash(_v))
        if _f["options"] and _f.get("widget") != "select":
            _vals = [str(o.get("value")) for o in _f["options"]]
            # "other" is the escape hatch beside a number the clinician types --
            # fentanyl's concentration offers preset strengths and `other`. Made
            # a dropdown, the field disappears and a word goes into arithmetic.
            if all(squash(v) in enrich.SENTINEL_VALUES for v in _vals):
                _f["options"] = None
                _f.setdefault("accepts_sentinel", _vals)
                continue
            if len(_vals) == 1:
                _partner = enrich.BINARY_PARTNER.get(squash(_vals[0]))
                if _partner:
                    _f["options"].append({"label": _partner, "value": _partner,
                                          "source": "binary partner"})
                else:
                    _f["options_incomplete"] = True
                    _f["options_known_value"] = _vals[0]
            _f["widget"] = "select"
            _f["constraints"] = {}
            _f["units"] = None
            _f["base_unit"] = None
            _f.setdefault("options_source", "pdf_script_comparisons")

    tidy_units(inputs, compute.get("outputs"))
    options_from_printed_cases(inputs, info.get("equation") or "")
    _apply_coefficient_ladders(inputs, compute, info.get("equation") or "")

    # A form whose every control is frozen returns the same number to every
    # patient. APACHE II ships 14 dropdowns and the PDF prints only the option
    # each one opened on, all worth 0 points -- so the score was always 0, and
    # the page gave no sign of it. Say so at the top of the calculator instead.
    # A score criterion the PDF printed only the default rung of can never add a
    # point: the palliative score's life-expectancy, WBC and lymphocyte ladders
    # all came out with their 0-point option alone, so three of its six criteria
    # were decoration.
    if scoring and (scoring.get("groups") or []):
        _stuck = [g.get("key") for g in scoring["groups"]
                  if len(g.get("options") or []) == 1
                  and not (g["options"][0].get("points"))]
        if _stuck:
            conflicts.append({
                "kind": "score_criteria_frozen", "severity": "critical",
                "detail": f"{len(_stuck)} of the {len(scoring['groups'])} criteria "
                          "offer only the 0-point choice the source PDF printed, "
                          "so they can never add to the total: "
                          + ", ".join(str(k) for k in _stuck[:4]),
                "fields": [str(k) for k in _stuck],
            })

    if renderer in ("formula", "score") and inputs:
        _movable = [
            i for i in inputs
            if not (i.get("options") is not None
                    and len({str(o.get("value")) for o in (i.get("options") or [])}) < 2)
        ]
        _frozen = [i for i in inputs if i not in _movable]
        if _frozen and len(_frozen) >= len(_movable):
            _what = ("every one of the %d controls" % len(inputs)
                     if not _movable
                     else "%d of the %d controls" % (len(_frozen), len(inputs)))
            conflicts.append({
                "kind": "result_cannot_vary" if not _movable
                        else "most_controls_frozen",
                "severity": "critical",
                "detail": f"{_what} on this form offer a single choice, because "
                          "the source PDF prints only the option each dropdown "
                          "opened on -- so the result barely moves, or does not "
                          "move at all, with the patient; use the source "
                          "calculator until the full option lists are available",
                "fields": [i["key"] for i in _frozen][:20],
            })

    spec = {
        "schema_version": SCHEMA_VERSION,
        "slug": pdf_spec["slug"],
        "title": title,
        "subtitle": (html_entry or {}).get("subtitle"),
        "category": pdf_spec.get("category") or (html_entry or {}).get("category"),
        "renderer": renderer,
        "engine": engine,
        "status": "draft",
        "version": 1,
        "inputs": inputs,
        "compute": compute,
        "scoring": scoring,
        "tables": tables or None,
        "display": pdf_spec.get("display") or {},
        "content": {
            "equation": info.get("equation"),
            "notes": info.get("notes"),
            "conditional_notes": info.get("conditional_notes"),
            "calc_details": info.get("calc_details"),
            "data_input_disclaimer": info.get("data_input_disclaimer"),
            "references": info.get("references") or [],
            "disclaimer": info.get("disclaimer"),
            "copyright": info.get("copyright"),
            "instructions": usage,
            "stated_limits": _stated_limits or None,
            "reference_tables": enrich.printed_reference_tables(
                sections.get("Calculator", ""), inputs) or None,
            "fixed_values": enrich.fixed_values(
                sections.get("Calculator", ""), inputs) or None,
            "form_sections": enrich.form_section_headings(
                sections.get("Calculator", ""), inputs,
                (compute or {}).get("outputs")) or None,
        },
        "help": {"general": _usable_general_help(helps.get("_general"), inputs),
                 "fields_with_help": helped},
        "provenance": {
            "pdf": pdf_spec.get("provenance"),
            "html": {"id": (html_entry or {}).get("id"),
                     "title": (html_entry or {}).get("title"),
                     "matched": html_entry is not None},
            "unit_registry_stems": sorted(registry.keys()) or None,
            "conflicts": conflicts,
            "built": date.today().isoformat(),
        },
        "tests": [],
    }
    spec["completeness"] = enrich.grade(
        spec, available={k for k in sections if k != "_preamble"}
    )
    return spec
