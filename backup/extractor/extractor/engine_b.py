"""Parser for the newer Wolters Kluwer calculator engine ("engine B").

47 of the 186 corpus PDFs use this instead of EBMcalc. Its shape:

    var ID_AMOUNT = 'drugAmount';          // DOM id constants name the fields
    var DOSE_MIN = 0.16667;  // mg/min     // bounds, unit in a trailing comment
    var DOSE_MAX = 1;        // mg/min

    function checkInputRanges() { ... }    // enforcement + messages
    function calculate() {
      var dose   = new Number(get(ID_DOSE));       // reads  -> inputs
      var rate   = (dose / (amount / volume)) * 60; // locals -> steps
      setNumber(ID_RATE, rate, 1);                  // writes -> outputs
      setHTML(NOTE_DIV, "Concentration is above 2 mg/mL; ...");  // conditional note
    }

Unlike EBMcalc there is no `_fx`/`minMaxCheck` pair and no unit-affine plumbing;
bounds live in module constants and units appear only as trailing comments, so
they are recovered textually and flagged when absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import jsblock
from .jsparse import (
    _slice_function,
    js_expr_to_canonical,
    normalize_text,
    to_snake,
)

# `var ID_AMOUNT = 'drugAmount';`
# Most files name their element ids `ID_*`, but not all: Treprostinil writes to
# OUTPUT_STRENGTH / OUTPUT_AMOUNT / OUTPUT_FINAL. Requiring the ID_ prefix made
# every one of its results invisible, so any SCREAMING constant holding a
# DOM-id-shaped string counts.
ID_CONST = re.compile(
    r"(?:var|let|const)\s+([A-Z][A-Z0-9_]*)\s*=\s*"
    r"['\"]([A-Za-z_][A-Za-z0-9_]{1,40})['\"]")
_ID_PREFIXES = ("ID_", "OUTPUT_", "INPUT_", "FIELD_", "UNITS_")



_ELEMENT_ID_USE = re.compile(
    r"\b(?:get|set|setNumber|setHTML|setText|getElementById)\s*\(\s*"
    r"([A-Z][A-Z0-9_]*)\s*[,)]"
)


def element_id_constants(js: str) -> set[str]:
    """Constants the script uses to name a DOM element.

    Such a constant is a field reference, and substituting its value turns a
    formula into arithmetic on an element id -- `(ID_DOSE * 15000)` becoming
    `('idDose' * 15000)`. Excluding the `ID_`-prefixed ones covered the common
    naming convention only: Treprostinil calls its elements FIELD_*/OUTPUT_*,
    and every field reference in its formulas was replaced by an id string. The
    test has to be how the script uses the name, and it has to stay narrow --
    the same file's YES/IV constants are genuine comparison values.
    """
    return set(_ELEMENT_ID_USE.findall(js or ""))



_DECL = re.compile(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)")
_PARAMS = re.compile(r"function\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\)")


def declared_names(js: str) -> set[str]:
    """Every name the script declares: variables, functions, parameters."""
    names = set(_DECL.findall(js or ""))
    names |= set(ANY_FN.findall(js or "") and
                 [m.group(1) for m in ANY_FN.finditer(js or "")])
    for m in _PARAMS.finditer(js or ""):
        names |= {p.strip() for p in m.group(1).split(",") if p.strip()}
    return names



def predicate_functions(js: str) -> dict[str, str]:
    """Zero-argument helpers that just test a field, as JS expressions.

    `if (isConcentrationOther())` is a real branch, but the call made the
    condition untranslatable, so both arms were dropped -- and Dobutamine's
    concentration then fell through to the raw dropdown value instead of the
    milligrams-per-millilitre it derives, a thousand-fold error that still
    computed and still passed validation.
    """
    out: dict[str, str] = {}
    for m in ANY_FN.finditer(js or ""):
        name, params = m.group(1), m.group(2)
        if params.strip():
            continue
        body = _slice_function(js[m.start():], re.escape(name))
        if body is None:
            continue
        if re.search(r"\b(set|setHTML|setNumber|setText|alert|clear)\s*\(", body):
            continue
        rm = re.search(r"\breturn\s+([^;{}]+?)\s*;", body)
        if not rm or not re.search(r"[=<>!]", rm.group(1)):
            continue
        expr = rm.group(1).strip()
        for am in re.finditer(
            r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;{}]+?)\s*;", body
        ):
            expr = re.sub(r"(?<![\w.])" + re.escape(am.group(1)) + r"(?![\w])",
                          f"({am.group(2).strip()})", expr)
        if re.search(r"(?<![\w.])(?!get\b)[A-Za-z_]\w*\s*\(", expr):
            continue                        # still calls something we cannot see
        out[name] = expr
    return out


_DISPLAY_ONLY_USE = re.compile(
    r"set(?:HTML|Text)\s*\(\s*\w+\s*,\s*get\s*\(\s*([A-Z][A-Z0-9_]*)\s*\)")
_INDEX_USE = re.compile(
    r"getElementById\s*\(\s*([A-Z][A-Z0-9_]*)\s*\)\s*\.\s*selectedIndex")


def display_only_ids(body: str) -> set[str]:
    """Controls the script never treats as a number.

    Pediatric Dosing reads its dose-unit dropdown twice -- once to label the
    result, once to ask which position was picked:

        setHTML(OUTPUT_DOSE_UNITS, get(ID_DOSE_UNITS));
        ... getElementById(ID_DOSE_UNITS).selectedIndex > 4 ...

    Neither is arithmetic, so the control is a unit selector, not a quantity.
    Left in, it became a second field labelled "Dose" that asked the clinician
    to type a number into a dropdown. A control that is coerced with
    `new Number(get(...))` anywhere is a real input and is left alone.
    """
    cand = set(_DISPLAY_ONLY_USE.findall(body or "")) | set(
        _INDEX_USE.findall(body or ""))
    numeric = set(re.findall(
        r"(?:new\s+Number|parseFloat|parseInt)\s*\(\s*get\s*\(\s*"
        r"([A-Z][A-Z0-9_]*)\s*\)", body or ""))
    return cand - numeric


def _is_element_id(name: str, id_map: dict) -> bool:
    """Only a constant that actually holds an element id names a field.

    Matching on the prefix alone swept in ordinary constants (CONCENTRATION_MIN
    and the like), which then looked like written outputs and were removed from
    the input list.
    """
    return name in (id_map or {})
# `var DOSE_MIN = 0.16667; // mg/min`   (an inline `//` comment may precede the value)
BOUND_CONST = re.compile(
    r"(?:var|let|const)\s+([A-Z][A-Z0-9_]*?)_(MIN|MAX)\s*=\s*"
    r"(?:/\*.*?\*/\s*)?(-?\d*\.?\d+)\s*(?://\s*(?://\s*)?([^\n;]*))?",
    re.S,
)
# reads: get(ID_X) possibly wrapped in Number()/parseFloat()
# Reads must recognise the same id constants writes do, or a file that names
# its elements OUTPUT_*/UNITS_* ends up with outputs but no inputs at all.
READ = re.compile(r"get\s*\(\s*([A-Z][A-Z0-9_]*)\s*\)")
# writes: set(ID_X, ...) / setNumber(ID_X, value, dp) / setHTML(ID_X, ...)
WRITE_NUM = re.compile(
    r"setNumber\s*\(\s*([A-Z][A-Z0-9_]*)\s*,\s*(.+?)\s*,\s*(\d+)\s*\)", re.S
)
WRITE_SET = re.compile(r"\bset\s*\(\s*([A-Z][A-Z0-9_]*)\s*,\s*(.+?)\s*\)\s*;", re.S)
NOTE = re.compile(r"setHTML\s*\(\s*NOTE_DIV\s*,\s*(.+?)\)\s*;", re.S)
# `var rate = <expr>;`
# Newer Engine-B files use `let`/`const`; matching only `var` missed every
# local in them, so their outputs referenced variables that were never captured.
LOCAL = re.compile(r"(?:var|let|const)\s+([a-z_]\w*)\s*=\s*([^;]+);", re.I)

PLUMBING = {
    "get", "set", "setNumber", "setHTML", "clear", "clearOutput", "alert",
    "console", "document", "checkInputsAreValid", "checkInputRanges",
    "getMultipleInputsNotFilledInMessage", "getOutputBelowMinimumError",
    "getOutputAboveMaximumError", "getMustRangeMessage", "getMetaContent",
    "isNaN", "Number", "String", "parseFloat", "parseInt",
}


@dataclass
class EngineBScript:
    id_map: dict[str, str] = field(default_factory=dict)      # ID_AMOUNT -> drugAmount
    inputs: list[str] = field(default_factory=list)           # ID_ constants read
    outputs: list[dict] = field(default_factory=list)         # {id_const, expr, decimals}
    steps: list[tuple[str, str]] = field(default_factory=list)
    bounds: dict[str, dict] = field(default_factory=dict)     # STEM -> {min,max,unit}
    notes: list[str] = field(default_factory=list)
    has_tests: bool = False
    helpers: dict = field(default_factory=dict)
    raw_body: str = ""        # calculate() body, for local-alias resolution
    # Values a field is compared against: the real option list of a dropdown,
    # which the print view reduces to whichever option happened to be selected.
    select_options: dict[str, list[str]] = field(default_factory=dict)
    # Unit dropdowns, recovered from the conversion each option triggers.
    unit_options: dict[str, list[dict]] = field(default_factory=dict)
    # ID constants that turned out to BE a unit dropdown, not a field of their own.
    unit_selector_ids: set = field(default_factory=set)
    # Branches we refused to translate, so a gap is reported rather than hidden.
    unresolved: list[dict] = field(default_factory=list)
    text_outputs: set = field(default_factory=set)
    # Limits (and the document's own field names) stated in checkInputRanges().
    range_bounds: dict[str, dict] = field(default_factory=dict)


_strip_comments = jsblock.strip_comments


def id_to_key(id_const: str, dom_id: Optional[str]) -> str:
    """Prefer the DOM id (camelCase, human-chosen) over the SCREAMING constant."""
    if dom_id:
        s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", dom_id)
        return re.sub(r"__+", "_", s).lower().strip("_")
    for pfx in _ID_PREFIXES:
        if id_const.startswith(pfx):
            return to_snake(id_const[len(pfx):])
    return to_snake(id_const)


# Unit converters the engine ships as named helpers. Each is a pure scale, so a
# `if (unit == POUNDS) weight = lbs_to_kg(weight)` branch tells us both that the
# field HAS a unit dropdown and what that option's factor is -- the only place
# either fact survives in an Engine-B export.
UNIT_FACTORS = {
    "lbs_to_kg": 0.45359237,
    "kg_to_lbs": 1 / 0.45359237,
    "inches_to_cm": 2.54,
    "cm_to_inches": 1 / 2.54,
    "in_to_cm": 2.54,
    "cm_to_in": 1 / 2.54,
    "g_to_mg": 1000.0,
    "mg_to_g": 0.001,
    "mcg_to_mg": 0.001,
    "mg_to_mcg": 1000.0,
    "L_to_mL": 1000.0,
    "mL_to_L": 0.001,
    "mL_to_tsp": 1 / 4.92892159375,
    "tsp_to_mL": 4.92892159375,
    # Creatinine's molar mass gives the standard 88.4 umol/L per mg/dL.
    "creatine_umol_per_L_to_mg_per_dL": 1 / 88.4,
    "creatinine_umol_per_L_to_mg_per_dL": 1 / 88.4,
    "creatine_mg_per_dL_to_umol_per_L": 88.4,
    "urea_mmol_per_L_to_mg_per_dL": 1 / 0.357,
    "albumin_g_per_L_to_g_per_dL": 0.1,
    "calcium_mmol_per_L_to_mg_per_dL": 4.008,
    "glucose_mmol_per_L_to_mg_per_dL": 18.0182,
}


def _unit_selectors(stmts: list, id_map: dict, consts: dict) -> tuple[dict, set]:
    """Recover each field's unit dropdown from the conversions it triggers.

    Engine B has no affine unit model. It reads a unit `<select>` and calls a
    converter, so a unit dropdown looks exactly like an ordinary input to a
    scan that only counts reads -- which is how a phantom `weight_unit_list`
    field appeared next to a Weight field that had no units at all.

    Returns the options per field local name, plus the ID constants that are
    unit selectors rather than fields.
    """
    options: dict[str, list[dict]] = {}
    selector_ids: set = set()
    handled: set = set()

    def walk(sts: list) -> None:
        for st in sts:
            if not isinstance(st, jsblock.If):
                continue
            walk(st.then)
            walk(st.els)
            m = re.search(
                r"get\s*\(\s*(ID_[A-Z0-9_]+)\s*\)\s*==\s*"
                r"(?:([A-Z][A-Z0-9_]*)|['\"]([^'\"]+)['\"])",
                st.cond,
            )
            if not m:
                continue
            idc = m.group(1)
            token = m.group(2) or m.group(3) or ""
            value = consts.get(token, token) if m.group(2) else token
            for inner in st.then:
                if not isinstance(inner, jsblock.Assign):
                    continue
                fm = re.match(
                    r"\s*(\w+)\s*\(\s*" + re.escape(inner.name) + r"\s*\)\s*$",
                    inner.expr,
                )
                if not fm:
                    continue
                selector_ids.add(idc)
                handled.add(st.cond)
                factor = UNIT_FACTORS.get(fm.group(1))
                opts = options.setdefault(inner.name, [])
                if not any(o["code"] == value for o in opts):
                    opts.append({
                        "code": value, "factor": factor, "offset": 0.0,
                        "converter": fm.group(1),
                        "source": "pdf_script_unit_branch",
                    })

    walk(stmts)
    return options, selector_ids, handled


class _WkResolver:
    """Translate Engine-B JavaScript atoms into our expression language."""

    def __init__(self, script: EngineBScript, alias: dict[str, str],
                 consts: dict[str, str], predicates: Optional[dict] = None):
        self.s = script
        self.predicates = predicates or {}
        self.alias = alias            # local name -> canonical input key
        self.consts = consts          # MALE -> 'male'
        self.input_keys = {
            id_to_key(i, script.id_map.get(i)) for i in script.inputs
        }
        self.getmap = {
            i: id_to_key(i, script.id_map.get(i)) for i in script.inputs
        }

    # -- naming ---------------------------------------------------------
    def key(self, js_name: str) -> str:
        if js_name in self.alias:
            return self.alias[js_name]
        k = camel_to_snake(js_name)
        # A working variable can share a field's name while another local holds
        # the field itself: Fentanyl reads its concentration dropdown into
        # `infusionConcentrationOption`, then computes the numeric value into a
        # separate `concentration`. Folding the two together made the computed
        # value look like a re-read of the field, so the branch that derived it
        # from drug amount and volume was dropped.
        if k in self.input_keys and k in set(self.alias.values()):
            return f"{k}_calc"
        return k

    # A field read has to survive identifier renaming intact. Writing the key
    # straight in let `key()` re-map it, and a script whose working variable
    # shares the field's name then turned `get(ID_CONCENTRATION) == 'other'`
    # into a test of the value being derived from it.
    _FIELD_TAG = "fld__"

    def _resolve_reads(self, e: str) -> str:
        for idc, k in self.getmap.items():
            e = re.sub(r"\bget\s*\(\s*" + re.escape(idc) + r"\s*\)",
                       self._FIELD_TAG + k, e)
        return e

    def _untag(self, e: str) -> str:
        return re.sub(r"(?<![\w.])" + self._FIELD_TAG + r"(\w+)", r"\1", e)

    def _rename(self, e: str) -> str:
        """Map every JS identifier to its canonical key, literals untouched.

        Renaming only the input aliases left `outputDose` in the expressions
        while the step it referred to had been emitted as `output_dose`, so the
        output referenced a name that did not exist.
        """
        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name.startswith(self._FIELD_TAG):
                return name             # already a resolved field read
            if name in _KEEP_NAMES or name in _EVALUABLE:
                return name
            if name in self.consts:
                return repr(self.consts[name])
            for cname, cval in self.consts.items():
                if cname.isupper() and name.lower() == cname.lower():
                    return repr(cval)
            return self.key(name)

        return _sub_outside_strings(_IDENT_READ, repl, e)

    # -- expressions ----------------------------------------------------
    def expr(self, js: str) -> Optional[str]:
        e = re.sub(r"\s+", " ", js or "").strip().rstrip(";").strip()
        if not e:
            return None
        # A lone string literal is a categorical result ('Conventional'); a
        # concatenation is prose being assembled, which is not a value we model.
        lone = re.fullmatch(r"'([^']*)'|\"([^\"]*)\"", e)
        if lone:
            return repr(lone.group(1) if lone.group(1) is not None else lone.group(2))
        # Quoted literals in comparisons (`gender == 'Male'`) are values.
        # Concatenation (`note + "..."`, `"At " + x`) is prose, not a number.
        if re.search(r"""['"]\s*\+|\+\s*['"]""", e):
            return None
        if e.startswith("new ") and "Number" not in e:
            return None                     # a container, not a value
        e = _clean_expr(e)
        e = self._resolve_reads(e)
        e = inline_calls(e, self.s.helpers)
        # Reduce the Math namespace BEFORE renaming identifiers: renaming
        # lower-cases `Math.round` to `math.round`, after which the mapping no
        # longer matches and the property check rejects the whole expression --
        # which is how Vancomycin lost its suggested maintenance dose.
        e = js_expr_to_canonical(e)
        e = self._untag(self._rename(e))
        e = js_expr_to_canonical(e)
        if re.search(r"\bget\s*\(", e) or "[" in e:
            return None
        if re.search(r"(?<![\w.])[A-Za-z_]\w*\s*\(", e):
            calls = set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", e))
            if calls - _EVALUABLE - {"if", "else"}:
                return None
        if _PROPERTY.search(e):
            return None                     # a live DOM/property read
        return e

    # -- conditions -----------------------------------------------------
    # `if (document.getElementById('VD_ROW').style.display != 'none')` decides
    # whether a result ROW is shown, not whether it is computed. Dropping the
    # branch as untranslatable discards the write inside it, so the calculator
    # loses that output entirely. The arithmetic is unconditional, so the guard
    # is treated as satisfied.
    _DISPLAY_GUARD = re.compile(
        r"document\s*\.\s*getElementById\s*\([^)]*\)\s*\.\s*style\s*\.\s*display"
        r"\s*(!==?|===?)\s*['\"]none['\"]"
    )

    def cond(self, js: str) -> Optional[str]:
        c = re.sub(r"\s+", " ", js or "").strip()
        if not c:
            return None
        m = self._DISPLAY_GUARD.search(c)
        if m and self._DISPLAY_GUARD.sub("", c).strip(" ()") == "":
            # `!= 'none'` means visible -> adopt; `== 'none'` means hidden -> skip
            return "1 == 1" if m.group(1).startswith("!") else "1 == 0"
        for pname, pexpr in self.predicates.items():
            c = re.sub(r"(?<![\w.])" + re.escape(pname) + r"\s*\(\s*\)",
                       f"({pexpr})", c)
        c = self._resolve_reads(c)
        # `gender == MALE` is only meaningful once MALE is its literal value.
        for name, val in sorted(self.consts.items(), key=lambda kv: -len(kv[0])):
            c = re.sub(r"(?<![\w.])" + re.escape(name) + r"(?![\w])",
                       repr(val), c)
        c = js_expr_to_canonical(c)
        c = self._untag(self._rename(c))
        c = c.replace("&&", " and ").replace("||", " or ")
        c = re.sub(r"(?<![=!<>])===?(?!=)", "==", c)
        c = re.sub(r"!==?", "!=", c)
        c = re.sub(r"!\s*(?=[\w(])", " not ", c)
        c = js_expr_to_canonical(c)
        if re.search(r"\bget\s*\(", c) or "[" in c or _PROPERTY.search(c):
            return None
        calls = set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", c))
        if calls - _EVALUABLE:
            return None
        return c

    # -- writes ---------------------------------------------------------
    def output_of(self, call: jsblock.Call) -> Optional[tuple]:
        if call.fn == "setNumber" and len(call.args) >= 2:
            idc = call.args[0]
            if not _is_element_id(idc, self.s.id_map):
                return None
            dp = None
            if len(call.args) >= 3 and re.fullmatch(r"\d+", call.args[2].strip()):
                dp = int(call.args[2])
            return (id_to_key(idc, self.s.id_map.get(idc)), call.args[1], dp)
        if call.fn in ("set", "setText") and len(call.args) == 2:
            idc, val = call.args[0], call.args[1].strip()
            if not _is_element_id(idc, self.s.id_map) or val in ("''", '""'):
                return None
            return (id_to_key(idc, self.s.id_map.get(idc)), val, None)
        return None

    def note_of(self, call: jsblock.Call) -> Optional[str]:
        if call.fn != "setHTML" or len(call.args) != 2:
            return None
        val = call.args[1].strip()
        m = re.fullmatch(r"'([^']*)'|\"([^\"]*)\"", val)
        return (m.group(1) or m.group(2)) if m else None


