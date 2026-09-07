"""Recover radio-button parameter groups as real inputs, not baked-in constants.

Many calculators select a coefficient from a radio group:

    rbchk = false;
    if (Sex_radio[0].checked){ rbchk = true; Sex = 0.85; }
    if (Sex_radio[1].checked){ rbchk = true; Sex = 1; }
    if (!rbchk) doCalc = false;

A statement-level scan sees two plain assignments to `Sex` and keeps the last
one, so the calculator silently computes every patient as male. Verification
caught exactly this in Cockcroft-Gault (female CrCl overestimated by 17.6%),
MDRD (sex AND race) and calcium correction -- specs that validated cleanly while
being wrong for half the population.

The group is therefore modelled as what it is: a required choice whose options carry
the coefficient(s). Labels come from the printed form, which prints them with
their values, e.g. "Female (0.85)" / "Male (1)".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .identity import squash

# `if (Sex_radio[0].checked){ ... }`  -- body captured without nested braces
RADIO_BRANCH = re.compile(
    r"if\s*\(\s*(\w+?)_radio\s*\[\s*(\d+)\s*\]\s*\.\s*checked\s*\)\s*\{([^{}]*)\}",
    re.S,
)
NUM_ASSIGN = re.compile(r"(\w+)\s*=\s*(-?\d+(?:\.\d+)?)\s*;")
# printed option line: " Female (0.85)" / "Black (1.212)" / "No (0)"
PRINTED_OPTION = re.compile(r"^\s*(.+?)\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)\s*$")

SKIP_VARS = {"rbchk", "doCalc", "param_value", "dp"}


@dataclass
class RadioGroup:
    name: str                                   # JS prefix, e.g. "Sex"
    options: list[dict] = field(default_factory=list)   # [{index, constants:{}}]
    labels: dict[int, str] = field(default_factory=dict)
    label_mismatch: bool = False
    start: int = -1

    @property
    def variables(self) -> list[str]:
        seen: list[str] = []
        for o in self.options:
            for k in o["constants"]:
                if k not in seen:
                    seen.append(k)
        return seen


RADIO_HEAD = re.compile(
    r"if\s*\(\s*(\w+?)_radio\s*\[\s*(\d+)\s*\]\s*\.\s*checked\s*\)\s*\{"
)


def _balanced_body(text: str, open_idx: int) -> str:
    """The block starting at `open_idx`, respecting nested braces."""
    depth = 0
    for j in range(open_idx, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1: j]
    return text[open_idx + 1:]


def find_input_overrides(body: str, input_names: set[str]) -> list[dict]:
    """Radio options that OVERRIDE an input rather than set a coefficient.

    MELD's dialysis question is the archetype:

        if (Hemodialysis_twice_in_week_prior_radio[0].checked){
            Creatinine_param.value = '4'; Creatinine = 4; ... }

    Only the "Yes" branch exists -- "No" simply leaves the entered value alone --
    so it never looks like a two-option group, and its target is an input rather
    than a coefficient. Dropped, the calculator ignores dialysis entirely and
    under-scores every dialysis patient.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    # A brace-balanced read is required here: the override branch often contains
    # a nested loop that re-selects the unit dropdown, and a body pattern that
    # cannot span nested braces simply never matches.
    for m in RADIO_HEAD.finditer(body):
        name, idx = m.group(1), int(m.group(2))
        inner = _balanced_body(body, m.end() - 1)
        for var, val in NUM_ASSIGN.findall(inner):
            if var in input_names and (name, var) not in seen:
                seen.add((name, var))
                out.append({
                    "group": name, "index": idx, "input": var,
                    "value": float(val), "pos": m.start(),
                })
    return out


def find_radio_groups(body: str) -> list[RadioGroup]:
    """Collect every radio group and the constants each option sets."""
    groups: dict[str, RadioGroup] = {}
    for m in RADIO_BRANCH.finditer(body):
        name, idx, inner = m.group(1), int(m.group(2)), m.group(3)
        consts = {
            k: float(v) for k, v in NUM_ASSIGN.findall(inner) if k not in SKIP_VARS
        }
        if not consts:
            continue
        g = groups.setdefault(name, RadioGroup(name=name, start=m.start()))
        existing = next((o for o in g.options if o["index"] == idx), None)
        if existing:
            existing["constants"].update(consts)
        else:
            g.options.append({"index": idx, "constants": consts})
    for g in groups.values():
        g.options.sort(key=lambda o: o["index"])
    # a single option is not a choice
    return [g for g in groups.values() if len(g.options) >= 2]


