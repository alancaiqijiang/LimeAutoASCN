#!/usr/bin/env python3
"""Derive the final English text (`catalog-translation-final-en.jsonl`) from the
consolidated export + the recorded text-layer pass.

Why this exists: the previous delivery emitted `catalog-translation-final-en.jsonl`
by hand, so re-running the pipeline silently lost the 631-row text-layer pass
(`Dual-Tone Interior` -> `Two-Tone Interior`, `Euro Standard` -> `EU Standard`, ...).
The pass is now a data file, and this script replays it on top of whatever the
current chain produced.  Re-running the pipeline therefore no longer regresses it.

Inputs
  consolidated/catalog-translation-consolidated.jsonl   effective text, full provenance
  translation/glossary/final-text-pass-20260912.json    the recorded replacements

Output
  consolidated/catalog-translation-final-en.jsonl       same rows + `final_pass` fields

Usage:
    .venv/bin/python tools/build_catalog_translation_final_en.py \
        --consolidated local-reports/translation-linguistic-pass-20260910/consolidated
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PASS_FILE = REPO / "translation/glossary/final-text-pass-20260912.json"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consolidated", type=Path, required=True,
                    help="directory holding catalog-translation-consolidated.jsonl")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    src = (args.consolidated / "catalog-translation-consolidated.jsonl").resolve()
    out = (args.out or src.parent / "catalog-translation-final-en.jsonl").resolve()
    rows = load_jsonl(src)

    passes = json.loads(PASS_FILE.read_text(encoding="utf-8"))
    table = passes["changes"]

    applied = missed = 0
    for row in rows:
        changes = table.get(str(row["term_id"]))
        row["final_pass"] = bool(changes)
        if not changes:
            continue
        text = str(row["english"])
        for change in changes:
            old, new = change["from"], change["to"]
            if old not in text:
                missed += 1
                continue
            text = text.replace(old, new, 1)
            applied += 1
        row["english"] = text
        row["final_pass_changes"] = changes
        row["changed"] = True

    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(json.dumps({
        "rows": len(rows),
        "final_pass_rows": sum(1 for r in rows if r["final_pass"]),
        "replacements_applied": applied,
        "replacements_unmatched": missed,
        "out": str(out.relative_to(REPO)),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
