"""Parse the EBMcalc calculator JavaScript that Lexicomp PDFs embed verbatim.

Every calculator follows the same generated template, which is what makes a
single parser viable across the whole corpus:

    function <Name>_fx() {
      with (document.<Name>_form) {
        doCalc = true;
        param_value = parseFloat(Age_param.value);          <- an INPUT
        if (isNaN(param_value)) { param_value=""; doCalc=false; }
        unit_parts = Age_unit.options[...].value.split('|');
        Age = param_value * parseFloat(unit_parts[0])
                          + parseFloat(unit_parts[1]);      <- unit affine, skip
        ...
        dp = decpts.options[decpts.selectedIndex].text;
        minMaxCheck();
        p_Atm = 760 * eTo(Elevation / -7000);               <- a STEP
        Expected_AaG = 2.5 + (0.21 * Age);                  <- a STEP feeding an output
        if (doCalc) Expected_AaG_param.value = fixDP(...);  <- an OUTPUT
      }
    }

and a sibling `minMaxCheck()` whose alert() strings carry the authoritative
bound, the field's display label and its base unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# The PDF text layer renders the ﬁ ligature literally in `fixDP`/`first`.
LIGATURES = {"ﬁ": "fi", "ﬂ": "fl"}

# EBMcalc's helper functions -> our canonical expression vocabulary.
FN_MAP = {
    "eTo": "exp",
    "sqr": "sqrt",
    "sq": "__sq",       # sq(i) = i*i, expanded below
    "power": "pow",
    "ln": "ln",
    "log": "log10",
    "Math.log": "ln",
    "Math.sqrt": "sqrt",
    "Math.exp": "exp",
    "Math.pow": "pow",
    "Math.abs": "abs",
    "Math.round": "round",
    "Math.floor": "floor",
    "Math.ceil": "ceil",
    "Math.min": "min",
    "Math.max": "max",
    "roundToNumberOfDecimals": "round",
    "ZtoPercentile": "z_to_percentile",
    "PercentileFromZ": "z_to_percentile",
}


@dataclass
class Bound:
    min: Optional[float] = None
    max: Optional[float] = None
    base_unit: Optional[str] = None
    label: Optional[str] = None
    min_message: Optional[str] = None
    max_message: Optional[str] = None


@dataclass
class ParsedScript:
    form_name: Optional[str] = None
    inputs: list[str] = field(default_factory=list)          # JS var names, in form order
    steps: list[tuple[str, str]] = field(default_factory=list)  # (name, expr)
    outputs: list[str] = field(default_factory=list)         # JS var names
    bounds: dict[str, Bound] = field(default_factory=dict)
    has_decimal_precision: bool = False
    select_inputs: set = field(default_factory=set)   # read from a <select>
    lookup_table: object = None
    lookup_tables: list = field(default_factory=list)
    output_ladder: dict = field(default_factory=dict)  # output -> ladder suffix
    ambiguous_constants: dict = field(default_factory=dict)  # name -> candidate values
    radio_groups: list = field(default_factory=list)         # engine_radio.RadioGroup
    input_overrides: list = field(default_factory=list)      # radio that rewrites an input
    unit_modes: dict = field(default_factory=dict)           # field -> number of entry modes
    input_aliases: dict = field(default_factory=dict)        # local name -> input it renames      # engine_lms.LmsTable when a ladder is found


def normalize_text(s: str) -> str:
    for lig, repl in LIGATURES.items():
        s = s.replace(lig, repl)
    return s


_NEXT_FUNCTION = re.compile(r"\bfunction\s+\w+\s*\(")


def _slice_function(js: str, name_pattern: str) -> Optional[str]:
    """Return the body of the first function whose name matches, brace-balanced.

    When the braces never balance -- a page seam cut a statement in half -- the
    scan would otherwise run to the end of the script and swallow every later
    function. The body is therefore capped at the next `function` declaration,
    so a damaged function stays damaged instead of absorbing its neighbours.
    """
    m = re.search(r"function\s+(" + name_pattern + r")\s*\([^)]*\)\s*\{", js)
    if not m:
        return None
    i = js.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i + 1 : j]
    return js[i + 1 :]


def balance_parens(expr: str) -> str:
    """Close parentheses a truncated capture left open.

    Expression capture stops at a brace, so a call split across a block boundary
    arrives as `roundToNumberOfDecimals(value, 1` -- unparseable, though the
    intended close is unambiguous. Extra closers are dropped from the right for
    the same reason. Only the delimiters are touched; no operand is invented.
    """
    if not expr:
        return expr
    depth = 0
    out = []
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                continue                    # stray closer, drop it
            depth -= 1
        out.append(ch)
    return "".join(out) + (")" * depth)


# `(outputDose = weight * 50)` -- JS assigns and yields the value in one step.
EMBEDDED_ASSIGN = re.compile(r"\(\s*([A-Za-z_]\w*)\s*=(?!=)\s*")


def strip_embedded_assignments(expr: str) -> str:
    """Reduce `(name = value)` to `(value)`.

    JavaScript lets an assignment stand in for its own value, and these
    calculators use it inside conditions: `if ((outputDose = weight * 50) < 500)`.
    As an expression that is a syntax error, which takes the whole calculator
    down; the value it yields is simply the right-hand side.
    """
    prev = None
    while prev != expr:
        prev = expr
        expr = EMBEDDED_ASSIGN.sub("(", expr)
    return expr


def ternary_to_conditional(expr: str) -> str:
    """Rewrite `cond ? a : b` as `(a) if (cond) else (b)`.

    JavaScript's conditional operator appears throughout these helpers
    (`concentration !== 'other' ? 1000 : 1`). Left as-is it is a syntax error,
    so the whole helper is discarded and every output that calls it is lost.
    Nesting is handled by working from the innermost `?` outwards.
    """
    for _ in range(8):
        q = expr.find("?")
        if q < 0:
            return expr
        # the matching ":" at the same nesting depth
        depth, colon = 0, -1
        for j in range(q + 1, len(expr)):
            c = expr[j]
            if c in "([":
                depth += 1
            elif c in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif c == "?":
                depth += 1          # a nested ternary consumes the next colon
            elif c == ":" and depth == 0:
                colon = j
                break
            elif c == ":":
                depth -= 1
        if colon < 0:
            return expr
        # the condition runs back to an unbalanced "(" or the start
        depth, start = 0, 0
        for j in range(q - 1, -1, -1):
            c = expr[j]
            if c in ")]":
                depth += 1
            elif c in "([":
                if depth == 0:
                    start = j + 1
                    break
                depth -= 1
        # the false-branch runs to an unbalanced ")" or the end
        depth, end = 0, len(expr)
        for j in range(colon + 1, len(expr)):
            c = expr[j]
            if c in "([":
                depth += 1
            elif c in ")]":
                if depth == 0:
                    end = j
                    break
                depth -= 1
        cond = expr[start:q].strip()
        yes = expr[q + 1:colon].strip()
        no = expr[colon + 1:end].strip()
        expr = f"{expr[:start]}(({yes}) if ({cond}) else ({no})){expr[end:]}"
    return expr


def js_expr_to_canonical(expr: str) -> str:
    """Rewrite an EBMcalc JS expression into our restricted expression language."""
    e = expr.strip().rstrip(";").strip()
    e = re.sub(r"\s+", " ", e)
    e = strip_embedded_assignments(e)
    e = ternary_to_conditional(e)
    e = balance_parens(e)

    # sq(x) -> (x)*(x) ; do this before the generic rename.
    while True:
        m = re.search(r"\bsq\s*\(", e)
        if not m:
            break
        start = m.end() - 1
        depth = 0
        for j in range(start, len(e)):
            if e[j] == "(":
                depth += 1
            elif e[j] == ")":
                depth -= 1
                if depth == 0:
                    inner = e[start + 1 : j]
                    e = e[: m.start()] + f"(({inner})*({inner}))" + e[j + 1 :]
                    break
        else:
            break

    for js_fn, canon in sorted(FN_MAP.items(), key=lambda kv: -len(kv[0])):
        if canon == "__sq":
            continue
        e = re.sub(r"(?<![\w.])" + re.escape(js_fn) + r"\s*\(", canon + "(", e)

    e = e.replace("Math.PI", "pi").replace("Math.E", "e")
    return e.strip()


def to_snake(js_name: str) -> str:
    """Age -> age ; Percent_Inspired_O2 -> percent_inspired_o2 ; p_aO2 -> p_ao2.

    EBMcalc already uses `_` as the word separator, so we deliberately do NOT
    split camelCase -- doing so mangles the chemistry/acronym names that appear
    throughout the corpus (p_H2O -> p_h2_o, Expected_AaG -> expected_aa_g).
    """
    return re.sub(r"__+", "_", js_name).lower().strip("_")


def rename_vars(expr: str, mapping: dict[str, str]) -> str:
    """Rewrite JS identifiers in an expression to their canonical snake keys."""
    if not expr:
        return expr
    pattern = re.compile(
        r"(?<![\w.])(" + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)) + r")(?![\w(])"
    ) if mapping else None
    if pattern is None:
        return expr
    return pattern.sub(lambda m: mapping[m.group(1)], expr)


def js_name_to_label(js_name: str) -> str:
    """Percent_Inspired_O2 -> 'Percent Inspired O2' (matches the printed form label)."""
    return js_name.replace("_", " ").strip()


# `if (COND) { X = A; } else { X = B; }`
# The condition must not span a statement boundary. Without excluding `;` the
# non-greedy capture still starts at an earlier `if(` -- there is no brace
# between them -- and swallows hundreds of characters of unrelated code.
COND_IF_ELSE = re.compile(
    r"if\s*\(([^{};]{1,160}?)\)\s*\{\s*(\w+)\s*=\s*([^;{}]{1,160}?)\s*;?\s*\}"
    r"\s*else\s*\{\s*(\w+)\s*=\s*([^;{}]{1,160}?)\s*;?\s*\}",
    re.S,
)
# `if (COND) { A = v; B = v; C = v; }` -- a whole parameter set behind one guard
COND_BLOCK = re.compile(
    r"if\s*\(([^{};]{1,160}?)\)\s*\{([^{}]{1,400}?)\}", re.S
)
BLOCK_ASSIGN = re.compile(r"(\w+)\s*=\s*([^;{}]{1,120}?)\s*;")

# `if (COND) X = A;`  and  `if (COND) { X = A; }`  with no else
COND_SIMPLE = re.compile(
    r"if\s*\(([^{};]{1,160}?)\)\s*\{?\s*(\w+)\s*=\s*([^;{}]{1,160}?)\s*;",
    re.S,
)


# `Heart_Rate_Measure_unit.selectedIndex == 0` -- the field's unit dropdown
# choosing an ENTRY MODE, not just a scale. QT accepts either a heart rate or an
# RR interval in the same box, and each mode derives the other; dropping the
# condition flattens both branches into a mutual cycle that can never evaluate.
UNIT_MODE = re.compile(
    r"(\w+?)_unit\s*\.\s*selectedIndex\s*(===?|!==?)\s*(\d+)")


def unit_mode_fields(body: str) -> dict[str, int]:
    """Fields whose unit dropdown selects an entry mode, and how many modes."""
    out: dict[str, int] = {}
    for m in UNIT_MODE.finditer(body or ""):
        name, idx = m.group(1), int(m.group(3))
        out[name] = max(out.get(name, 0), idx + 1)
    return out


def _cond_to_expr(cond: str) -> str:
    """Normalise a JS condition into the evaluator's syntax."""
    c = re.sub(r"\s+", " ", cond).strip()
    # Translate a unit-mode test into a comparison on that field's mode input.
    c = UNIT_MODE.sub(
        lambda m: f"{to_snake(m.group(1))}_unit_mode "
                  f"{'==' if m.group(2).startswith('=') else '!='} {m.group(3)}",
        c)
    c = c.replace("&&", " and ").replace("||", " or ")
    c = re.sub(r"(?<![=!<>])===?(?!=)", "==", c)
    c = re.sub(r"!==?", "!=", c)
    return js_expr_to_canonical(c)


