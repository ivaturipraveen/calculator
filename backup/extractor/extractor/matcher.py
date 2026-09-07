"""Pair each PDF-derived calculator with its counterpart in the mockup HTML.

Matching is tiered and never silently guesses. Every pairing carries the method
that produced it and a score, so a reviewer can audit the decisions instead of
trusting them:

  exact      normalised titles are identical                -> auto-accept
  squashed   identical once punctuation/spacing is removed  -> auto-accept
  alias      a known editorial rename                       -> auto-accept
  fuzzy      high token similarity, below certainty         -> REVIEW
  none       no candidate cleared the floor                 -> PDF-only

Collisions (two PDFs claiming one mockup entry) are detected and reported rather
than resolved by whichever happened to be processed first.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Optional

from .identity import normalize, squash

# Titles the mockup deliberately reworded. Extend as review surfaces more.
ALIASES: dict[str, str] = {
    # The mockup spells out what the PDF abbreviates.
    "amoxicillin dental": "Amoxicillin (Dental Prophylaxis)",
    "azithromycin dental": "Azithromycin (Dental Prophylaxis)",
    "newborn hyperbilirubinemia assessment 35 weeks gestation":
        "newborn hyperbilirubinemia assessment >=35 weeks gestation",
}

FUZZY_ACCEPT = 0.93     # at or above: still needs a human glance, but likely right
FUZZY_FLOOR = 0.82      # below: not a candidate at all


@dataclass
class Match:
    pdf_slug: str
    pdf_title: str
    html_id: Optional[str] = None
    html_title: Optional[str] = None
    method: str = "none"
    score: float = 0.0
    needs_review: bool = False
    note: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "pdf_slug": self.pdf_slug,
            "pdf_title": self.pdf_title,
            "html_id": self.html_id,
            "html_title": self.html_title,
            "method": self.method,
            "score": round(self.score, 4),
            "needs_review": self.needs_review,
            "note": self.note,
        }


@dataclass
class MatchIndex:
    """Lookup tables over the mockup's calculators."""

    by_norm: dict[str, dict] = field(default_factory=dict)
    by_squash: dict[str, dict] = field(default_factory=dict)
    entries: list[dict] = field(default_factory=list)

    @classmethod
    def from_harvest(cls, harvest: dict) -> "MatchIndex":
        idx = cls()
        for entry in (harvest.get("per_calculator") or {}).values():
            idx._add(entry, generic=False)
        for bucket in ("scores", "formulas", "converts", "roadmap"):
            for item in (harvest.get("new_calcs") or {}).get(bucket) or []:
                idx._add(
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "category": item.get("cat"),
                        "generic": True,
                        "kind": bucket,
                        "constants": {},
                        "sources": {},
                        "defaults": {},
                        "spec": item,
                    },
                    generic=True,
                )
        return idx

    def _add(self, entry: dict, generic: bool) -> None:
        title = entry.get("title")
        if not title:
            return
        entry = dict(entry)
        entry.setdefault("generic", generic)
        n, s = normalize(title), squash(title)
        # Bespoke entries win a key collision: they are the richer implementation.
        if n not in self.by_norm or not entry["generic"]:
            self.by_norm[n] = entry
        if s not in self.by_squash or not entry["generic"]:
            self.by_squash[s] = entry
        self.entries.append(entry)

    def lookup(self, title: str) -> Match:
        m = Match(pdf_slug="", pdf_title=title)
        n, s = normalize(title), squash(title)

        hit = self.by_norm.get(n)
        if hit:
            return self._hit(m, hit, "exact", 1.0)

        alias = ALIASES.get(n)
        if alias:
            hit = self.by_norm.get(normalize(alias)) or self.by_squash.get(squash(alias))
            if hit:
                return self._hit(m, hit, "alias", 1.0)

        hit = self.by_squash.get(s)
        if hit:
            return self._hit(m, hit, "squashed", 0.99)

        best, best_score = None, 0.0
        for cand_norm, entry in self.by_norm.items():
            score = difflib.SequenceMatcher(None, n, cand_norm).ratio()
            if score > best_score:
                best, best_score = entry, score

        if best is not None and best_score >= FUZZY_FLOOR:
            m = self._hit(m, best, "fuzzy", best_score)
            m.needs_review = True
            m.note = (
                f"fuzzy {best_score:.3f} -- confirm this is the same calculator"
                if best_score < FUZZY_ACCEPT
                else f"fuzzy {best_score:.3f} -- near-certain, confirm wording"
            )
            return m

        m.method = "none"
        m.note = (
            f"no mockup entry above {FUZZY_FLOOR:.2f}"
            + (f" (closest {best_score:.3f})" if best else "")
        )
        return m

    @staticmethod
    def _hit(m: Match, entry: dict, method: str, score: float) -> Match:
        m.html_id = entry.get("id")
        m.html_title = entry.get("title")
        m.method = method
        m.score = score
        return m


def detect_collisions(matches: list[Match]) -> dict[str, list[str]]:
    """Mockup entries claimed by more than one PDF."""
    seen: dict[str, list[str]] = {}
    for m in matches:
        if m.html_id:
            seen.setdefault(m.html_id, []).append(m.pdf_title)
    return {k: v for k, v in seen.items() if len(v) > 1}


def unmatched_html(index: MatchIndex, matches: list[Match]) -> list[dict[str, Any]]:
    """Mockup calculators that no PDF claimed."""
    claimed = {m.html_id for m in matches if m.html_id}
    out, seen = [], set()
    for e in index.entries:
        eid = e.get("id")
        if eid in claimed or eid in seen:
            continue
        seen.add(eid)
        out.append({"id": eid, "title": e.get("title"), "generic": e.get("generic")})
    return sorted(out, key=lambda x: (x["title"] or ""))
