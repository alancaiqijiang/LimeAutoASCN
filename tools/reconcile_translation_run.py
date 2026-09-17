#!/usr/bin/env python3
"""Reconcile stale metadata for a completed, immutable translation run."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temp = Path(name)
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    run = args.run_dir
    manifest = read_jsonl(run / "manifest.jsonl")
    drafts = read_jsonl(run / "ai_draft.jsonl")
    usage = read_jsonl(run / "usage.jsonl")
    state_path = run / "state.json"
    report_path = run / "qa_report.json"
    state = read_json(state_path)
    report = read_json(report_path)
    unique_ids = {str(row.get("term_id")) for row in manifest}
    draft_ids = {str(row.get("term_id")) for row in drafts}
    batch_paths = sorted((run / "batches").glob("batch-*.json"))

    if draft_ids != unique_ids:
        raise SystemExit(
            f"draft coverage mismatch unique_manifest={len(unique_ids)} draft={len(draft_ids)}"
        )
    if len(batch_paths) != int(state.get("batch_count") or 0):
        raise SystemExit("batch artifact count does not match recorded batch_count")
    if len(usage) != len(batch_paths) or any(
        row.get("status") != "ok" for row in usage
    ):
        raise SystemExit("usage artifacts are incomplete or contain non-ok batches")

    observed_at = now_iso()
    legacy = {
        "run_status": state.get("run_status"),
        "pause_reason": state.get("pause_reason"),
        "paused_at": state.get("paused_at"),
        "paused_after_batch": state.get("paused_after_batch"),
        "remaining_rows": state.get("remaining_rows"),
        "remaining_batches": state.get("remaining_batches"),
        "completed_raw_rows": state.get("completed_raw_rows"),
        "checkpoint_rows": state.get("checkpoint_rows"),
    }
    state.update(
        {
            "run_status": "initial_draft_complete",
            "completion_stage": "ai_draft_complete",
            "semantic_review_status": "pending",
            "processed_batches": len(batch_paths),
            "completed_rows": len(draft_ids),
            "completed_raw_rows": len(manifest),
            "remaining_rows": 0,
            "remaining_batches": 0,
            "state_reconciled_at": observed_at,
            "legacy_checkpoint": legacy,
            "updated_at": observed_at,
        }
    )
    report.update(
        {
            "run_status": "initial_draft_complete",
            "completion_stage": "ai_draft_complete",
            "manifest_count": len(manifest),
            "manifest_unique_count": len(unique_ids),
            "manifest_duplicate_entries": len(manifest) - len(unique_ids),
            "translated_rows_written": len(draft_ids),
            "missing_rows": 0,
            "missing_unique_rows": 0,
            "semantic_review_required": len(draft_ids),
            "state_reconciled_at": observed_at,
            "legacy_checkpoint": legacy,
            "updated_at": observed_at,
        }
    )
    atomic_json(state_path, state)
    atomic_json(report_path, report)

    meta = read_json(run / "manifest_meta.json")
    evidence = {
        "schema": "limeauto.translation-reconciliation.v2",
        "observed_at": observed_at,
        "run_id": run.name,
        "release_no": meta["release_no"],
        "source_snapshot_fingerprint": meta["source_fingerprint"],
        "manifest": {
            "raw_rows": len(manifest),
            "unique_term_ids": len(unique_ids),
            "duplicate_entries": len(manifest) - len(unique_ids),
            "sha256": sha256(run / "manifest.jsonl"),
        },
        "batch_artifacts": {
            "batch_files": len(batch_paths),
            "batch_range": f"1-{len(batch_paths)}",
            "usage_batches": len(usage),
            "all_usage_status_ok": True,
        },
        "coverage": {
            "ai_draft_unique_term_ids": len(draft_ids),
            "missing_unique_term_ids": len(unique_ids - draft_ids),
            "extra_unique_term_ids": len(draft_ids - unique_ids),
            "coverage_percent": 100.0,
            "status_counts": report.get("status_counts", {}),
            "semantic_review_required": len(draft_ids),
        },
        "metadata_reconciliation": {
            "current_run_status": "initial_draft_complete",
            "legacy_checkpoint": legacy,
            "api_continuation_required": False,
        },
        "artifacts": {
            name: sha256(run / name)
            for name in [
                "manifest.jsonl",
                "manifest_meta.json",
                "ai_draft.jsonl",
                "review_queue.jsonl",
                "usage.jsonl",
                "state.json",
                "qa_report.json",
            ]
        },
        "runtime": {
            "translation_overlay_sqlite_exists": False,
            "catalog_runtime_translation_integration": True,
            "production_publishable": False,
        },
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.evidence, evidence)
    print(
        json.dumps(
            {
                "run_status": state["run_status"],
                "batches": len(batch_paths),
                "unique_terms": len(unique_ids),
                "missing_unique_terms": 0,
                "evidence": str(args.evidence),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