def extract_conditional_assignments(
    body: str,
    existing: dict[str, str],
    protected: set[str] | None = None,
    spans_out: list | None = None,
) -> list[tuple[int, str, str]]:
    """Recover assignments the vendor writes as branches, as ternary expressions.

    Clinical formulas are full of clamps and case splits that a statement-level
    parser skips entirely, because the statement does not begin with `name =`:

        if (Weight >= IBW) {Weight_CrCl = IBW;} else {Weight_CrCl = Weight};
        if (Weight < IBW) DW = Weight;          // overrides an earlier DW

    Skipping them leaves the variable undefined (or, worse, leaves an earlier
    unconditional value in place), so each is rewritten as
    `A if COND else B`, with the no-else form falling back to whatever the
    variable already held.
    """
    found: list[tuple[int, str, str]] = []       # (position, name, expr)
    consumed: list[tuple[int, int]] = []
    # An input must never be shadowed by a recovered step. Calculators re-assign
    # their own input variables for unit handling ("if unit is months, Age =
    # Age/12"), which as a ternary becomes `age = ... else (age)` -- a self
    # reference that can never resolve, taking the whole spec down with it.
    # Unit handling is the unit system's job, so those are dropped here.
    blocked = protected or set()

    # `if (isNaN(X)) X = 0;` supplies a default for an empty field. It is not a
    # computation, and adopting it pins the field to zero -- which made TPN
    # divide by a total volume of nought.
    def is_empty_default(cond: str, var: str, val: str) -> bool:
        return (re.search(r"\bisNaN\s*\(", cond or "") is not None
                and re.fullmatch(r"-?\d+(?:\.\d+)?", (val or "").strip()) is not None)

    def usable(*parts: str) -> bool:
        # A condition or value touching the DOM (`x.checked`) or indexing an
        # array (`TABLE[i]`) cannot become a pure expression. A quoted string is
        # fine -- comparing a dropdown's value is exactly how these calculators
        # select between formulas.
        for x in parts:
            stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "", x)
            # a unit-mode test is translatable even though it reads a property
            stripped = UNIT_MODE.sub("MODE", stripped)
            if "[" in stripped:
                return False
            # `get(ID_X)` is a live DOM read, and the SCREAMING_CASE constants it
            # is compared against are module globals we do not carry.
            if re.search(r"\bget\s*\(", stripped):
                return False
            if "." in stripped and not re.search(r"\d\.\d", stripped):
                return False
        return True

    for m in COND_IF_ELSE.finditer(body):
        cond, v1, a, v2, b = m.groups()
        if v1 != v2 or v1 in blocked or not usable(cond, a, b):
            continue
        expr = (f"({js_expr_to_canonical(a)}) if ({_cond_to_expr(cond)}) "
                f"else ({js_expr_to_canonical(b)})")
        found.append((m.start(), v1, expr))
        consumed.append(m.span())

    # A guard can set a whole parameter set at once:
    #   if (Sex == 1){ SexFactor = 1.012; alpha = -0.241; kappa = 0.7; }
    #   if (Sex == 2){ SexFactor = 1;     alpha = -0.302; kappa = 0.9; }
    # Reading only the first assignment per block leaves alpha and kappa to the
    # plain scan, where the last branch silently wins -- every patient then gets
    # the male CKD-EPI coefficients.
    simple: dict[str, list[tuple[int, str, str]]] = {}
    for m in COND_BLOCK.finditer(body):
        if any(lo <= m.start() < hi for lo, hi in consumed):
            continue
        cond, inner = m.group(1), m.group(2)
        assigns = BLOCK_ASSIGN.findall(inner)
        if len(assigns) < 2:
            continue                       # single-assignment: COND_SIMPLE covers it
        if not usable(cond):
            # An untranslatable guard with no `else` is an input-presence check
            # ("once a diameter and a date are entered, compute these"). Since
            # evaluation only happens with valid inputs, its body is adopted
            # unconditionally; refusing it discarded every measurement
            # Gestational Age derives. A guard with an `else` is a real choice
            # and is still left alone.
            tail = body[m.end():m.end() + 12]
            if re.match(r"\s*else\b", tail):
                continue
            for var, val in assigns:
                if var in blocked or var in ("param_value", "doCalc", "dp", "rbchk"):
                    continue
                if is_empty_default(cond, var, val):
                    continue
                if usable(val):
                    found.append((m.start(), var, js_expr_to_canonical(val)))
            consumed.append(m.span())
            continue
        for var, val in assigns:
            if var in blocked or var in ("param_value", "doCalc", "dp", "rbchk"):
                continue
            if not usable(val):
                continue
            simple.setdefault(var, []).append((m.start(), cond, val))
        consumed.append(m.span())

    for m in COND_SIMPLE.finditer(body):
        if any(lo <= m.start() < hi for lo, hi in consumed):
            continue                       # already taken by the if/else form
        cond, var, val = m.groups()
        if var in ("param_value", "doCalc", "dp") or var.endswith("_param"):
            continue
        if var in blocked or not usable(cond, val):
            continue
        if is_empty_default(cond, var, val):
            continue
        simple.setdefault(var, []).append((m.start(), cond, val))

    for var, items in simple.items():
        prior = next((e for _, n, e in reversed(found) if n == var), None) \
            or existing.get(var)

        if prior is None and len(items) < 2:
            continue                       # a lone guard with no default

        if prior is None:
            # An exhaustive guard chain -- dosing by weight band, for instance:
            #   if (W < 10)            V = 100 * W;
            #   if (W >= 10 && W < 20) V = 1000 + 50 * (W - 10);
            #   if (W >= 20)           V = 1500 + 20 * (W - 20);
            # Fold it into nested ternaries, using the final branch as the
            # default since the guards are written to cover every case.
            expr = js_expr_to_canonical(items[-1][2])
            for pos, cond, val in reversed(items[:-1]):
                expr = (f"({js_expr_to_canonical(val)}) "
                        f"if ({_cond_to_expr(cond)}) else ({expr})")
            found.append((items[0][0], var, expr))
            continue

        expr = prior
        for pos, cond, val in items:
            expr = (f"({js_expr_to_canonical(val)}) "
                    f"if ({_cond_to_expr(cond)}) else ({expr})")
        found.append((items[-1][0], var, expr))

    if spans_out is not None:
        spans_out.extend(consumed)
    return found