STOP_TOKENS = {"calculate", "reset", "result", "results", "input", "inputs",
               "decimal precision"}


def labels_from_printed(
    sec_calc: str,
    group_names: list[str],
    expected: Optional[dict[str, int]] = None,
    other_labels: Optional[set[str]] = None,
) -> dict[str, list[str]]:
    """Read each group's option labels from the printed form.

    The form prints the coefficient alongside the label -- "Female (0.85)" --
    which lets the labels be matched to the JS branches by value rather than by
    position, so a reordered form cannot mislabel a coefficient.
    """
    if not sec_calc:
        return {}
    lines = [l.rstrip() for l in sec_calc.split("\n") if l.strip()]
    wanted = {squash(n): n for n in group_names}
    others = {squash(x) for x in (other_labels or set())}
    want_n = expected or {}
    out: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in lines:
        s = line.strip()
        canon = wanted.get(squash(s))
        if canon:
            current = canon
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        if PRINTED_OPTION.match(line):
            out[current].append(line.strip())
            continue
        # Groups whose options carry no coefficient print the bare label
        # ("Female" / "Male"), so a value-bearing pattern alone leaves them
        # unnamed and they fall back to "Option 1"/"Option 2".
        stripped = s
        need = want_n.get(current)
        room = need is None or len(out[current]) < need
        if (room
                and stripped
                and len(stripped) <= 48
                and squash(stripped) not in wanted
                and squash(stripped) not in others
                and stripped.lower() not in STOP_TOKENS
                and not stripped.endswith((":",))
                and not re.fullmatch(r"-?\d+(\.\d+)?", stripped)):
            out[current].append(stripped)
            continue
        if out.get(current):
            current = None                  # group's option run has ended
    return {k: v for k, v in out.items() if v}


def attach_labels(groups: list[RadioGroup], printed: dict[str, list[str]]) -> None:
    """Match printed labels to JS branches by their coefficient value."""
    for g in groups:
        lines = printed.get(g.name) or []
        parsed: list[tuple[str, Optional[float]]] = []
        for line in lines:
            m = PRINTED_OPTION.match(line)
            if m:
                parsed.append((m.group(1).strip(), float(m.group(2))))
            else:
                parsed.append((line.strip(), None))
        if not parsed:
            continue

        # Bare labels carry no value to cross-check, so position is all there is.
        if all(v is None for _, v in parsed) and len(parsed) == len(g.options):
            for opt, (label, _) in zip(g.options, parsed):
                g.labels[opt["index"]] = label
            continue
        parsed = [(l, v) for l, v in parsed if v is not None] or parsed

        # Position first, value only as a check. Several options can legitimately
        # share a coefficient -- MDRD gives 1.0 to every race except Black -- so
        # matching by value alone hands them all the first matching label.
        if len(parsed) == len(g.options):
            for opt, (label, val) in zip(g.options, parsed):
                vals = list(opt["constants"].values())
                if val is not None and not any(abs(val - v) < 1e-9 for v in vals):
                    g.label_mismatch = True
                g.labels[opt["index"]] = label
            continue

        # Counts disagree: fall back to matching each branch by its value, and
        # consume each printed label at most once.
        remaining = [(l, v) for l, v in parsed if v is not None]
        for opt in g.options:
            vals = list(opt["constants"].values())
            for i, (label, val) in enumerate(remaining):
                if any(abs(val - v) < 1e-9 for v in vals):
                    g.labels[opt["index"]] = label
                    remaining.pop(i)
                    break
        g.label_mismatch = True


def build_input(g: RadioGroup) -> dict:
    """Turn a radio group into a select input.

    When the group sets exactly one variable, the option's value IS that
    coefficient and the input can be used directly in expressions. When it sets
    several (MDRD's sex and race each scale different terms), the option carries
    the whole constant set and `variant_constants` records them.
    """
    single = len(g.variables) == 1
    var = g.variables[0] if single else None
    options = []
    for opt in g.options:
        label = g.labels.get(opt["index"]) or f"Option {opt['index'] + 1}"
        entry: dict = {"label": label, "index": opt["index"]}
        if single:
            entry["value"] = opt["constants"][var]
        else:
            entry["value"] = opt["index"]
            entry["constants"] = opt["constants"]
        options.append(entry)

    key = _snake(var if single else g.name)
    return {
        "key": key,
        "js_name": var if single else g.name,
        "label": g.name.replace("_", " ").strip(),
        "widget": "select",
        "required": True,
        "default": None,
        "base_unit": None,
        "units": None,
        "constraints": {},
        "options": options,
        "options_source": "pdf_script+printed_form",
        "variant_variables": None if single else g.variables,
        "source": {"kind": "radio_group", "js_group": g.name},
    }