# A property read (`x.checked`, `document.title`) cannot become a pure
# expression. Matching a bare `\w.\w` also matches the `1.2` in a threshold,
# which rejected every expression containing a decimal -- silently dropping the
# ideal-body-weight and adjusted-body-weight formulas.
_PROPERTY = re.compile(r"[A-Za-z_]\w*\s*\.\s*[A-Za-z_]")

_EVALUABLE = {
    "abs", "round", "min", "max", "pow", "sqrt", "exp", "ln", "log", "log10",
    "floor", "ceil", "z_to_percentile", "roundToNumberOfDecimals",
    "roundToNearest5", "roundToNearest10", "roundToNearest25",
    "roundToNearest50", "roundToNearest100",
    "getOutputUnit",
}

_IDENT_READ = re.compile(r"(?<![\w.'\"])([A-Za-z_]\w*)(?![\w(])")

_KEEP_NAMES = {
    "if", "else", "and", "or", "not", "true", "false", "None", "pi", "e",
} | _EVALUABLE | PLUMBING


def _sub_outside_strings(pat: re.Pattern, repl, s: str) -> str:
    """Apply a regex replacement only outside quoted strings."""
    out: list[str] = []
    i, n = 0, len(s or "")
    while i < n:
        if s[i] in "'\"":
            q = s[i]
            j = i + 1
            while j < n:
                if s[j] == "\\":
                    j += 2
                    continue
                if s[j] == q:
                    j += 1
                    break
                j += 1
            out.append(s[i:j])
            i = j
            continue
        nxt = n
        for q in "'\"":
            k = s.find(q, i)
            if 0 <= k < nxt:
                nxt = k
        out.append(pat.sub(repl, s[i:nxt]))
        i = nxt
    return "".join(out)