def _first_index(body: str, name: str) -> int:
    m = re.search(r"(?<![\w.])" + re.escape(name) + r"\b", body)
    return m.start() if m else len(body)


def _ladder_before(tables: list, pos: int):
    """The ladder whose branches most recently executed before `pos`."""
    best = None
    for t in tables:
        if t.end <= pos and (best is None or t.end > best.end):
            best = t
    return best or (tables[0] if tables else None)


MODULE_CONST = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*=\s*(-?\d+(?:\.\d+)?)\s*;")


def module_constants(js: str) -> tuple[dict[str, float], dict[str, list[float]]]:
    """Constants assigned outside the calculator function, and the ambiguous ones.

    Several calculators park their coefficients in setup functions rather than in
    `_fx()` -- Framingham keeps one full coefficient set per sex in `varload1()`
    and `varload2()`. A name assigned exactly one value module-wide can be
    adopted safely. A name assigned SEVERAL different values is variant-
    dependent, and picking one would silently return another patient group's
    answer, so those are reported instead of guessed.
    """
    seen: dict[str, list[float]] = {}
    for m in MODULE_CONST.finditer(js):
        name, val = m.group(1), float(m.group(2))
        seen.setdefault(name, [])
        if val not in seen[name]:
            seen[name].append(val)
    single = {n: v[0] for n, v in seen.items() if len(v) == 1}
    ambiguous = {n: v for n, v in seen.items() if len(v) > 1}
    return single, ambiguous


