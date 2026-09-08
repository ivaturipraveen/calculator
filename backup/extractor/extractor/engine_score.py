"""Parser for point-based score calculators ("engine C").

~38 corpus PDFs. These ship almost no JavaScript -- only `togCB`/`setRB` DOM
helpers -- because the scoring is entirely declarative: the printed Calculator
table carries the criteria, their options, and each option's point value.

    Thrombocytopenia
    Platelet count fall >50 percent AND nadir >=20,000/microL (2 points)
    Platelet count fall 30 to 50 percent OR nadir 10 to 19,000/microL (1 point)
    ...
    Timing of platelet count fall
    Clear onset between days 5 and 10 ... (2 points)

So the grammar is: a line ending in `(N point[s])` is an OPTION; any other line
starts a new criterion GROUP. Options wrap across lines, so a line is only a new
group when the previous option is complete -- i.e. the point marker has closed.

Interpretation bands come from the Results/Additional Information prose:
"0 to 3 points: Low probability".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .jsparse import normalize_text

# "(2 points)" / "(1 point)" / "(0 points)" / "(-1 point)" / "(+2 points)"
POINTS = re.compile(r"\(\s*([+-]?\d+(?:\.\d+)?)\s*points?\s*\)\s*$", re.I)
# "0 to 3 points: Low probability" / "6-8 points = High" / "Score 0-1: low"
BAND = re.compile(
    r"^\s*(?:score\s*)?([+-]?\d+(?:\.\d+)?)\s*(?:to|-|–|—)\s*([+-]?\d+(?:\.\d+)?)\s*"
    r"points?\s*[:=-]\s*(.+?)\s*$",
    re.I,
)
BAND_SINGLE = re.compile(
    r"^\s*(?:score\s*)?([+-]?\d+(?:\.\d+)?)\s*points?\s*[:=-]\s*(.+?)\s*$", re.I
)
# "Score > 6: High probability" -- a form that names the scale in the prefix
# does not repeat the word "points", and demanding it lost every band the
# Wells score prints.
BAND_CMP = re.compile(
    r"^\s*(?:score\s*)?([<>]=?)\s*([+-]?\d+(?:\.\d+)?)\s*(?:points?)?\s*"
    r"[:=-]\s*(.+?)\s*$",
    re.I,
)
# "Score >= 2 and <= 6: Moderate probability"
BAND_BETWEEN = re.compile(
    r"^\s*(?:score\s*)?>=?\s*([+-]?\d+(?:\.\d+)?)\s*and\s*<=?\s*"
    r"([+-]?\d+(?:\.\d+)?)\s*(?:points?)?\s*[:=-]\s*(.+?)\s*$",
    re.I,
)

SKIP_LINES = {
    "calculate", "reset", "input", "results", "score", "total", "total score",
    "calculator", "points",
}


@dataclass
class ScoreGroup:
    label: str
    options: list[dict] = field(default_factory=list)
    selection: str = "single"      # single (radio) | multiple (checkbox)


@dataclass
class ParsedScore:
    groups: list[ScoreGroup] = field(default_factory=list)
    bands: list[dict] = field(default_factory=list)
    total_label: Optional[str] = None
    uses_checkbox: bool = False
    uses_radio: bool = False


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def parse_calculator_section(sec: str) -> list[ScoreGroup]:
    """Split the printed table into criterion groups and their point options."""
    if not sec:
        return []
    raw = [l for l in (x.rstrip() for x in sec.split("\n")) if l.strip()]

    lines = [_clean(l) for l in raw]
    lines = [l for l in lines if l and l.lower() not in SKIP_LINES]

    groups: list[ScoreGroup] = []
    pending: list[str] = []          # lines of an option still wrapping
    current: Optional[ScoreGroup] = None

    for i, s in enumerate(lines):
        pending.append(s)
        joined = " ".join(pending)
        m = POINTS.search(joined)
        if m:
            label = POINTS.sub("", joined).strip(" .;:")
            if current is None:
                current = ScoreGroup(label="(ungrouped)")
                groups.append(current)
            # The page drawn twice repeats the last option verbatim. Two
            # identical choices are not a choice, and on a checkbox group they
            # would count the same criterion twice.
            if not any(o["label"] == label and o["points"] == _num(m.group(1))
                       for o in current.options):
                current.options.append({"label": label, "points": _num(m.group(1))})
            pending = []
            continue

        # Mid-option text: only a line that is NOT a continuation opens a group.
        if len(pending) == 1 and not _is_continuation(s, lines[i + 1] if i + 1 < len(lines) else None):
            current = ScoreGroup(label=s)
            groups.append(current)
            pending = []

    return [g for g in groups if g.options]


# Cues that a line is the first half of a wrapped option rather than a heading.
_TRAILING_CUE = re.compile(
    r"(?:,|;|:|\b(?:if|or|and|but|the|a|an|of|to|in|at|for|with|after|within|"
    r"than|then|from|by|on|per|is|are|was|were|has|have|no|not|nor|eg|ie|"
    r"between|during|without|including|following)\s*)$",
    re.I,
)


def _is_continuation(line: str, nxt: Optional[str]) -> bool:
    """True if `line` is the start of a wrapped option, not a criterion heading.

    Options routinely span two or three printed lines, so length alone cannot
    separate them from headings. Two reliable cues do:

      * the line ends mid-clause ("... fall at <=1 day if")
      * the following line resumes in lower case ("unfractionated heparin bolus")

    Either one means the text continues, so the line cannot be a heading.
    """
    if _TRAILING_CUE.search(line):
        return True
    if nxt:
        first = nxt.lstrip("([")[:1]
        if first and first.islower():
            return True
        # A line whose next line is nothing but the point marker is the first
        # half of a wrapped option -- "(1 point)" alone below it. Read as a
        # heading, it opened a new criterion and swallowed the options after
        # it: NIH Stroke's "Extinction and Inattention" kept only "Normal".
        if re.fullmatch(r"\(\s*[+-]?\d+(?:\.\d+)?\s*points?\s*\)", nxt.strip(), re.I):
            return True
    return False


def parse_bands(*sections: str) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for sec in sections:
        if not sec:
            continue
        # "0 Points:" on one line and its meaning on the next is common in the
        # print layout; join those before matching.
        raw = [l.rstrip() for l in sec.split("\n")]
        joined: list[str] = []
        for i, l in enumerate(raw):
            t = l.strip()
            # A band header may be split from its meaning. It is only a header
            # if it names the scale ("6 points:") or is plainly a comparison
            # ("Score > 6:"); a bare number is a value, and joining that to the
            # next line invents bands.
            _hdr = re.match(
                r"^(?:(?:score\s*)?[<>]=?\s*[\d.]+"
                r"(?:\s*and\s*<=?\s*[\d.]+)?\s*(?:points?)?"
                r"|[\d.]+(?:\s*(?:to|-|–)\s*[\d.]+)?\s*points?)\s*:?\s*$",
                t, re.I)
            if _hdr and i + 1 < len(raw) and raw[i + 1].strip():
                joined.append(t.rstrip(":") + ": " + raw[i + 1].strip())
            else:
                joined.append(l)
        sec = "\n".join(joined)
        heading = ""
        for line in sec.split("\n"):
            s = _clean(line)
            if not s:
                continue
            if _OUTCOME_HEADING.match(s):
                heading = s.rstrip(":").strip()
                continue
            m = BAND.match(s)
            if m:
                lo, hi, label = _num(m.group(1)), _num(m.group(2)), m.group(3).strip()
                key = (lo, hi, label)
                if key not in seen:
                    seen.add(key)
                    out.append({"min": lo, "max": hi, "label": label, "raw": s,
                                "heading": heading})
                continue
            m = BAND_BETWEEN.match(s)
            if m:
                lo, hi, label = _num(m.group(1)), _num(m.group(2)), m.group(3).strip()
                key = (lo, hi, label)
                if key not in seen:
                    seen.add(key)
                    out.append({"min": lo, "max": hi, "label": label, "raw": s,
                                "heading": heading})
                continue
            m = BAND_CMP.match(s)
            if m:
                op, v, label = m.group(1), _num(m.group(2)), m.group(3).strip()
                lo, hi = (None, v) if op.startswith("<") else (v, None)
                key = (lo, hi, label)
                if key not in seen:
                    seen.add(key)
                    out.append({"min": lo, "max": hi, "label": label, "raw": s,
                                "heading": heading})
                continue
            m = BAND_SINGLE.match(s)
            if m:
                v, label = _num(m.group(1)), m.group(2).strip()
                if len(label) <= 90:
                    key = (v, v, label)
                    if key not in seen:
                        seen.add(key)
                        out.append({"min": v, "max": v, "label": label,
                                    "raw": s, "heading": heading})
    return _qualify_colliding_bands(out)


# A heading over a run of bands names the outcome they score. Fracture Index
# prints three of them -- nonvertebral, hip and vertebral 5-year risk -- one
# after another, so "1 to 2 Points" arrives three times carrying 8.6%, 0.4% and
# 1.2%. Flattened without their headings those read as one table contradicting
# itself, and the page answers a woman who scored 2 with whichever came first.
_OUTCOME_HEADING = re.compile(
    r"""^(?!.*\d\s*(?:to|-|\u2013)?\s*\d*\s*points?\b)  # not itself a band
         (?=(?:.*[A-Za-z]){4})                       # has real words
         (?!.*[:%])                                  # headings carry neither
         .{4,70}$""", re.I | re.X)


def _qualify_colliding_bands(bands: list[dict]) -> list[dict]:
    """Name the outcome only where two bands claim the same score.

    Prefixing every band with its heading would rewrite labels across the whole
    corpus to no purpose. The ambiguity is what needs fixing, so this touches
    only the ranges that are genuinely claimed twice.
    """
    spans = Counter((b["min"], b["max"]) for b in bands)
    for b in bands:
        head = b.pop("heading", "") or ""
        if head and spans[(b["min"], b["max"])] > 1:
            b["label"] = f"{head}: {b['label']}"
    return bands


def _num(s: str) -> float | int:
    f = float(s)
    return int(f) if f == int(f) else f


def parse(sections: dict[str, str], js: str = "") -> ParsedScore:
    js = normalize_text(js or "")
    out = ParsedScore()
    out.uses_checkbox = "togCB" in js
    out.uses_radio = "setRB" in js

    out.groups = parse_calculator_section(sections.get("Calculator", ""))
    # A calculator that only offers checkboxes is multi-select per group.
    if out.uses_checkbox and not out.uses_radio:
        for g in out.groups:
            g.selection = "multiple"

    # Interpretation bands are frequently printed inside the Calculator block
    # itself, under a heading like "4 Ts score interpretation", rather than in a
    # Results or Notes section -- so those must be searched too or the bands look
    # absent when the document plainly states them.
    _band_sections = (
        sections.get("Results", ""),
        sections.get("Additional Information", ""),
        sections.get("Notes", ""),
        sections.get("Calculation Details", ""),
        sections.get("Calculator", ""),
    )
    out.bands = split_marked_columns(parse_bands(*_band_sections), *_band_sections)
    return out


# EBMcalc prints an interpretation table's column rule as "**" in the text
# layer, so a row arrives as "3% ** 5%". Those are two different outcomes, not
# a range: TIMI UA/NSTEMI states death/MI beside death/MI/urgent revascular-
# isation, and its second column is the 4.7/8.3/13.2/19.9/26.2/40.9 series from
# Antman 2000. Left as "3% ** 5%" the page shows a clinician a number they
# cannot act on and hides which endpoint it belongs to.
_COL_SEP = re.compile(r"\s*\*\*\s*")
# The columns that follow the outcome ones carry no number, so they ride along
# on the header line and have to come off it.
_COL_TAIL = re.compile(r"\s+(?:interpretation|notes?|comments?)\s*$", re.I)
# "<Name> Score and <first outcome>" is the house style for the header's left
# half; everything before "and" is the score column, not an outcome.
_SCORE_COL = re.compile(r"^.*?\bscores?\b\s+and\s+(.+)$", re.I)


def _column_names(*sections: str) -> list[str] | None:
    """Name the two outcome columns an interpretation table separates with "**".

    Only a header can name them, and a header is the line that has the marker
    but no percentage in it. Returns None when no such line reads cleanly, so
    the caller can still de-mangle the rows without inventing column names.
    """
    for sec in sections:
        for line in (sec or "").split("\n"):
            s = line.strip()
            if not s or "**" not in s or "%" in s:
                continue
            parts = _COL_SEP.split(s)
            if len(parts) != 2:
                continue
            left, right = parts[0].strip(), _COL_TAIL.sub("", parts[1]).strip()
            m = _SCORE_COL.match(left)
            if m:
                left = m.group(1).strip()
            if left and right and len(left) <= 70 and len(right) <= 70:
                return [left, right]
    return None


def split_marked_columns(bands: list[dict], *sections: str) -> list[dict]:
    """Turn "3% ** 5%" back into two named outcomes."""
    names = _column_names(*sections)
    for b in bands:
        parts = [x.strip() for x in _COL_SEP.split(b.get("label") or "")]
        if len(parts) != 2 or not all(parts):
            continue
        b["columns"] = (
            [{"name": names[i], "value": parts[i]} for i in range(2)]
            if names else [{"name": None, "value": p} for p in parts]
        )
        b["label"] = (
            f"{names[0]}: {parts[0]} \u00b7 {names[1]}: {parts[1]}"
            if names else f"{parts[0]} \u00b7 {parts[1]}"
        )
    return bands


def max_total(groups: list[ScoreGroup]) -> float:
    total = 0.0
    for g in groups:
        pts = [o["points"] for o in g.options]
        if not pts:
            continue
        total += max(pts) if g.selection == "single" else sum(p for p in pts if p > 0)
    return total


def min_total(groups: list[ScoreGroup]) -> float:
    total = 0.0
    for g in groups:
        pts = [o["points"] for o in g.options]
        if not pts:
            continue
        total += min(pts) if g.selection == "single" else sum(p for p in pts if p < 0)
    return total