def camel_to_snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"__+", "_", s).lower().strip("_")


def parse(js_raw: str) -> EngineBScript:
    js = normalize_text(js_raw)
    # `document.getElementById(ID_X).value` is the same field read as `get(ID_X)`;
    # a few scripts use the long form and the field then looks unread. Norepi's
    # concentration selector was lost exactly this way.
    js = re.sub(
        r"document\s*\.\s*getElementById\s*\(\s*([A-Za-z_]\w*)\s*\)\s*\.\s*value",
        r"get(\1)", js,
    )
    out = EngineBScript()
    out.has_tests = "function test(" in js

    for m in ID_CONST.finditer(js):
        out.id_map[m.group(1)] = m.group(2)

    for m in BOUND_CONST.finditer(js):
        stem, kind, val, unit = m.group(1), m.group(2), m.group(3), (m.group(4) or "")
        entry = out.bounds.setdefault(stem, {})
        try:
            entry[kind.lower()] = float(val)
        except ValueError:
            continue
        unit = unit.strip().strip(";").strip()
        # a trailing comment is only a unit if it is short and not prose
        if unit and len(unit) <= 24 and not unit[0].isdigit():
            entry.setdefault("unit", unit)

    body = _slice_function(js, "calculate")
    if body is None:
        return out
    # PDF text layer sometimes drops the semicolon after `new Number(...)`,
    # gluing the next statement on: `var volume = new Number(get(ID_VOLUME))
    # if (checkInputRanges() == 0) return;` — or even eating the constructor
    # entirely (`var volume = new if(...)`). Reinstate the boundary.
    body = re.sub(r"\bnew\s+(?=if\b)", "", body)
    body = re.sub(
        r"(\bnew\s+Number\s*\([^;]{0,80}\))\s+(?=if\b)",
        r"\1; ",
        body,
    )
    body = re.sub(
        r"\b(var|let|const)\s+([A-Za-z_]\w*)\s+(?=if\b)",
        r"\1 \2; ",
        body,
    )
    body_nc = jsblock.drop_lhs_call_prose(_strip_comments(body))
    out.raw_body = body_nc
    module_nc = _strip_comments(js)
    out.helpers = simple_functions(module_nc)

    stmts, body_nc = jsblock.parse_block_repaired(body_nc)

    # ---- which ids are written, and so are outputs rather than inputs
    written: set[str] = set()

    def collect_writes(sts: list) -> None:
        for st in sts:
            if isinstance(st, jsblock.If):
                collect_writes(st.then)
                collect_writes(st.els)
            elif isinstance(st, jsblock.Call):
                if st.fn in ("set", "setNumber", "setText") and st.args:
                    idc = st.args[0]
                    if _is_element_id(idc, out.id_map) and not (
                        len(st.args) > 1 and st.args[1].strip() in ("''", '""')
                    ):
                        written.add(idc)

    collect_writes(stmts)

    read_ids: list[str] = []
    for m in READ.finditer(js):
        name = m.group(1)
        # Only constants that actually hold an element id are fields.
        if name in out.id_map and name not in read_ids:
            read_ids.append(name)

    # A constant that names a DOM element is a field, not a value. Excluding
    # only the `ID_`-prefixed ones was a naming convention, not a rule:
    # Treprostinil calls its elements FIELD_*/OUTPUT_*/UNITS_*, so every field
    # reference in its formulas was replaced by the element's id string. The
    # test is how the script uses the constant, not what it is called -- and it
    # has to stay narrow, because the same file's YES/IV constants are genuine
    # comparison values that the expressions do need.
    element_ids = element_id_constants(js)
    consts = {k: v for k, v in jsblock.string_constants(js).items()
              if not k.startswith("ID_") and k not in element_ids}

    # A unit `<select>` is read exactly like a field but is not one; identifying
    # it here keeps it from becoming a phantom input and hands its options to
    # the field it converts.
    unit_opts_by_local, selector_ids, handled_conds = _unit_selectors(
        stmts, out.id_map, consts)
    out.unit_selector_ids = selector_ids
    # An id that the form READS is a field, even if the code also writes to it:
    # calculators routinely reset or normalise a dropdown they read from. Only
    # ids that are exclusively written are outputs. Treating any write as
    # disqualifying dropped Norepinephrine's concentration selector, leaving its
    # own formula referencing a value no input supplied.
    # An id the form READS is a field even when the code also writes to it:
    # calculators routinely clear or normalise a control they read from.
    selector_ids |= display_only_ids(body_nc)
    out.unit_selector_ids = selector_ids
    # An id `calculate()` WRITES and never reads is an output, whatever some
    # other function does with it. Ethanol's print helper reads the maintenance
    # dose back off the page to copy it, and that one `get` turned the result
    # row into a required input -- the form refused to calculate until the
    # clinician typed in the answer.
    _read_in_body = {m.group(1) for m in READ.finditer(body_nc or "")}
    out.inputs = [
        i for i in read_ids
        if i not in selector_ids and not (i in written and i not in _read_in_body)
    ]

    # `var weight = new Number(get(ID_WEIGHT))` -- the local IS that input.
    alias: dict[str, str] = {}
    for idc in out.inputs:
        local = local_name_for_read(body_nc, idc)
        if local:
            alias[local] = id_to_key(idc, out.id_map.get(idc))
    out.unit_options = {
        alias.get(local, camel_to_snake(local)): opts
        for local, opts in unit_opts_by_local.items()
    }

    out.range_bounds = range_check_bounds(js)

    rv = _WkResolver(out, alias, consts, predicate_functions(js))
    resolver = jsblock.Resolver(
        expr=rv.expr, cond=rv.cond, key=rv.key,
        output_of=rv.output_of, note_of=rv.note_of,
        inputs=rv.input_keys, handled=handled_conds,
        declared=declared_names(js),
        unread_inputs={
            id_to_key(i, out.id_map.get(i))
            for i in written
            if i not in {m.group(1) for m in READ.finditer(body_nc or "")}
        },
    )
    result = jsblock.execute(stmts, resolver)

    out.steps = list(result.steps)
    out.notes.extend(result.notes)
    out.unresolved = result.unresolved
    for o in result.outputs:
        out.outputs.append({
            "id_const": next(
                (i for i in written
                 if id_to_key(i, out.id_map.get(i)) == o["key"]), o["key"]),
            "key": o["key"], "expr": o["expr"], "decimals": o["decimals"],
        })

    # The literals a field is compared against are its dropdown's real values.
    out.select_options = {
        alias.get(n, camel_to_snake(n)): vals
        for n, vals in jsblock.collect_string_compares(
            stmts, set(alias) | rv.input_keys, consts).items()
    }

    # A result that can come out as a word ('Conventional') is not a number, and
    # must not be validated or rendered as one.
    for o in out.outputs:
        if re.search(r"'[^']*'|\"[^\"]*\"", o["expr"] or ""):
            out.text_outputs.add(o["key"])

    # Values assigned outside calculate() -- Engine B factors some arithmetic
    # into setup helpers -- for anything the expressions still leave undefined.
    known = {n for n, _ in out.steps} | rv.input_keys
    referenced: set[str] = set()
    for _, e in out.steps:
        referenced |= set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?![\w(])", e))
    for o in out.outputs:
        referenced |= set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?![\w(])",
                                     o["expr"] or ""))
    for m in LOCAL.finditer(module_nc):
        name, expr = m.group(1), m.group(2).strip()
        key = camel_to_snake(name)
        if key in known or key not in referenced or READ.search(expr):
            continue
        if not re.search(r"[\d)\w]\s*[-+*/]\s*[\w(\d]", expr):
            continue
        if "[" in expr or re.search(r"\bget\s*\(", expr):
            continue
        out.steps.insert(0, (key, js_expr_to_canonical(_clean_expr(expr))))
        known.add(key)

    return out


