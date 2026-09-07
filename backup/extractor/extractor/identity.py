"""Resolve and verify a calculator's identity from its PDF.

With ~200 files, silently pairing the wrong PDF with the wrong mockup entry
would put one calculator's formula under another's name -- a patient-safety
defect that no downstream test would catch, because both halves are internally
consistent. So identity is established from several independent signals and any
disagreement is surfaced rather than resolved by guesswork.

Signals, strongest first:

  doc_id     the vendor's own numeric id, printed under "Include Documents".
             Stable across releases and unique per calculator -> primary key.
  title      the printed heading. Authoritative for display.
  form_name  the JS identifier (`AAGradient_form` / `AAGradient_fx`). Machine
             stable; normalises to the title for a genuine cross-check.
  filename   weakest. Often an export default ("Lexidrug.pdf") that carries no
             information at all, so it can corroborate but never override.

The verification is deliberately asymmetric: agreement raises confidence,
disagreement lowers it and records a flag, and nothing is auto-resolved when the
strong signals conflict.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

# Export defaults that identify the app, not the calculator.
UNINFORMATIVE_FILENAMES = {
    "lexidrug", "lexicomp", "uptodate", "document", "print", "untitled",
    "ebmcalc", "calculator", "scan", "image", "file", "download",
}


def normalize(s: Optional[str]) -> str:
    """Aggressive fold for comparing names across sources.

    Beyond case and punctuation, this reconciles the two spellings the vendor
    uses interchangeably for age ranges: a filename says "(under 24 months)"
    where the printed title says "(<24 months)". Treating those as different
    names produced 10 spurious mismatches and 5 spurious fuzzy matches across
    the corpus, so they are folded to one form here.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[®™©]", "", s)
    # "under 24" / "less than 24" / "<24" / "≤24" -> "lt24"
    s = re.sub(r"(?:under|less\s+than|below)\s*(\d)", r"lt\1", s)
    s = re.sub(r"[<≤]\s*(\d)", r"lt\1", s)
    s = re.sub(r"(?:over|greater\s+than|above)\s*(\d)", r"gt\1", s)
    s = re.sub(r"[>≥]\s*(\d)", r"gt\1", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def squash(s: Optional[str]) -> str:
    """Fold to bare alphanumerics: 'A-a Gradient' and 'AAGradient' both -> 'aagradient'."""
    return re.sub(r"[^a-z0-9]", "", normalize(s))


def title_from_filename(name: str) -> Optional[str]:
    """Recover a candidate title from a filename, or None if uninformative."""
    stem = re.sub(r"\.pdf$", "", name, flags=re.I)
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)      # "... (2).pdf" duplicates
    stem = re.sub(r"^\d+[\s.\-]+", "", stem)        # "012 - Foo.pdf"
    stem = stem.strip()
    if not stem or normalize(stem) in UNINFORMATIVE_FILENAMES:
        return None
    if re.fullmatch(r"[\d\s.\-]+", stem):
        return None
    return stem


@dataclass
class Identity:
    doc_id: Optional[str] = None
    title: Optional[str] = None
    form_name: Optional[str] = None
    filename_title: Optional[str] = None
    pdf_meta_title: Optional[str] = None

    confidence: float = 0.0
    verified: bool = False
    flags: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "form_name": self.form_name,
            "filename_title": self.filename_title,
            "confidence": round(self.confidence, 3),
            "verified": self.verified,
            "flags": self.flags,
            "checks": self.checks,
        }


DOC_ID = re.compile(r"Include Documents\s*\n\s*(\d+)")
FORM = re.compile(r"document\s*\.\s*(\w+)_form")
FX = re.compile(r"function\s+(\w+)_fx\s*\(")


def extract_identity(
    text: str,
    filename: str,
    printed_title: Optional[str],
    pdf_meta_title: Optional[str] = None,
) -> Identity:
    """Establish identity and cross-verify every available signal."""
    ident = Identity(
        title=printed_title,
        filename_title=title_from_filename(filename),
        pdf_meta_title=pdf_meta_title,
    )

    m = DOC_ID.search(text)
    if m:
        ident.doc_id = m.group(1)

    m = FORM.search(text) or FX.search(text)
    if m:
        ident.form_name = m.group(1)

    score = 0.0

    # --- the printed title is the anchor ---------------------------------
    if ident.title:
        score += 0.35
        ident.checks["title"] = f"present ({ident.title!r})"
    else:
        ident.flags.append("no_printed_title")
        ident.checks["title"] = "MISSING"

    # doc_id is recorded for provenance only. Measured across the corpus it is
    # NOT unique -- 180 of 186 files share 70017 -- so it is a document-class
    # constant, not a per-calculator key. It must never drive identity.
    ident.checks["doc_id"] = f"present ({ident.doc_id})" if ident.doc_id else "absent"

    # --- primary verification: printed title vs filename -----------------
    # With real exported filenames this is the strongest independent signal
    # (173/186 exact across the corpus).
    if ident.filename_title and ident.title:
        a, b = squash(ident.title), squash(ident.filename_title)
        if a == b:
            score += 0.55
            ident.checks["title_vs_filename"] = f"MATCH ({b})"
        elif a in b or b in a:
            score += 0.40
            ident.checks["title_vs_filename"] = f"PARTIAL ({a} ~ {b})"
            ident.flags.append("title_filename_partial")
        else:
            # The filename names a different calculator than the document does:
            # a genuine hazard suggesting a wrong or mis-renamed export.
            ident.checks["title_vs_filename"] = f"MISMATCH ({a} != {b})"
            ident.flags.append("title_filename_mismatch")
    elif ident.title:
        ident.checks["title_vs_filename"] = "filename uninformative (ignored)"

    # --- corroboration only: printed title vs JS form name ---------------
    # The vendor's identifiers are deliberate abbreviations and eponyms
    # (Basal Energy Expenditure -> HarrisBenedict; APACHE II ... by Diagnosis
    # -> ApacheScoreDx), so agreement is evidence but disagreement is not.
    if ident.title and ident.form_name:
        a, b = squash(ident.title), squash(ident.form_name)
        if a == b:
            score += 0.10
            ident.checks["title_vs_form"] = f"MATCH ({a})"
        elif a.startswith(b) or b.startswith(a) or a in b or b in a:
            score += 0.07
            ident.checks["title_vs_form"] = f"PARTIAL ({a} ~ {b})"
        else:
            ident.checks["title_vs_form"] = (
                f"differs ({a} vs {b}) - expected, vendor abbreviates"
            )
    elif ident.form_name:
        ident.checks["title_vs_form"] = "form name only (no title to compare)"
    else:
        # Score-type calculators ship no _fx()/_form at all.
        ident.checks["title_vs_form"] = "no form name (score-type calculator)"

    ident.confidence = min(1.0, score)
    hard_conflicts = {"no_printed_title", "title_filename_mismatch"}
    ident.verified = (
        ident.confidence >= 0.85 and not (hard_conflicts & set(ident.flags))
    )
    return ident