# `<expr> <Identifier> =` with no separator: a semicolon the PDF text lost.
LOST_SEMI = re.compile(
    r"(?<=[\w)\]])\s+([A-Za-z_]\w*)\s*=\s*(?![=>])"
)


_CONTROL_KW = re.compile(r"\b(?:if|while|for|switch|catch)\s*$")


def _closes_control_header(body: str, pos: int) -> bool:
    """Whether the `)` just before `pos` closes an if/while/for header."""
    j = pos - 1
    while j >= 0 and body[j].isspace():
        j -= 1
    if j < 0 or body[j] != ")":
        return False
    depth = 0
    while j >= 0:
        if body[j] == ")":
            depth += 1
        elif body[j] == "(":
            depth -= 1
            if depth == 0:
                return bool(_CONTROL_KW.search(body[max(0, j - 12):j]))
        j -= 1
    return False


def _resplit_lost_semicolons(body: str) -> str:
    """Reinstate statement boundaries the PDF text layer dropped."""
    def repl(m: re.Match) -> str:
        name = m.group(1)
        # `a = b` inside a condition or a comparison is not a new statement.
        return f"; {name} = "
    out, last = [], 0
    for m in LOST_SEMI.finditer(body):
        # only split when the text before really looks like a finished expression
        before = body[max(0, m.start() - 40):m.start()]
        if before.rstrip().endswith((",", "(", "&&", "||", "=", "<", ">")):
            continue
        # `if (isNaN(x)) x = 0;` is one guarded statement. Splitting after the
        # header turns its body into an unconditional assignment -- which pinned
        # TPN's volumes to zero and made it divide by nothing.
        if _closes_control_header(body, m.start()):
            continue
        out.append(body[last:m.start()])
        out.append(f"; {m.group(1)} = ")
        last = m.end()
    out.append(body[last:])
    return "".join(out)