# Engine B ships tiny unit helpers instead of an affine unit model. Inlining
# them keeps the expression pure arithmetic rather than a call the evaluator
# would have to whitelist.
# `new Array(0.5,1.5,2.5,...)` -- the dose ladder of a titration table.
DOSE_ARRAY = re.compile(
    r"var\s+(TABLE_[A-Z0-9_]+)\s*=\s*new\s*Array\s*\(([^)]*)\)?", re.S
)


ROW_LOOP = re.compile(r"for\s*\(\s*\w+\s+in\s+(TABLE_[A-Z0-9_]+)\s*\)\s*\{")


def parse_row_body(js: str, ladder_name: str) -> list[tuple[str, str]]:
    """The per-row arithmetic a titration table computes inside its loop.

    A titration calculator emits one row per dose:

        for (x in TABLE_VALUES_COL_1) {
            var value = (TABLE_VALUES_COL_1[x] * weight * 60) / (1000 * conc);
            tableValuesCol2[x] = roundToNumberOfDecimals(value, 2); }

    The loop is a construct the statement parser steps over, so the row formula
    -- the only thing the table actually computes -- was never captured, and the
    output referencing `value` had nothing to resolve.
    """
    out: list[tuple[str, str]] = []
    for m in ROW_LOOP.finditer(js or ""):
        if m.group(1) != ladder_name:
            continue
        body = jsblock._balanced_after(js, m.end() - 1)
        for am in re.finditer(
            r"(?:var|let|const)?\s*\b([a-z_]\w*)\s*=\s*([^;{}]{1,220}?)\s*;",
            jsblock.strip_comments(body), re.I,
        ):
            name, expr = am.group(1), am.group(2).strip()
            if "[" in name or not re.search(r"[-+*/]", expr):
                continue
            out.append((name, expr))
        break
    return out


