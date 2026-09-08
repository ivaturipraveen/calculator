"""Fill in everything the core parsers do not carry, and report what is missing.

The goal is a spec that needs no other source: every field, its units and their
conversion factors, every dropdown option, every help/tooltip message, and the
supporting prose. No single source has all of it --

    PDF   formulas, bounds, defaults, help/alert text, references, notes
    HTML  unit option lists + factors, select options, categories, LMS/dose data

-- so this merges both and then grades the result, because a spec that is
silently incomplete is worse than one that says which part is missing.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .identity import squash

# `alert('Weight must be greater than 0 kg')` / alert("...")
ALERT = re.compile(r"alert\s*\(\s*(['\"])(.*?)(?<!\\)\1", re.S)
# `showXHelp()` style handlers name the field they document
HELP_FN = re.compile(r"function\s+show(\w+?)Help\s*\(")

# Boilerplate that is not field-specific guidance.
GENERIC_ALERTS = (
    "improperly formatted",
    "you may only input",
    "please select an option",
    "must be filled in",
)


def extract_help_messages(js: str) -> dict[str, list[str]]:
    """Recover per-field help text from the calculator's own alert() strings.

    These are the messages a clinician sees from the "?" affordance and on a
    range violation, so they are the authoritative tooltip copy -- far better
    than paraphrasing the bounds ourselves.
    """
    out: dict[str, list[str]] = {}
    for m in ALERT.finditer(js):
        text = re.sub(r"\s+", " ", m.group(2)).strip()
        # The PDF text layer keeps the JS escapes, so a message arrives as
        # "...is 0 yr.\\ If you are specifying..." -- unescape and drop the
        # stray line-continuation backslashes.
        text = text.replace("\\'", "'").replace('\\"', '"')
        text = re.sub(r"\\+\s*", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(text) < 8:
            continue
        low = text.lower()
        if any(g in low for g in GENERIC_ALERTS):
            out.setdefault("_general", []).append(text)
            continue
        # "The minimum value for Patient Temp is 0 degC." -> Patient Temp
        fm = re.search(
            r"(?:value for|for)\s+(.+?)\s+is\b", text, re.I
        ) or re.search(r"^(.+?)\s+must\b", text, re.I)
        key = squash(fm.group(1)) if fm else "_general"
        out.setdefault(key or "_general", []).append(text)
    for k in out:
        seen, uniq = set(), []
        for t in out[k]:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        out[k] = uniq
    return out


# `function showDoseHelp() { alert(getMustRangeMessage("Dose", DOSE_MIN,
#                                   DOSE_MAX, CURRENT_MILLIGRAMS_PER_MINUTE)); }`
HELP_RANGE = re.compile(
    r"function\s+show(\w+?)Help\s*\([^)]*\)\s*\{[^}]*?"
    r"get(\w*?)Message\s*\(\s*(['\"])(.*?)\3\s*(?:,\s*([^)]*))?\)",
    re.S,
)
STR_CONST = re.compile(r"var\s+([A-Z][A-Z0-9_]*)\s*=\s*(['\"])(.*?)\2")
NUM_CONST = re.compile(r"var\s+([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)")



_SHOW_HELP_FN = re.compile(r"function\s+show\w*Help\s*\([^)]*\)\s*\{")
_MESSAGE_CALL = re.compile(
    r"get\w*Message\s*\(\s*(['\"])(.*?)\1\s*(?:,\s*([^)]*))?\)", re.S)


def _help_range_calls(js: str) -> list[tuple[str, str]]:
    """Every `get*Message("Label", MIN, MAX, UNITS)` inside a `show*Help()`.

    The body is taken by brace balance, not by "everything up to the next `}`":
    these helpers convert the bounds for the chosen unit first, so the message
    call sits after an `if` block and a non-greedy scan never reached it. That
    is why sixteen calculators defined tooltips that were never attached.
    """
    from .jsblock import _balanced_after

    out: list[tuple[str, str]] = []
    for fn in _SHOW_HELP_FN.finditer(js):
        body = _balanced_after(js, fn.end() - 1)
        # `var minimum = HEIGHT_MIN;` -- the call passes the local, so without
        # tracing it back the bounds are lost and the tooltip reads "must be a
        # valid value", which tells the reader nothing.
        aliases = dict(
            re.findall(r"(?:var|let|const)\s+(\w+)\s*=\s*([A-Z][A-Z0-9_]*)\s*;", body)
        )
        for call in _MESSAGE_CALL.finditer(body):
            args = call.group(3) or ""
            for local, const in aliases.items():
                args = re.sub(r"(?<![\w.])" + re.escape(local) + r"(?![\w])",
                              const, args)
            out.append((call.group(2).strip(), args))
    # Some scripts call the builder outside a named helper as well.
    for call in _MESSAGE_CALL.finditer(js):
        pair = (call.group(2).strip(), call.group(3) or "")
        if pair not in out and not any(p[0] == pair[0] for p in out):
            out.append(pair)
    return out


def help_from_range_functions(js: str) -> dict[str, dict]:
    """Recover Engine-B tooltips, and the bounds they quote.

    Engine B has no minMaxCheck() alert text to mine. Its "?" affordance calls a
    message builder with the field label and its MIN/MAX constants, so parsing
    those calls yields the tooltip copy AND a second, independent reading of the
    numeric bounds -- the only place several of these calculators state them.
    """
    strings = {m.group(1): m.group(3) for m in STR_CONST.finditer(js)}
    numbers = {m.group(1): float(m.group(2)) for m in NUM_CONST.finditer(js)}

    out: dict[str, dict] = {}
    for m in _help_range_calls(js):
        label, arg_text = m
        args = [a.strip() for a in (arg_text or "").split(",") if a.strip()]
        lo = hi = unit = None
        for a in args:
            if a in numbers:
                if a.endswith("_MIN") and lo is None:
                    lo = numbers[a]
                elif a.endswith("_MAX") and hi is None:
                    hi = numbers[a]
                elif lo is None:
                    lo = numbers[a]
                elif hi is None:
                    hi = numbers[a]
            elif a in strings and unit is None:
                unit = strings[a]
            elif a.startswith(("'", '"')) and unit is None:
                unit = a.strip("'\"")
        parts = [f"{label} must be"]
        if lo is not None and hi is not None:
            parts.append(f"between {_fmt(lo)} and {_fmt(hi)}")
        elif lo is not None:
            parts.append(f"at least {_fmt(lo)}")
        elif hi is not None:
            parts.append(f"at most {_fmt(hi)}")
        else:
            parts.append("a valid value")
        if unit:
            parts.append(unit)
        out[squash(label)] = {
            "label": label, "min": lo, "max": hi, "unit": unit,
            "message": " ".join(parts).strip() + ".",
        }
    return out


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


def attach_range_help(inputs: list[dict], ranges: dict[str, dict]) -> dict[str, int]:
    """Apply recovered tooltips and fill any bound the parser did not already have."""
    stats = {"help": 0, "bounds": 0}
    for inp in inputs:
        for c in (squash(inp.get("label") or ""), squash(inp["key"])):
            r = ranges.get(c)
            if not r:
                continue
            if not inp.get("help"):
                inp["help"] = [r["message"]]
                stats["help"] += 1
            cons = inp.setdefault("constraints", {})
            if r["min"] is not None and "min" not in cons:
                cons["min"] = r["min"]
                stats["bounds"] += 1
            if r["max"] is not None and "max" not in cons:
                cons["max"] = r["max"]
                stats["bounds"] += 1
            if r["unit"] and not inp.get("base_unit"):
                inp["base_unit"] = r["unit"]
            break
    return stats


def attach_help(inputs: list[dict], helps: dict[str, list[str]]) -> int:
    """Attach help text to the field it describes. Returns how many matched.

    The alert names the quantity, which is rarely the field's name letter for
    letter: "Heart rate" and "RR interval" are two modes of one Heart Rate
    Measure field, and "For this patient population the serum albumin ..."
    buries the field name mid-sentence. An exact match found neither, so the
    tooltips the calculator itself writes went unused.
    """
    n = 0
    claimed: set[str] = set()
    for inp in inputs:
        # `<field>_unit_mode` is the entry-mode selector this module synthesises
        # for a field, not a field the alerts can be describing.
        if inp["key"].endswith("_unit_mode"):
            continue
        cands = {squash(inp.get("label") or ""), squash(inp["key"])}
        cands.discard("")
        texts: list[str] = []
        for key, msgs in helps.items():
            if key == "_general" or not key:
                continue
            if any(key == c or (len(key) >= 5 and (key in c or c in key))
                   for c in cands):
                claimed.add(key)
                texts.extend(m for m in msgs if m not in texts)
        if texts:
            inp["help"] = texts
            n += 1
    # A message no field claimed still belongs to the calculator -- QT's RR
    # interval limits describe the other entry mode of a field this cannot
    # name. Losing them would lose the only statement of those bounds.
    leftover: list[str] = []
    for key, msgs in list(helps.items()):
        if key in claimed or key == "_general":
            continue
        leftover.extend(msgs)
    if leftover:
        general = helps.setdefault("_general", [])
        general.extend(m for m in leftover if m not in general)
    return n


# --------------------------------------------------------------------------
# select / dropdown options
# --------------------------------------------------------------------------

# `X_pulldown.options[...] = new Option('label', 'value')`
NEW_OPTION = re.compile(
    r"(\w+?)_pulldown\.options\[[^\]]*\]\s*=\s*new\s+Option\s*\(\s*"
    r"(['\"])(.*?)\2\s*,\s*(['\"])(.*?)\4",
    re.S,
)


def options_from_js(js: str) -> dict[str, list[dict]]:
    """Dropdown options the script builds itself (label + numeric value)."""
    out: dict[str, list[dict]] = {}
    for m in NEW_OPTION.finditer(js):
        field, label, value = m.group(1), m.group(3), m.group(5)
        try:
            val: Any = float(value)
        except ValueError:
            val = value
        out.setdefault(squash(field), []).append(
            {"label": re.sub(r"\s+", " ", label).strip(), "value": val}
        )
    return out


def options_from_printed(sec_calc: str, labels: list[str]) -> dict[str, dict]:
    """The single option the print view shows as selected, e.g. '70-109 mmHg (0)'.

    Only the chosen option survives printing, so this is a default rather than a
    full list -- recorded as such so the gap is visible instead of implied.
    """
    if not sec_calc:
        return {}
    lines = [l.strip() for l in sec_calc.split("\n") if l.strip()]
    idx = {squash(l): l for l in labels}
    out: dict[str, dict] = {}
    for i, line in enumerate(lines):
        canon = idx.get(squash(line))
        if not canon or i + 1 >= len(lines):
            continue
        nxt = lines[i + 1]
        m = re.match(r"^(.*?)\s*\(\s*([+-]?\d+(?:\.\d+)?)\s*\)\s*$", nxt)
        if m:
            out[squash(canon)] = {
                "label": m.group(1).strip(),
                "value": float(m.group(2)),
                "is_default_only": True,
            }
    return out


def options_from_html(html_entry: Optional[dict], field_key: str) -> list[dict]:
    """Full option lists as the mockup implements them."""
    if not html_entry:
        return []
    spec = html_entry.get("spec") or {}
    for item in spec.get("items") or []:
        if squash(item.get("label", "")) == squash(field_key):
            return [{"label": o.get("label"), "value": o.get("pts")}
                    for o in item.get("options") or []]
    consts = html_entry.get("constants") or {}
    k = squash(field_key)
    for name, val in consts.items():
        if not isinstance(val, list) or not val:
            continue
        if squash(name.split("_")[-1]) in k or k in squash(name):
            if all(isinstance(x, str) for x in val):
                return [{"label": x, "value": x} for x in val]
    return []


def attach_options(
    inputs: list[dict],
    js: str,
    sec_calc: str,
    html_entry: Optional[dict],
) -> dict[str, int]:
    """Give every select-style field its option list, from whichever source has it."""
    js_opts = options_from_js(js)
    printed = options_from_printed(sec_calc, [i.get("label") or i["key"] for i in inputs])
    stats = {"from_js": 0, "from_html": 0, "default_only": 0, "missing": 0}

    for inp in inputs:
        if inp.get("widget") != "select":
            continue
        # A radio group already carries its full option list, recovered from the
        # script branches and labelled from the printed form. The printed view
        # shows only the selected option, so overwriting here would silently
        # reduce a two-way choice to a single hardcoded value -- the exact defect
        # this modelling exists to prevent.
        if inp.get("options") and inp.get("options_source", "").startswith("pdf_script"):
            stats["from_js"] += 1
            continue
        k = squash(inp["key"])
        lk = squash(inp.get("label") or "")
        opts = js_opts.get(k) or js_opts.get(lk)
        if opts:
            inp["options"] = opts
            inp["options_source"] = "pdf_script"
            stats["from_js"] += 1
            continue
        html_opts = options_from_html(html_entry, inp.get("label") or inp["key"])
        if html_opts:
            inp["options"] = html_opts
            inp["options_source"] = "html_mockup"
            stats["from_html"] += 1
            continue
        d = printed.get(k) or printed.get(lk)
        if d:
            inp["options"] = [d]
            inp["options_source"] = "pdf_printed_default_only"
            inp["options_incomplete"] = True
            stats["default_only"] += 1
        else:
            inp["options_incomplete"] = True
            stats["missing"] += 1
    return stats


# --------------------------------------------------------------------------
# engine-B "Additional Information"
# --------------------------------------------------------------------------

def parse_additional_information(sec: str) -> dict:
    """Split the Wolters Kluwer catch-all section into its real parts.

    Engine-B documents have no Equation / Notes / References headings. Instead a
    single "Additional Information" block carries the usage instructions, any
    "Note:" lines, and a "Formulas:" sub-block with the printed formula -- so
    without this those calculators look like they have no formula text and no
    notes at all, when in fact both are present.
    """
    out = {"instructions": None, "notes": [], "equation": None, "rest": None}
    if not sec:
        return out

    body = sec
    m = re.search(r"\n\s*Formulas?\s*:\s*\n", "\n" + body)
    if m:
        cut = m.start()
        eq = ("\n" + body)[m.end():].strip()
        body = ("\n" + body)[:cut].strip()
        out["equation"] = re.sub(r"\n{2,}", "\n", eq).strip() or None

    lines = [l.strip() for l in body.split("\n")]
    instructions: list[str] = []
    notes: list[str] = []
    current: Optional[list] = None
    for line in lines:
        if not line:
            continue
        if re.match(r"^Notes?\s*:", line, re.I):
            notes.append(re.sub(r"^Notes?\s*:\s*", "", line, flags=re.I))
            current = notes
        elif current is notes and line[:1].islower():
            notes[-1] += " " + line          # wrapped continuation
        elif current is notes and not re.match(r"^[A-Z]", line):
            notes[-1] += " " + line
        else:
            if current is notes and re.match(r"^[A-Z]", line) and not line.endswith(":"):
                notes[-1] += " " + line
                continue
            instructions.append(line)
            current = instructions

    out["instructions"] = " ".join(instructions).strip() or None
    out["notes"] = [re.sub(r"\s+", " ", n).strip() for n in notes if n.strip()]
    return out


# --------------------------------------------------------------------------
# dropdown options recovered from the comparisons a field is subjected to
# --------------------------------------------------------------------------

# `get(ID_GENDER) == 'Male'` / `get(ID_WEIGHT_UNITS) != KILOGRAMS`
COMPARED = re.compile(
    r"get\s*\(\s*(ID_[A-Z0-9_]+)\s*\)\s*[!=]==?\s*"
    r"(?:(['\"])([^'\"]{1,40})\2|([A-Z][A-Z0-9_]*))"
)
# `gender == 'Male'` -- a helper comparing its own parameter
LOCAL_COMPARED = re.compile(
    r"(?<![\w.])([a-z]\w{2,})\s*[!=]==?\s*"
    r"(?:(['\"])([^'\"]{1,40})\2|([A-Z][A-Z0-9_]{2,}))"
)


def options_from_comparisons(js: str, id_map: dict, str_consts: dict) -> dict[str, list]:
    """The values a dropdown is tested against ARE its option list.

    Engine B never prints its `<select>` options -- the print view collapses each
    one to whichever was selected -- but the code has to compare the field
    against every value it accepts. Collecting those comparisons recovers the
    real list, which is otherwise only obtainable from the live app.

    Returns {dom_id: [values]} in first-seen order.
    """
    out: dict[str, list] = {}
    for m in COMPARED.finditer(js or ""):
        idc = m.group(1)
        val = m.group(3) if m.group(3) is not None else str_consts.get(m.group(4))
        if val is None:
            continue
        dom = (id_map or {}).get(idc) or idc
        vals = out.setdefault(dom, [])
        if val not in vals:
            vals.append(val)

    # A field passed into a helper is compared against the PARAMETER, not the
    # get() call -- `devineMethod(get(ID_GENDER), height)` then
    # `if (gender == 'Male')`. Those comparisons are keyed by the local name and
    # matched back to the field afterwards.
    for m in LOCAL_COMPARED.finditer(js or ""):
        name = m.group(1)
        val = m.group(3) if m.group(3) is not None else str_consts.get(m.group(4))
        if val is None or name in ("i", "j", "n"):
            continue
        vals = out.setdefault("~" + name, [])
        if val not in vals:
            vals.append(val)
    return out


# A binary field is usually tested against one value only ("if gender == MALE"),
# so the code names one option and never the other. These are the standard
# pairings; anything outside them is left flagged rather than guessed.
# The escape hatch beside a list of presets: choosing it means "I will type the
# number myself", so the field is still a quantity.
SENTINEL_VALUES = {"other", "selectavalue", "select", "none", "custom"}

BINARY_PARTNER = {
    "male": "Female", "female": "Male",
    "kg": "lbs", "lbs": "kg", "lb": "kg",
    "cm": "in", "in": "cm", "inches": "cm", "centimeters": "inches",
    "yes": "No", "no": "Yes",
    "mg": "g", "ml": "L",
}


def _sibling_values(value: str, str_consts: dict) -> list[str]:
    """Values of constants that share a suffix with the one holding `value`."""
    # ID_* constants hold DOM ids, not option values, and would otherwise be
    # pulled in as siblings by their shared suffix (ID_METHOD / DEVINE_METHOD).
    str_consts = {k: v for k, v in (str_consts or {}).items()
                  if not k.startswith("ID_")}
    owner = next((k for k, v in str_consts.items() if v == value), None)
    if not owner or "_" not in owner:
        return [value]
    suffix = owner.rsplit("_", 1)[-1]
    if len(suffix) < 3:
        return [value]
    out = [v for k, v in str_consts.items()
           if k.rsplit("_", 1)[-1] == suffix and v]
    seen, uniq = set(), []
    for v in ([value] + out):
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def attach_comparison_options(
    inputs: list[dict], js: str, id_map: dict, str_consts: dict,
    printed: Optional[dict] = None,
) -> int:
    """Turn a field that is only ever compared against strings into a select."""
    found = options_from_comparisons(js, id_map, str_consts)
    if not found:
        return 0
    by_dom = {}
    for i in inputs:
        dom = (i.get("source") or {}).get("dom_id")
        if dom:
            by_dom[dom] = i
        by_dom.setdefault(i["key"], i)

    n = 0
    for dom, vals in found.items():
        local = dom.startswith("~")
        bare = dom.lstrip("~")
        inp = by_dom.get(bare)
        if inp is None:
            key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", bare).lower()
            inp = by_dom.get(key)
        if inp is None and local:
            # match a helper's parameter name back to the field it came from
            b = squash(bare)
            for cand in inputs:
                ck = squash(cand["key"])
                if b and (b in ck or ck.startswith(b)) and len(b) >= 4:
                    inp = cand
                    break
        if inp is None or inp.get("options") or not vals:
            continue

        # A numeric field with an "other" escape is compared against that
        # sentinel, but it is still a number the user types -- converting it to
        # a two-option select loses the field and puts a word into arithmetic.
        if any(squash(v) in SENTINEL_VALUES for v in vals):
            inp.setdefault("accepts_sentinel", [v for v in vals
                                                if squash(v) in SENTINEL_VALUES])
            continue
        if len(vals) == 1:
            # Constants naming the same kind of choice share a suffix
            # (DEVINE_METHOD / ROBINSON_METHOD), so the siblings of the one the
            # code tests are the rest of the option list.
            sib = _sibling_values(vals[0], str_consts)
            if len(sib) >= 2:
                vals = sib
            partner = None if len(vals) >= 2 else BINARY_PARTNER.get(squash(vals[0]))
            if len(vals) < 2:
                if not partner:
                    inp["options_incomplete"] = True
                    inp["options_known_value"] = vals[0]
                    continue
                vals = [vals[0], partner]
        # Dropdown values often encode a number ("2_5" for 2.5); keeping them as
        # strings puts text where the formula expects a quantity.
        def _val(v: str):
            t = v.replace("_", ".")
            try:
                f = float(t)
            except ValueError:
                return v
            return int(f) if f == int(f) else f

        inp["widget"] = "select"
        inp["options"] = [{"label": v, "value": _val(v)} for v in vals]
        inp["options_source"] = "pdf_script_comparisons"
        inp["constraints"] = {}
        inp["units"] = None
        inp["base_unit"] = None
        n += 1
    return n


# `if (concentration == "250mg_per_550mL") { concentration = 250/550; }`
# Anchored and bounded on both sides: the back-reference plus an unbounded gap
# made this pattern backtrack catastrophically over a large script.
CODE_TO_VALUE = re.compile(
    r"\(\s*(\w{1,40})\s*===?\s*['\"]([^'\"]{1,40})['\"]\s*\)\s*\{?[ \t\n]{0,4}"
    r"\1[ \t]{0,4}=[ \t]{0,4}([0-9][0-9./*+\- ]{0,38})[ \t]{0,4};"
)


def resolve_option_codes(inputs: list[dict], js: str) -> int:
    """Replace a dropdown's opaque code with the number it stands for.

    These calculators put a code in the `<option value>` and immediately map it
    to a quantity: "250mg_per_550mL" becomes 250/550. Leaving the code as the
    option's value means the formula divides by a string; leaving the mapping
    out of the spec means the UI cannot compute at all. The label keeps the code
    so the clinician still sees what they picked.
    """
    mapping: dict[str, dict[str, float]] = {}
    for m in CODE_TO_VALUE.finditer(js or ""):
        var, code, rhs = m.group(1), m.group(2), m.group(3).strip()
        if not re.fullmatch(r"[0-9./*+\- ]+", rhs):
            continue
        try:
            val = eval(rhs, {"__builtins__": {}}, {})   # digits and operators only
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(val, (int, float)):
            mapping.setdefault(squash(var), {})[code] = float(val)

    n = 0
    for inp in inputs:
        codes = mapping.get(squash(inp["key"])) or mapping.get(
            squash(inp.get("label") or ""))
        if not codes or not inp.get("options"):
            continue
        changed = False
        for o in inp["options"]:
            v = o.get("value")
            if isinstance(v, str) and v in codes:
                o["label"] = o.get("label") or v
                o["code"] = v
                o["value"] = codes[v]
                changed = True
        if changed:
            inp["options_source"] = (inp.get("options_source") or "") + "+code_map"
            n += 1
    return n


# --------------------------------------------------------------------------
# output units and display precision
# --------------------------------------------------------------------------

# `var CURRENT_MILLILITERS_PER_HOUR = 'mL/hour';`
UNIT_STRING_CONST = re.compile(
    r"var\s+([A-Z][A-Z0-9_]*)\s*=\s*(['\"])([^'\"]{1,24})\2"
)
UNITY = re.compile(r"^[A-Za-zµ%°]+[A-Za-z0-9µ%°/\.\^\- ]*$")


def output_units_from_js(js: str) -> list[str]:
    """Unit strings the script declares, e.g. 'mL/hour', 'mcg/kg/minute'.

    Engine-B documents have no printed Results table, so an output's unit exists
    only as a module constant. These are the labels the calculator itself shows
    beside its result.
    """
    out: list[str] = []
    for m in UNIT_STRING_CONST.finditer(js):
        val = m.group(3).strip()
        if not val or not UNITY.match(val):
            continue
        if "/" in val or val.lower() in {"kg", "lb", "lbs", "ml", "mg", "mcg", "g"}:
            if val not in out:
                out.append(val)
    return out


# "• Infusion Rate is calculated in mL/hour" / "• Dose is input in mg/minute"
FORMULA_UNIT = re.compile(
    r"[•\-\*]?\s*(.+?)\s+is\s+(?:calculated|input|expressed|entered|reported)\s+in\s+"
    # Read to the end of the clause, not to a fixed 28 characters. The cap was
    # there to stop a whole sentence being taken for a unit, but it cut
    # "g, mcg, mEq, mg, mmol, or units" to "...or un" -- text that reads as a
    # unit and is wrong. `tidy_units` decides what is a unit; this only has to
    # avoid mangling what it hands over.
    r"([^\n•]{1,70}?)\s*(?:[.;]|$)",
    re.I,
)


def units_from_formula_prose(text: str) -> dict[str, str]:
    """Field units the vendor spells out beneath its formula.

    Engine-B documents print no units in the form and no Results table, but the
    "Formulas:" block states each one explicitly. This is the vendor naming the
    unit of every input and output, so it is authoritative rather than inferred.
    """
    out: dict[str, str] = {}
    for line in (text or "").split("\n"):
        m = FORMULA_UNIT.match(line.strip())
        if not m:
            continue
        name, unit = m.group(1).strip(), m.group(2).strip().rstrip(".,;")
        if not name or not unit or len(name) > 40:
            continue
        out.setdefault(squash(name), unit)
    return out


def attach_units_from_prose(fields: list[dict], prose_units: dict[str, str],
                            attr: str = "base_unit") -> int:
    """Fill a missing unit from the prose, matching on label then key."""
    n = 0
    for f in fields:
        if f.get(attr):
            continue
        for cand in (squash(f.get("label") or ""), squash(f["key"])):
            if cand and cand in prose_units:
                f[attr] = prose_units[cand]
                f["unit_source"] = "formula_prose"
                n += 1
                break
    return n


def attach_output_units(outputs: list[dict], js: str, printed_unit: dict) -> int:
    """Give each output a unit, preferring the printed table then the script."""
    candidates = output_units_from_js(js)
    n = 0
    for i, o in enumerate(outputs):
        if o.get("base_unit"):
            continue
        hit = printed_unit.get(squash(o.get("label") or o["key"]))
        if not hit and len(candidates) == 1:
            hit = candidates[0]
        elif not hit and i < len(candidates):
            hit = candidates[i]
        if hit:
            o["base_unit"] = hit
            o["unit_source"] = "printed" if printed_unit else "script_constant"
            n += 1
    return n


DP_ASSIGN = re.compile(r"\bdp\s*=\s*(\d+)\s*;")


def decimal_precision(js: str, outputs: list[dict]) -> Optional[int]:
    """The precision the calculator displays at.

    EBMcalc assigns `dp` directly when there is no user control; Engine B passes
    it as the third argument of setNumber(). Losing it means a result renders at
    full float width instead of the clinically intended rounding.
    """
    dps = [o["decimals"] for o in outputs if isinstance(o.get("decimals"), int)]
    if dps:
        return max(set(dps), key=dps.count)
    m = DP_ASSIGN.search(js or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# completeness grading
# --------------------------------------------------------------------------

def grade(spec: dict, available: Optional[set] = None) -> dict:
    """Score how much of the source data this spec actually carries.

    `available` names the sections the source document actually has, so a spec
    is never marked down for omitting something its source never contained --
    Engine-B documents carry no Copyright or References block at all, and
    penalising them for that hides the real gaps behind noise.
    """
    avail = available if available is not None else None

    def applicable(section: str) -> bool:
        return True if avail is None else section in avail
    inputs = spec.get("inputs") or []
    content = spec.get("content") or {}
    scoring = spec.get("scoring") or {}
    renderer = spec.get("renderer")

    missing: list[str] = []

    def need(cond: bool, label: str) -> int:
        if cond:
            return 1
        missing.append(label)
        return 0

    checks: list[int] = []
    checks.append(need(bool(spec.get("title")), "title"))
    checks.append(need(bool(spec.get("category")), "category"))
    if applicable("References"):
        checks.append(need(bool(content.get("references")), "references"))
    if applicable("Disclaimer"):
        checks.append(need(bool(content.get("disclaimer")), "disclaimer"))
    if applicable("Copyright"):
        checks.append(need(bool(content.get("copyright")), "copyright"))
    if applicable("Data Input Disclaimer"):
        checks.append(need(bool(content.get("data_input_disclaimer")),
                           "data_input_disclaimer"))

    if renderer in ("formula", "lms", "titration_table"):
        checks.append(need(bool(inputs), "inputs"))
        checks.append(need(bool((spec.get("compute") or {}).get("outputs")), "outputs"))
        checks.append(need(
            all(o.get("expr") for o in (spec.get("compute") or {}).get("outputs") or []),
            "every output has an expression"))
        checks.append(need(bool(content.get("equation")), "printed equation text"))
        checks.append(need(bool(content.get("notes")), "notes"))
        with_units = [i for i in inputs if i.get("units")]
        checks.append(need(len(with_units) == len(inputs) if inputs else False,
                           "unit options on every field"))
        unresolved = [i["key"] for i in inputs
                      for u in (i.get("units") or []) if not u.get("resolved")]
        checks.append(need(not unresolved, "conversion factor for every unit option"))
        checks.append(need(any(i.get("constraints") for i in inputs),
                           "validation bounds"))
        checks.append(need(any(i.get("help") for i in inputs), "field help text"))
        incomplete = [i["key"] for i in inputs if i.get("options_incomplete")]
        checks.append(need(not incomplete, "complete option lists"))

    if renderer == "score":
        checks.append(need(bool(scoring.get("groups")), "criterion groups"))
        checks.append(need(
            all(g.get("options") for g in scoring.get("groups") or []),
            "options in every group"))
        checks.append(need(bool(scoring.get("bands")), "interpretation bands"))

    if renderer in ("convert", "dose_table", "tree"):
        tables = spec.get("tables") or {}
        checks.append(need(any(tables.get(k) for k in
                               ("pairs", "drugs", "nodes", "thresholds", "admit_dx")),
                           "data table"))

    score = round(100.0 * sum(checks) / len(checks), 1) if checks else 0.0
    return {
        "score": score,
        "checks_passed": sum(checks),
        "checks_total": len(checks),
        "missing": missing,
    }


# --------------------------------------------------------------------------
# printed-equation fallback
# --------------------------------------------------------------------------

_EQ_ASSIGN = re.compile(
    r"^([A-Za-z_][\w]*)\s*=\s*(.+)$", re.M
)
_EQ_SKIP = re.compile(
    r"^(note|notes|where|if|when|for|using|see|the)\b", re.I
)


def outputs_from_equation(equation: str, known: set[str]) -> list[dict]:
    """Turn printed `LHS = expr` lines into compute outputs when JS yielded none.

    The Equation section is vendor text, not executable JS, so this only keeps
    assignments whose right-hand side is arithmetic over already-known names
    (or earlier equation lines). Interpretation prose is ignored.
    """
    if not equation:
        return []
    from .jsparse import js_expr_to_canonical, to_snake
    from .evaluator import ExpressionError, free_names

    available = set(known)
    out: list[dict] = []
    for m in _EQ_ASSIGN.finditer(equation):
        lhs, rhs = m.group(1), m.group(2).strip()
        if _EQ_SKIP.match(lhs):
            continue
        if re.search(r"\b(is calculated|input in|measured in)\b", rhs, re.I):
            continue
        rhs = re.sub(r"\bfixDP\s*\((.+),\s*-?\d+\s*\)", r"\1", rhs)
        rhs = re.sub(r"\be\s*\(", "exp(", rhs)
        try:
            expr = js_expr_to_canonical(rhs)
            names = free_names(expr)
        except ExpressionError:
            continue
        canon = {squash(n): n for n in available}
        unresolved = []
        mapping = {}
        for n in names:
            hit = canon.get(squash(n))
            if hit is None:
                sn = squash(n)
                hit = next((v for k, v in canon.items()
                            if sn and (sn in k or k in sn)
                            and min(len(sn), len(k)) >= 8), None)
            if hit and hit != n:
                mapping[n] = hit
            elif hit is None:
                unresolved.append(n)
        if unresolved:
            continue
        if mapping:
            from .jsparse import rename_vars
            expr = rename_vars(expr, mapping)
        key = to_snake(lhs)
        out.append({
            "key": key,
            "label": lhs.replace("_", " "),
            "expr": expr,
            "decimals": None,
            "base_unit": None,
            "kind": "number",
            "source": "printed_equation",
        })
        available.add(key)
    return out


# The print view is the only place a field's real name appears for the newer
# engine: the script knows it as `cp`, the form calls it "Measured Peak level
# (Cp)". Without this the spec ships initials, and the coverage audit reports
# the printed line as content the extraction lost -- which, for a label, it had.
_ABBREV = re.compile(r"\(([A-Za-z][A-Za-z0-9_ /]{0,14})\)")
_PLACEHOLDER_UNITS = {"mg", "ml", "mcg", "kg", "lb", "hr", "hrs", "min", "l",
                      "mgdl", "mcgml", "mmoll", "mlhr", "mgmin", "years", "kgm2"}


# Words a key carries for the engine's benefit, which a readable label drops.
_KEY_PLUMBING = {"input", "field", "value", "val", "list", "id"}


def _label_is_derived(item: dict) -> bool:
    """True while the label is nothing more than the key spelled out.

    The key is spelled out with its plumbing words removed -- `field_weight` is
    shown as "Weight" -- so the comparison has to drop them too. Left in, every
    Treprostinil field looked like it had been named already and none was ever
    offered the name its printed form gives it.
    """
    label = squash(item.get("label") or "")
    key = item.get("key") or ""
    if label == squash(key):
        return True
    trimmed = "_".join(w for w in key.split("_")
                       if w and w.lower() not in _KEY_PLUMBING)
    return bool(trimmed) and label == squash(trimmed)


def attach_printed_labels(inputs: list[dict], outputs: list[dict],
                          sec_calc: str, sec_results: str = "") -> int:
    """Label inputs from the form's input rows and results from its result rows.

    Several calculators expose a result as a readable field too, so the same key
    exists on both sides; matching against one pooled list let the input claim
    the line and left every result named after its variable.
    """
    from .pdf_extract import split_calculator_results
    calc_in, calc_res = split_calculator_results(sec_calc)
    if not calc_res:
        # Most forms print no "Result" heading at all -- the result rows simply
        # follow the Calculate/Reset buttons.
        lines = sec_calc.split("\n")
        cut = max((i for i, l in enumerate(lines)
                   if l.strip().lower().rstrip(" .") in ("reset", "reset form")),
                  default=-1)
        if cut >= 0:
            calc_in, calc_res = "\n".join(lines[:cut]), "\n".join(lines[cut + 1:])
    # Inputs are paired off in order only as a last resort, and only when the
    # counts match exactly. That was unsafe while buttons were still in the
    # pool -- every infusion calculator came out with a field called
    # "Calculate" -- so chrome is filtered first and a shared word overrides
    # position wherever one exists.
    n = _attach_labels_from(inputs, calc_in or sec_calc, positional=True)
    # The results block lists one line per result, in the order the spec holds
    # them, so once the unambiguous matches are made a like-for-like remainder
    # is safe to pair off in order -- otherwise a result the form calls
    # "5 Year Hip Fracture Risk" ships as "Hip".
    n += _attach_labels_from(
        outputs, "\n".join(x for x in (calc_res, sec_results) if x),
        positional=True)
    return n


def _attach_labels_from(items: list[dict], sec_calc: str,
                        positional: bool = False) -> int:
    """Give fields and results the names the printed form shows.

    A printed line is matched to a field only through an unambiguous key: the
    parenthetical abbreviation the form prints beside the name, or the name
    itself once case and punctuation are folded. Anything longer than a short
    phrase is prose, not a label.
    """
    if not sec_calc:
        return 0
    by_key: dict[str, dict] = {}
    for item in list(items):
        k = squash(item.get("key") or "")
        if k and _label_is_derived(item):
            by_key.setdefault(k, item)
    if not by_key:
        return 0

    taken: set[int] = set()
    spare: list[str] = []
    changed = 0
    for line in (l.strip() for l in sec_calc.split("\n")):
        if not line or len(line) > 70 or line.count(" ") > 7:
            continue
        abbrevs = [squash(a) for a in _ABBREV.findall(line)]
        stripped = _ABBREV.sub("", line).strip(" ?:.")
        cands = list(abbrevs)
        # "Suggested Dose (MD)" is `suggested_md`, not `md` -- the abbreviation
        # alone names the input it is derived from, so the qualifier in front of
        # it has to be part of the match.
        for w in stripped.split():
            for a in abbrevs:
                cands.append(squash(w) + a)
        cands += [squash(stripped), squash(line.rstrip("? "))]
        for c in cands:
            item = by_key.get(c)
            if item is None or id(item) in taken or c in _PLACEHOLDER_UNITS:
                continue
            item["label"] = line.rstrip("? ").strip()
            item["label_source"] = "printed form"
            taken.add(id(item))
            by_key.pop(c, None)
            changed += 1
            break
        else:
            # A button is not a label. Left in the pool, "Calculate" was the
            # only word left for `calculated_md` and became its name.
            if line.lower().strip(": ") not in _FORM_CHROME:
                spare.append(line)

    changed += _match_by_tokens(by_key, spare, taken)
    _ALL_ITEMS[id(by_key)] = list(items)
    try:
        changed += _match_by_distinctive_word(by_key, spare, taken)
        changed += _match_by_trailing_word(by_key, spare, taken)
    finally:
        _ALL_ITEMS.pop(id(by_key), None)

    if positional:
        left = [i for i in by_key.values() if id(i) not in taken]
        already = {squash(i.get("label") or "") for i in items
                   if i.get("label_source")}
        # An option printed under its field -- "Female", "Male" -- is part of
        # that field, not a spare label. Counting them kept the counts from
        # matching, so the pairing never ran on the forms that needed it.
        already |= {squash(o.get("label") or "")
                    for i in items for o in (i.get("options") or [])}
        already.discard("")
        cands: list[str] = []
        for l in spare:
            if (_UNIT_LINE.fullmatch(l) or squash(l) in _PLACEHOLDER_UNITS
                    or l.lower().strip(": ") in _FORM_CHROME
                    or len(l) <= 2 or l.count(" ") > 8):
                continue
            # "Output Unit" heads the unit selector above the results, not a
            # result; and the page drawn twice repeats a row verbatim.
            if re.fullmatch(r"output\s+units?", l.strip(), re.I):
                continue
            # A fragment that opens in lower case or mid-parenthesis is the tail
            # of the sentence above it, not a label. Left in the pool it broke
            # the count, and the pairing never ran on the forms that needed it.
            if l[:1].islower() or l[:1] in "(“‘":
                continue
            if cands and squash(cands[-1]) == squash(l):
                continue
            # A row already claimed by name is not a second result's label.
            if squash(l) in already:
                continue
            cands.append(l)
        if left and len(left) == len(cands):
            for item, line in _pair_up(left, cands):
                item["label"] = line.rstrip("? :").strip()
                item["label_source"] = "printed form (position)"
                changed += 1
    return changed


def _pair_up(items: list[dict], lines: list[str]) -> list[tuple[dict, str]]:
    """Pair equal-length lists, letting a shared word override plain order.

    Order alone swapped Treprostinil's two rates: the form lists SUBQ first and
    the spec holds IV first, and both rows are called "... Rate". A word the
    two do not share -- `iv` against `subq` -- settles it, so any pair with
    evidence is matched first and only the rest fall back to position.
    """
    def toks(item: dict) -> set[str]:
        return {t for t in re.split(r"[^a-z0-9]+", (item.get("key") or "").lower())
                if len(t) >= 2 and t not in _KEY_NOISE}

    scored = []
    for i, item in enumerate(items):
        it = toks(item)
        for j, line in enumerate(lines):
            words = {w for w in re.split(r"[^a-z0-9]+", line.lower()) if w}
            hits = sum(1 for t in it
                       if any(w == t or w.startswith(t) for w in words))
            if hits:
                scored.append((hits, i, j))
    scored.sort(reverse=True)

    out: list[tuple[dict, str]] = []
    used_i: set[int] = set()
    used_j: set[int] = set()
    for hits, i, j in scored:
        if i in used_i or j in used_j:
            continue
        # An ambiguous best is no evidence at all; leave it to position.
        if sum(1 for h, a, b in scored if a == i and h == hits) > 1:
            continue
        used_i.add(i)
        used_j.add(j)
        out.append((items[i], lines[j]))
    rest_i = [i for i in range(len(items)) if i not in used_i]
    rest_j = [j for j in range(len(lines)) if j not in used_j]
    out.extend((items[i], lines[j]) for i, j in zip(rest_i, rest_j))
    return out


# A key's engine prefix says where the value lives, not what it means.
_KEY_NOISE = {"field", "units", "unit", "output", "out", "id", "val", "value",
              "input", "row", "calc", "v2", "v3", "time", "date"}




_ALL_ITEMS: dict[int, list[dict]] = {}


def _match_by_trailing_word(by_key: dict[str, dict], lines: list[str],
                            taken: set[int]) -> int:
    """A qualifier in front of the name: "Dosage Form" for the field `form`.

    The whole-name rule misses it and the distinctive-word rule will not look at
    a word this short. Valganciclovir's dosage form came out labelled "Form"
    while the line naming it sat unclaimed. Both sides must be unique, and the
    line must not already be some other field's name.
    """
    left = [i for i in by_key.values() if id(i) not in taken]
    if not left or not lines:
        return 0
    everyone = list(by_key.values()) + list(_ALL_ITEMS.get(id(by_key), []))
    named = {squash(i.get("label") or "") for i in everyone if i.get("label_source")}
    changed = 0
    used: set[int] = set()
    for item in left:
        k = squash(item.get("key") or "")
        if len(k) < 4:
            continue
        hits = [(idx, l) for idx, l in enumerate(lines)
                if idx not in used
                and squash(l.split()[-1] if l.split() else "") == k
                and squash(l) != k]
        if len(hits) != 1:
            continue
        idx, line = hits[0]
        if squash(line) in named:
            continue
        rivals = [o for o in everyone
                  if o is not item and squash(o.get("key") or "") == k]
        if rivals:
            continue
        item["label"] = line.rstrip("? :").strip()
        item["label_source"] = "printed form (qualified name)"
        used.add(idx)
        taken.add(id(item))
        changed += 1
    return changed


def _match_by_distinctive_word(by_key: dict[str, dict], lines: list[str],
                               taken: set[int]) -> int:
    """Match on one long word, but only when it can mean one thing.

    `calvert_dosage` against "Carboplatin Dosage" shares only `dosage`, which
    the whole-name rule rejects. A word that long, appearing in exactly one
    remaining line and claimed by exactly one remaining field, is not a
    coincidence -- and requiring both sides to be unique is what stops it
    becoming one.
    """
    left = [i for i in by_key.values() if id(i) not in taken]
    if not left or not lines:
        return 0

    def words_of(text: str) -> set[str]:
        return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) >= 5}

    def key_words(item: dict) -> set[str]:
        return {t for t in re.split(r"[^a-z0-9]+", (item.get("key") or "").lower())
                if len(t) >= 5 and t not in _KEY_NOISE}

    # A field that already has its name is still a rival for that name. Judging
    # only against the fields still unnamed let `units_weight` claim "Dosing
    # Weight" the moment `field_weight` had taken it, and two fields on one form
    # ended up with the same label.
    everyone = list(by_key.values()) + [i for i in _ALL_ITEMS.get(id(by_key), [])]
    changed = 0
    used: set[int] = set()
    for item in left:
        toks = {t for t in re.split(r"[^a-z0-9]+", (item.get("key") or "").lower())
                if len(t) >= 5 and t not in _KEY_NOISE}
        if not toks:
            continue
        hits = [
            (idx, line) for idx, line in enumerate(lines)
            if idx not in used and toks & words_of(line)
        ]
        if len(hits) != 1:
            continue
        idx, line = hits[0]
        # The line must not be a better fit for some other field.
        rivals = [
            other for other in everyone
            if other is not item and key_words(other) & words_of(line)
        ]
        if rivals:
            continue
        if squash(line) in {squash(i.get("label") or "") for i in everyone}:
            continue                        # already some field's name
        item["label"] = line.rstrip("? :").strip()
        item["label_source"] = "printed form (shared word)"
        used.add(idx)
        taken.add(id(item))
        changed += 1
    return changed

def _abbreviates(short: str, word: str) -> bool:
    """Whether `short` is `word` with letters dropped -- `sr` for `serum`."""
    if not short or not word or short[0] != word[0]:
        return False
    it = iter(word)
    return all(ch in it for ch in short)


def _match_by_tokens(by_key: dict[str, dict], lines: list[str],
                     taken: set[int]) -> int:
    """Match remaining fields to remaining printed lines by shared words.

    An exact key match cannot reach a form that writes out what the script
    abbreviates -- `field_freq_final_change` against "Frequency of Final
    Treprostinil Reservoir Change". Word overlap can, and stays honest by
    refusing anything that is not a clear single winner.
    """
    if not by_key or not lines:
        return 0
    used: set[int] = set()
    changed = 0
    def _toks(item: dict) -> list[str]:
        return [t for t in re.split(r"[^a-z0-9]+", (item.get("key") or "").lower())
                if t and t not in _KEY_NOISE and len(t) > 2]

    # The most specific name goes first: `field_freq_final_change` and
    # `field_freq_change` both fit "Frequency of ... Change", and whichever
    # claims a line first must be the one with more of the name in it.
    for item in sorted(by_key.values(),
                       key=lambda i: (-len(_toks(i)), i.get("key") or "")):
        if id(item) in taken:
            continue
        toks = _toks(item)
        if not toks:
            # A key made only of short abbreviations -- `sr_cr`, `inf_t` -- has
            # no token long enough to search on, but its parts still spell out
            # the printed label word for word.
            toks = [p for p in re.split(r"[^a-z0-9]+",
                                        (item.get("key") or "").lower()) if p]
        if not toks:
            continue
        scored = []
        for idx, line in enumerate(lines):
            if idx in used:
                continue
            words = [w for w in re.split(r"[^a-z0-9]+", line.lower()) if w]
            hits = sum(1 for t in toks
                       if any(w == t or w.startswith(t) or t.startswith(w)
                              for w in words if len(w) > 2))
            # A form spells out what the script abbreviates to initials:
            # `lmp_time` against "Last Menstrual Period". That is a whole-name
            # match even though the abbreviation is short.
            initials = "".join(w[0] for w in words if len(w) > 2)
            by_initials = any(t == initials for t in toks)
            # `cr_cl` against "Clcr": the form writes the abbreviation as one
            # word and in the other order. Joining the key's parts either way
            # and comparing to the whole label settles it without guessing.
            joined = "".join(re.split(r"[^a-z0-9]+", (item.get("key") or "").lower()))
            label_squash = "".join(words)
            if joined and label_squash in (joined, joined[::-1]):
                by_initials = True
            # A date field's key abbreviates the thing being dated, and the
            # form names it "<Thing> Date": `us_time` under "Ultrasound Date".
            bare = [p for p in re.split(r"[^a-z0-9]+",
                                        (item.get("key") or "").lower())
                    if p and p not in _KEY_NOISE]
            if ((item.get("widget") == "date" or item.get("type") == "date")
                    and words and words[-1] == "date"
                    and len(bare) == 1 and _abbreviates(bare[0], words[0])):
                by_initials = True
            parts_rev = [p for p in reversed(
                [x for x in re.split(r"[^a-z0-9]+", (item.get("key") or "").lower()) if x])]
            if len(parts_rev) > 1 and "".join(parts_rev) == label_squash:
                by_initials = True
            # `sr_cr` against "Serum Creatinine", `inf_t` against "Infusion
            # Time": each part of the key abbreviates the word in the same
            # position, which is a whole-name match however short the parts.
            parts = [p for p in re.split(r"[^a-z0-9]+",
                                         (item.get("key") or "").lower()) if p]
            # The parenthetical is the form's own abbreviation of the label,
            # so it is not one of the words the key spells out.
            bare = [w for w in re.split(r"[^a-z0-9]+",
                                        _ABBREV.sub("", line).lower()) if w]
            if (len(parts) == len(bare) and len(parts) > 1
                    and all(_abbreviates(p, w) for p, w in zip(parts, bare))):
                by_initials = True
            if by_initials:
                hits = len(toks)
            scored.append((hits, by_initials, idx))
        scored.sort(reverse=True)
        need = len(toks)
        if not scored or scored[0][0] < need:
            continue
        # Every part of the name must appear. A single short word only counts
        # when it is the form's own abbreviation of the whole label.
        if need == 1 and len(toks[0]) < 5 and not scored[0][1]:
            continue
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            continue                        # ambiguous; leave it alone
        idx = scored[0][2]
        item["label"] = lines[idx].rstrip("? :").strip()
        item["label_source"] = "printed form (word match)"
        used.add(idx)
        taken.add(id(item))
        changed += 1
    return changed


_PLACEHOLDER_OPT = re.compile(r"^\s*option\s+\d+\s*$", re.I)


def labels_from_printed_options(inputs: list[dict], sec_calc: str) -> int:
    """Recover a dropdown's option text from the lines printed beneath its label.

    The print view renders a `<select>` as its label followed by one line per
    option. Where the script never compares the option against a literal there
    is nothing else to name them by, and the spec ships "Option 1/2/3" -- a
    coefficient the user is asked to choose blind.
    """
    if not sec_calc:
        return 0
    lines = [l.rstrip() for l in sec_calc.split("\n")]
    field_labels = {squash(i.get("label") or "") for i in inputs}
    field_labels |= {squash(i.get("key") or "") for i in inputs}
    chrome = {"calculate", "reset", "input", "inputs", "results", "result", ""}

    changed = 0
    for inp in inputs:
        opts = inp.get("options") or []
        if len(opts) < 2 or not all(
            _PLACEHOLDER_OPT.match(str(o.get("label") or "")) for o in opts
        ):
            continue
        want = squash(inp.get("label") or "")
        start = next((i for i, l in enumerate(lines) if squash(l) == want), None)
        if start is None:
            continue
        found: list[str] = []
        for l in lines[start + 1:]:
            t = l.strip()
            low = t.lower().rstrip(": ")
            if low in chrome:
                break
            if not t:
                continue
            if squash(t) in field_labels:
                break
            if found and squash(t) == squash(found[-1]):
                continue                    # the page drawn twice
            found.append(t)
            if len(found) == len(opts):
                break
        if len(found) != len(opts):
            continue
        for o, label in zip(opts, found):
            o["label"] = label
            o["label_source"] = "printed form"
        changed += 1
    return changed


# A band line always names its scale ("points") or introduces its meaning with
# a colon. Without one of those, `30 Day Probability of Survival` read as a band
# beginning at 30 -- and the heading it actually is went uncaptured.
_BAND_LINE = re.compile(
    r"^\s*(?:score\s*)?[<>]?=?\s*[\d.]+(?:\s*(?:to|-|–|—|and)\s*[<>]?=?\s*[\d.]+)?"
    r"\s*(?:points?\b|[:=])", re.I)
_CHROME_LINE = re.compile(r"^\s*(reset(\s+form)?|calculate|clear|print)\s*$", re.I)


def score_result_labels(sec_calc: str, sec_results: str = "",
                        band_labels: Optional[set] = None) -> list[str]:
    """The name a score calculator gives its interpretation row.

    Every band is captured, but the row's heading -- "CTP Score Interpretation",
    "Adjusted Stroke Rate", "30 Day Probability of Survival" -- is a printed
    field like any other, and a spec that omits it cannot label the result it
    shows the clinician.
    """
    text = "\n".join(x for x in (sec_calc, sec_results) if x)
    if not text:
        return []
    lines = [l.strip() for l in text.split("\n")]
    start = max((i for i, l in enumerate(lines) if _CHROME_LINE.match(l)), default=-1)
    out: list[str] = []
    for l in lines[start + 1:]:
        if not l or _BAND_LINE.match(l) or _CHROME_LINE.match(l):
            continue
        if squash(l).startswith("totalcriteriapointcount"):
            continue
        if len(l) > 110 or l.count(" ") > 16:
            continue
        # "3% ** 5%" is the risk pair belonging to a band, not a heading.
        if not re.search(r"[A-Za-z]{3,}", l):
            continue
        if out and squash(l) == squash(out[-1]):
            continue                        # the page drawn twice
        if squash(l) in (band_labels or set()):
            continue                        # a band's meaning, not a heading
        out.append(l)
        if len(out) >= 4:
            break
    if not out:
        # Some forms print the heading above the band list rather than after
        # the buttons; it names itself.
        out = [l for l in lines
               if "interpretation" in l.lower() and 3 < len(l) <= 110][:1]
    return out


_UNIT_LINE = re.compile(
    r"^(mg/dL|mcg/mL|g/dL|mmol/L|mL|L|kg|lb|g|mg|mcg|hr|hrs|hours|min|wks|weeks|"
    r"days|yr|years|cm|in|m|mmHg|%|mL/min|mL/hr|points?)$", re.I)
_YESNO = {"yes", "no", "unknown", "n/a", "not applicable"}
_FORM_CHROME = {"calculate", "reset", "reset form", "input", "inputs", "input:",
                "results", "results:", "result", "clear", "print", "back",
                "next", "restart", "create chart", "show chart", "submit",
                "close", "pull-down", "select drug"}


def form_section_headings(sec_calc: str, inputs: list[dict],
                          outputs: Optional[list[dict]] = None) -> list[str]:
    """Headings the printed form uses to group its fields.

    "Risk factors for neurotoxicity", "1st Trimester Measurement" -- these
    organise the form without being fields, and a spec that drops them loses
    the only grouping the calculator states.
    """
    if not sec_calc:
        return []
    known = {squash(i.get("label") or "") for i in inputs}
    known |= {squash(i.get("key") or "") for i in inputs}
    known |= {squash(o.get("label") or "")
              for i in inputs for o in (i.get("options") or [])}
    # A result row is not a heading over the form: Treprostinil prints
    # "Treprostinil IV Rate" as an answer, not as a group.
    known |= {squash(o.get("label") or "") for o in (outputs or [])}
    known |= {squash(o.get("key") or "") for o in (outputs or [])}
    out: list[str] = []
    lines = [l.strip() for l in sec_calc.split("\n")]
    for i, l in enumerate(lines):
        if not l or len(l) > 60 or l.count(" ") > 7:
            continue
        if l.lower().strip(": ") in _FORM_CHROME or _UNIT_LINE.fullmatch(l):
            continue
        # "mL 15% Solution" is the unit line under a field, not a heading over
        # one. Looking further ahead for the field made these eligible.
        if _UNIT_LINE.match(l.split()[0]) or re.match(r"^[\d.]+\s*%", l):
            continue
        if squash(l) in known or l.lower().rstrip("? ") in _YESNO:
            continue
        # A question is a field; a bare number or a short token is a value or
        # a unit. Neither heads a group.
        if "?" in l or re.fullmatch(r"[\d.,/%-]+", l):
            continue
        if " " not in l and len(l) <= 4:
            continue
        # A heading introduces something: the line after it must be a field.
        # A heading introduces a field, but the line straight after it can be
        # that field's unit ("Additives" then "Calcium Chloride" then "mEq"),
        # so look a little further before giving up.
        ahead = [x.strip() for x in lines[i + 1: i + 4] if x.strip()]
        if any(squash(x) in known for x in ahead) and squash(l) not in known:
            if l not in out:
                out.append(l)
    # A heading that repeats a field's own label is that label caught twice --
    # "Grouped under: Gender" above a Gender select says nothing. A single word
    # that names no field is a real heading, though: TPN groups its
    # electrolytes under "Additives".
    #
    # And a heading looks like a heading. Without this last filter the rule kept
    # 119 lines across the corpus of which 111 were a unit ("meters", "mEq/L"),
    # an option ("Female (0.85)") or a point value ("70-109 mmHg (0)"), and the
    # A-a Gradient page carried the subtitle "Grouped on the source form under:
    # meters - ratio".
    return [h for h in out if squash(h) not in known and _looks_like_a_heading(h)]


# A bare number, or one in brackets, is a value or a point count -- but "1st
# Trimester Measurement" is a heading, so an ordinal is not a number.
_BARE_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!(?:st|nd|rd|th)\b)(?![\w.])")


def _looks_like_a_heading(line: str) -> bool:
    text = line.strip()
    # A trailing colon is the form SAYING this line heads a group.
    explicit = text.endswith(":")
    text = text.rstrip(":").strip()
    # "Liquid Formulation (optional):" qualifies the heading; "Name (Optional)"
    # with no colon is a field of its own, and "Female (0.85)" is an option.
    if explicit:
        text = re.sub(r"\s*\([^()\d]*\)$", "", text).strip()
    if any(c in text for c in "()[]{}/:%°"):
        return False
    if _BARE_NUMBER.search(text):
        return False
    if len(text) > 40 or len(text.split()) > 5:
        return False
    # "Output Unit" heads the unit picker above the results, not a group of
    # fields.
    if re.fullmatch(r"output\s+units?", text, re.I):
        return False
    # The first WORD carries the case, and an ordinal is not that word:
    # "1st Trimester Measurement" is a heading, and the "s" of "1st" says
    # nothing about it.
    words = [w for w in text.split()
             if not re.fullmatch(r"\d+(?:st|nd|rd|th)|and|or|to|&", w, re.I)]
    if not words or not words[0][:1].isupper():
        return False                    # the tail of the sentence above it
    return True


def inputs_from_printed_form(sec_calc: str) -> list[dict]:
    """Read a calculator's fields straight off the printed form.

    A few calculators carry no script the parsers can use -- their logic lives
    in the app, and the PDF is the only statement of what the clinician is
    asked for. Without this they ship with the data table recovered and no
    fields at all, which is a reference table, not a calculator.
    """
    if not sec_calc:
        return []
    lines = [l.strip() for l in sec_calc.split("\n")]
    fields: list[dict] = []
    seen: set[str] = set()
    for raw in lines:
        if not raw or raw.lower().rstrip(":") in _FORM_CHROME:
            continue
        low = raw.lower().rstrip("? ")
        if _UNIT_LINE.fullmatch(raw):
            if fields and not fields[-1].get("unit"):
                fields[-1]["unit"] = raw
                fields[-1]["base_unit"] = raw
            continue
        if low in _YESNO:
            if fields:
                opts = fields[-1].setdefault("options", [])
                if not any(squash(o["label"]) == squash(raw) for o in opts):
                    opts.append({"label": raw, "value": len(opts)})
            continue
        # "Serum albumin less than 3.0 g/dL? Yes" -- the form wraps the first
        # option onto the label's own line.
        trailing = re.match(r"^(.*\?)\s+(Yes|No|Unknown)$", raw, re.I)
        if trailing:
            raw, tail = trailing.group(1), trailing.group(2)
            low = raw.lower().rstrip("? ")
        else:
            tail = None
        if len(raw) > 80 or raw.count(" ") > 10:
            continue                        # prose, not a field
        key = to_snake_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        entry: dict = {
            "key": key, "label": raw.rstrip("? ").strip(), "type": "number",
            "required": False, "constraints": {},
            "source": {"kind": "printed_form"},
        }
        if raw.rstrip().endswith("?"):
            entry["type"] = "select"
            entry["options"] = []
        if tail:
            entry["options"] = [{"label": tail, "value": 0}]
        fields.append(entry)
    # A bare line with no unit and no options that introduces a run of
    # questions is a section heading -- "Risk factors for neurotoxicity" is not
    # something the clinician types a number into.
    out: list[dict] = []
    for i, f in enumerate(fields):
        if f.get("unit") or f.get("options"):
            out.append(f)
            continue
        if any(later.get("type") == "select" for later in fields[i + 1:]):
            continue
        out.append(f)
    return out


def to_snake_key(text: str) -> str:
    t = re.sub(r"[^A-Za-z0-9]+", "_", text.strip().rstrip("?")).strip("_").lower()
    return re.sub(r"__+", "_", t)[:60]


_BOUND_MSG = re.compile(
    r"the\s+(minimum|maximum)\s+value\s+for\s+(.+?)\s+is\s+"
    r"(-?\d+(?:\.\d+)?)\s*([A-Za-z%/µ]*)", re.I)


def bounds_from_help(inputs: list[dict], general: Optional[list] = None) -> int:
    """Read a field's limits out of the messages it shows when they are broken.

    A field that accepts two kinds of entry has two sets of limits -- QT's
    Heart Rate Measure is 1-500 BPM or 120-6000 millisec -- and the script
    states them only in its alerts. Recorded per mode, they are the only
    validation those entries have.
    """
    n = 0
    for inp in inputs:
        msgs = list(inp.get("help") or [])
        if inp.get("unit_modes") or inp["key"].endswith("_unit_mode"):
            continue
        # A field with an entry-mode selector has a second set of limits, and
        # the alert naming them ("RR interval") cannot be matched to the field
        # by name -- so for those the unmatched messages are in scope.
        multimode = any(i["key"] == inp["key"] + "_unit_mode" for i in inputs)
        if general and (multimode or len(msgs) < 2):
            msgs += [m for m in general if m not in msgs]
        by_subject: dict[str, dict] = {}
        for m in msgs:
            bm = _BOUND_MSG.search(m)
            if not bm:
                continue
            kind, subject, value, unit = bm.groups()
            slot = by_subject.setdefault(
                subject.strip(), {"subject": subject.strip(), "unit": unit or None})
            slot["min" if kind.lower() == "minimum" else "max"] = float(value)
        modes = [v for v in by_subject.values()
                 if "min" in v or "max" in v]
        if not modes:
            continue
        c = inp.setdefault("constraints", {})
        if c.get("min") is None and c.get("max") is None:
            c["min"], c["max"] = modes[0].get("min"), modes[0].get("max")
            c["source"] = "alert text"
            n += 1
        if len(modes) > 1:
            inp["constraints_by_mode"] = modes
    return n


# A printed data table survives extraction as one enormous run of numbers. It is
# not a note -- the same table is already held row by row under `tables.lookups`,
# where it can be looked up and rendered. Left in the prose it is 14,000
# characters of digits under a heading, which is how a growth chart came to
# print its whole LMS table into the page body.
def strip_data_dumps(notes: list) -> tuple[list, int]:
    """Drop notes that are a printed data table, and their orphaned headings."""
    if not notes:
        return notes or [], 0
    kept: list = []
    dropped = 0
    for n in notes:
        text = n if isinstance(n, str) else str(n)
        digits = sum(c.isdigit() for c in text)
        if len(text) > 400 and digits / max(len(text), 1) > 0.35:
            dropped += 1
            # The lines directly above a dump are its column headings, and on
            # their own they read as fragments: "Power [L]", "Median [M]".
            while kept and _looks_like_heading(kept[-1]):
                kept.pop()
            continue
        kept.append(n)
    return kept, dropped


_HEADING_HINT = __import__("re").compile(
    r"^(power|median|variation|age|length|height|weight|helpful tips)\b|\[[LMS]\]", __import__("re").I
)


def _looks_like_heading(note) -> bool:
    text = (note if isinstance(note, str) else str(note)).strip()
    return len(text) <= 80 and bool(_HEADING_HINT.search(text))


_FIXED_VALUE = re.compile(r"^(.{2,44}?)\s+(-?\d+(?:\.\d+)?)\s*([A-Za-z%/µ]{1,10})$")
# A stated quantity is named ("Infusate Volume 250 mL"). A row of an
# interpretation table is not: TIMI UA/NSTEMI's "3% ** 5%" matched with the
# label "3% **" and the value 5, putting seven nonsense entries on the page and
# two identical ones next to each other. A label that is itself just a number,
# or that reads as a score band, is a row -- not a quantity the form states.
_NOT_A_LABEL = re.compile(
    r"""^(?: [\d.]+\s*%?\s*\**            # "3% **", "12"
          | .*?\b\d+(?:\s*to\s*\d+)?\s*points?\b.*   # "6 to 7 Points: 19% **"
        )$""", re.I | re.X)


def fixed_values(sec_calc: str, inputs: list[dict]) -> list[dict]:
    """Quantities the form states rather than asks for.

    Labetalol's form prints "Infusate Volume 250 mL" -- a number the
    calculation folds into its constant and never offers as a field. Read as a
    missing input it looked like a gap; recorded as what it is, it is a fact the
    page should show, because the rate only holds for a 250 mL bag.
    """
    if not sec_calc:
        return []
    known = {squash(i.get("label") or "") for i in inputs}
    known |= {squash(i.get("key") or "") for i in inputs}
    out: list[dict] = []
    for raw in sec_calc.split("\n"):
        m = _FIXED_VALUE.match(raw.strip())
        if not m:
            continue
        label, value, unit = m.group(1).strip(), m.group(2), m.group(3)
        if squash(label) in known or _UNIT_LINE.fullmatch(label):
            continue
        if _NOT_A_LABEL.match(label):
            continue
        out.append({"label": label, "value": float(value), "unit": unit})
    return out


_TABLE_HEAD = re.compile(r"\b(limit|range|normal|interval|threshold|factor)s?\b", re.I)
_ROW_LABEL = re.compile(r"^([A-Za-z][\w '()\-/]{1,44}?)\s*(-?\d+(?:\.\d+)?)?$")


def printed_reference_tables(sec_calc: str, inputs: list[dict]) -> list[dict]:
    """Tables the form prints beside the calculator, as rows rather than lines.

    QT Interval Correction prints the normal QTc range for each formula, by
    sex. Flattened into lines it read as two lost fields ("Framingham 351") and
    the rest silently became prose -- so the limits a clinician needs in order
    to interpret the answer were the one thing the page did not carry.
    """
    if not sec_calc:
        return []
    lines = [l.strip() for l in sec_calc.split("\n")]
    known = {squash(i.get("label") or "") for i in inputs}
    known |= {squash(i.get("key") or "") for i in inputs}

    tables: list[dict] = []
    i = 0
    while i < len(lines) - 2:
        title = lines[i]
        header = lines[i + 1] if i + 1 < len(lines) else ""
        columns_line = lines[i + 2] if i + 2 < len(lines) else ""
        # A title, then a column-name line, then a line naming the limits.
        if (not title or any(c.isdigit() for c in title) or len(title) > 60
                or squash(title) in known or not _TABLE_HEAD.search(columns_line)):
            i += 1
            continue

        columns = [c.strip() for c in re.split(r"\s{2,}|\)\s+", columns_line) if c.strip()]
        columns = [c if c.endswith(")") or "(" not in c else c + ")" for c in columns]
        rows: list[dict] = []
        j = i + 3
        while j < len(lines):
            m = _ROW_LABEL.match(lines[j])
            if not m or not lines[j]:
                break
            label, first = m.group(1).strip(), m.group(2)
            values = [first] if first else []
            k = j + 1
            while k < len(lines) and re.fullmatch(r"-?\d+(?:\.\d+)?", lines[k] or ""):
                values.append(lines[k])
                k += 1
            if not values:
                break
            rows.append({"label": label, "values": [float(v) for v in values]})
            j = k
        if len(rows) >= 2 and all(len(r["values"]) >= 1 for r in rows):
            tables.append({
                "title": title,
                "column_header": header,
                "columns": columns,
                "rows": rows,
            })
            i = j
            continue
        i += 1
    return tables
