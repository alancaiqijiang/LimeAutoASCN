#!/usr/bin/env python3
"""Build the consolidated LimeAuto catalogue translation deliverable (read-only).

Integrates every reviewed lane into ONE artifact:

  * the effective English text of every term (the same `effective_text`
    entrypoint the audits use, so this is the reviewed text and not the draft)
  * per-term provenance: which reviewer won the override, and the previous text
  * per-term review status from the candidate overlay
  * the finish/terminology decisions that apply to the row

Outputs (all under the directory given by --out):
  catalog-translation-consolidated.jsonl   one record per term, full provenance
  catalog-translation-consolidated.csv     term_id, kind, source, english, status
  catalog-translation-consolidated.json    summary + counts + hashes
  catalog-translation-review-gaps.json     rows whose review level is lowest

This tool NEVER writes to release.sqlite, staging or production, and it does
not publish anything (the overlay's `published` stays 0).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

LOCAL = Path(__file__).resolve().parent
REPO = LOCAL.parents[0] if (LOCAL / "audit_translation_integrity.py").exists() else LOCAL.parents[1]
sys.path.insert(0, str(LOCAL))
sys.path.insert(0, str(REPO))

from tools.audit_translation_integrity import effective_text, load_review_overrides  # noqa: E402

RUN = REPO / "translation/runs/full-mimo-20260902"
OVERLAY = REPO / "translation/reviews/codex-luna-20260910/catalog_translation_candidate_wp2.sqlite"

# Same order the overlay builder uses, so "who won" matches the shipped table.
CHAIN = [
    "codex-luna-20260910",
    "codex-luna-navigation-20260910",
    "main-process-navigation-20260910",
    "main-process-materials-20260910",
    "main-process-repairs-20260910",
]


def chain_dirs() -> list[Path]:
    named = [d for d in CHAIN if (REPO / "translation/reviews" / d / "output").is_dir()]
    auto = sorted(
        d.name
        for d in (REPO / "translation/reviews").glob("*20260910")
        if (d / "output").is_dir() and d.name not in named
        and (d.name.startswith(("wp2-", "wp3-", "wp5-")))
    )
    return [REPO / "translation/reviews" / n / "output" for n in named + auto]


def load_overlay() -> dict[str, dict]:
    con = sqlite3.connect(f"file:{OVERLAY}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    for row in con.execute(
        "SELECT term_id, term_kind, status, reviewer, review_note, engine, updated_at "
        "FROM translation_terms"
    ):
        out[row["term_id"]] = dict(row)
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0, help="debug: only N terms")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    dirs = chain_dirs()
    overrides = load_review_overrides(dirs)
    overlay = load_overlay()
    ledger = json.loads((REPO / "translation/glossary/terminology-ledger.json").read_text(encoding="utf-8"))
    policy = json.loads((REPO / "translation/glossary/naming-policy.json").read_text(encoding="utf-8"))

    manifest = [json.loads(l) for l in (RUN / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    drafts = {
        json.loads(l)["term_id"]: json.loads(l)
        for l in (RUN / "ai_draft.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()
    }

    records = []
    stats = Counter()
    seen: dict[str, dict] = {}
    for m in manifest:
        tid = m["term_id"]
        draft = str((drafts.get(tid) or {}).get("translated_text") or "")
        eng = effective_text(overrides, tid, draft)
        ov = overrides.get(tid) or {}
        meta = overlay.get(tid) or {}
        if not meta:
            stats["missing_in_overlay"] += 1
        if not eng:
            stats["empty_effective_text"] += 1
        if eng != draft:
            stats["differs_from_draft"] += 1
        rec = {
            "term_id": tid,
            "term_kind": m.get("term_kind"),
            "source_text": str(m.get("source_text") or ""),
            "english": eng,
            "draft": draft,
            "changed": eng != draft,
            "status": meta.get("status") or "",
            "reviewer": ov.get("reviewer") or meta.get("reviewer") or "",
            "override_decision": ov.get("decision") or "",
            "engine": meta.get("engine") or "",
            "source_snapshot_fingerprint": m.get("source_snapshot_fingerprint") or "",
            "updated_at": meta.get("updated_at") or "",
        }
        stats[f"kind:{m.get('term_kind')}"] += 1
        stats[f"status:{meta.get('status') or '<none>'}"] += 1
        if tid in seen:
            # The manifest repeats a handful of term_ids (41,207 rows / 41,189 unique
            # terms — a documented property of the release). Emit each term ONCE so
            # the deliverable is keyed by term, and report the repeats explicitly
            # rather than silently duplicating or silently dropping them.
            prev = seen[tid]
            if prev["english"] != eng or prev["source_text"] != rec["source_text"]:
                stats["duplicate_term_id_conflicting"] += 1
            else:
                stats["duplicate_term_id_identical"] += 1
            continue
        seen[tid] = rec
        records.append(rec)
    stats["manifest_rows"] = len(manifest)
    stats["unique_terms"] = len(records)
    # `differs_from_draft` above counts MANIFEST rows; the deliverable is keyed by
    # term, so also report the unique-term figure. They differ by the repeated ids.
    stats["changed_terms"] = sum(1 for r in records if r["changed"])
    for key in ("duplicate_term_id_conflicting", "duplicate_term_id_identical"):
        stats.setdefault(key, 0)
    for key in ("missing_in_overlay", "empty_effective_text"):
        stats.setdefault(key, 0)
    if args.limit:
        records = records[: args.limit]
        stats["limited_to"] = len(records)

    jl = args.out / "catalog-translation-consolidated.jsonl"
    jl.write_text(
        "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )

    cs = args.out / "catalog-translation-consolidated.csv"
    with cs.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["term_id", "term_kind", "source_text", "english", "status", "reviewer", "changed"])
        for r in records:
            w.writerow([r["term_id"], r["term_kind"], r["source_text"], r["english"],
                        r["status"], r["reviewer"], "1" if r["changed"] else "0"])

    # Review gaps: rows whose ONLY coverage is the deterministic layer.
    by_status = Counter(r["status"] for r in records)
    needs_human = [r for r in records if r["status"] == "needs_human"]
    gaps = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "chain_dirs": [str(d.relative_to(REPO)) for d in dirs],
        "needs_human": [{"term_id": r["term_id"], "source_text": r["source_text"],
                         "english": r["english"], "term_kind": r["term_kind"]} for r in needs_human],
        "needs_human_count": len(needs_human),
        "status_counts": dict(by_status),
        "registered_residues": {
            k: v.get("rows") for k, v in (policy.get("registered_residues") or {}).items()
        },
        "registered_exceptions": {
            "identifier_contains_chinese": [
                {"term_id": r["term_id"], "source_text": r["source_text"], "english": r["english"]}
                for r in records if "SMT_PCBA-SJJC" in r["source_text"]
            ]
        },
        "not_done": {
            "WP4_stratified_sampling": "1,500 rows — separate slice, needs user sign-off",
            "runtime_wiring": "overlay is NOT wired into the runtime; published stays 0",
            "WP7_naming_decision": "identifier-internal `2层` (keep vs 2-Layer) awaits a naming decision",
        },
    }
    gp = args.out / "catalog-translation-review-gaps.json"
    gp.write_text(json.dumps(gaps, ensure_ascii=False, indent=1), encoding="utf-8")

    published = sum(1 for r in overlay.values() if r.get("status") == "published")
    summary = {
        "schema": "limeauto.catalog-translation-consolidated.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "release_no": "rayah-n7-final-epc-remote-20260830-e7thumb-idxfx-20260901",
        "terms": len(records),
        "counts": dict(stats),
        "status_counts": dict(by_status),
        "rule_counts": {"text_rules": len(policy.get("text_rules") or []),
                        "registered_residues": len(policy.get("registered_residues") or {})},
        "ledger": {"chunks": ledger.get("chunk_count"), "finishes": ledger.get("finish_count")},
        "published": published,
        "chain_dirs": [str(d.relative_to(REPO)) for d in dirs],
        "outputs": {
            "jsonl": {"path": jl.name, "sha256": hashlib.sha256(jl.read_bytes()).hexdigest()},
            "csv": {"path": cs.name, "sha256": hashlib.sha256(cs.read_bytes()).hexdigest()},
            "gaps": {"path": gp.name, "sha256": hashlib.sha256(gp.read_bytes()).hexdigest()},
        },
    }
    (args.out / "catalog-translation-consolidated.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    assert published == 0, f"published must stay 0, got {published}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
