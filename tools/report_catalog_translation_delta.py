#!/usr/bin/env python3
"""Report what a NEW catalog release changes about the English translation snapshot.

Why this exists: when the material library is updated, the rebuilt release carries a new
`source_snapshot_fingerprint`. The runtime speaks English only while the glossary matches
that fingerprint, so the operator has to rebuild the glossary against the new release. That
rebuild is only safe if the *term set* did not lose or rename anything — which is exactly
what this report decides, before anything is published.

It is read-only and offline: it opens the new release's manifest and the currently published
runtime glossary, and prints a JSON delta. Nothing is written except the requested --output.

Inputs
  --manifest   catalog-translation-manifest.jsonl for the NEW release
               (tools/extract_catalog_translation_manifest.py --release <new>)
  --glossary   the currently published runtime glossary sqlite

Output (JSON, also written to --output when given)
  counts        rows on each side
  added         terms the new release has that the glossary cannot translate  -> worklist
  removed       glossary terms no longer present in the new release           -> dead rows
  by_kind       per-kind coverage
  safe_to_carry_forward
                true when nothing was lost that the glossary used to cover

A *renamed* source term is not guessable from IDs alone: it shows up as one `removed` and
one `added` entry with similar text. Both lists are printed so the operator can pair them.

Usage
    .venv/bin/python tools/report_catalog_translation_delta.py \
        --manifest new-manifest.jsonl \
        --glossary local-reports/.../runtime/catalog_translation_en.sqlite \
        --output /tmp/delta.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.catalog_translation import PUBLISHED_STATUS, SUPPORTED_LANG  # noqa: E402

DEFAULT_DETAIL_LIMIT = 200


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_glossary(path: Path) -> tuple[dict[str, dict], str]:
    """Return {term_id: row} for published English rows plus the stored fingerprint."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        fingerprint_row = connection.execute(
            "SELECT value FROM translation_meta WHERE key = 'source_snapshot_fingerprint'"
        ).fetchone()
        rows = connection.execute(
            "SELECT term_id, term_kind, source_text, translated_text, status,"
            "       source_snapshot_fingerprint"
            "  FROM translation_terms WHERE lang = ? AND status = ?",
            (SUPPORTED_LANG, PUBLISHED_STATUS),
        ).fetchall()
    return {str(r["term_id"]): dict(r) for r in rows}, (str(fingerprint_row[0]) if fingerprint_row else "")


def classify(manifest: list[dict], glossary: dict[str, dict]) -> dict:
    manifest_ids: dict[str, dict] = {}
    for row in manifest:
        term_id = str(row.get("term_id") or "")
        if term_id:
            manifest_ids[term_id] = row

    glossary_ids = set(glossary)
    added_ids = sorted(set(manifest_ids) - glossary_ids)
    removed_ids = sorted(glossary_ids - set(manifest_ids))

    # A term present on both sides can still be stale: the manifest is authoritative for
    # kind and source text, so compare them rather than trusting the ID alone.
    drifted = [
        term_id for term_id in sorted(set(manifest_ids) & glossary_ids)
        if str(manifest_ids[term_id].get("term_kind") or "") != str(glossary[term_id].get("term_kind") or "")
        or str(manifest_ids[term_id].get("source_text") or "") != str(glossary[term_id].get("source_text") or "")
    ]

    def entry(term_id: str, side: dict) -> dict:
        return {
            "term_id": term_id,
            "term_kind": str(side.get("term_kind") or ""),
            "source_text": str(side.get("source_text") or ""),
            "usage_count": int(side.get("usage_count") or 0),
            "english": str(side.get("translated_text") or ""),
        }

    added = sorted((entry(t, manifest_ids[t]) for t in added_ids),
                   key=lambda r: (-r["usage_count"], r["term_kind"], r["source_text"]))
    removed = sorted((entry(t, glossary[t]) for t in removed_ids),
                     key=lambda r: (r["term_kind"], r["source_text"]))

    kinds: list[str] = sorted({str(r.get("term_kind") or "") for r in manifest_ids.values()} |
                              {str(r.get("term_kind") or "") for r in glossary.values()})
    by_kind = {}
    for kind in kinds:
        # Count DISTINCT term_ids, not manifest rows: the manifest legitimately carries
        # duplicate term_ids (one term used by several source occurrences), and counting
        # rows here would report phantom gaps.
        want_ids = {t for t, r in manifest_ids.items() if str(r.get("term_kind") or "") == kind}
        have_ids = want_ids & set(glossary)
        by_kind[kind] = {
            "release_terms": len(want_ids),
            "translated": len(have_ids),
            "missing": len(want_ids) - len(have_ids),
            "coverage": round(len(have_ids) / len(want_ids), 6) if want_ids else 1.0,
        }

    return {
        "counts": {
            "release_terms": len(manifest_ids),
            "glossary_terms": len(glossary),
            "translated": len(set(manifest_ids) & glossary_ids) - len(drifted),
            "missing": len(added_ids),
            "dead_glossary_rows": len(removed_ids),
            "drifted": len(drifted),
        },
        "by_kind": by_kind,
        "added": added,
        "removed": removed,
        "drifted": drifted,
        "safe_to_carry_forward": not removed_ids and not drifted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--glossary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--detail-limit", type=int, default=DEFAULT_DETAIL_LIMIT,
                        help="cap the added/removed lists in the printed report")
    args = parser.parse_args()

    if not args.manifest.is_file():
        print(json.dumps({"error": f"missing manifest {args.manifest}"}, ensure_ascii=False))
        return 2
    if not args.glossary.is_file():
        print(json.dumps({"error": f"missing glossary {args.glossary}"}, ensure_ascii=False))
        return 2

    manifest = load_jsonl(args.manifest)
    glossary, fingerprint = read_glossary(args.glossary)
    report = classify(manifest, glossary)
    report["manifest"] = str(args.manifest)
    report["glossary"] = str(args.glossary)
    report["glossary_fingerprint"] = fingerprint
    report["manifest_fingerprint"] = next(
        (str(r.get("source_snapshot_fingerprint") or "") for r in manifest if r.get("source_snapshot_fingerprint")),
        "",
    )
    report["fingerprint_changed"] = bool(
        report["manifest_fingerprint"] and report["manifest_fingerprint"] != fingerprint
    )

    printable = dict(report)
    if args.detail_limit >= 0:
        printable["added"] = report["added"][: args.detail_limit]
        printable["removed"] = report["removed"][: args.detail_limit]
        printable["added_truncated"] = max(0, len(report["added"]) - args.detail_limit)
        printable["removed_truncated"] = max(0, len(report["removed"]) - args.detail_limit)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps(printable, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
