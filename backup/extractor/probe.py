#!/usr/bin/env python3
"""Scratch harness: parse one PDF's script and show what the block parser makes of it."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor import jsblock
from extractor.jsblock import strip_comments
from extractor.jsparse import _slice_function, normalize_text
from extractor.pdf_extract import read_pdf_text, split_sections, strip_boilerplate

PDFS = Path(__file__).parent.parent / "source-pdfs"


def script_for(pattern: str) -> tuple[str, str]:
    hits = sorted(p for p in PDFS.rglob("*.pdf") if pattern.lower() in p.name.lower())
    if not hits:
        raise SystemExit(f"no pdf matching {pattern!r}")
    path = hits[0]
    sections = split_sections(normalize_text(strip_boilerplate(read_pdf_text(path))))
    return path.name, sections.get("Scripts", "")


def dump(stmts, depth=0):
    pad = "  " * depth
    for st in stmts:
        if isinstance(st, jsblock.Assign):
            print(f"{pad}{st.name} {st.op} {st.expr[:100]}")
        elif isinstance(st, jsblock.If):
            print(f"{pad}IF ({st.cond[:90]})")
            dump(st.then, depth + 1)
            if st.els:
                print(f"{pad}ELSE")
                dump(st.els, depth + 1)
        elif isinstance(st, jsblock.Call):
            print(f"{pad}CALL {st.fn}({', '.join(a[:40] for a in st.args)})")
        elif isinstance(st, jsblock.Ret):
            print(f"{pad}RETURN")


if __name__ == "__main__":
    name, js = script_for(sys.argv[1])
    fn = sys.argv[2] if len(sys.argv) > 2 else "calculate"
    print(f"### {name}  fn={fn}\n")
    body = _slice_function(normalize_text(js), fn)
    if body is None:
        print("no such function; available:",
              re.findall(r"function\s+(\w+)", normalize_text(js)))
        raise SystemExit(1)
    stmts = jsblock.parse_block(strip_comments(body))
    dump(stmts)
    print("\n--- module string constants ---")
    print(jsblock.string_constants(normalize_text(js)))
