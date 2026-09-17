#!/usr/bin/env python3
"""Build a separate, fingerprint-fenced LimeAuto catalog translation overlay."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.catalog_translation import stable_term_id
from tools.translation_token_rules import translation_token_errors

STATUS_VALUES = {
    "ai_draft",
    "human_review",
    "luna_reviewed",
    "main_process_reviewed",
    "needs_human",
    "rejected_with_source_fallback",
    "published",
}

# A review may come from the Codex/GPT-5.6-Luna linguistic pass or from the
# main-process deterministic fallback used when that path times out. The
# fallback checks identifiers/values and output hygiene only, so it may never
# reach ``published`` and is recorded under its own status.
REVIEWER_POLICIES = {
    "gpt-5.6-luna": {"status": "luna_reviewed", "publishable": True},
    "main-process-deterministic-v1": {
        "status": "main_process_reviewed",
        "publishable": False,
    },
    # WP2 batches: the corpus majority wording is applied to the minority rows. They are
    # deterministic rewrites of existing text, so they may not reach ``published`` either.
    "main-process-majority-unification-v1": {
        "status": "main_process_reviewed",
        "publishable": False,
    },
    "main-process-finish-canonical-v1": {
        "status": "main_process_reviewed",
        "publishable": False,
    },
    "main-process-separator-normalisation-v1": {
        "status": "main_process_reviewed",
        "publishable": False,
    },
    # WP3: term corrections read out row by row from the high-usage batches.
    "main-process-term-rule-v1": {
        "status": "main_process_reviewed",
        "publishable": False,
    },
    # Restores the trailing finish rendering that the WP2 chunk batch dropped. The text
    # itself is not new: the tail is the row's own earlier rendering, so it stays
    # unpublished like every other deterministic rewrite.
    "main-process-finish-restore-v1": {
        "status": "main_process_reviewed",
        "publishable": False,
    },
}
SCHEMA = """
PRAGMA journal_mode = DELETE;
PRAGMA foreign_keys = ON;
CREATE TABLE translation_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE translation_terms (
    term_id TEXT NOT NULL,
    term_kind TEXT NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL DEFAULT '',
    lang TEXT NOT NULL,
    status TEXT NOT NULL,
    source_snapshot_fingerprint TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (term_id, lang),
    CHECK (status IN (
        'ai_draft', 'human_review', 'luna_reviewed', 'needs_human',
        'main_process_reviewed', 'rejected_with_source_fallback', 'published'
    )),
    CHECK (lang = 'en')
);
CREATE INDEX translation_terms_kind_source_lang_idx
    ON translation_terms (term_kind, source_text, lang);
CREATE INDEX translation_terms_status_lang_idx
    ON translation_terms (status, lang);
CREATE INDEX translation_terms_fingerprint_lang_idx
    ON translation_terms (source_snapshot_fingerprint, lang);
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_reviews(review_dirs: list[Path]) -> tuple[dict[str, dict[str, Any]], int]:
    """Merge review runs left to right; a later run overrides an earlier one.

    A deterministic repair run intentionally overrides the draft-level review of
    the same term, so a duplicate must override rather than abort. The override
    count is reported so the merge stays visible.
    """
    result: dict[str, dict[str, Any]] = {}
    overridden = 0
    for review_dir in review_dirs:
        for path in sorted(review_dir.glob("batch-*.jsonl")):
            for row in load_jsonl(path):
                term_id = str(row.get("term_id") or "")
                if not term_id:
                    raise ValueError(f"missing review term_id in {path}")
                if term_id in result:
                    overridden += 1
                result[term_id] = row
    return result, overridden


