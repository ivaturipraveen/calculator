"""Block-structured parsing and symbolic execution of the calculators' JavaScript.

Both vendor engines were previously read with statement-level regexes, which
cannot see block structure. That silently produced wrong medicine, because the
source is full of case splits and the last textual assignment won:

    var ibw = 0;
    if (gender == MALE) { ibw = 50   + 2.3 * (height - 60); }
    else                { ibw = 45.5 + 2.3 * (height - 60); }

A statement scan sees two plain assignments to `ibw` and keeps the second, so
every patient is dosed as female. The same flattening hit sex/race coefficients
in eGFR, route selection in acetylcysteine, weight-band fluid volumes, renal
dose intervals -- specs that validated cleanly while being wrong.

So the JS is parsed into a real statement tree and then symbolically executed
into our expression language. At a branch, both sides run and their results are
merged into a conditional expression -- an SSA phi node:

    ibw = (50 + 2.3 * (height - 60)) if (gender == 'male')
          else (45.5 + 2.3 * (height - 60))

Two properties make this safe rather than merely convenient. Merging is
*conditional*, so no branch is ever dropped or preferred; and the merged form is
lazy, so a guard that exists to prevent a division by zero still prevents it
after translation.

Only the subset the calculators actually use is modelled: assignment,
if/else-if/else, and the DOM read/write calls. Loops, closures and everything
else are skipped rather than guessed at, and a condition that cannot be
translated aborts that branch's merge and is reported instead of flattened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

IDENT = r"[A-Za-z_$][\w$]*"
_KEYWORDS = re.compile(
    r"(if|else|for|while|function|return|switch|do|try|catch|break|continue|new)\b"
)
_ASSIGN = re.compile(
    rf"^(?:(?:var|let|const)\s+)?({IDENT})\s*(\+=|-=|\*=|/=|=)(?!=)\s*(.*)$", re.S
)
_CALL = re.compile(rf"^({IDENT})\s*\(", re.S)


# --------------------------------------------------------------------------
# statement tree
# --------------------------------------------------------------------------

@dataclass
class Assign:
    name: str
    expr: str
    op: str = "="
    pos: int = 0


@dataclass
class If:
    cond: str
    then: list = field(default_factory=list)
    els: list = field(default_factory=list)
    pos: int = 0


@dataclass
class Call:
    fn: str
    args: list[str] = field(default_factory=list)
    pos: int = 0


@dataclass
class Ret:
    pos: int = 0


# A line comment ends at a newline -- except the PDF text layer does not
# preserve the source's line breaks, so `// Cockcroft-Gault Method` and the
# statement that followed it arrive on one line. Stripping to the next newline
# then deletes real code: this is how `crcl = ((140 - age) * weight) / (72 *
# srcr)` vanished from the aminoglycoside dose, leaving a spec that computed
# creatinine clearance as the constant zero. So a comment also ends wherever
# something that can only be code resumes.
_CODE_RESUMES = re.compile(
    r"\b(?:var|let|const|if|else|return|function|for|while|switch)\b"
    r"|\b[A-Za-z_$][\w$]*\s*(?:\+|-|\*|/)?=(?!=)"
    r"|\b(?:set|setNumber|setHTML|setText|clear|clearOutput|alert|document)\s*\("
)


from .dedup import collapse_repeats, imbalance, is_truncated  # noqa: E402


def parse_block_repaired(body: str) -> tuple[list, str]:
    """Parse a body, de-duplicating first if that recovers more of it.

    A page drawn twice can cut a statement at the seam, leaving an unclosed
    brace that makes the parser swallow everything after it. Bracket balance is
    not a reliable acceptance test -- the slice has usually already run past the
    damage -- so the honest measure is whether de-duplication yields more
    statements. If it does, the seam was real; if not, nothing is changed.
    """
    plain = parse_block(body)
    if not is_truncated(body):
        return plain, body
    fixed = collapse_repeats(body)
    if fixed == body:
        return plain, body
    alt = parse_block(fixed)
    if _weight(alt) > _weight(plain):
        return alt, fixed
    return plain, body


def _weight(stmts: list) -> int:
    """Statements recovered, counting nested branches."""
    n = 0
    for st in stmts:
        n += 1
        if isinstance(st, If):
            n += _weight(st.then) + _weight(st.els)
    return n


def strip_comments(js: str) -> str:
    """Remove comments without eating code, and without tripping on strings."""
    out, i, n = [], 0, len(js)
    while i < n:
        ch = js[i]
        if ch in "'\"":
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == ch:
                    j += 1
                    break
                j += 1
            out.append(js[i:j])
            i = j
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "*":
            k = js.find("*/", i + 2)
            i = n if k < 0 else k + 2
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "/":
            nl = js.find("\n", i)
            m = _CODE_RESUMES.search(js, i + 2)
            end = min(nl if nl >= 0 else n, m.start() if m else n)
            out.append(" ")
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _balanced_after(src: str, open_idx: int) -> str:
    """Contents of the brace block that starts at `open_idx`."""
    depth = 0
    for j in range(open_idx, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1: j]
    return src[open_idx + 1:]


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _balanced(s: str, i: int, op: str, cl: str) -> tuple[str, int]:
    """`s[i]` is `op`; return its contents and the index just past the closer.

    String literals are tracked so a brace or paren inside a note's text cannot
    unbalance the scan -- these functions build their messages inline.
    """
    depth, j, quote = 0, i, None
    while j < len(s):
        ch = s[j]
        if quote:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == op:
            depth += 1
        elif ch == cl:
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
        j += 1
    return s[i + 1 :], len(s)          # unterminated: the PDF cut it off


# A statement can also end without its semicolon: the vendor relies on JS
# automatic semicolon insertion at a line break, and the PDF text layer then
# drops the line break too, gluing the next statement on -- `crcl = (98 - 0.8 *
# (age - 20)) / srcr if (gender == FEMALE) { ... }`. Reading to the next `;`
# swallows the `if`, so the female adjustment is lost. These keywords can only
# begin a statement, never continue an expression, so they terminate one.
# A write call also begins a statement. The PDF text layer drops semicolons, so
# `rate = roundToNumberOfDecimals(rate, 1) setNumber(ID_RATE, rate, 1);` arrives
# as one run -- and without recognising `setNumber` as a fresh statement the
# result write is swallowed into the assignment and the calculator has no output.
_STMT_STARTS = re.compile(
    r"\b(?:if|var|let|const|return|for|while|function|switch"
    r"|set|setNumber|setHTML|setText|clear|clearOutput|alert)\b")


def _read_simple(s: str, i: int) -> tuple[str, int]:
    """Read one simple statement: to the next `;`, `}`, or start of a new one.

    A statement whose brackets never close is not a parse failure to give up on
    -- it is a statement the PDF truncated. Titration calculators declare a dose
    ladder as `new Array(0.5, 1.5, ...)` that runs off the page, and treating the
    unclosed paren as "keep reading" swallowed the entire remainder of the
    function, including the write that produces the result. Recovery re-scans
    for the next `;` so everything after the damaged statement still parses.
    """
    depth, j, quote = 0, i, None
    while j < len(s):
        ch = s[j]
        if quote:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0 and ch == "}":
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            return s[i:j], j + 1
        elif depth == 0 and j > i and (ch.isalpha() or ch == "_"):
            m = _STMT_STARTS.match(s, j)
            if m and not (s[j - 1].isalnum() or s[j - 1] in "_$."):
                return s[i:j], j
        j += 1

    if depth > 0:
        k = _next_semicolon(s, i)
        if k is not None:
            return s[i:k], k + 1
    return s[i:j], j


def _next_semicolon(s: str, i: int) -> Optional[int]:
    """First `;` at or after `i` that is not inside a string."""
    quote = None
    for j in range(i, len(s)):
        ch = s[j]
        if quote:
            if ch == "\\":
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            return j
    return None


def _skip_construct(s: str, i: int) -> int:
    """Step over a construct we do not model, header and body together.

    When the body's brace never closes -- a page seam cut the loop in half --
    consuming to the end of the source discards every statement after it,
    including the write that produces the result. Falling back to the first
    closing brace, or failing that the next statement boundary, keeps the rest
    of the function parseable.
    """
    j = i
    while j < len(s) and s[j] not in "({;":
        j += 1
    if j < len(s) and s[j] == "(":
        _, j = _balanced(s, j, "(", ")")
    j = _skip_ws(s, j)
    if j < len(s) and s[j] == "{":
        start = j
        _, j = _balanced(s, start, "{", "}")
        if j >= len(s):                    # never closed: recover
            k = s.find("}", start + 1)
            if k >= 0:
                return k + 1
            k = _next_semicolon(s, start + 1)
            if k is not None:
                return k + 1
        return j
    _, j = _read_simple(s, j)
    return j


def _classify(src: str, pos: int) -> Optional[object]:
    s = src.strip().lstrip("{}) \t\r\n").strip()
    if not s:
        return None
    m = _ASSIGN.match(s)
    if m and "." not in m.group(1) and "[" not in m.group(1):
        return Assign(name=m.group(1), expr=m.group(3).strip(), op=m.group(2), pos=pos)
    m = _CALL.match(s)
    if m:
        k = s.index("(")
        inner, _ = _balanced(s, k, "(", ")")
        return Call(fn=m.group(1), args=_split_args(inner), pos=pos)
    return None


def _split_args(inner: str) -> list[str]:
    args, depth, cur, quote = [], 0, "", None
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def parse_block(src: str, base: int = 0) -> list:
    """Parse a function body into a flat list of statements and nested `If`s."""
    out: list = []
    i, n = 0, len(src)
    while i < n:
        i = _skip_ws(src, i)
        if i >= n:
            break
        ch = src[i]
        if ch in ";}":
            i += 1
            continue
        if ch == "{":
            inner, j = _balanced(src, i, "{", "}")
            out.extend(parse_block(inner, base + i + 1))
            i = j
            continue
        m = _KEYWORDS.match(src, i)
        if m:
            kw = m.group(1)
            if kw == "function":
                # A damaged body can run past its own closing brace, so stop at
                # the next declaration rather than absorbing the statements of
                # the function that follows.
                break
            if kw == "if":
                stmt, i = _parse_if(src, i, base)
                if stmt is not None:
                    out.append(stmt)
                continue
            if kw == "return":
                _, i = _read_simple(src, i)
                out.append(Ret(pos=base + i))
                continue
            if kw == "else":
                i += 4
                continue
            if kw == "new":
                pass                       # part of an expression, not a construct
            else:
                i = _skip_construct(src, i)
                continue
        stmt_src, j = _read_simple(src, i)
        st = _classify(stmt_src, base + i)
        if st is None:
            # A `//` comment that wrapped onto the next line leaves its tail
            # behind as prose ("Predicted Trough at Steady State (Ctr)") glued
            # to the declaration that follows. The statement will not classify,
            # but the declaration inside it is real -- recover from there rather
            # than discarding both.
            m2 = _DECL_INSIDE.search(stmt_src)
            if m2 and m2.start() > 0:
                st = _classify(stmt_src[m2.start():], base + i + m2.start())
        if st is not None:
            out.append(st)
        i = j if j > i else i + 1
    return out


_DECL_INSIDE = re.compile(r"(?:var|let|const)\s+[A-Za-z_]\w*\s*=(?!=)")


def _parse_branch(src: str, j: int, base: int) -> tuple[list, int]:
    j = _skip_ws(src, j)
    if j < len(src) and src[j] == "{":
        inner, k = _balanced(src, j, "{", "}")
        return parse_block(inner, base + j + 1), k
    if _KEYWORDS.match(src, j) and src[j : j + 2] == "if":
        st, k = _parse_if(src, j, base)
        return ([st] if st is not None else []), k
    stmt_src, k = _read_simple(src, j)
    st = _classify(stmt_src, base + j)
    return ([st] if st is not None else []), k


def _parse_if(src: str, i: int, base: int) -> tuple[Optional[If], int]:
    j = _skip_ws(src, i + 2)
    if j >= len(src) or src[j] != "(":
        return None, i + 2
    cond, j = _balanced(src, j, "(", ")")
    then, j = _parse_branch(src, j, base)
    els: list = []
    k = _skip_ws(src, j)
    if src[k : k + 4] == "else" and not re.match(r"\w", src[k + 4 : k + 5] or " "):
        k = _skip_ws(src, k + 4)
        els, k = _parse_branch(src, k, base)
        j = k
    return If(cond=cond.strip(), then=then, els=els, pos=base + i), j


# --------------------------------------------------------------------------
# symbolic execution
# --------------------------------------------------------------------------

@dataclass
class Resolver:
    """Engine-specific translation hooks.

    expr      JS expression  -> our expression language, identifiers canonical
    cond      JS condition   -> ditto, or None when it cannot be translated
    key       JS identifier  -> canonical key
    output_of a write call    -> (key, value_expr, decimals) or None
    note_of   a write call    -> literal note text or None
    """

    expr: Callable[[str], Optional[str]]
    cond: Callable[[str], Optional[str]]
    key: Callable[[str], str]
    output_of: Callable[[Call], Optional[tuple]] = lambda c: None
    note_of: Callable[[Call], Optional[str]] = lambda c: None
    inputs: set = field(default_factory=set)
    # Input keys the calculation never actually reads -- a results field the
    # page also happens to expose. Assigning to one of these IS a computation,
    # not the re-read of a field, so it must not be suppressed as shadowing.
    unread_inputs: set = field(default_factory=set)
    # Every name the script actually declares, in its original spelling. Used
    # to recognise comment prose that lost its `//` in the PDF: Fentanyl's
    # "Rate = InfusionDoseWB/Concentration" is a note about the formula, not an
    # assignment, and executing it overwrote the rate with two undefined names.
    declared: set = field(default_factory=set)
    # Branches another pass already accounted for -- unit conversions become the
    # field's unit dropdown, so reporting them as untranslatable would bury the
    # branches that genuinely are.
    handled: set = field(default_factory=set)


@dataclass
class Result:
    steps: list[tuple[str, str]] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    string_compares: dict = field(default_factory=dict)   # key -> [literal, ...]


_MAX_EXPR = 4000


def _carries_value(stmts: list) -> bool:
    """Whether a branch does anything we model, i.e. is worth translating.

    `if (checkInputsAreValid() == 0) return;` and the display-toggling branches
    assign nothing; reporting those as untranslatable would bury the branches
    that genuinely were.
    """
    for st in stmts:
        if isinstance(st, Assign):
            return True
        if isinstance(st, Call):
            return True
        if isinstance(st, If) and (
            _carries_value(st.then) or _carries_value(st.els)
        ):
            return True
    return False


class _Exec:
    def __init__(self, rv: Resolver):
        self.rv = rv
        self.res = Result()
        self.versions: dict[str, int] = {}
        # Conditions guarding the statement being executed. A result written
        # inside a branch replaces the unconditional one only under its guard --
        # aminoglycoside dosing overwrites the numeric dose with the word
        # "Conventional" when clearance is too low, and taking the last write
        # unconditionally made the dose always read "Conventional".
        self.path: list[str] = []
        # field key -> derived key, once a dropdown code has been resolved
        self.derived: dict[str, str] = {}

    # -- expression plumbing ------------------------------------------------

    def _subst(self, expr: str, env: dict[str, str]) -> str:
        """Replace each identifier by whatever it currently stands for."""
        if not env:
            return expr
        names = sorted(env, key=len, reverse=True)
        pat = re.compile(
            r"(?<![\w.'\"])(" + "|".join(re.escape(n) for n in names) + r")(?![\w(])"
        )

        def repl(m: re.Match) -> str:
            val = env[m.group(1)]
            return val if re.fullmatch(r"[\w.]+", val) else f"({val})"

        return pat.sub(repl, expr)

    def _emit(self, key: str, expr: str) -> str:
        """Record a top-level definition, versioning a name assigned twice."""
        self.versions[key] = self.versions.get(key, 0) + 1
        n = self.versions[key]
        ssa = key if n == 1 else f"{key}_v{n}"
        self.res.steps.append((ssa, expr))
        return ssa

    # -- statements ---------------------------------------------------------

    def run(self, stmts: list, env: dict[str, str], defer: bool) -> None:
        for st in stmts:
            if isinstance(st, Assign):
                self._assign(st, env, defer)
            elif isinstance(st, If):
                self._if(st, env, defer)
            elif isinstance(st, Call):
                self._call(st, env, defer)

    _WORD = re.compile(r"[A-Za-z_$][\w$]*")

    def _is_prose(self, st: Assign) -> bool:
        """An 'assignment' the script never declares either side of.

        The PDFs drop `//` from some comment lines, so a note explaining the
        formula arrives glued to the statement after it. Both halves give it
        away: JS is case-sensitive, and neither `Rate` nor `InfusionDoseWB` is
        ever declared -- whereas `rate` and `concentration` are.
        """
        if not self.rv.declared or st.name in self.rv.declared:
            return False
        return any(w not in self.rv.declared and not w.isupper()
                   for w in self._WORD.findall(st.expr or ""))

    _EMBEDDED = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*(=|\+=|-=|\*=|/=)(?!=)\s*")

    def _assign(self, st: Assign, env: dict[str, str], defer: bool) -> None:
        if self._is_prose(st):
            # The prose is glued to the statement that followed it, so the real
            # assignment is still in there -- take the last one whose target the
            # script actually declares. Dropping the whole run instead lost
            # Fentanyl's `rate = roundToNumberOfDecimals(rate, 2)`.
            tail = None
            for m in self._EMBEDDED.finditer(st.expr or ""):
                if m.group(1) in self.rv.declared:
                    tail = m
            if tail is None:
                return
            st = Assign(name=tail.group(1), expr=st.expr[tail.end():],
                        op=tail.group(2), pos=st.pos)
        key = self.rv.key(st.name)
        if key in self.rv.inputs and key not in self.rv.unread_inputs:
            return                     # unit handling / re-reads are not steps
        raw = self.rv.expr(st.expr)
        if raw is None:
            return
        if st.op != "=":
            prev = env.get(key, key)
            raw = f"({prev}) {st.op[0]} ({raw})"
        val = self._subst(raw, env)
        if len(val) > _MAX_EXPR:
            self.res.unresolved.append(
                {"kind": "expression_too_large", "name": key, "size": len(val)})
            return
        env[key] = val if defer else self._emit(key, val)

    def _if(self, st: If, env: dict[str, str], defer: bool) -> None:
        if not _carries_value(st.then) and not _carries_value(st.els):
            return                     # an input-validation guard, or DOM-only
        if st.cond in self.rv.handled:
            return
        cond_raw = self.rv.cond(st.cond)
        if cond_raw is None:
            # A guard with no `else` is an input-presence check -- "compute this
            # once the fields are filled in". Since evaluation only ever happens
            # with valid inputs, adopting the branch reproduces what the
            # calculator does; refusing it silently discards the arithmetic
            # inside, which is how Gestational Age lost every one of its
            # measurements. A branch that HAS an else is a real choice and is
            # still refused, because picking a side there would invent an answer.
            if (st.els or not _carries_value(st.then)
                    or not _is_presence_guard(st.cond)):
                self.res.unresolved.append(
                    {"kind": "condition_not_translatable",
                     "condition": st.cond[:160]})
                return
            self.res.unresolved.append(
                {"kind": "guard_assumed_satisfied", "condition": st.cond[:160]})
            self.run(st.then, env, defer)
            return
        cond = self._subst(cond_raw, env)

        then_env, else_env = dict(env), dict(env)
        self.path.append(cond)
        self.run(st.then, then_env, defer=True)
        self.path[-1] = f"not ({cond})"
        self.run(st.els, else_env, defer=True)
        self.path.pop()

        for key in list(then_env) + [k for k in else_env if k not in then_env]:
            t, e = then_env.get(key), else_env.get(key)
            if t == env.get(key) and e == env.get(key):
                continue               # untouched by both branches
            prior = env.get(key)
            t = t if t is not None else prior
            e = e if e is not None else prior
            if t is None or e is None:
                # Assigned on one side only, with no prior value. The vendor
                # relies on the other path never reading it; 0 keeps the merged
                # expression total without inventing a clinical number, and the
                # lazy conditional means it is only selected when unread.
                t, e = (t or "0"), (e or "0")
            if t == e:
                continue
            merged = f"({t}) if ({cond}) else ({e})"
            if len(merged) > _MAX_EXPR:
                self.res.unresolved.append(
                    {"kind": "expression_too_large", "name": key,
                     "size": len(merged)})
                continue
            env[key] = merged if defer else self._emit(key, merged)

    def _call(self, st: Call, env: dict[str, str], defer: bool) -> None:
        note = self.rv.note_of(st)
        if note:
            self.res.notes.append(note)
            return
        got = self.rv.output_of(st)
        if not got:
            return
        key, expr, decimals = got
        raw = self.rv.expr(expr)
        if raw is None:
            return
        val = self._subst(raw, env)
        existing = next((o for o in self.res.outputs if o["key"] == key), None)
        if existing is None:
            if self.path:
                guard = " and ".join(f"({c})" for c in self.path)
                val = f"({val}) if ({guard}) else (0)"
            self.res.outputs.append(
                {"key": key, "expr": val, "decimals": decimals})
            return
        if existing["expr"] == val:
            return
        if self.path:
            guard = " and ".join(f"({c})" for c in self.path)
            existing["expr"] = f"({val}) if ({guard}) else ({existing['expr']})"
        else:
            existing["expr"] = val
        if decimals is not None:
            existing["decimals"] = decimals


# A guard that only asks "has the user filled this in yet?" -- against zero, an
# empty string, or a validity helper. Adopting one of those reproduces what the
# calculator does, because evaluation only ever happens with valid inputs.
_PRESENCE_ATOM = re.compile(
    r"""^\s*!?\s*
        (?: \w+\s*\(\s*\)                          # checkInputsAreValid()
          | get\s*\(\s*\w+\s*\)                    # get(ID_AGE)
          | [\w.$]+
        )
        (?:\s*(?:===?|!==?|>=?|<=?)\s*
           (?:0|0\.0+|''|""|null|undefined|-1))?
        \s*$""",
    re.X,
)


def _is_presence_guard(cond: str) -> bool:
    """Is this guard a "fields are filled in" check rather than a real choice?

    Pediatric Dosing reads:

        if (frequency > 0 && getElementById(ID_DOSE_UNITS).selectedIndex > 4)
            outputDose = weight * dose * frequency / 24;

    Both halves are a choice, not a precondition: the second asks which unit the
    dose was entered in. Adopting it because there was no `else` made every
    result frequency/24 times the printed formula. A guard qualifies only if
    each of its terms tests presence -- against 0, empty, or a validity helper.
    """
    c = re.sub(r"\s+", " ", cond or "").strip()
    if not c:
        return False
    if re.search(r"\.(selectedIndex|checked|value|style|options|length)\b", c):
        return False
    for atom in re.split(r"&&|\|\|", c):
        if not _PRESENCE_ATOM.match(atom.strip().strip("()")):
            return False
    return True


def drop_page_seam(text: str, min_len: int = 25) -> str:
    """Remove a line the PDF drew twice across a page break.

    The exports repeat the last line of a page as the first line of the next,
    with only blank lines between:

        11| 0.09 * weight; } } else { doseMin = 7.6 * weight; ... {
        12|
        13| 0.09 * weight; } } else { doseMin = 7.6 * weight; ... {

    The braces no longer balance, so the block parser has to repair the body --
    and Ethanol lost the whole `if (route == ORAL)` branch, dosing an oral order
    with the intravenous coefficients. A repeat this exact, separated by nothing
    but blank lines, is the page seam and never the program.
    """
    lines = (text or "").split("\n")
    out: list[str] = []
    for line in lines:
        if line.strip() and len(line.strip()) >= min_len:
            back = len(out) - 1
            while back >= 0 and not out[back].strip():
                back -= 1
            if back >= 0 and out[back].strip() == line.strip() and back < len(out) - 1:
                continue
        out.append(line)
    return "\n".join(out)


def execute(stmts: list, rv: Resolver) -> Result:
    """Symbolically execute a parsed body into steps, outputs and notes."""
    ex = _Exec(rv)
    env: dict[str, str] = {}
    ex.run(stmts, env, defer=False)

    # An output whose expression is just a name is that step's value; keep the
    # step so the dependency is explicit rather than duplicating the expression.
    return ex.res


# --------------------------------------------------------------------------
# helpers shared by the engine adapters
# --------------------------------------------------------------------------

STRING_CONST = re.compile(
    rf"var\s+([A-Z][A-Z0-9_]*)\s*=\s*(['\"])(.*?)\2\s*;"
)


# `note =\nnote + '\n\nnote + '\nDose rounded...'` -- the assignment's tail drawn
# twice, the second copy landing INSIDE the string the first one opened.
_DOUBLED_TAIL = re.compile(r"(?<![\w.$])(\w+)\s*\+\s*(['\"])\s*\1\s*\+\s*\2")


def drop_doubled_string_tail(text: str) -> str:
    """Repair a fragment the PDF drew twice inside a string literal.

    Valganciclovir's oral-solution branch ends:

        note =
        note + '

        note + '
        Dose rounded to the nearest 10 mg increment...';

    `note + '` appears twice. The quote count goes odd, so everything after it
    parses as string content and the braces stop balancing -- which left the
    calculator's dose output built from the wrong branch entirely, guarded by
    the conditions of the other one, and answering 0 mg to a patient who should
    have had 900. The doubled tail is never the program: an assignment does not
    open two string literals in a row with nothing between them.
    """
    out = _DOUBLED_TAIL.sub(r"\1 + \2", text or "")
    return out


# `Dose(mcg) = Weight * Bolus Dose` -- the left of an assignment is never a
# call in JavaScript, so this line is prose.
_LHS_CALL = re.compile(r"(?<![\w.$])[A-Za-z_]\w*\s*\([^()]{0,60}\)\s*=(?!=)")
_ASSIGN_START = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*=(?!=)")


def drop_lhs_call_prose(text: str) -> str:
    """Remove a comment the PDF wrapped onto the next line.

    These exports lose `//` when a comment wraps, so the tail arrives glued to
    the statement below it:

        if (bolusDoseOption == 'yes') {
        Dose(mcg) = Weight * Bolus Dose          <- the rest of a comment
        calculatedBolusDoseMl = round(...);

    The first line parses as a CALL to `Dose`, which swallows the assignment
    after it -- so fentanyl's bolus volume was 0 whichever way the clinician
    answered. The giveaway is an assignment whose left side is a call; drop
    everything from there up to the last real assignment in the statement.
    """
    out, pos = [], 0
    for m in _LHS_CALL.finditer(text or ""):
        if m.start() < pos:
            continue
        end = (text or "").find(";", m.end())
        end = len(text) if end < 0 else end
        segment = text[m.start():end]
        # The call's own `=` has a `)` in front of it, so it is not among these;
        # anything here is a real assignment the comment landed on top of.
        later = list(_ASSIGN_START.finditer(segment))
        keep = later[-1].start() if later else len(segment)
        out.append(text[pos:m.start()])
        out.append(segment[keep:])
        pos = end
    out.append(text[pos:])
    return "".join(out)


def string_constants(js: str) -> dict[str, str]:
    """Module-level string constants, e.g. `var MALE = 'male';`.

    Conditions compare a dropdown's value against these, so without them the
    branch is untranslatable and the case split is lost.
    """
    return {m.group(1): m.group(3) for m in STRING_CONST.finditer(js)}


def collect_string_compares(stmts: list, keys: set[str],
                           consts: dict[str, str] | None = None
                           ) -> dict[str, list[str]]:
    """Literals each field is compared against -- i.e. its real option values.

    A field the JS only ever compares to a fixed set of strings is a dropdown,
    and those strings are its options. That is the only place the option VALUES
    survive in the PDF, which otherwise prints just the selected label.

    The comparison is usually written against a named constant -- `route == ORAL`
    -- so the constants have to be substituted first or the field looks like a
    number the clinician has to type.
    """
    found: dict[str, list[str]] = {}
    consts = consts or {}

    def literalise(cond: str) -> str:
        for name, val in sorted(consts.items(), key=lambda kv: -len(kv[0])):
            cond = re.sub(r"(?<![\w.$'\"])" + re.escape(name) + r"(?![\w])",
                          "'" + val.replace("'", "") + "'", cond)
        return cond

    def walk(sts: list) -> None:
        for st in sts:
            if isinstance(st, If):
                st_cond = literalise(st.cond)
                for m in re.finditer(
                    rf"({IDENT})\s*[=!]=\s*(?:'([^']*)'|\"([^\"]*)\")", st_cond
                ):
                    name, lit = m.group(1), (m.group(2) or m.group(3) or "")
                    if name in keys and lit:
                        found.setdefault(name, [])
                        if lit not in found[name]:
                            found[name].append(lit)
                for m in re.finditer(
                    rf"(?:'([^']*)'|\"([^\"]*)\")\s*[=!]=\s*({IDENT})", st_cond
                ):
                    name, lit = m.group(3), (m.group(1) or m.group(2) or "")
                    if name in keys and lit:
                        found.setdefault(name, [])
                        if lit not in found[name]:
                            found[name].append(lit)
                walk(st.then)
                walk(st.els)

    walk(stmts)
    return found