def parse_fx(js: str) -> ParsedScript:
    out = ParsedScript()
    js = normalize_text(js)

    m = re.search(r"function\s+(\w+)_fx\s*\(", js)
    if m:
        out.form_name = m.group(1)

    body = _slice_function(js, r"\w+_fx")
    if body is None:
        return out

    # A page drawn twice can cut a statement in half inside a long lookup
    # ladder, leaving an unmatched brace that closes the function early. WHO's
    # 2-5 year charts lost three of their four results that way. De-duplicating
    # the script and re-slicing recovers them; it is kept only if it does.
    _OUT_WRITE = re.compile(r"(\w+)_param\s*\.\s*value\s*=\s*fixDP")
    if len(_OUT_WRITE.findall(body)) < len(_OUT_WRITE.findall(js)):
        from .dedup import collapse_repeats
        _fixed = _slice_function(collapse_repeats(js), r"\w+_fx")
        if _fixed and (len(_OUT_WRITE.findall(_fixed))
                       > len(_OUT_WRITE.findall(body))):
            # De-duplication also deletes text, and what it deleted here were
            # two of the three field reads. The repaired body is used for
            # everything downstream, but the fields are taken from both so a
            # recovered result cannot cost an input.
            body_variants = [_fixed, body]
            body = _fixed
        else:
            body_variants = [body]
    else:
        body_variants = [body]

    if "decpts" in body:
        out.has_decimal_precision = True

    # A radio whose branches BRANCH AGAIN selects a whole coefficient table, not
    # a single coefficient. Flattening it into branches on both conditions has
    # to happen BEFORE anything reads the body: the case-split reader and the
    # plain statement scan are reconciled by source position, so they must be
    # looking at the same text or a folded branch is re-applied unconditionally
    # afterwards -- which pinned ACC/AHA's survival term to the last block.
    from . import engine_radio as _radio
    _nested = [g for g in _radio.nested_radio_groups(body)
               if g.name not in {x.name for x in _radio.find_radio_groups(body)}]
    if _nested:
        body = _radio.flatten_nested_radio(body, {g.name for g in _nested})

    # --- inputs, in form order ---
    # Two read styles exist: a free-text field (`X_param.value`) and a
    # point-selection dropdown (`X_pulldown.options[...].value`), the latter used
    # by scoring calculators such as APACHE II where each option carries its own
    # score. Both are inputs; missing the dropdown form left every one of those
    # identifiers unresolved in the expressions that consume them.
    seen: set[str] = set()
    for _b in body_variants:
        for mm in re.finditer(
            r"parseFloat\s*\(\s*(\w+?)_(?:param\s*\.\s*value"
            r"|pulldown\s*\.\s*options)",
            _b,
        ):
            name = mm.group(1)
            if name not in seen:
                seen.add(name)
                out.inputs.append(name)
                if "pulldown" in mm.group(0):
                    out.select_inputs.add(name)

    skip_lhs = {"param_value", "unit_parts", "doCalc", "dp", "calctxt", "xmltxt",
                "htmtxt", "interptxt", "interphtm", "interpxml", "xmlresult",
                "postNow", "printing"}

    # --- outputs: `if (doCalc) X_param.value = fixDP(...)` ---
    for mm in re.finditer(r"(\w+)_param\s*\.\s*value\s*=\s*fixDP", body):
        if mm.group(1) not in out.outputs:
            out.outputs.append(mm.group(1))
    # `ToxLevel = fixDP(...)` is the result even when it is never written
    # back through `_param.value` (interpretation-only calculators).
    # Only used when the usual write-back was not found -- otherwise every
    # `Value = fixDP(Value, n)` rounding line would become a phantom output.
    if not out.outputs:
        for mm in re.finditer(
            r"(?:var\s+)?([A-Za-z_]\w*)\s*=\s*fixDP\s*\(", body
        ):
            name = mm.group(1)
            if (name in skip_lhs or name.endswith("_param") or name in out.inputs
                    or name.lower() in ("value", "result", "tmp", "temp")):
                continue
            if name not in out.outputs:
                out.outputs.append(name)

    # --- steps: plain assignments that are not template plumbing ---
    # `X = param_value * parseFloat(unit_parts[0]) + parseFloat(unit_parts[1])`
    unit_affine = re.compile(r"param_value\s*\*\s*parseFloat\s*\(\s*unit_parts")

    # The PDF text layer occasionally drops a semicolon, gluing two statements
    # together: "... * Math.pow(0.996, Age) * Sex eGFRcys = Math.round(...)".
    # Splitting on ";" alone then yields one malformed assignment and loses the
    # second variable entirely, so re-split where an identifier is immediately
    # followed by "=" in the middle of an expression.
    body = _resplit_lost_semicolons(body)

    positioned: list[tuple[int, str, str]] = []
    offset = 0
    for stmt in re.split(r";", body):
        start = offset
        offset += len(stmt) + 1
        # A statement often arrives with the tail of the previous block glued to
        # it ("}} MELD_Score = ..."), because the split is on `;` alone. Trim any
        # leading brace/paren/newline noise before looking for an assignment,
        # otherwise every assignment that follows an if/for block is missed.
        s = stmt.strip().lstrip("}){ \t\r\n")
        s = s.strip()
        if not s or "==" in s.split("=")[0]:
            continue
        mm = re.match(r"^(?:var\s+)?([A-Za-z_]\w*)\s*=\s*(.+)$", s, re.S)
        if not mm:
            continue
        lhs, rhs = mm.group(1), mm.group(2).strip()
        if lhs in skip_lhs or lhs.endswith("_param") or "." in lhs:
            continue
        if unit_affine.search(rhs):
            continue                      # input unit conversion, handled by units
        if rhs.startswith("null") or rhs in ("true", "false", "''", '""'):
            continue
        if "_param" in rhs or "options[" in rhs:
            continue
        if "fixDP" in rhs:
            # unwrap fixDP(expr, n) so the assignment is a real step
            rhs = re.sub(
                r"fixDP\s*\((.+),\s*-?\d+\s*\)\s*$", r"\1", rhs, flags=re.S
            )
            # `ToxLevel = fixDP(ToxLevel, 1)` is rounding, not a new formula.
            # Keeping it as last-write would overwrite the real equation
            # with a self-reference.
            if re.fullmatch(rf"{re.escape(lhs)}\s*", rhs.strip()):
                continue
        positioned.append((start, lhs, js_expr_to_canonical(rhs)))

    # Radio-button parameter groups are patient-selectable coefficients, not
    # constants. Left to the statement scan they collapse to whichever branch
    # was written last -- silently computing every patient as the final option.
    from . import engine_radio
    out.unit_modes = unit_mode_fields(body)
    out.radio_groups = engine_radio.find_radio_groups(body)
    if _nested:
        out.radio_groups.extend(
            g for g in _nested
            if g.name not in {x.name for x in out.radio_groups})
    out.input_overrides = engine_radio.find_input_overrides(body, set(out.inputs))
    # Coefficient tables (Framingham Age_Factor, etc.) live in helper
    # loaders, not the main _fx body.
    extra = engine_radio.find_radio_groups(js)
    seen = {g.name for g in out.radio_groups}
    for g in extra:
        if g.name not in seen:
            out.radio_groups.append(g)
            seen.add(g.name)
    radio_vars = {v for g in out.radio_groups for v in g.variables}
    if radio_vars:
        positioned = [t for t in positioned if t[1] not in radio_vars]

    # Conditional assignments (clamps, case splits) are invisible to the
    # statement scan above, so recover them as ternaries and merge BY SOURCE
    # POSITION. Appending them instead would reorder execution: a clamp written
    # halfway through the function would run after everything that reads it.
    existing = {n: e for _, n, e in positioned}
    cond_spans: list = []
    conditionals = extract_conditional_assignments(
        body, existing, protected=set(out.inputs) | radio_vars, spans_out=cond_spans
    )
    # A branch body's own assignments were already folded into the ternary above.
    # Left in, they sit at a LATER source position than the ternary and the
    # last-write rule reinstates whichever branch happened to be written last --
    # exactly the CKD-EPI defect, where every patient got the male coefficients.
    if cond_spans:
        cond_names = {n for _, n, _ in conditionals}
        positioned = [
            t for t in positioned
            if not (t[1] in cond_names
                    and any(lo <= t[0] < hi for lo, hi in cond_spans))
        ]
    positioned += conditionals
    positioned.sort(key=lambda t: t[0])

    # A step must never shadow an input. Calculators declare `var Age = 0;`
    # alongside the field they read into `Age`, and treating that as a step
    # OVERWRITES the patient's value with zero -- silently returning a result
    # computed from nothing rather than failing. Unit re-assignment of an input
    # is likewise the unit system's job, not a step's.
    inputs_set = set(out.inputs)
    positioned = [t for t in positioned if t[1] not in inputs_set]

    # JS keeps the last write; emit each name once, at its final position.
    last: dict[str, tuple[int, str]] = {}
    for pos, name, expr in positioned:
        last[name] = (pos, expr)
    out.steps = [(n, e) for n, (p, e) in
                 sorted(last.items(), key=lambda kv: kv[1][0])]

    # `AA_Vol = parseFloat(Amino_Acids_param.value)` renames an input rather than
    # computing anything. Emitted as a step it shadows nothing useful, and the
    # empty-field guard beside it then pins it to zero.
    for am in re.finditer(
        r"(?<![\w.])([A-Za-z_]\w*)\s*=\s*parseFloat\s*\(\s*(\w+)_param\s*\.\s*value\s*\)",
        body,
    ):
        local, field = am.group(1), am.group(2)
        if local == field or field not in out.inputs:
            continue
        out.input_aliases[local] = field

    # A lookup ladder (CDC/WHO growth charts) writes the same few variables in
    # hundreds of branches. Those are table data, not computation steps -- left
    # in, the last branch silently wins for every patient.
    from . import engine_lms
    tables = engine_lms.extract_all(body)
    if tables:
        drop: set[str] = set()
        for t in tables:
            drop |= engine_lms.branch_assigned_names(t)
        out.steps = [(n, e) for n, e in out.steps if n not in drop]

        # Parallel variants (same key, same span -- the girls' ladder and the
        # boys') must NOT be namespaced: exactly one applies per patient, so they
        # share column names. Only sequential ladders, which run one after the
        # other and both feed the same formula, need distinct columns.
        def _span(t):
            th = [r["threshold"] for r in t.rows]
            return (t.key_var, min(th), max(th)) if th else None

        # Variants come in equal-sized groups per key (one per sex). Ranges
        # differ slightly between the sexes, so compare the SHAPE -- how many
        # ladders share each key -- rather than exact spans.
        keys = [t.key_var for t in tables]
        per_key = {k: keys.count(k) for k in set(keys)}
        parallel = len(tables) > 1 and len(set(per_key.values())) == 1 \
            and next(iter(per_key.values())) > 1

        if len(tables) > 1 and not parallel:
            # Several ladders reuse the same column names (L/M/S), each feeding
            # the assignment that follows it. Namespace per ladder, then rewrite
            # each consumer to the ladder that most recently preceded it in the
            # source -- which is exactly what sequential execution would do.
            for idx, t in enumerate(tables, start=1):
                t.suffix = f"_{idx}"
            renamed: list[tuple[str, str]] = []
            for name, expr in out.steps:
                pos = _first_index(body, name)
                t = _ladder_before(tables, pos)
                if t is not None:
                    expr = rename_vars(
                        expr, {c: c + t.suffix for c in t.columns}
                    )
                renamed.append((name, expr))
            out.steps = renamed
            out.output_ladder = {}
            for o in out.outputs:
                pos = _first_index(body, o + "_param")
                t = _ladder_before(tables, pos)
                if t is not None:
                    out.output_ladder[o] = t.suffix
        out.lookup_tables = tables
        out.lookup_table = tables[0]

    # Adopt module-level constants the function body references but never sets.
    single, ambiguous = module_constants(js)
    defined = {n for n, _ in out.steps} | set(out.inputs) | set(out.outputs)
    referenced: set[str] = set()
    for _, expr in out.steps:
        referenced |= set(re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?![\w(])", expr))
    for name in sorted(referenced - defined):
        if name in single:
            out.steps.insert(0, (name, repr(single[name])))
        elif name in ambiguous:
            out.ambiguous_constants[name] = ambiguous[name]

    out.bounds = parse_min_max(js)
    return out


_ALERT_MIN = re.compile(
    r'alert\s*\(\s*"The minimum value for\s+(?P<label>.+?)\s+is\s+'
    r'(?P<val>-?[\d.]+)\s*(?P<unit>[^.".]*?)\s*\.',
    re.S,
)
_ALERT_MAX = re.compile(
    r'alert\s*\(\s*"The maximum value for\s+(?P<label>.+?)\s+is\s*'
    r'(?P<val>-?[\d.]+)\s*(?P<unit>[^.".]*?)\s*\.',
    re.S,
)


def parse_min_max(js: str) -> dict[str, Bound]:
    """Recover per-field bounds from minMaxCheck().

    Two independent signals are cross-checked:
      * the numeric comparison   `if (X_param.value && (X < (0 - 0.00001)))`
      * the alert() text         "The minimum value for Age is 0 yr."
    The alert additionally yields the printed label and the field's BASE unit,
    which is the unit every bound is expressed in.
    """
    body = _slice_function(js, "minMaxCheck")
    if body is None:
        return {}
    body = normalize_text(body)
    bounds: dict[str, Bound] = {}

    def get(name: str) -> Bound:
        return bounds.setdefault(name, Bound())

    # numeric comparisons
    for mm in re.finditer(
        r"if\s*\(\s*(\w+)_param\s*\.\s*value\s*&&\s*\(?\s*(\w+)\s*<\s*\(?\s*(-?[\d.]+)",
        body,
    ):
        get(mm.group(1)).min = float(mm.group(3))
    for mm in re.finditer(
        r"if\s*\(\s*(\w+)_param\s*\.\s*value\s*&&\s*\(?\s*(\w+)\s*>\s*\(?\s*(-?[\d.]+)",
        body,
    ):
        get(mm.group(1)).max = float(mm.group(3))

    # alert text -> label + base unit (and a redundant copy of the bound)
    chunks = re.split(r"(?=if\s*\()", body)
    for chunk in chunks:
        fm = re.search(r"(\w+)_param\s*\.\s*value", chunk)
        if not fm:
            continue
        b = get(fm.group(1))
        for rx, is_min in ((_ALERT_MIN, True), (_ALERT_MAX, False)):
            am = rx.search(chunk)
            if not am:
                continue
            label = re.sub(r"\s+", " ", am.group("label")).strip()
            unit = re.sub(r"\s+", " ", am.group("unit")).strip() or None
            val = float(am.group("val"))
            b.label = b.label or label
            b.base_unit = b.base_unit or unit
            if is_min:
                b.min = val if b.min is None else b.min
                b.min_message = f"The minimum value for {label} is {am.group('val')}{' ' + unit if unit else ''}."
            else:
                b.max = val if b.max is None else b.max
                b.max_message = f"The maximum value for {label} is {am.group('val')}{' ' + unit if unit else ''}."

    # `X < (0 - 0.00001)` is EBMcalc's float-tolerant ">= 0"; snap it back.
    for b in bounds.values():
        if b.min is not None and abs(b.min - round(b.min)) < 1e-4:
            b.min = float(round(b.min))
    return bounds
