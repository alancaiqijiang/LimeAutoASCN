#!/usr/bin/env python3
"""Derive the runtime English glossary from the frozen final text.

Why this exists: the reviewed candidate overlay
(`translation/reviews/codex-luna-20260910/catalog_translation_candidate_wp2.sqlite`)
is not the shipping text -- it differs from `catalog-translation-final-en.jsonl`
on 556 rows (the 631-row text-layer pass was applied only to the jsonl) and every
one of its rows is unpublished, so the runtime lookup returns nothing from it.

The runtime artifact is therefore derived from the frozen final text, and only
this artifact may carry `status='published'`.

Binding: rows are keyed by `(term_kind, source_text)`, never by material code.
98 source strings are shared across kinds and 16 of them render differently per
kind (`主轴组件` -> node `Main Shaft Assembly` / part `Main Shaft Component`).
Material codes are release-local and are re-issued when a release is rebuilt;
source text can be reused across releases.

Usage:
    .venv/bin/python tools/build_catalog_translation_runtime.py --check
    .venv/bin/python tools/build_catalog_translation_runtime.py --write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.catalog_translation import stable_term_id  # noqa: E402

DEFAULT_JSONL = (
    REPO
    / "local-reports/translation-linguistic-pass-20260910/consolidated"
    / "catalog-translation-final-en.jsonl"
)
DEFAULT_OUT = (
    REPO
    / "local-reports/translation-linguistic-pass-20260910/runtime"
    / "catalog_translation_en.sqlite"
)
EXPECTED_SHA256 = "ce915737f08affeb16be79d7a8ef187c44b1ce0f1ce1e146ee47cd84370acf73"
EXPECTED_ROWS = 41189
EXPECTED_KINDS = {"series": 253, "model": 4037, "node": 707, "part": 36192}

SCHEMA = """
PRAGMA journal_mode = DELETE;
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

PUBLISHED = "published"
RUNTIME_REVIEWER = "final-text-runtime-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_final_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def verify_rows(
    rows: list[dict],
    *,
    expected_rows: int = EXPECTED_ROWS,
    expected_kinds: dict[str, int] | None = None,
    allow_growth: bool = False,
) -> list[str]:
    """Return every contract violation; empty means the input is shippable.

    ``allow_growth`` is used by the material-import path: after new source terms are
    translated the frozen final text grows, so an exact row count can no longer hold.
    Growth is only ever allowed *upwards* — losing a term, losing an entire kind, or
    leaking an unknown kind is still refused, which is what catches a truncated or
    wrong-file input. The frozen default stays exact equality.
    """
    kinds = EXPECTED_KINDS if expected_kinds is None else expected_kinds
    problems: list[str] = []

    counts: dict[str, int] = {}
    empty = 0
    mismatched: list[str] = []
    for row in rows:
        kind = str(row.get("term_kind") or "")
        source = str(row.get("source_text") or "")
        counts[kind] = counts.get(kind, 0) + 1
        if not str(row.get("english") or "").strip():
            empty += 1
        if str(row.get("term_id") or "") != stable_term_id(kind, source):
            mismatched.append(str(row.get("term_id") or "?"))

    if allow_growth:
        if len(rows) < expected_rows:
            problems.append(f"row count {len(rows)} < {expected_rows} (growth must not lose rows)")
        shrank = {k: (counts.get(k, 0), v) for k, v in kinds.items() if counts.get(k, 0) < v}
        if shrank:
            problems.append(f"kind counts shrank {shrank} (baseline {kinds})")
        unknown = sorted(set(counts) - set(kinds))
        if unknown:
            problems.append(f"unknown term kinds: {unknown}")
    else:
        if len(rows) != expected_rows:
            problems.append(f"row count {len(rows)} != {expected_rows}")
        if counts != kinds:
            problems.append(f"kind counts {counts} != {kinds}")

    if empty:
        problems.append(f"empty english rows: {empty}")
    if mismatched:
        problems.append(f"term_id mismatch rows: {len(mismatched)} e.g. {mismatched[:3]}")

    seen: set[str] = set()
    dupes = [str(r.get("term_id")) for r in rows if str(r.get("term_id")) in seen or seen.add(str(r.get("term_id")))]
    if dupes:
        problems.append(f"duplicate term_ids: {len(dupes)} e.g. {dupes[:3]}")
    return problems


