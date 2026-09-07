"""Collapse the duplicated text some Lexicomp PDFs emit.

A number of these PDFs draw their page twice -- a shadow layer beneath the
visible one. Extraction has to preserve whitespace to stop the two passes
interleaving and dropping characters, and the cost of that is seeing both
copies. Usually harmless, but not always:

  * a lookup ladder appears twice, and the second copy reads as a second table
  * a statement is cut off mid-way by the seam, leaving an unclosed brace that
    swallows the entire rest of the function -- which is how Nitroprusside lost
    the `set(ID_RATE, rate)` that produces its only result

The duplication is literal and adjacent, so it can be removed by finding a span
that repeats immediately after itself and keeping one copy. Nothing is dropped
unless an identical copy of it remains.
"""

from __future__ import annotations

import re

MIN_BLOCK = 60          # shorter repeats are ordinary code, not a redraw
MAX_SEAM = 400          # garbage the seam may inject between the two copies


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def imbalance(body: str) -> int:
    """How far the body's brackets are from balancing (0 = balanced)."""
    depth = {"(": 0, "{": 0, "[": 0}
    pair = {")": "(", "}": "{", "]": "["}
    quote = None
    esc = False
    for ch in body:
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in depth:
            depth[ch] += 1
        elif ch in pair:
            depth[pair[ch]] -= 1
    return sum(abs(v) for v in depth.values())


def is_truncated(body: str) -> bool:
    """True if the body's brackets do not balance, ignoring string contents."""
    return imbalance(body) > 0


def repair_truncated(body: str) -> str:
    """Collapse duplication ONLY when it has actually broken the structure.

    Blanket de-duplication is unsafe: a growth-chart ladder is hundreds of
    near-identical branches, and collapsing those destroys the table. Repair is
    therefore attempted only on a body whose brackets do not balance -- the
    signature of a seam cutting a statement in half -- and kept only if it
    actually restores the balance.
    """
    before = imbalance(body) if body else 0
    if not before:
        return body
    fixed = collapse_repeats(body)
    # Accept a repair that moves the structure closer to balanced. Demanding a
    # perfect balance rejects every case where the slice already ran past the
    # damage -- which is precisely when the repair is most needed.
    return fixed if imbalance(fixed) < before else body


def collapse_repeats(text: str, min_block: int = MIN_BLOCK) -> str:
    """Remove an adjacent duplicate of any substantial span.

    Scans for a position where the text ahead repeats what just appeared,
    allowing for the seam's partial line in between, and drops the earlier
    copy plus the seam. Runs to a fixed point so a page drawn twice in several
    pieces is fully collapsed.
    """
    if not text:
        return text

    # A page can be drawn twice in many pieces, so the fixed point needs room:
    # a cap of 8 stopped before reaching the seam that mattered.
    for _ in range(64):                      # fixed point, bounded
        cut = _find_repeat(text, min_block)
        if cut is None:
            return text
        start, end = cut
        text = text[:start] + text[end:]
    return text


def _find_repeat(text: str, min_block: int) -> tuple[int, int] | None:
    """Locate `A <seam> A` and return the span to delete (the first A + seam)."""
    n = len(text)
    # The probe has to land inside the repeated span to recognise it, so the
    # stride must be well under a typical span. A coarse stride silently skipped
    # seams whose repeated block was only ~90 characters.
    step = 12
    i = 0
    while i < n - 2 * min_block:
        probe = _norm(text[i:i + min_block])
        if len(probe) < min_block // 2:
            i += step
            continue
        # Where does this span next occur? Try the raw text first: seam copies
        # are byte-identical, and the normalised search has to map an offset
        # back through collapsed whitespace, which grows unreliable over a long
        # body and silently missed real duplicates.
        window = text[i + min_block: i + min_block + MAX_SEAM + min_block]
        raw_probe = text[i:i + min_block]
        pos = window.find(raw_probe)
        if pos < 0:
            pos = _norm_find(window, probe)
        if pos is not None and pos >= 0:
            second = i + min_block + pos
            # Confirm the repeat is substantial, not a coincidence. The match is
            # bounded by the distance between the two copies, so a fixed
            # threshold rejects a genuine seam whose repeated block is barely
            # shorter than the probe -- one such match came to 59 of 60. What
            # matters is that essentially everything between the copies repeats.
            span = _match_length(text, i, second)
            gap = second - i
            if span >= min_block or span >= max(40, int(0.75 * gap)):
                return (i, second)
        i += step
    return None


def _norm_find(hay: str, needle_norm: str) -> int | None:
    """Index in `hay` where whitespace-normalised `needle_norm` begins."""
    hay_norm = _norm(hay)
    k = hay_norm.find(needle_norm)
    if k < 0:
        return None
    # map the normalised offset back to a raw offset
    seen, raw = 0, 0
    prev_space = True
    for raw, ch in enumerate(hay):
        if ch.isspace():
            if not prev_space:
                seen += 1
            prev_space = True
        else:
            prev_space = False
            if seen >= k:
                return raw
            seen += 1
    return None


def _match_length(text: str, a: int, b: int) -> int:
    """How far the spans at `a` and `b` agree, ignoring whitespace."""
    i, j, matched = a, b, 0
    while j < len(text) and matched < 4000:
        ca, cb = text[i], text[j]
        if ca.isspace():
            i += 1
            continue
        if cb.isspace():
            j += 1
            continue
        if ca != cb:
            break
        matched += 1
        i += 1
        j += 1
        if i >= b:                           # ran into the second copy
            break
    return matched