def _snake(name: str) -> str:
    return re.sub(r"__+", "_", name).lower().strip("_")


# `function varload1(){ document.X_form.Sex_radio[0].checked = true;
#   Age_Factor = 2.32888; ... }`
VARLOAD_FN = re.compile(r"function\s+varload(\d+)\s*\(\s*\)\s*\{")
VARLOAD_RADIO = re.compile(r"(\w+?)_radio\s*\[\s*(\d+)\s*\]\s*\.\s*checked\s*=\s*true")


def find_varload_groups(js: str) -> list[RadioGroup]:
    """Radio groups whose coefficients are swapped by a `varloadN` handler.

    Framingham 2008 does not set its coefficients inside the calculation at all:
    the `_fx` body only checks that SOME sex is selected, and each radio's
    onclick calls `varload1()` or `varload2()`, which reassign five module
    constants and rebuild three dropdowns. Read as a statement sequence the file
    therefore looks like it has one fixed coefficient set -- the male one, being
    last -- and no sex input whatsoever, so every woman was scored as a man.
    """
    if not js:
        return []
    bodies: dict[int, str] = {}
    for m in VARLOAD_FN.finditer(js):
        n = int(m.group(1))
        if n in bodies:
            continue
        bodies[n] = _balanced_body(js, m.end() - 1)
    if len(bodies) < 2:
        return []

    name = None
    for body in bodies.values():
        rm = VARLOAD_RADIO.search(body)
        if rm:
            name = rm.group(1)
            break
    if not name:
        return []

    group = RadioGroup(name=name, start=js.find("function varload"))
    for n in sorted(bodies):
        consts: dict[str, float] = {}
        for am in NUM_ASSIGN.finditer(bodies[n]):
            var, val = am.group(1), am.group(2)
            if var in SKIP_VARS or var.endswith("_radio") or "." in var:
                continue
            if var.endswith("length") or var.endswith("selectedIndex"):
                continue
            consts[var] = float(val)
        if consts:
            group.options.append({"index": n - 1, "constants": consts})
    if len(group.options) < 2:
        return []
    # Two options that set the same numbers are not a choice.
    if all(o["constants"] == group.options[0]["constants"] for o in group.options):
        return []
    return [group]


def options_by_varload(js: str) -> dict[str, dict[int, list[dict]]]:
    """Each `varloadN` handler's dropdown contents, per field.

    The same three pulldowns are rebuilt with a different option list per sex,
    so pooling every `new Option` in the file produced dropdowns holding both
    sexes' coefficients back to back -- four choices where the form offers two.
    """
    from .enrich import NEW_OPTION
    out: dict[str, dict[int, list[dict]]] = {}
    for m in VARLOAD_FN.finditer(js or ""):
        n = int(m.group(1)) - 1
        body = _balanced_body(js, m.end() - 1)
        for om in NEW_OPTION.finditer(body):
            field, label, value = om.group(1), om.group(3), om.group(5)
            try:
                val: object = float(value)
            except ValueError:
                val = value
            out.setdefault(squash(field), {}).setdefault(n, []).append(
                {"label": re.sub(r"\s+", " ", label).strip(), "value": val}
            )
    return out


def nested_radio_groups(body: str) -> list[RadioGroup]:
    """Radio groups whose branch bodies contain further branching.

    ACC/AHA's Pooled Cohort Equations pick their coefficient set from sex AND
    race, written as `if (Sex_radio[0].checked){ if (Race == 0) {...} if (Race
    == 1) {...} }`. The flat scan cannot read that -- its branch pattern stops
    at the first nested brace -- so the sex level was lost entirely and every
    coefficient ended up conditioned on race twice over, which is how a
    calculator with no sex input at all came to have four coefficient sets.
    """
    groups: dict[str, RadioGroup] = {}
    for m in RADIO_HEAD.finditer(body or ""):
        name, idx = m.group(1), int(m.group(2))
        inner = _balanced_body(body, m.end() - 1)
        if "{" not in inner:
            continue                        # the flat form; handled elsewhere
        g = groups.setdefault(name, RadioGroup(name=name, start=m.start()))
        if not any(o["index"] == idx for o in g.options):
            g.options.append({"index": idx, "constants": {}})
    for g in groups.values():
        g.options.sort(key=lambda o: o["index"])
    return [g for g in groups.values() if len(g.options) >= 2]


