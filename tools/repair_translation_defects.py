#!/usr/bin/env python3
"""Deterministic repair of LimeAuto translation defects, without a model call.

Runs against the integrity audit's findings. For every defective row it tries,
in order of how much evidence each option carries:

1. **reuse an existing review** -- a Luna-reviewed rendering for the same
   term_id that passes the strengthened gate;
2. **glossary repair** -- replace a half-translated colour/finish tail with the
   corpus-dominant English rendering from
   ``translation/glossary/color-finish-en.json``;
3. **misplaced-translation recovery** -- a neighbouring row's stored text that
   fails its own row but satisfies this one is the signature of a shifted
   output row, so it is reclaimed for this term;
4. otherwise the row is recorded as ``needs_human`` with its concrete problems.

It never invents English. Output uses the same batch shape as the reviewer runs
so the overlay builder consumes it uniformly, and it writes only under
``--review-root`` -- the source translation run is never modified.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tools.audit_translation_integrity import (
    COLOUR_MORPHEMES,
    audit_row,
    load_glossary_values,
    load_jsonl,
)

REVIEWER = "main-process-deterministic-v1"
CJK_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Greedy on the left so the *last* delimiter splits head from finish. A
# non-greedy match would split `Co-Pilot PLP Cover - X` at the hyphen inside
# `Co-Pilot` and produce nonsense.
DELIMITER_RE = re.compile(r"(?s)^(.*[-_]\s*)(.*)$")
CODE_RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*")
DIGITS_RE = re.compile(r"\d+")


def _edit_distance_at_most(left: str, right: str, limit: int) -> bool:
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        for j, b in enumerate(right, start=1):
            current[j] = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (a != b),
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def code_restore(source: str, translated: str, damaged: list[str]) -> str | None:
    """Restore a source code that the translation mistyped.

    `EKEA-6104110` came out as `EQEA-6104110`: same digits, one letter changed.
    Rewriting the mistyped token back to the source spelling is evidence-bound
    (the digits match exactly and the token sits where the code belongs), so it
    is applied instead of dropping the row to a human.
    """
    result = translated
    for code in damaged:
        code_digits = DIGITS_RE.findall(code)
        if not code_digits:
            continue
        for match in CODE_RUN_RE.finditer(result):
            token = match.group(0)
            if DIGITS_RE.findall(token) != code_digits:
                continue
            if token == code:
                break
            if _edit_distance_at_most(token.casefold(), code.casefold(), 2):
                result = result.replace(token, code, 1)
                return result
    return None


def strip_unexpected_finish(
    source: str, translated: str, glossary_zh: set[str], glossary_en: set[str], morphemes: str
) -> str | None:
    """Drop a colour finish the source cannot carry.

    Used for the shifted rows: source `ISOFIX钢丝罩盖` (no colour) holding the
    neighbour's `- Oat Beige`.
    """
    source_tail = re.split(r"[-_]", source)[-1].strip()
    if source_tail in glossary_zh or any(ch in morphemes for ch in source_tail):
        return None
    match = DELIMITER_RE.match(translated.strip())
    if not match:
        return None
    head, tail = match.group(1), match.group(2).strip()
    if any(tail.casefold() == value.casefold() for value in glossary_en):
        return head.rstrip(" -_") if head.rstrip(" -_") else None
    return None


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def tail_segment(value: str) -> str:
    return re.split(r"[-_]", value)[-1].strip()


def glossary_repair(source: str, translated: str, glossary: dict[str, Any]) -> str | None:
    """Replace a half-translated colour/finish term with its canonical rendering."""
    terms = glossary.get("terms") or {}
    fragments = glossary.get("non_colour_fragments") or {}
    entry = terms.get(tail_segment(source))
    if entry:
        match = DELIMITER_RE.match(translated.strip())
        if not match:
            return None
        prefix, tail = match.group(1), match.group(2)
        candidate = (
            f"{prefix}{entry['en']}"
            if CJK_RE.search(tail) or tail.strip() != entry["en"]
            else translated
        )
        if candidate != translated:
            return _replace_fragments(candidate, fragments) or candidate
        return _replace_fragments(translated, fragments)
    return _replace_fragments(translated, fragments)


def _replace_fragments(text: str, fragments: dict[str, str]) -> str | None:
    """Replace leftover Chinese fragments that are not part of the finish tail.

    Chinese text runs into the surrounding Latin without a space
    (`3rd Row Seat Front扣手 Screw Plug`), so the replacement adds the space the
    English needs instead of gluing the words together.
    """
    result = text
    for fragment, rendering in fragments.items():
        if fragment not in result:
            continue
        result = result.replace(fragment, rendering)
        result = re.sub(
            rf"(?<=[A-Za-z0-9]){re.escape(rendering)}", f" {rendering}", result
        )
        result = re.sub(r"\s{2,}", " ", result)
    return result if result != text else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--glossary", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--reuse-review-dir", type=Path, action="append",
                        help="repeatable: a prior review run whose text may be reused")
    parser.add_argument("--run-id", default="main-process-repairs")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    manifest = load_jsonl(args.manifest)
    drafts = {str(row["term_id"]): row for row in load_jsonl(args.draft)}
    glossary = json.loads(args.glossary.read_text(encoding="utf-8"))
    glossary_zh, glossary_en = load_glossary_values(args.glossary)

    reusable: dict[str, str] = {}
    for root in args.reuse_review_dir or []:
        for path in sorted(root.glob("batch-*.jsonl")):
            for row in load_jsonl(path):
                decision = str(row.get("decision") or "")
                if decision in {"approve", "revise"}:
                    reusable[str(row["term_id"])] = str(row.get("reviewed_translation") or "")

    def gate(row: dict[str, Any], text: str) -> dict[str, Any] | None:
        probe = dict(row)
        probe["translated_text"] = text
        return audit_row(row, probe, glossary_zh, glossary_en)

    repairs: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()

    draft_texts = {
        tid: str(row.get("translated_text") or "") for tid, row in drafts.items()
    }
    # Which stored texts are misplaced (fail their own row)? Only those may be
    # reclaimed, so a legitimately correct neighbour is never stolen.
    misplaced: dict[int, bool] = {}
    for index, row in enumerate(manifest):
        text = draft_texts.get(str(row["term_id"]), "")
        misplaced[index] = bool(text) and gate(row, text) is not None

    claimed: set[int] = set()

    for index, row in enumerate(manifest):
        term_id = str(row["term_id"])
        draft_row = drafts.get(term_id)
        if draft_row is None:
            continue
        problems = gate(row, draft_texts.get(term_id, ""))
        if problems is None:
            continue

        chosen: str | None = None
        origin: str | None = None

        reuse = reusable.get(term_id)
        if reuse and gate(row, reuse) is None:
            chosen, origin = reuse, "reused_review"

        if chosen is None:
            damaged = list(problems.get("damaged_codes") or [])
            for flag in problems.get("gate_errors") or []:
                marker = "protected token missing after review: "
                if marker in flag:
                    damaged.append(flag.split(marker, 1)[1].strip())
            if damaged:
                candidate = code_restore(row["source_text"], draft_texts.get(term_id, ""), damaged)
                if candidate and gate(row, candidate) is None:
                    chosen, origin = candidate, "code_restore"

        if chosen is None:
            candidate = glossary_repair(row["source_text"], draft_texts.get(term_id, ""), glossary)
            if candidate and gate(row, candidate) is None:
                chosen, origin = candidate, "glossary"

        if chosen is None:
            candidate = strip_unexpected_finish(
                row["source_text"], draft_texts.get(term_id, ""),
                glossary_zh, glossary_en, COLOUR_MORPHEMES,
            )
            if candidate and gate(row, candidate) is None:
                chosen, origin = candidate, "stripped_unexpected_finish"

        if chosen is None:
            for offset in (-1, -2, 1, 2):
                j = index + offset
                if j < 0 or j >= len(manifest) or j in claimed or not misplaced.get(j):
                    continue
                text = draft_texts.get(str(manifest[j]["term_id"]), "")
                if not text or gate(row, text) is not None:
                    continue
                chosen, origin = text, f"reclaimed_shift_{offset:+d}"
                claimed.add(j)
                break

        if chosen is None:
            reasons.update(problems.keys() if isinstance(problems, dict) else [])
            repairs.append({
                "term_id": term_id,
                "decision": "needs_human",
                "reviewed_translation": draft_texts.get(term_id, ""),
                "reason": "main-process repair could not fix this row: "
                + "; ".join(f"{k}={v}" for k, v in problems.items()),
                "qa_flags": [f"{k}: {v}" for k, v in problems.items()],
                "preserved_source_tokens": [],
                "confidence": "low",
                "reviewer": REVIEWER,
            })
            continue

        reasons[origin or "unknown"] += 1
        repairs.append({
            "term_id": term_id,
            "decision": "revise",
            "reviewed_translation": chosen,
            "reason": f"main-process deterministic repair ({origin}); source text unchanged",
            "qa_flags": [],
            "preserved_source_tokens": [],
            "confidence": "medium",
            "reviewer": REVIEWER,
        })

    output_dir = args.review_root / "output"
    for start in range(0, len(repairs), args.batch_size):
        chunk = repairs[start : start + args.batch_size]
        atomic_write(
            output_dir / f"batch-{start // args.batch_size + 1:04d}.jsonl",
            "".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                for item in chunk
            ),
        )

    state = {
        "run_id": args.run_id,
        "reviewer": REVIEWER,
        "mode": "deterministic_repair_no_model",
        "manifest": str(args.manifest),
        "rows_examined": len(manifest),
        "rows_repaired": len(repairs),
        "decision_counts": dict(Counter(r["decision"] for r in repairs)),
        "repair_origin_counts": dict(reasons),
        "glossary": str(args.glossary),
        "batch_count": (len(repairs) + args.batch_size - 1) // args.batch_size,
        "run_status": "complete",
        "updated_at": now_iso(),
    }
    atomic_write(
        args.review_root / "state.json",
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps(state, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