def parse_dose_ladder(js: str) -> Optional[dict]:
    """Extract a titration table's dose column.

    These calculators emit one row per dose rather than a single result, so they
    are a different shape entirely -- a table generator, not a scalar formula.

    The printed array is frequently cut off by the page boundary, so the values
    that survive are reported along with the arithmetic step they follow and the
    DOSE_MAX the module declares. The continuation is described, never invented:
    silently extending a clinical dose ladder is not a safe default.
    """
    m = DOSE_ARRAY.search(js)
    if not m:
        return None
    raw = m.group(2)
    vals: list[float] = []
    for tok in raw.split(","):
        tok = tok.strip()
        mm = re.match(r"^-?\d+(?:\.\d+)?$", tok)
        if mm:
            vals.append(float(tok))
        elif vals:
            break                      # hit the truncation point
    if len(vals) < 2:
        return None
    steps = {round(b - a, 6) for a, b in zip(vals, vals[1:])}
    step = steps.pop() if len(steps) == 1 else None
    return {
        "name": m.group(1),
        "values": vals,
        "count": len(vals),
        "step": step,
        "truncated_in_pdf": ")" not in raw,
    }


UNIT_HELPERS = {
    "lbs_to_kg": "({0} * 0.45359237)",
    "kg_to_lbs": "({0} / 0.45359237)",
    "inches_to_cm": "({0} * 2.54)",
    "cm_to_inches": "({0} / 2.54)",
    "g_to_mg": "({0} * 1000)",
    "mg_to_g": "({0} / 1000)",
    # takes (value, decimals); `round` accepts the same pair
    "roundToNumberOfDecimals": "round({0})",
    "roundToNearest10": "(round(({0}) / 10) * 10)",
    "mL_to_tsp": "(({0}) / 4.92892159375)",
    "tsp_to_mL": "(({0}) * 4.92892159375)",
    "in_to_cm": "(({0}) * 2.54)",
    "cm_to_in": "(({0}) / 2.54)",
    "mg_per_min_to_g_per_hr": "(({0}) * 0.06)",
    "g_per_hr_to_mg_per_min": "(({0}) / 0.06)",
    # More of the shared library. These live in an include the PDFs do not
    # carry, so a call to one was a call the extractor could not resolve --
    # and the branch containing it was dropped rather than mistranslated.
    # Each is a fixed conversion, not a clinical judgement: creatinine's molar
    # mass is 113.12 g/mol, so 1 mg/dL is 88.4 micromol/L.
    "L_to_mL": "(({0}) * 1000)",
    "mL_to_L": "(({0}) / 1000)",
    "mcg_to_mg": "(({0}) / 1000)",
    "mg_to_mcg": "(({0}) * 1000)",
    "days_to_hr": "(({0}) * 24)",
    "hr_to_days": "(({0}) / 24)",
    "hr_to_min": "(({0}) * 60)",
    "min_to_hr": "(({0}) / 60)",
    "creatine_umol_per_L_to_mg_per_dL": "(({0}) / 88.4)",
    "creatine_mg_per_dL_to_umol_per_L": "(({0}) * 88.4)",
    "mg_to_ng": "(({0}) * 1000000)",
    "ng_to_mg": "(({0}) / 1000000)",
    "mcg_to_ng": "(({0}) * 1000)",
    "ng_to_mcg": "(({0}) / 1000)",
    "g_to_mcg": "(({0}) * 1000000)",
    "mcg_to_g": "(({0}) / 1000000)",
}