def rewrite_radio_conditions(body: str, names: set[str]) -> str:
    """Turn `if (Sex_radio[0].checked)` into `if (Sex == 0)`.

    Once the choice is an ordinary comparison, the conditional-assignment
    reader handles the nested branches like any other case split, and the
    coefficients come out conditioned on both dimensions.
    """
    if not names:
        return body

    def repl(m: re.Match) -> str:
        if m.group(1) not in names:
            return m.group(0)
        return f"if ({m.group(1)} == {m.group(2)}) {{"

    return RADIO_HEAD.sub(repl, body)


_INNER_IF = re.compile(r"if\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*\{")


def _split_top_ifs(text: str) -> tuple[list[tuple[str, str]], str]:
    """Top-level `if (cond) { body }` blocks in `text`, and everything else."""
    blocks: list[tuple[str, str]] = []
    plain: list[str] = []
    pos = 0
    while pos < len(text):
        m = _INNER_IF.search(text, pos)
        if m is None:
            plain.append(text[pos:])
            break
        plain.append(text[pos:m.start()])
        open_idx = m.end() - 1
        body = _balanced_body(text, open_idx)
        blocks.append((m.group(1).strip(), body))
        pos = open_idx + 1 + len(body) + 1
    return blocks, "".join(plain).strip()


def _flatten_under(outer: str, body: str, depth: int = 2) -> list[str]:
    """Rewrite a guarded block as branches on the outer AND inner conditions."""
    blocks, plain = _split_top_ifs(body)
    if not blocks or depth <= 0:
        return [f"if ({outer}) {{{body}}} "]
    out: list[str] = []
    if plain:
        out.append(f"if ({outer}) {{ {plain} }} ")
    for cond, inner in blocks:
        out.extend(_flatten_under(f"({outer}) && ({cond})", inner, depth - 1))
    return out


def flatten_nested_radio(body: str, names: set[str]) -> str:
    """Flatten `if (Sex == 0){ if (Race == 0){A} if (Race == 1){B} }`.

    The case-split reader recognises one level of branching, so a nested pair
    reads as the inner condition alone -- ACC/AHA's four coefficient sets came
    out conditioned on race twice and on sex not at all. Rewriting each nested
    pair as a single branch on both conditions preserves the meaning in a shape
    the reader already handles.

    The flattening recurses, because a page drawn twice can leave a sibling
    branch swallowed inside the one before it; hoisting only the first level
    left that sibling guarded by race alone, wrong for the other sex.
    """
    if not names:
        return body
    span = _radio_span(body, names)
    if span is None:
        return body
    start, end = span
    region = body[start:end]
    flat = _flatten_pass(region, names)

    # A page drawn twice can cut the wrapper short, so a sibling branch ends up
    # outside it and keeps only the inner condition -- wrong for the other
    # option. De-duplicating repairs the nesting, but it also deletes text, so
    # it is applied to the radio block alone and kept only when it recovers
    # more branches. Collapsing the whole function removed the reads of three
    # of its four inputs.
    from .dedup import collapse_repeats
    fixed = collapse_repeats(region)
    if fixed != region:
        alt = _flatten_pass(fixed, names)
        if alt.count("&&") > flat.count("&&"):
            flat = alt
    return body[:start] + flat + body[end:]


def _radio_span(body: str, names: set[str]) -> Optional[tuple[int, int]]:
    """The stretch of the body covered by the named radio groups' branches."""
    lo = hi = None
    for m in RADIO_HEAD.finditer(body):
        if m.group(1) not in names:
            continue
        open_idx = m.end() - 1
        close = open_idx + 1 + len(_balanced_body(body, open_idx)) + 1
        lo = m.start() if lo is None else min(lo, m.start())
        hi = close if hi is None else max(hi, close)
    return None if lo is None else (lo, min(hi, len(body)))


def _flatten_pass(body: str, names: set[str]) -> str:
    out = body
    for _ in range(6):
        m = next((c for c in RADIO_HEAD.finditer(out) if c.group(1) in names), None)
        if m is None:
            break
        open_idx = m.end() - 1
        inner = _balanced_body(out, open_idx)
        close_idx = open_idx + 1 + len(inner)
        pieces = _flatten_under(f"{m.group(1)} == {m.group(2)}", inner)
        out = out[:m.start()] + "".join(pieces) + out[close_idx + 1:]
    return out
