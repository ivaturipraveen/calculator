"""A restricted arithmetic evaluator for extracted calculator expressions.

The expressions come from scraped PDFs, so `eval()` is not an option -- a
malicious or malformed source file would be arbitrary code execution inside the
API process. Instead we parse to an AST and walk it, permitting only arithmetic
node types and a fixed function whitelist.

This module is deliberately dependency-free so the FastAPI service can import it
directly as its compute core.
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any, Mapping


class ExpressionError(ValueError):
    """Raised for a malformed, unsafe, or non-evaluable expression."""


def _log10(x: float) -> float:
    return math.log10(x)


def _safe_div(a: float, b: float) -> float:
    if b == 0:
        raise ExpressionError("division by zero")
    return a / b


def _z_to_percentile(z: float) -> float:
    """Standard normal CDF, as a percentage.

    Growth-chart calculators call this `ZtoPercentile`; the vendor ships it as a
    1000-entry lookup, but the underlying function is exactly the normal CDF, so
    erf gives the same answer without carrying the table.
    """
    return 100.0 * 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _round_to_nearest(x: float, step: float) -> float:
    return round(x / step) * step


# Rounding helpers the vendor calls but does not print: they live in a shared
# library the export never includes, so they cannot be inlined from source and
# are supported natively instead. All are pure arithmetic.
FUNCTIONS: dict[str, Any] = {
    "z_to_percentile": _z_to_percentile,
    "ZtoPercentile": _z_to_percentile,
    "roundToNumberOfDecimals": lambda x, n=0: round(x, int(n)),
    "roundToNearest5": lambda x: _round_to_nearest(x, 5),
    "roundToNearest10": lambda x: _round_to_nearest(x, 10),
    "roundToNearest25": lambda x: _round_to_nearest(x, 25),
    "roundToNearest50": lambda x: _round_to_nearest(x, 50),
    "roundToNearest100": lambda x: _round_to_nearest(x, 100),
    # Engine-B output-unit selector (mg vs g). The numeric default is mg (=1);
    # a real selector is modelled as an input when the script exposes one.
    "getOutputUnit": lambda: 1.0,
    "lbs_to_kg": lambda x: x * 0.45359237,
    "kg_to_lbs": lambda x: x / 0.45359237,
    "in_to_cm": lambda x: x * 2.54,
    "cm_to_in": lambda x: x / 2.54,
    "mL_to_tsp": lambda x: x / 4.92892159375,
    "tsp_to_mL": lambda x: x * 4.92892159375,
    "exp": math.exp,
    "ln": math.log,
    "log": math.log,
    "log10": _log10,
    "sqrt": math.sqrt,
    "pow": math.pow,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "atan": math.atan,
}

# `doCalc` is EBMcalc's own "every input is valid" flag, which the vendor tests
# before writing a result. Evaluation only ever happens with valid inputs, so it
# is true by definition here; leaving it undefined stranded whole calculators on
# a guard that can never be false.
CONSTANTS: dict[str, float] = {
    "pi": math.pi, "e": math.e,
    "doCalc": 1.0, "docalc": 1.0, "do_calc": 1.0,
    "rbchk": 1.0, "showTable": 1.0, "showtable": 1.0, "show_table": 1.0,
    # Warning helpers return 0 for "no problem"; the guarded branch is the one
    # that runs for valid input.
    "retval": 0.0, "warn": 0.0, "warning": 0.0, "errcount": 0.0,
}

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
    ast.IfExp,
    ast.Compare,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)

_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: _safe_div,
    ast.Pow: lambda a, b: a ** b,
    ast.Mod: lambda a, b: a % b,
}

_CMPOPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


def normalize(expr: str) -> str:
    """Bring JS-flavoured operators into Python syntax."""
    e = expr.replace("&&", " and ").replace("||", " or ")
    return _negations_to_not(e)


def _negations_to_not(e: str) -> str:
    """`!x * y` is `(not x) * y`, not `not (x * y)`.

    JavaScript binds `!` tighter than `*`; Python binds `not` looser than every
    arithmetic operator. Substituting the word without bracketing its operand
    turned ACC/AHA's off-medication blood-pressure term into `not (0 * c *
    ln(sbp))` -- the number replaced by the constant 1, and a 55-year-old man's
    ten-year risk reported as 0.01% instead of 5.4%.
    """
    out: list[str] = []
    i, n = 0, len(e)
    while i < n:
        ch = e[i]
        if ch != "!" or (i + 1 < n and e[i + 1] == "="):
            out.append(ch)
            i += 1
            continue
        j = i + 1
        while j < n and e[j].isspace():
            j += 1
        # a run of leading `!`s negates the same operand
        if j < n and e[j] == "!" and not (j + 1 < n and e[j + 1] == "="):
            out.append("not ")
            i = j
            continue
        start = j
        if j < n and e[j] == "(":
            depth = 0
            while j < n:
                if e[j] == "(":
                    depth += 1
                elif e[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
        else:
            while j < n and (e[j].isalnum() or e[j] in "._"):
                j += 1
            if j < n and e[j] == "(":            # a call: take its arguments too
                depth = 0
                while j < n:
                    if e[j] == "(":
                        depth += 1
                    elif e[j] == ")":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
        if j == start:                            # nothing to negate
            out.append(" not ")
            i += 1
            continue
        out.append(f"(not {e[start:j]})")
        i = j
    return "".join(out)


def compile_expr(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(normalize(expr), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"cannot parse expression: {expr!r} ({exc.msg})") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"disallowed syntax {type(node).__name__} in expression: {expr!r}"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                name = getattr(node.func, "id", "<expr>")
                raise ExpressionError(f"call to non-whitelisted function {name!r}")
            if node.keywords:
                raise ExpressionError("keyword arguments are not supported")
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (int, float, bool, str)
        ):
            raise ExpressionError("only numeric and string literals are allowed")
    return tree


def free_names(expr: str) -> set[str]:
    """Identifiers an expression depends on, excluding functions and constants."""
    tree = compile_expr(expr)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in CONSTANTS:
            out.add(node.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.discard(node.func.id)
    return out


def _eval(node: ast.AST, env: Mapping[str, float]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in CONSTANTS:
            return CONSTANTS[node.id]
        raise ExpressionError(f"undefined variable {node.id!r}")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"unsupported operator {type(node.op).__name__}")
        return op(_eval(node.left, env), _eval(node.right, env))
    if isinstance(node, ast.UnaryOp):
        val = _eval(node.operand, env)
        if isinstance(node.op, ast.USub):
            return -val
        if isinstance(node.op, ast.UAdd):
            return +val
        if isinstance(node.op, ast.Not):
            return not val
    if isinstance(node, ast.Call):
        fn = FUNCTIONS[node.func.id]
        return fn(*[_eval(a, env) for a in node.args])
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, env)
            fn = _CMPOPS.get(type(op))
            if fn is None:
                raise ExpressionError("unsupported comparison")
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, env) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    raise ExpressionError(f"cannot evaluate node {type(node).__name__}")


# String literals are permitted because many calculators branch on a dropdown's
# value ("if the concentration option is 'other', derive it from amount/volume").
# They are compared, never executed, so they carry no evaluation risk -- the node
# whitelist still excludes every construct that could run code.


def evaluate(expr: str, env: Mapping[str, float]) -> float:
    """Evaluate a single expression against a variable environment."""
    try:
        return _eval(compile_expr(expr), env)
    except ExpressionError:
        raise
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        raise ExpressionError(f"math error in {expr!r}: {exc}") from exc


def resolve_variants(variants: dict, inputs: Mapping[str, Any]) -> dict[str, float]:
    """Expand a selected option into the constants it stands for.

    A radio group that sets several coefficients at once (Amikacin's sex picks
    both IBW_Sex and CrCl_Sex) is modelled as one select whose options carry the
    whole set. The expressions still reference the individual coefficients, so
    the chosen option has to be expanded before evaluation -- otherwise those
    names are simply undefined.
    """
    out: dict[str, float] = {}
    for key, spec in (variants or {}).items():
        if key not in inputs:
            continue
        chosen = inputs[key]
        for opt in spec.get("options") or []:
            if opt.get("value") == chosen or (
                isinstance(chosen, (int, float))
                and isinstance(opt.get("value"), (int, float))
                and abs(opt["value"] - chosen) < 1e-9
            ):
                out.update(opt.get("constants") or {})
                break
    return out


def _is_conditional(expr: str) -> bool:
    """Whether an expression selects between alternatives at evaluation time."""
    return " if " in (expr or "") and " else " in (expr or "")


def run_compute(
    compute: dict,
    inputs: Mapping[str, float],
    lookup_table: dict | list | None = None,
    variants: dict | None = None,
    env_out: dict | None = None,
) -> dict[str, float]:
    """Evaluate a spec's steps and outputs in DEPENDENCY order.

    The step/output split is presentational -- in the source JavaScript every
    assignment is one sequential block, so a "step" may legitimately depend on
    an "output" computed earlier (Amikacin's weight_crcl needs ibw, which the
    form also displays). Evaluating all steps before all outputs therefore fails
    on perfectly valid specs.

    Instead each expression is evaluated as soon as its free variables are
    available, repeating until no further progress is possible. That is
    order-independent and still terminates: every pass either resolves at least
    one expression or stops.
    """
    env: dict[str, float] = dict(inputs)
    if variants:
        env.update(resolve_variants(variants, inputs))

    # One calculator may carry several ladders (length-for-age and
    # weight-for-length), each with its own key and namespaced columns.
    tables: list[dict] = ([] if lookup_table is None
                          else lookup_table if isinstance(lookup_table, list)
                          else [lookup_table])
    tables = [t for t in tables if t]
    if tables:
        from .engine_lms import lookup as _lookup

    pending: list[tuple[str, str, bool]] = []      # (key, expr, is_output)
    for step in compute.get("steps") or []:
        if step.get("expr"):
            pending.append((step["key"], step["expr"], False))
    for out in compute.get("outputs") or []:
        if out.get("expr"):
            pending.append((out["key"], out["expr"], True))

    results: dict[str, float] = {}
    pending_tables = list(tables)
    last_error: Exception | None = None

    while pending:
        progressed = False

        # Each ladder is keyed on a computed value, so resolve it as soon as its
        # key exists rather than at a fixed point in the sequence.
        still: list[dict] = []
        for t in pending_tables:
            # A ladder bound to a selector applies only when that option is
            # chosen; the others are the wrong patient group's table.
            skey = t.get("select_key")
            if skey is not None:
                if skey not in env:
                    still.append(t)
                    continue
                want, got = t.get("select_value"), env[skey]
                same = (want == got) or (
                    isinstance(want, (int, float)) and isinstance(got, (int, float))
                    and abs(want - got) < 1e-9
                )
                if not same:
                    continue                # not this patient's table
            key = t.get("key") or ""
            hit = None
            for cand in (key, key.lower(), key.replace("_", "").lower()):
                if cand in env:
                    hit = cand
                    break
            if hit is None:
                still.append(t)
                continue
            row = _lookup(t, env[hit])
            if row is None:
                raise ExpressionError(
                    f"{key}={env[hit]} falls outside the {key} lookup table")
            env.update(row)
            progressed = True
        pending_tables = still

        remaining: list[tuple[str, str, bool]] = []
        for key, expr, is_out in pending:
            try:
                needed = free_names(expr) - {key}
            except ExpressionError:
                needed = set()
            # A conditional only needs the branch it takes. `free_names` reports
            # every name in both, so gating on it defers expressions that are
            # perfectly evaluable -- QT derives heart rate from RR or the reverse
            # depending on the entry mode, and each branch alone is resolvable.
            if needed - env.keys() and not _is_conditional(expr):
                remaining.append((key, expr, is_out))
                continue
            try:
                val = evaluate(expr, env)
            except ExpressionError as exc:
                last_error = exc
                remaining.append((key, expr, is_out))
                continue
            env[key] = val
            if is_out:
                results[key] = val
            progressed = True

        if not progressed:
            # Report what is actually blocked, not whichever expression happened
            # to be evaluated last: an output that merely echoes a step ("cptr =
            # cptr") reports itself while the real gap sits upstream.
            blocked: set[str] = set()
            for key, expr, _ in remaining:
                try:
                    blocked |= free_names(expr) - env.keys() - {key}
                except ExpressionError:
                    blocked.add(key)
            if blocked:
                raise ExpressionError(
                    "unresolvable: " + ", ".join(sorted(blocked)[:6]))
            if pending_tables:
                keys = ", ".join(str(t.get("key")) for t in pending_tables)
                raise ExpressionError(f"lookup key(s) never computed: {keys}")
            names = ", ".join(sorted(k for k, _, _ in remaining))
            if last_error is not None:
                raise last_error
            raise ExpressionError(f"cannot resolve: {names}")
        pending = remaining

    # A caller that wants to SHOW its work -- which ladder row a growth chart
    # read, what an intermediate step came to -- needs the environment, not
    # just the outputs. Handing back a copy keeps that available without a
    # second evaluation that could disagree with this one.
    if env_out is not None:
        env_out.update(env)
    return results
