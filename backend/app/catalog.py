"""The spec catalogue: loaded once, indexed for search, served read-only.

178 specs come to about 30 MB of JSON, so they are held in memory rather than
in a database. That is a deliberate choice, not a shortcut:

  * a spec is a versioned build artefact, not a row anyone edits -- the source
    of truth is the PDF and the extractor, and a database would become a second
    place the clinical content could be changed without a rebuild
  * every read is by slug or a whole-catalogue filter, which is a dict lookup
    and a list comprehension over 178 items
  * a rebuild is `build_all.py`, and reloading is re-reading a directory

If specs ever need per-tenant overrides or an audit trail of edits, that is the
point to put them behind a database -- and the shape here is already the shape
a row would take.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import CALCULATORS


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


@dataclass
class Entry:
    """A calculator's searchable summary, kept beside the full spec."""

    slug: str
    title: str
    subtitle: Optional[str]
    category: Optional[str]
    renderer: str
    status: str
    input_count: int
    output_count: int
    completeness: float
    haystack: str
    spec: dict


class Catalog:
    def __init__(self, root: Path = CALCULATORS) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self.entries: dict[str, Entry] = {}
        self.units: dict = {}
        self.generated: Optional[str] = None
        self.load()

    # -- loading ---------------------------------------------------------
    def load(self) -> None:
        folder = self.root
        if not folder.is_dir():
            raise RuntimeError(
                f"no published specs at {folder}. Build them with:\n"
                f"  cd backup/extractor && python3 build_all.py ../source-pdfs"
            )
        entries: dict[str, Entry] = {}
        for path in sorted(folder.glob("*.json")):
            if path.name in ("index.json", "categories.json"):
                continue                      # indexes, not calculators
            spec = json.loads(path.read_text())
            entries[spec["slug"]] = _entry_for(spec)

        units_path = self.root / "units" / "registry.json"
        units = json.loads(units_path.read_text()) if units_path.exists() else {}

        index_path = self.root / "index.json"
        generated = None
        if index_path.exists():
            generated = json.loads(index_path.read_text()).get("generated")

        with self._lock:
            self.entries = entries
            self.units = units
            self.generated = generated

    # -- reads -----------------------------------------------------------
    def get(self, key: str) -> Optional[Entry]:
        """Find a calculator by slug, or by the name a person would type.

        The slug is the stable identifier, but nobody reads a chart and thinks
        `creatinine-clearance-by-cockcroft-gault-age-16-years`. Resolving the
        printed title too -- and then the title with punctuation and case
        folded away -- means every endpoint takes whichever the caller has.
        """
        if key in self.entries:
            return self.entries[key]
        want = _norm(key)
        if not want:
            return None
        for e in self.entries.values():
            if _norm(e.title) == want or _norm(e.slug) == want:
                return e
        squashed = want.replace(" ", "")
        for e in self.entries.values():
            if _norm(e.title).replace(" ", "") == squashed:
                return e
        # A memorable fragment resolves only when it can mean one calculator.
        # "cockcroft gault" is unambiguous; "creatinine" is not, and guessing
        # between them would hand back the wrong formula.
        hits = self.search(q=key)
        return hits[0] if len(hits) == 1 else None

    def names(self) -> list[dict[str, Any]]:
        """Every calculator's name, for a picker or an autocomplete."""
        return [
            {
                "name": e.title,
                "slug": e.slug,
                "category": e.category,
                "type": e.renderer,
                "inputs": e.input_count,
            }
            for e in sorted(self.entries.values(), key=lambda x: x.title.lower())
        ]

    def categories(self) -> list[dict]:
        counts: dict[str, int] = {}
        for e in self.entries.values():
            counts[e.category or "Uncategorised"] = (
                counts.get(e.category or "Uncategorised", 0) + 1
            )
        return [{"name": k, "count": v} for k, v in sorted(counts.items())]

    def renderers(self) -> list[dict]:
        counts: dict[str, int] = {}
        for e in self.entries.values():
            counts[e.renderer] = counts.get(e.renderer, 0) + 1
        return [{"name": k, "count": v} for k, v in sorted(counts.items())]

    def search(
        self,
        q: Optional[str] = None,
        category: Optional[str] = None,
        renderer: Optional[str] = None,
    ) -> list[Entry]:
        """Filter the catalogue. Ranking favours a title match over a body match.

        Clinicians search by drug or score name, and a substring of the title is
        almost always what they meant -- a calculator that merely mentions
        "creatinine" in its notes should not outrank the one named for it.
        """
        items: Iterable[Entry] = self.entries.values()
        if category:
            items = [e for e in items if (e.category or "") == category]
        if renderer:
            items = [e for e in items if e.renderer == renderer]
        items = list(items)

        if not q or not q.strip():
            return sorted(items, key=lambda e: e.title.lower())

        needle = _norm(q)
        terms = [t for t in needle.split() if t]
        scored: list[tuple[tuple, Entry]] = []
        for e in items:
            title = _norm(e.title)
            if not all(t in e.haystack for t in terms):
                continue
            if title.startswith(needle):
                rank = 0
            elif needle in title:
                rank = 1
            elif all(t in title for t in terms):
                rank = 2
            else:
                rank = 3
            scored.append(((rank, len(e.title), e.title.lower()), e))
        return [e for _, e in sorted(scored, key=lambda p: p[0])]


def _entry_for(spec: dict) -> Entry:
    parts = [
        spec.get("title") or "",
        spec.get("subtitle") or "",
        spec.get("category") or "",
        spec["slug"].replace("-", " "),
    ]
    parts += [i.get("label") or "" for i in (spec.get("inputs") or [])]
    parts += [o.get("label") or "" for o in ((spec.get("compute") or {}).get("outputs") or [])]
    for g in ((spec.get("scoring") or {}).get("groups") or []):
        parts.append(g.get("label") or "")
    content = spec.get("content") or {}
    notes = content.get("notes")
    if isinstance(notes, list):
        parts += [str(n) for n in notes[:4]]
    if content.get("equation"):
        parts.append(str(content["equation"])[:400])

    return Entry(
        slug=spec["slug"],
        title=spec.get("title") or spec["slug"],
        subtitle=spec.get("subtitle"),
        category=spec.get("category"),
        renderer=spec.get("renderer") or "unknown",
        status=spec.get("status") or "draft",
        input_count=len(spec.get("inputs") or []),
        output_count=len(((spec.get("compute") or {}).get("outputs")) or []),
        completeness=float((spec.get("completeness") or {}).get("score") or 0.0),
        haystack=_norm(" ".join(parts)),
        spec=spec,
    )


_catalog: Optional[Catalog] = None


def catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
    return _catalog


def summary(e: Entry) -> dict[str, Any]:
    return {
        "slug": e.slug,
        "title": e.title,
        "subtitle": e.subtitle,
        "category": e.category,
        "renderer": e.renderer,
        "status": e.status,
        "inputs": e.input_count,
        "outputs": e.output_count,
        "completeness": e.completeness,
        # A calculator the source document left unusable -- APACHE II, whose
        # fourteen dropdowns the PDF prints one option each of -- should say so
        # on the card, not on the page after it has been opened.
        "limited": _worst_caveat(e.spec),
    }


def _worst_caveat(spec: dict) -> Optional[str]:
    """The one-line reason this calculator cannot be fully trusted, if any."""
    for c in ((spec.get("provenance") or {}).get("conflicts") or []):
        if c.get("severity") == "critical":
            return c.get("kind")
    return None
