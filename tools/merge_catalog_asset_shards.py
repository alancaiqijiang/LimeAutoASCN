#!/usr/bin/env python3
"""Merge independent catalog asset-shard reports.

The merger reads JSON reports only.  It never opens or changes a release
artifact or asset tree.  A merged report is complete only when shard ranges
cover the database rowid span without overlap and their row counts equal the
catalog_assets total.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "limeauto-catalog-assets-shard-merge.v1"
SHARD_SCHEMA_VERSION = "limeauto-catalog-assets-shard.v1"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temporary = Path(temporary_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n", closefd=True) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SHARD_SCHEMA_VERSION:
        raise ValueError(f"invalid shard report schema: {path}")
    summary = payload.get("asset_summary")
    if not isinstance(summary, dict):
        raise ValueError(f"missing asset summary: {path}")
    for key in ("rowid_start", "rowid_end", "database_total_rows", "database_min_rowid", "database_max_rowid"):
        if not isinstance(payload.get(key), int):
            raise ValueError(f"missing integer {key}: {path}")
    if not isinstance(summary.get("rows"), int):
        raise ValueError(f"missing integer asset_summary.rows: {path}")
    return payload


def _identity(report: dict[str, Any]) -> tuple[Any, ...]:
    return (
        report.get("database"),
        report.get("database_bytes"),
        report.get("database_mtime_ns"),
        report.get("artifact_sha256"),
        report.get("asset_root"),
        report.get("database_total_rows"),
        report.get("database_min_rowid"),
        report.get("database_max_rowid"),
    )


def merge_reports(paths: Iterable[Path], *, require_complete: bool = False) -> dict[str, Any]:
    paths = list(paths)
    reports = [_load(path) for path in paths]
    if not reports:
        raise ValueError("at least one shard report is required")
    identities = {_identity(report) for report in reports}
    if len(identities) != 1:
        raise ValueError("shard reports do not share one database/asset identity")
    reports.sort(key=lambda report: (report["rowid_start"], report["rowid_end"]))
    errors: list[dict[str, Any]] = []
    previous_end: int | None = None
    for report in reports:
        start = report["rowid_start"]
        end = report["rowid_end"]
        if start < 1 or end <= start:
            errors.append({"kind": "invalid_range", "start": start, "end": end})
        if previous_end is not None:
            if start < previous_end:
                errors.append({"kind": "overlap", "previous_end": previous_end, "start": start})
            elif start > previous_end:
                errors.append({"kind": "gap", "previous_end": previous_end, "start": start})
        previous_end = end if previous_end is None else max(previous_end, end)

    first = reports[0]
    last = reports[-1]
    database_min = first["database_min_rowid"]
    database_max = first["database_max_rowid"]
    database_total = first["database_total_rows"]
    covered_start = first["rowid_start"]
    covered_end = last["rowid_end"]
    row_count = sum(report["asset_summary"]["rows"] for report in reports)
    complete = (
        not errors
        and covered_start == database_min
        and covered_end == database_max + 1
        and row_count == database_total
    )
    if covered_start != database_min:
        errors.append({"kind": "incomplete_start", "expected": database_min, "actual": covered_start})
    if covered_end != database_max + 1:
        errors.append({"kind": "incomplete_end", "expected": database_max + 1, "actual": covered_end})
    if row_count != database_total:
        errors.append({"kind": "row_count_mismatch", "expected": database_total, "actual": row_count})
    if require_complete and not complete:
        errors.append({"kind": "complete_coverage_required"})

    totals = Counter()
    for report in reports:
        for key, value in report["asset_summary"].items():
            if isinstance(value, int):
                totals[key] += value
    blocking_errors = sum((report.get("blocking_errors") or [] for report in reports), [])
    blocking_error_count = sum(
        int(report.get("blocking_error_count", len(report.get("blocking_errors") or [])))
        for report in reports
    )
    problem_counts = Counter(
        problem
        for error in blocking_errors
        for problem in error.get("problems", [error.get("kind", "unknown")])
    )
    return {
        "tool": "merge_catalog_asset_shards",
        "schema_version": SCHEMA_VERSION,
        "shard_count": len(reports),
        "reports": [str(path) for path in paths],
        "database": first["database"],
        "database_bytes": first["database_bytes"],
        "database_mtime_ns": first["database_mtime_ns"],
        "artifact_sha256": first.get("artifact_sha256"),
        "asset_root": first["asset_root"],
        "database_min_rowid": database_min,
        "database_max_rowid": database_max,
        "database_total_rows": database_total,
        "covered_rowid_start": covered_start,
        "covered_rowid_end": covered_end,
        "coverage_complete": complete,
        "asset_summary": dict(totals),
        "asset_hash_dedup_scope": "within_shard_only",
        "asset_hash_note": (
            "files_checked and inode_reused are summed per shard; they are not "
            "a global unique-physical-file count across shards"
        ),
        "blocking_error_count": blocking_error_count,
        "blocking_error_kinds": dict(problem_counts),
        "range_errors": errors,
        "status": "pass" if complete and not blocking_errors and not errors else "fail",
        "read_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = merge_reports(args.reports, require_complete=args.require_complete)
        atomic_write_json(args.output, result)
    except Exception as exc:
        print(f"merge_catalog_asset_shards failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "report": str(args.output),
                "shard_count": result["shard_count"],
                "coverage_complete": result["coverage_complete"],
                "range_error_count": len(result["range_errors"]),
                "blocking_error_count": result["blocking_error_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