def display_path(path: Path) -> str:
    """Repo-relative when possible; absolute otherwise.

    Import snapshots do not have to live inside the repository, so this must never raise.
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def verify_baseline_survives(rows: list[dict], baseline: list[dict]) -> list[str]:
    """Every term_id in ``baseline`` must still be present in ``rows``.

    Counts alone cannot express this: losing one part and gaining another keeps the row
    count and the per-kind counts unchanged, so only an ID-set comparison catches it.
    That is the guarantee an import needs — it may add, never drop.
    """
    present = {str(r.get("term_id") or "") for r in rows}
    lost = sorted({str(r.get("term_id") or "") for r in baseline} - present)
    if not lost:
        return []
    names = {str(r.get("term_id") or ""): str(r.get("source_text") or "") for r in baseline}
    sample = [f"{tid} ({names.get(tid, '')})" for tid in lost[:3]]
    return [f"lost {len(lost)} baseline terms e.g. {sample}"]


def build(rows: list[dict], *, fingerprint: str, jsonl_sha256: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    os.close(fd)
    temp = Path(temp_name)
    meta = {
        "schema_version": "limeauto.catalog_translation.v1",
        "artifact": "runtime_english_glossary",
        "built_from": "catalog-translation-final-en.jsonl",
        "source_jsonl_sha256": jsonl_sha256,
        "source_snapshot_fingerprint": fingerprint,
        "rows": str(len(rows)),
        "binding_key": "term_kind+source_text",
    }
    try:
        with sqlite3.connect(temp) as connection:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO translation_meta (key, value) VALUES (?, ?)", sorted(meta.items())
            )
            connection.executemany(
                """
                INSERT INTO translation_terms (
                    term_id, term_kind, source_text, translated_text, lang,
                    status, source_snapshot_fingerprint, reviewer, review_note,
                    engine, updated_at
                ) VALUES (?, ?, ?, ?, 'en', ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["term_id"], row["term_kind"], row["source_text"],
                        str(row["english"]).strip(), PUBLISHED, fingerprint,
                        RUNTIME_REVIEWER, str(row.get("reviewer") or ""),
                        str(row.get("engine") or ""), str(row.get("updated_at") or ""),
                    )
                    for row in rows
                ],
            )
            connection.commit()
        os.replace(temp, out)
    finally:
        if temp.exists():
            temp.unlink()