def _inline_helpers(expr: str) -> str:
    for name, tmpl in UNIT_HELPERS.items():
        while True:
            m = re.search(r"\b" + re.escape(name) + r"\s*\(", expr)
            if not m:
                break
            start = m.end() - 1
            depth = 0
            for j in range(start, len(expr)):
                if expr[j] == "(":
                    depth += 1
                elif expr[j] == ")":
                    depth -= 1
                    if depth == 0:
                        inner = expr[start + 1: j]
                        expr = expr[: m.start()] + tmpl.format(inner) + expr[j + 1:]
                        break
            else:
                break
    return expr


SIMPLE_FN = re.compile(
    r"function\s+(\w+)\s*\(([^)]*)\)\s*\{\s*return\s+([^;{}]+?)\s*;?\s*\}", re.S
)


ANY_FN = re.compile(r"function\s+(\w+)\s*\(([^)]*)\)\s*\{")


# A quoted literal is not an identifier. Scanning raw text for free names read
# `concentration !== 'other'` as a dependency on a variable called `other`, and
# the helper carrying it was discarded as unresolvable.
_STR_LIT = re.compile(r"'[^']*'|\"[^\"]*\"")


def _free_names(expr: str) -> set[str]:
    """Identifiers an expression reads, ignoring strings and call targets."""
    return set(re.findall(r"(?<![\w.])([a-z_]\w*)(?![\w(])",
                          _STR_LIT.sub("''", expr), re.I))



def _norm_eq(expr: str) -> str:
    """JS strict comparisons are not Python. `!==` reaches the evaluator as a
    syntax error, which silently discards the step that carries it."""
    expr = re.sub(r"(?<![=!<>])===?(?!=)", "==", expr)
    return re.sub(r"!==?", "!=", expr)



# `x = expr;` or `if (cond) { x = expr; }` / `if (cond) x = expr;` -- the
# optional guard is captured so a conditional write is not read as a later one.
_LOCAL_WRITE = re.compile(
    r"(?:if\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*\{?\s*)?"
    r"(?:var|let|const)?\s*\b([a-z_]\w*)\s*=\s*([^;{}]+?)\s*;",
    re.I,
)


