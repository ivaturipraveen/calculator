"""Name folding, shared with the extractor's matcher.

Keys, labels and printed names differ only by case, spaces and punctuation, so
one folding rule keeps `Serum creatinine`, `serum_creatinine` and `SerumCreat`
resolving to the same field.
"""

from __future__ import annotations

import re
from typing import Optional


def squash(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