def audit(path: Path, rows: list[dict], *, fingerprint: str) -> dict:
    """Read the artifact back and prove it satisfies the runtime contract."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        total = connection.execute("SELECT COUNT(*) FROM translation_terms").fetchone()[0]
        published = connection.execute(
            "SELECT COUNT(*) FROM translation_terms WHERE status = ?", (PUBLISHED,)
        ).fetchone()[0]
        empty = connection.execute(
            "SELECT COUNT(*) FROM translation_terms WHERE TRIM(translated_text) = ''"
        ).fetchone()[0]
        stored_fp = connection.execute(
            "SELECT COUNT(*) FROM translation_terms WHERE source_snapshot_fingerprint = ?",
            (fingerprint,),
        ).fetchone()[0]
        kind_rows = connection.execute(
            "SELECT term_kind, COUNT(*) FROM translation_terms GROUP BY term_kind"
        ).fetchall()

        def lookup(kind: str, source: str) -> str | None:
            row = connection.execute(
                "SELECT translated_text FROM translation_terms "
                "WHERE term_id = ? AND term_kind = ? AND lang = 'en' AND status = ?",
                (stable_term_id(kind, source), kind, PUBLISHED),
            ).fetchone()
            return (row[0] or None) if row else None

        sample = [r for r in rows if r["term_kind"] == "part"][:1]
        sample += [r for r in rows if r["term_kind"] == "node"][:1]
        sample += [r for r in rows if r["term_kind"] == "model"][:1]
        sample += [r for r in rows if r["term_kind"] == "series"][:1]
        probes = [
            {
                "term_kind": r["term_kind"], "source_text": r["source_text"],
                "expected": str(r["english"]).strip(), "got": lookup(r["term_kind"], r["source_text"]),
            }
            for r in sample
        ]

    return {
        "total": total,
        "published": published,
        "empty_translated": empty,
        "fingerprint_match_rows": stored_fp,
        "kinds": {r[0]: r[1] for r in kind_rows},
        "probes_ok": all(p["got"] == p["expected"] for p in probes),
        "probes": probes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--fingerprint", required=False, default="",
                    help="release source_snapshot_fingerprint; required for --write")
    ap.add_argument("--expected-sha256", default=None,
                    help="pin the input sha256; default pins the frozen final text "
                         "(and is not applied under --allow-growth unless given)")
    ap.add_argument("--baseline-jsonl", type=Path, default=DEFAULT_JSONL,
                    help="under --allow-growth: every term_id in this file must survive")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    ap.add_argument("--allow-growth", action="store_true",
                    help="accept a snapshot that grew (material import); never accepts losses")
    ap.add_argument("--baseline-rows", type=int, default=EXPECTED_ROWS,
                    help="frozen row baseline used with --allow-growth")
    args = ap.parse_args()

    jsonl = args.jsonl.resolve()
    if not jsonl.is_file():
        print(json.dumps({"error": f"missing input {jsonl}"}, ensure_ascii=False))
        return 2

    digest = sha256_file(jsonl)
    pin = args.expected_sha256
    if pin is None:
        # The sha pin exists to prove "this is the frozen final text".  An import
        # input is by definition a different file, so the pin is only applied when
        # the operator states it explicitly.
        pin = "" if args.allow_growth else EXPECTED_SHA256
    if pin and digest != pin:
        print(json.dumps({
            "error": "input sha256 does not match the frozen final text",
            "expected": pin, "actual": digest,
        }, ensure_ascii=False, indent=1))
        return 3

    rows = load_final_rows(jsonl)
    problems = verify_rows(
        rows,
        expected_rows=args.baseline_rows,
        allow_growth=args.allow_growth,
    )
    if args.allow_growth:
        baseline_path = args.baseline_jsonl.resolve()
        if not baseline_path.is_file():
            problems.append(f"baseline jsonl is missing: {baseline_path}")
        elif baseline_path == jsonl:
            # A silent no-op here would defeat the whole check, so refuse it outright.
            problems.append(
                "baseline jsonl equals the input; point --baseline-jsonl at the frozen "
                "pre-import snapshot so lost terms can actually be detected"
            )
        else:
            problems.extend(verify_baseline_survives(rows, load_final_rows(baseline_path)))
    report: dict = {
        "jsonl": display_path(jsonl),
        "sha256": digest,
        "rows": len(rows),
        "problems": problems,
    }
    if args.allow_growth:
        report["growth_mode"] = {
            "baseline_rows": args.baseline_rows,
            "delta_rows": len(rows) - args.baseline_rows,
            "baseline_jsonl": str(args.baseline_jsonl),
        }
    if problems:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 4

    fingerprint = args.fingerprint.strip()
    if args.write and not fingerprint:
        print(json.dumps({"error": "--write requires --fingerprint"}, ensure_ascii=False))
        return 5

    if args.check:
        report["binding"] = {
            "key": "term_kind+source_text",
            "cross_kind_shared_sources": 98,
            "cross_kind_divergent_renderings": 16,
        }
        report["out"] = display_path(args.out)
        report["would_publish"] = len(rows)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    out = args.out.resolve()
    build(rows, fingerprint=fingerprint, jsonl_sha256=digest, out=out)
    report["written"] = str(out)
    report["audit"] = audit(out, rows, fingerprint=fingerprint)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
