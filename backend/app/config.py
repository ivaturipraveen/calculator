"""Where the published specs live, and how the evaluator is reached.

The API owns no clinical data of its own. Every number it returns comes from
the `calculators/` folder the extractor publishes, and every expression it
evaluates goes through the same AST-whitelisted evaluator the extractor
validates with -- so the API and the build cannot drift into computing two
different answers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = API_ROOT.parent

# The served specs: one flat folder of JSON, published by `build_all.py`.
CALCULATORS = Path(os.environ.get("CALC_CALCULATORS", PROJECT_ROOT / "calculators"))

# The extractor is kept out of the serving path, but its evaluator is imported
# rather than reimplemented: a second implementation is a second set of
# rounding rules and a second chance to be wrong.
EXTRACT_ROOT = Path(
    os.environ.get("CALC_EXTRACT_ROOT", PROJECT_ROOT / "backup" / "extractor")
)
if str(EXTRACT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXTRACT_ROOT))

CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CALC_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173",
    ).split(",")
    if o.strip()
]