def simple_functions(js: str) -> dict[str, tuple[list[str], str]]:
    """Helpers that reduce to a single arithmetic expression, ready to inline.

    Engine B factors arithmetic into helpers, and most are not one-liners:

        function getConcentrationRate(dose, weight, concentrationValue) {
          let result = ((dose * weight) * 60) / (concentrationValue * 1000);
          result = roundToNumberOfDecimals(result, 2);
          return result;
        }

    So the returned name is traced back through the body's assignments until it
    resolves to an expression over the parameters alone. An output that calls
    one of these is otherwise a call the evaluator must refuse.
    """
    out: dict[str, tuple[list[str], str]] = {}
    for m in ANY_FN.finditer(js):
        name, params = m.group(1), m.group(2)
        body = _slice_function(js[m.start():], re.escape(name))
        if body is None:
            continue
        if re.search(r"\b(get|set|setHTML|setNumber|document|alert|clear)\s*\(", body):
            continue
        rm = re.search(r"\breturn\s+([^;{}]+?)\s*;", body)
        if not rm:
            continue
        args = [a.strip() for a in params.split(",") if a.strip()]

        # Local assignments in order, folding each into what came before, so
        # `result = f(result)` does not become a circular leftover.
        #
        # A conditional reassignment is a branch, not a later value:
        #
        #     var crcl = (140 - age) * weight / (72 * srcr);
        #     if (gender == FEMALE) { crcl = crcl * 0.85; }
        #
        # Read as two writes with the last winning, the 0.85 applies to every
        # patient -- which is how the aminoglycoside dose came to score every
        # man as a woman, 15% low. It has to be folded as the condition it is.
        locals_: dict[str, str] = {}
        for am in _LOCAL_WRITE.finditer(body):
            cond, lhs, rhs = am.group(1), am.group(2), am.group(3).strip()
            if lhs in ("return",):
                continue
            for n, e in list(locals_.items()):
                rhs = re.sub(
                    r"(?<![\w.])" + re.escape(n) + r"(?![\w])", f"({e})", rhs
                )
            if cond:
                prior = locals_.get(lhs, lhs)
                for n, e in list(locals_.items()):
                    cond = re.sub(
                        r"(?<![\w.])" + re.escape(n) + r"(?![\w])", f"({e})", cond
                    )
                locals_[lhs] = f"(({rhs}) if ({cond.strip()}) else ({prior}))"
            else:
                locals_[lhs] = rhs

        expr = rm.group(1).strip()
        for _ in range(8):
            names = _free_names(expr)
            todo = [n for n in names if n in locals_ and n not in args]
            if not todo:
                break
            for n in todo:
                expr = re.sub(r"(?<![\w.])" + re.escape(n) + r"(?![\w])",
                              f"({locals_[n]})", expr)

        # A guard compares against a module constant -- `gender == FEMALE` --
        # and the constant is not an argument, so the helper looked like it
        # depended on module state and was discarded whole. Its value is known;
        # substituting it keeps the branch instead of losing the function.
        for cname, cval in jsblock.string_constants(js).items():
            expr = re.sub(
                r"(?<![\w.])" + re.escape(cname) + r"(?![\w])", repr(cval), expr)

        leftover = _free_names(expr)
        # A folded branch reads as `A if (cond) else B`; `if` and `else` are
        # the language, not module state.
        if leftover - set(args) - set(UNIT_HELPERS) - _EVALUABLE - {"if", "else"}:
            continue                            # still depends on module state
        out[name] = (args, _norm_eq(_inline_helpers(expr)))

    # if (cond) return A; else return B;  -- IBW Devine/Robinson, etc.
    for m in ANY_FN.finditer(js):
        name = m.group(1)
        if name in out:
            continue
        params = m.group(2)
        body = _slice_function(js[m.start():], re.escape(name))
        if body is None:
            continue
        if re.search(r"\b(get|set|setHTML|setNumber|document|alert|clear)\s*\(", body):
            continue
        args = [a.strip() for a in params.split(",") if a.strip()]
        m2 = re.search(
            r"if\s*\(([^)]+)\)\s*\{?\s*return\s+([^;]+);\s*\}?"
            r"\s*else\s*\{?\s*return\s+([^;]+);",
            body, re.S,
        )
        if not m2:
            continue
        cond, a, b = (m2.group(1).strip(), m2.group(2).strip(), m2.group(3).strip())
        expr = f"({_inline_helpers(a)}) if ({cond}) else ({_inline_helpers(b)})"
        leftover = _free_names(expr)
        if leftover - set(args) - set(UNIT_HELPERS) - _EVALUABLE - {"if", "else"}:
            continue
        out[name] = (args, _norm_eq(expr))

    # Devine/Robinson: clamp height, then if (gender == MALE) ibw = A else B; return ibw
    for m in ANY_FN.finditer(js):
        name = m.group(1)
        if name in out:
            continue
        params = m.group(2)
        body = _slice_function(js[m.start():], re.escape(name))
        if body is None or re.search(r"\b(get|set|setHTML|setNumber|document)\s*\(", body):
            continue
        args = [a.strip() for a in params.split(",") if a.strip()]
        m3 = re.search(
            r"if\s*\(\s*(\w+)\s*==\s*(\w+)\s*\)\s*\{[^}]*?\bibw\s*=\s*([^;]+);"
            r"[^}]*\}\s*else\s*\{[^}]*?\bibw\s*=\s*([^;]+);",
            body, re.S,
        )
        if not m3:
            continue
        gender, male, a, b = m3.groups()
        a, b = _clean_expr(a), _clean_expr(b)
        # `if (h < 60) h = 60` is a floor, not a clinical guess
        if re.search(r"if\s*\(\s*h\s*<\s*60\s*\)\s*h\s*=\s*60", body):
            a = re.sub(r"(?<![\w.])h(?![\w])", "max(height, 60)", a)
            b = re.sub(r"(?<![\w.])h(?![\w])", "max(height, 60)", b)
        expr = f"({a}) if ({gender} == {male}) else ({b})"
        for cname, cval in jsblock.string_constants(js).items():
            expr = re.sub(
                r"(?<![\w.])" + re.escape(cname) + r"(?![\w])", repr(cval), expr
            )
        leftover: set[str] = set()
        def _collect(m: re.Match) -> str:
            leftover.add(m.group(1))
            return m.group(0)
        _sub_outside_strings(
            re.compile(r"(?<![\w.'\"])([A-Za-z_]\w*)(?![\w(])"), _collect, expr
        )
        if leftover - set(args) - set(UNIT_HELPERS) - _EVALUABLE - {"max", "height", "if", "else"}:
            continue
        out[name] = (args, _norm_eq(expr))
    return out


def inline_calls(expr: str, fns: dict[str, tuple[list[str], str]], depth: int = 0) -> str:
    """Substitute helper calls with their bodies, arguments bound positionally."""
    if depth > 4 or not expr:
        return expr
    for name, (params, body) in fns.items():
        while True:
            m = re.search(r"(?<![\w.])" + re.escape(name) + r"\s*\(", expr)
            if not m:
                break
            start = m.end() - 1
            depth_p, args, cur = 0, [], ""
            for j in range(start, len(expr)):
                ch = expr[j]
                if ch == "(":
                    depth_p += 1
                    if depth_p == 1:
                        continue
                elif ch == ")":
                    depth_p -= 1
                    if depth_p == 0:
                        args.append(cur.strip())
                        break
                elif ch == "," and depth_p == 1:
                    args.append(cur.strip())
                    cur = ""
                    continue
                cur += ch
            else:
                return expr
            args = [a for a in args if a != ""]
            sub = body
            for pname, aval in zip(params, args):
                sub = re.sub(r"(?<![\w.])" + re.escape(pname) + r"(?![\w])",
                             f"({aval})", sub)
            expr = expr[: m.start()] + f"({sub})" + expr[j + 1:]
    return inline_calls(expr, fns, depth + 1)


def _clean_expr(expr: str) -> str:
    expr = re.sub(r"\bnew\s+Number\s*\(", "(", expr)
    expr = re.sub(r"\bparseFloat\s*\(", "(", expr)
    expr = re.sub(r"\bNumber\s*\(", "(", expr)
    from .jsparse import balance_parens
    return balance_parens(_inline_helpers(expr).strip())


def local_name_for_read(body: str, id_const: str) -> Optional[str]:
    """Find the local variable a field is read into: `var dose = ...get(ID_DOSE)...`."""
    m = re.search(
        r"(?:var|let|const)\s+([a-z_]\w*)\s*=\s*[^;]*get\s*\(\s*"
        + re.escape(id_const) + r"\s*\)",
        body, re.I,
    )
    return m.group(1) if m else None


