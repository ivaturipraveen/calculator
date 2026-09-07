"""Recover LMS reference tables that growth-chart calculators encode as branches.

CDC/WHO percentile calculators have no closed-form solution: a z-score comes
from the LMS parameters for the patient's age (or length), which the vendor
ships as a long if/else-if ladder rather than a data structure:

    Age_Months = Age * 12;
    var L = 0; var M = 0; var S = 0;
    if(Age_Months < 25){L = -0.216501213; M = 12.74154396; S = 0.108166006;}
    else if(Age_Months < 26){L = -0.239790488; M = 12.88102276; S = 0.108274706;}
    ...                                              (~200 branches)
    Z_Score = (pow((Weight / M), L) - 1) / (L * S);

Treated naively every branch looks like an assignment, so a statement-level
parser emits hundreds of "steps" whose last write wins -- silently producing the
oldest age band's parameters for every patient. Recovering the ladder as a table
is both correct and far smaller, and the table is the vendor's own data, so no
external growth reference has to be trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# `if(Age_Months < 25){L = -0.21; M = 12.7; S = 0.10;}`
#            or `<=`, and the trailing `else` form
BRANCH = re.compile(
    r"(?:else\s+)?if\s*\(\s*(\w+)\s*(<=?)\s*(-?[\d.]+)\s*\)\s*\{([^{}]*)\}",
    re.S,
)
ASSIGN = re.compile(r"(\w+)\s*=\s*(-?[\d.]+(?:[eE][-+]?\d+)?)")
# the final `else { ... }` catch-all
ELSE_TAIL = re.compile(r"\belse\s*\{([^{}]*)\}\s*$", re.S)


@dataclass
class LmsTable:
    key_var: str
    operator: str                       # "<" or "<="
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    fallback: Optional[dict] = None
    start: int = -1                     # source span, for ordering multiple ladders
    end: int = -1
    suffix: str = ""                    # namespacing applied when >1 ladder exists
    _sig: tuple = ()

    def _signature(self) -> tuple:
        if not self._sig:
            self._sig = (self.key_var, self.operator, tuple(self.columns),
                         tuple(tuple(sorted(r.items())) for r in self.rows))
        return self._sig

    def as_dict(self) -> dict:
        return {
            "key": self.key_var,
            "operator": self.operator,
            "columns": self.columns,
            "row_count": len(self.rows),
            "rows": self.rows,
            "fallback": self.fallback,
            "suffix": self.suffix or None,
        }


def extract_all(body: str, min_branches: int = 8) -> list["LmsTable"]:
    """Recover EVERY lookup ladder in a function body, in source order.

    WHO malnutrition calculators run two ladders back to back -- length-for-age,
    then weight-for-length -- and both write the same L/M/S variables, with a
    z-score read off between them. Capturing only the longest run silently
    computes both z-scores from one table. Each ladder therefore keeps its
    source position so its columns can be namespaced and its consumers rewired.
    """
    matches = list(BRANCH.finditer(body))
    if not matches:
        return []

    # A page drawn twice leaves a branch cut in half -- `else if(Length < 96.5)
    # {L = 0.08963;}` -- with the intact copy a few branches later. Left in, the
    # stub not only carries a row missing its columns, it also breaks the
    # threshold sequence, which splits the ladder in two and strands everything
    # after it. Dropped here, before the runs are found, the seam disappears.
    widest = max((len(ASSIGN.findall(m.group(4))) for m in matches), default=0)
    if widest > 1:
        whole = {m.group(3) for m in matches
                 if len(ASSIGN.findall(m.group(4))) == widest}
        matches = [
            m for m in matches
            if len(ASSIGN.findall(m.group(4))) == widest
            or m.group(3) not in whole
        ]
    if not matches:
        return []

    # A run ends when the key variable changes OR when the threshold sequence
    # goes backwards. The reset matters just as much: two tables keyed on the
    # same variable sit back to back in the source -- the girls' ladder then the
    # boys' -- and merging them makes every row after the reset unreachable,
    # because the lookup returns the first threshold that matches.
    runs: list[list] = []
    run: list = []
    for m in matches:
        if run:
            prev = run[-1]
            new_key = (m.group(1) != prev.group(1) or m.group(2) != prev.group(2))
            try:
                here, before = float(m.group(3)), float(prev.group(3))
                went_back = here < before
                # An EQUAL threshold is the page drawn twice, not a second
                # table: the seam repeats a branch and cuts the first copy
                # short. Splitting there stranded the intact copy in a run of
                # its own, and the truncated row -- missing its S -- was the
                # one that survived.
                repeated = here == before
            except ValueError:
                went_back = repeated = False
            if new_key or went_back:
                runs.append(run)
                run = []
        run.append(m)
    if run:
        runs.append(run)

    tables: list[LmsTable] = []
    for r in runs:
        if len(r) < min_branches:
            continue
        t = _table_from_run(r, body)
        if t is not None:
            tables.append(t)

    # The PDF text layer draws some pages twice, so an identical ladder can
    # appear more than once. That is duplication, not a variant: keep one.
    unique: list[LmsTable] = []
    for t in tables:
        if any(t._signature() == u._signature() for u in unique):
            continue
        unique.append(t)

    return _rejoin_segments(unique)


def _span(t: "LmsTable") -> tuple[float, float]:
    th = [r["threshold"] for r in t.rows]
    return (min(th), max(th))


def _rejoin_segments(tables: list["LmsTable"]) -> list["LmsTable"]:
    """Merge pieces of ONE table; keep genuinely separate variants apart.

    Splitting on a threshold reset separates two different situations, and they
    need opposite treatment:

      continuation  25-63, 63-169, 169-241  -- one long table the duplicated
                    seam row falsely split; merge, dropping the repeat
      variant       1-25, 1-25              -- the girls' ladder followed by the
                    boys'; keep separate so a sex selector can pick between them

    The discriminator is whether the next piece continues forward from where the
    last one ended, or restarts at the same place.
    """
    if len(tables) < 2:
        return tables
    out: list[LmsTable] = [tables[0]]
    for t in tables[1:]:
        prev = out[-1]
        if t.key_var != prev.key_var or t.operator != prev.operator:
            out.append(t)
            continue
        p_lo, p_hi = _span(prev)
        t_lo, t_hi = _span(t)
        # continues forward from the previous piece's end, not back to its start
        if t_lo >= p_hi - 1e-9 and t_hi > p_hi and t_lo > p_lo:
            seen = {r["threshold"] for r in prev.rows}
            prev.rows.extend(r for r in t.rows if r["threshold"] not in seen)
            prev.rows.sort(key=lambda r: r["threshold"])
            prev.end = max(prev.end, t.end)
            prev._sig = ()
            for c in t.columns:
                if c not in prev.columns:
                    prev.columns.append(c)
        else:
            out.append(t)
    return out


def _table_from_run(best: list, body: str) -> Optional["LmsTable"]:
    key_var, op = best[0].group(1), best[0].group(2)
    columns: list[str] = []
    rows: list[dict] = []
    for m in best:
        assigns = ASSIGN.findall(m.group(4))
        if not assigns:
            continue
        row: dict[str, float] = {"threshold": float(m.group(3))}
        for name, val in assigns:
            if name not in columns:
                columns.append(name)
            try:
                row[name] = float(val)
            except ValueError:
                continue
        # A page drawn twice repeats a branch, and the seam cuts the first copy
        # short: `S = = 0.048...` leaves a row carrying L and M but no S. Both
        # copies parse, so the LATER one -- the intact copy that follows the
        # seam -- replaces the truncated one at the same threshold.
        prior = next((r for r in rows if r["threshold"] == row["threshold"]), None)
        if prior is not None:
            if len(row) >= len(prior):
                rows[rows.index(prior)] = row
            continue
        rows.append(row)
    if not rows or not columns:
        return None
    t = LmsTable(key_var=key_var, operator=op, columns=columns, rows=rows)
    t.start = best[0].start()
    t.end = best[-1].end()
    tail = ELSE_TAIL.search(body[t.end:t.end + 400])
    if tail:
        fb = {n: float(v) for n, v in ASSIGN.findall(tail.group(1)) if _is_float(v)}
        if fb:
            t.fallback = fb
    return t


def extract(body: str, min_branches: int = 8) -> Optional[LmsTable]:
    """Recover a lookup ladder from a function body, or None if there isn't one."""
    matches = list(BRANCH.finditer(body))
    if len(matches) < min_branches:
        return None

    # Group consecutive branches that test the same variable with the same op.
    best: list[re.Match] = []
    run: list[re.Match] = []
    for m in matches:
        if run and (m.group(1) != run[-1].group(1) or m.group(2) != run[-1].group(2)):
            if len(run) > len(best):
                best = run
            run = []
        run.append(m)
    if len(run) > len(best):
        best = run
    if len(best) < min_branches:
        return None

    key_var, op = best[0].group(1), best[0].group(2)
    columns: list[str] = []
    rows: list[dict] = []
    for m in best:
        assigns = ASSIGN.findall(m.group(4))
        if not assigns:
            continue
        row: dict[str, float] = {"threshold": float(m.group(3))}
        for name, val in assigns:
            if name not in columns:
                columns.append(name)
            try:
                row[name] = float(val)
            except ValueError:
                continue
        rows.append(row)

    if not rows or not columns:
        return None

    table = LmsTable(key_var=key_var, operator=op, columns=columns, rows=rows)

    tail = ELSE_TAIL.search(body[best[-1].end():best[-1].end() + 400])
    if tail:
        fb = {n: float(v) for n, v in ASSIGN.findall(tail.group(1))
              if _is_float(v)}
        if fb:
            table.fallback = fb
    return table


def _is_float(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def branch_assigned_names(table: LmsTable) -> set[str]:
    """Variable names the ladder writes; these must not be emitted as steps."""
    return set(table.columns)


def lookup(table: dict, x: float) -> Optional[dict]:
    """Evaluate a recovered ladder: first row whose threshold matches wins."""
    op = table.get("operator", "<")
    for row in table.get("rows", []):
        t = row.get("threshold")
        if t is None:
            continue
        if (x < t) if op == "<" else (x <= t):
            return {k: v for k, v in row.items() if k != "threshold"}
    return table.get("fallback")