def make_records(
    manifest: list[dict[str, Any]],
    drafts: list[dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
    *,
    publish_luna: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    manifest_by_id: dict[str, dict[str, Any]] = {}
    for row in manifest:
        term_id = str(row.get("term_id") or "")
        if term_id:
            manifest_by_id.setdefault(term_id, row)
    draft_by_id: dict[str, dict[str, Any]] = {}
    for row in drafts:
        term_id = str(row.get("term_id") or "")
        if not term_id or term_id in draft_by_id:
            raise ValueError("draft contains duplicate or missing term_id")
        draft_by_id[term_id] = row
    if set(draft_by_id) != set(manifest_by_id):
        missing = sorted(set(manifest_by_id) - set(draft_by_id))
        extra = sorted(set(draft_by_id) - set(manifest_by_id))
        raise ValueError(f"draft coverage mismatch missing={missing[:3]} extra={extra[:3]}")

    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for term_id, draft in draft_by_id.items():
        source = str(draft.get("source_text") or "")
        kind = str(draft.get("term_kind") or "")
        if stable_term_id(kind, source) != term_id:
            raise ValueError(f"term_id does not match source: {term_id}")
        fingerprint = str(draft.get("source_snapshot_fingerprint") or "")
        translated = str(draft.get("translated_text") or "").strip()
        status = str(draft.get("status") or "human_review")
        reviewer = ""
        note = "; ".join(str(x) for x in draft.get("qa_errors") or [])
        review = reviews.get(term_id)
        if review:
            reviewer_name = str(review.get("reviewer") or "")
            policy = REVIEWER_POLICIES.get(reviewer_name)
            if policy is None:
                raise ValueError(f"unexpected reviewer for {term_id}: {reviewer_name!r}")
            decision = str(review.get("decision") or "")
            reviewed = str(review.get("reviewed_translation") or "").strip()
            if decision == "reject_with_source_fallback":
                translated = ""
                status = "rejected_with_source_fallback"
            elif decision in {"approve", "revise"}:
                if not reviewed:
                    raise ValueError(f"empty reviewed translation for {term_id}")
                token_errors = translation_token_errors(
                    source, reviewed, draft.get("qa_errors") or []
                )
                if token_errors:
                    raise ValueError(
                        f"translation token gate failed for {term_id}: "
                        + "; ".join(token_errors)
                    )
                translated = reviewed
                if review.get("qa_flags"):
                    status = "needs_human"
                elif publish_luna and policy["publishable"]:
                    status = "published"
                else:
                    status = policy["status"]
            elif decision == "needs_human":
                translated = reviewed
                status = "needs_human"
            else:
                raise ValueError(f"invalid review decision for {term_id}: {decision}")
            reviewer = reviewer_name
            flags = [str(flag) for flag in review.get("qa_flags") or []]
            note = str(review.get("reason") or "")
            if flags:
                note = note + " | qa_flags: " + "; ".join(flags)
        if status not in STATUS_VALUES:
            raise ValueError(f"invalid status for {term_id}: {status}")
        record = {
            "term_id": term_id,
            "term_kind": kind,
            "source_text": source,
            "translated_text": translated,
            "lang": "en",
            "status": status,
            "source_snapshot_fingerprint": fingerprint,
            "reviewer": reviewer,
            "review_note": note,
            "engine": str(draft.get("engine") or ""),
        }
        records.append(record)
        counts[status] = counts.get(status, 0) + 1
    if set(reviews) - set(draft_by_id):
        extra = sorted(set(reviews) - set(draft_by_id))
        raise ValueError(f"reviews contain unknown term_ids: {extra[:3]}")
    records.sort(key=lambda row: row["term_id"])
    return records, counts


def build_overlay(
    output: Path,
    records: list[dict[str, Any]],
    *,
    release_no: str,
    fingerprint: str,
    manifest_sha256: str,
    source_run: str,
    counts: dict[str, int],
    reviewers: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with sqlite3.connect(temp) as connection:
            connection.executescript(SCHEMA)
            metadata = {
                "schema_version": "limeauto.catalog_translation.v1",
                "source_run": source_run,
                "release_no": release_no,
                "source_snapshot_fingerprint": fingerprint,
                "manifest_sha256": manifest_sha256,
                "generated_at": now_iso(),
                "reviewer": reviewers,
                "status_counts_json": json.dumps(counts, ensure_ascii=False, sort_keys=True),
            }
            connection.executemany(
                "INSERT INTO translation_meta (key, value) VALUES (?, ?)",
                metadata.items(),
            )
            connection.executemany(
                """
                INSERT INTO translation_terms (
                    term_id, term_kind, source_text, translated_text, lang,
                    status, source_snapshot_fingerprint, reviewer, review_note,
                    engine, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["term_id"], row["term_kind"], row["source_text"],
                        row["translated_text"], row["lang"], row["status"],
                        row["source_snapshot_fingerprint"], row["reviewer"],
                        row["review_note"], row["engine"], now_iso(),
                    )
                    for row in records
                ],
            )
            connection.commit()
        os.replace(temp, output)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument(
        "--reviews-dir", type=Path, action="append", required=True
    )
    parser.add_argument("--release-no", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-luna", action="store_true")
    args = parser.parse_args()

    manifest = load_jsonl(args.manifest)
    drafts = load_jsonl(args.draft)
    reviews, overridden = load_reviews(args.reviews_dir)
    records, counts = make_records(
        manifest, drafts, reviews, publish_luna=args.publish_luna
    )
    reviewers = "+".join(
        sorted({row["reviewer"] for row in records if row["reviewer"]})
    ) or "none"
    actual_fingerprint = {
        str(row.get("source_snapshot_fingerprint") or "") for row in drafts
    }
    if actual_fingerprint != {args.fingerprint}:
        raise SystemExit("draft fingerprint does not match requested release fingerprint")
    build_overlay(
        args.output,
        records,
        release_no=args.release_no,
        fingerprint=args.fingerprint,
        manifest_sha256=sha256_file(args.manifest),
        source_run=args.source_run,
        counts=counts,
        reviewers=reviewers,
    )
    print(json.dumps({
        "output": str(args.output),
        "records": len(records),
        "reviews": len(reviews),
        "status_counts": counts,
        "reviewers": reviewers,
        "publish_luna": args.publish_luna,
        "overridden_reviews": overridden,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
