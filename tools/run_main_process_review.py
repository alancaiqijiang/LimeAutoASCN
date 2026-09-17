#!/usr/bin/env python3
"""Main-process deterministic catalog translation review.

Runs when the Codex/GPT-5.6-Luna path is unavailable for a long time (upstream
timeouts). It applies every check the main process can decide on its own
evidence -- identifier and value preservation, residual Chinese, empty or
untranslated output, term identity -- and records the outcome in the same
review-batch shape the Luna runs use, so the overlay builder consumes both
uniformly.

It never invents a translation: the reviewed text is either the existing AI
draft, unchanged, or withheld as ``needs_human``. Rows that need real
linguistic judgement stay queued for the Luna path; nothing produced here is
published.
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
from app.catalog_translation import stable_term_id
from tools.audit_translation_integrity import audit_row, load_glossary_values
from tools.translation_token_rules import translation_token_errors

REVIEWER = "main-process-deterministic-v1"
CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
LATIN_RE = re.compile(r"[A-Za-z]")
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|FIXME|XXX|\?\?\?)\b", re.IGNORECASE)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


GLOSSARY_ZH: set[str] = set()
GLOSSARY_EN: set[str] = set()


def check_row(row: dict[str, Any]) -> list[str]:
    """Return the deterministic problems found in one draft row.

    Shares the integrity checks with tools/audit_translation_integrity.py so the
    reviewer and the auditor can never disagree: identifier and value
    preservation, part-number integrity (digits and letter groups), cross-row
    code contamination, residual Chinese, empty or source-identical output and
    placeholder markers.
    """
    problems: list[str] = []
    source = str(row.get("source_text") or "")
    kind = str(row.get("term_kind") or "")
    term_id = str(row.get("term_id") or "")
    translated = str(row.get("translated_text") or "").strip()

    if stable_term_id(kind, source) != term_id:
        problems.append("term_id does not match source_text")

    if not translated:
        problems.append("empty translation")
        return problems

    # Only demand English when the source actually carries something to
    # translate: a punctuation-only term such as "/" is legitimately "/".
    translatable = bool(CJK_RE.search(source) or LATIN_RE.search(source))
    if translatable and not LATIN_RE.search(translated):
        problems.append("translation contains no Latin letters")

    findings = audit_row(row, row, GLOSSARY_ZH, GLOSSARY_EN) or {}
    for key, value in findings.items():
        if key == "empty_translation":
            continue
        if isinstance(value, list):
            problems.extend(str(item) for item in value)
        else:
            problems.append(key)

    return problems


def make_review(row: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    translated = str(row.get("translated_text") or "").strip()
    if problems:
        return {
            "term_id": row["term_id"],
            "decision": "needs_human",
            "reviewed_translation": translated,
            "reason": "main-process deterministic checks failed: "
            + "; ".join(problems),
            "qa_flags": problems,
            "preserved_source_tokens": [],
            "confidence": "low",
            "reviewer": REVIEWER,
        }
    return {
        "term_id": row["term_id"],
        "decision": "approve",
        "reviewed_translation": translated,
        "reason": "main-process deterministic checks passed: identifier and "
        "value preservation, no residual CJK, non-empty English output; "
        "linguistic equivalence not yet judged by the Luna path",
        "qa_flags": [],
        "preserved_source_tokens": [],
        "confidence": "medium",
        "reviewer": REVIEWER,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--statuses", default="ai_draft")
    parser.add_argument("--term-kinds", default="")
    parser.add_argument(
        "--exclude-review-root", type=Path, action="append",
        help="repeatable: skip term_ids already reviewed by this run directory",
    )
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--run-id", default="main-process-review")
    parser.add_argument("--scope", default="")
    parser.add_argument("--glossary", type=Path,
                        help="colour/finish glossary; enables the unexpected-finish-term check")
    args = parser.parse_args()

    global GLOSSARY_ZH, GLOSSARY_EN
    GLOSSARY_ZH, GLOSSARY_EN = load_glossary_values(args.glossary)

    statuses = {item.strip() for item in args.statuses.split(",") if item.strip()}
    kinds = {item.strip() for item in args.term_kinds.split(",") if item.strip()}

    excluded: set[str] = set()
    for root in args.exclude_review_root or []:
        for path in (root / "output").glob("batch-*.jsonl"):
            for row in load_jsonl(path):
                excluded.add(str(row.get("term_id")))

    rows = [
        row
        for row in load_jsonl(args.source)
        if str(row.get("status") or "") in statuses
        and (not kinds or str(row.get("term_kind") or "") in kinds)
        and str(row.get("term_id") or "") not in excluded
    ]

    output_dir = args.review_root / "output"
    batches = [
        rows[i : i + args.batch_size] for i in range(0, len(rows), args.batch_size)
    ]
    if args.max_batches:
        batches = batches[: args.max_batches]

    decision_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    for number, batch in enumerate(batches, start=1):
        reviews = []
        for row in batch:
            review = make_review(row, check_row(row))
            reviews.append(review)
            decision_counts[review["decision"]] += 1
            for flag in review["qa_flags"]:
                flag_counts[flag.split(":")[0]] += 1
        atomic_write(
            output_dir / f"batch-{number:04d}.jsonl",
            "".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                for item in reviews
            ),
        )
        print(
            json.dumps(
                {
                    "batch": number,
                    "batches": len(batches),
                    "rows": sum(len(b) for b in batches[:number]),
                    "mode": REVIEWER,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    state = {
        "run_id": args.run_id,
        "reviewer": REVIEWER,
        "scope": args.scope,
        "mode": "deterministic_main_process",
        "severity": "structural/semantic-rule checks only; not a linguistic review",
        "source": str(args.source),
        "statuses": sorted(statuses),
        "term_kinds": sorted(kinds),
        "batch_size": args.batch_size,
        "input_rows": len(rows),
        "batch_count": len(batches),
        "completed_batches": len(batches),
        "completed_rows": sum(len(b) for b in batches),
        "decision_counts": dict(decision_counts),
        "flag_counts": dict(flag_counts),
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