# `if (auc < AUC_MIN) { msg += getInputBelowMinimumMessage("Desired AUC", AUC_MIN, ...) }`
_RANGE_GUARD = re.compile(
    r"if\s*\(\s*([A-Za-z_]\w*)\s*(<=?|>=?)\s*([A-Z][A-Z0-9_]*|-?\d*\.?\d+)\s*\)"
    r"(?P<tail>[^{}]*(?:\{[^{}]*\})?)"
)
_RANGE_LABEL = re.compile(
    r"get(?:Input(?:Below|Above)\w*|Range)Message\s*\(\s*[\"']([^\"']+)[\"']")
_NUM_CONST = re.compile(
    r"(?:var|let|const)\s+([A-Z][A-Z0-9_]*)\s*=\s*(-?\d*\.?\d+)")


def range_check_bounds(js: str) -> dict[str, dict]:
    """Limits a calculator enforces in `checkInputRanges()` rather than `minMaxCheck`.

    Calvert refuses an AUC outside 1.5-7.5 and a GFR outside 5-150, but it says
    so in prose it builds for an alert:

        var auc = get(ID_AUC);
        if (auc < AUC_MIN) msg += getInputBelowMinimumMessage("Desired AUC", AUC_MIN, ...);

    Only `minMaxCheck` was being read, so those four limits reached nobody: the
    form accepted an AUC of 40. The message also carries the document's own name
    for the field -- "Desired AUC", not `calvert_auc` -- which is the only place
    some forms state it, their printed input table having failed to extract.

    Returns `{id_const: {"min": .., "max": .., "label": ..}}`.
    """
    fn = _slice_function(js or "", "checkInputRanges")
    if not fn:
        return {}
    values = {n: float(v) for n, v in _NUM_CONST.findall(js or "")}
    reads = {m.group(1): m.group(2)
             for m in re.finditer(
                 r"(?:var|let|const)\s+([A-Za-z_]\w*)\s*=\s*"
                 r"(?:(?:new\s+)?Number\s*\(\s*|parseFloat\s*\(\s*"
                 r"|parseInt\s*\(\s*)?"
                 r"get\s*\(\s*([A-Z][A-Z0-9_]*)\s*\)", fn)}
    out: dict[str, dict] = {}
    for m in _RANGE_GUARD.finditer(fn):
        local, op, const = m.group(1), m.group(2), m.group(3)
        idc = reads.get(local)
        if idc is None:
            continue
        if const in values:
            limit = values[const]
        else:
            try:
                limit = float(const)          # `if (weight <= 0)` -- a literal
            except ValueError:
                continue
        slot = out.setdefault(idc, {})
        # `< MIN` rejects anything below it, so MIN is allowed; `<= 0` rejects
        # zero itself, so the floor is exclusive.
        if op.startswith("<"):
            slot["min"] = limit
            if op == "<=":
                slot["exclusive_min"] = True
        else:
            slot["max"] = limit
            if op == ">=":
                slot["exclusive_max"] = True
        lm = _RANGE_LABEL.search(m.group("tail"))
        if lm and not slot.get("label"):
            slot["label"] = lm.group(1).strip()
    return out


def match_bounds(key: str, bounds: dict[str, dict]) -> dict:
    """Match a field key to its `<STEM>_MIN/_MAX` constants."""
    k = key.replace("_", "").lower()
    for stem, b in bounds.items():
        if stem.replace("_", "").lower() == k:
            return b
    # The LONGEST overlap wins. Bivalirudin declares DOSE_MIN/MAX for its bolus
    # and INFUSION_MIN/MAX for its infusion; matching on the first stem merely
    # contained in the key gave `infusion_dose` the bolus's 0.1-1 limits, so the
    # form refused every infusion the drug is actually licensed at.
    best, best_len = {}, 0
    for stem, b in bounds.items():
        s = stem.replace("_", "").lower()
        if s and (s in k or k in s) and min(len(s), len(k)) >= 4 and len(s) > best_len:
            best, best_len = b, len(s)
    return best


# `document.getElementById('K_AGE_M_ROW').style.display = 'none'`
_ROW_HIDE = re.compile(
    r"getElementById\s*\(\s*['\"]?([\w$]+)['\"]?\s*\)\s*\.\s*style\s*\."
    r"(?:display\s*=\s*(['\"])(\w*)\2|removeProperty\s*\(\s*['\"]display['\"]\s*\))"
)
# `const gender = document.getElementById('ID_GENDER').value; if (gender === 'FEMALE') {`
_TOGGLE_FN = re.compile(r"function\s+(\w+)\s*\(\s*\)\s*\{")


def visibility_rules(js: str, id_map: dict) -> dict[str, dict]:
    """Which control decides whether each field is on the form at all.

    Several calculators ask for a coefficient that only applies to one sex, and
    hide the other row as the selector changes. Modelled as two always-present
    fields, the selector looked dead -- both were filled, so it changed nothing
    -- and the form asked for a number the patient could not have. This records
    the rule the page enforces, so the field can be hidden and the selector
    treated as the choice it is.

    Returns `{row_id: {"field": control_key, "equals": [values]}}`.
    """
    if not js:
        return {}
    out: dict[str, dict] = {}
    for m in _TOGGLE_FN.finditer(js):
        body = jsblock._balanced_after(js, m.end() - 1)
        if ".style." not in body or "getElementById" not in body:
            continue
        ctrl = re.search(
            r"getElementById\s*\(\s*['\"]?([\w$]+)['\"]?\s*\)\s*\.\s*value", body)
        if not ctrl:
            continue
        control = ctrl.group(1)
        # Each `if (x === 'V') { ... }` names the rows visible under that value.
        for cond in re.finditer(r"if\s*\(([^)]*)\)\s*\{", body):
            branch = jsblock._balanced_after(body, cond.end() - 1)
            lit = re.search(r"===?\s*['\"]([^'\"]+)['\"]", cond.group(1))
            if not lit or ".style." not in branch:
                continue
            value = lit.group(1)
            for hit in _ROW_HIDE.finditer(branch):
                row, shown = hit.group(1), hit.group(3)
                # `= 'none'` hides; `= ''` or removeProperty shows.
                visible = shown != "none"
                entry = out.setdefault(
                    row, {"field": control, "equals": [], "hidden_for": []})
                bucket = "equals" if visible else "hidden_for"
                if value not in entry[bucket]:
                    entry[bucket].append(value)
    key_of = {k: v for k, v in (id_map or {}).items()}
    return {
        row: {**rule, "control_const": rule["field"],
              "control_dom": key_of.get(rule["field"], rule["field"])}
        for row, rule in out.items()
        if rule["equals"] or rule["hidden_for"]
    }
