#!/usr/bin/env python3
"""Validate one bounded rowid shard of a catalog asset tree.

The release database and asset tree are opened read-only.  Each invocation is
independent, so completed shard reports can be retained and a later run can
resume from the next rowid range without repeating earlier work.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Support both ``python -m``/pytest and the documented direct CLI form.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.validate_catalog_release import (
    ALLOWED_ASSET_STATUSES,
    ALLOWED_ASSET_TYPES,
    MIME_TO_MAGIC,
    _asset_magic,
    artifact_sha256 as file_sha256,
    _integer,
)

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)
IMAGE_TYPES = {"material_image", "epc_drawing", "thumbnail"}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _unsafe_object_key(value: Any) -> bool:
    object_key = str(value or "")
    parts = object_key.split("/")
    return not (
        object_key.strip()
        and not object_key.startswith(("/", "\\"))
        and not object_key.startswith("//")
        and "\\" not in object_key
        and ".." not in parts
        and not (len(object_key) >= 2 and object_key[1] == ":")
        and OBJECT_KEY_RE.fullmatch(object_key)
    )


def _field_problems(row: sqlite3.Row) -> list[str]:
    problems: list[str] = []
    asset_key = str(row["asset_key"] or "")
    object_key = str(row["object_key"] or "")
    asset_type = str(row["asset_type"] or "")
    status = str(row["status"] or "")
    if not asset_key.strip():
        problems.append("empty_asset_key")
    if _unsafe_object_key(object_key):
        problems.append("unsafe_asset_object_key")
    if asset_type not in ALLOWED_ASSET_TYPES:
        problems.append("invalid_asset_type")
    if status not in ALLOWED_ASSET_STATUSES:
        problems.append("invalid_asset_status")
    source_sha = row["source_sha256"]
    if source_sha and not SHA256_RE.fullmatch(str(source_sha)):
        problems.append("invalid_asset_sha256")
    if row["size_bytes"] is not None:
        try:
            if isinstance(row["size_bytes"], bool) or int(row["size_bytes"]) < 0:
                problems.append("invalid_asset_size")
        except (TypeError, ValueError, OverflowError):
            problems.append("invalid_asset_size")
    material_bound = bool(str(row["material_code"] or "").strip())
    node_bound = bool(str(row["system_node_key"] or "").strip())
    if material_bound == node_bound:
        problems.append("asset_binding_count")
    expected_magic = MIME_TO_MAGIC.get(str(row["mime_type"] or "").strip().lower())
    if asset_type in IMAGE_TYPES and status in {"ready", "done", "available"}:
        if not expected_magic or not source_sha or row["size_bytes"] is None:
            problems.append("publishable_fields")
    return problems


def _check_asset_file(
    *,
    root: Path,
    object_key: str,
    expected_size: int | None,
    expected_sha256: str | None,
    expected_mime: str | None,
    hashed: dict[tuple[int, int], str],
) -> tuple[dict[str, Any], tuple[int, int] | None, bool]:
    """Check one path and reuse hashes for hard-linked files in this shard."""
    result: dict[str, Any] = {
        "state": "not_checked",
        "problems": [],
        "size_checked": False,
        "sha256_checked": False,
        "mime_checked": False,
    }
    try:
        candidate = (root / object_key).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        result["state"] = "fail"
        result["problems"].append("asset_path_escape")
        return result, None, False
    try:
        if not candidate.is_file():
            result["state"] = "fail"
            result["problems"].append("asset_file_missing")
            return result, None, False
        stat = candidate.stat()
    except OSError:
        result["state"] = "fail"
        result["problems"].append("asset_file_unreadable")
        return result, None, False

    inode = (stat.st_dev, stat.st_ino)
    result["actual_size_bytes"] = stat.st_size
    if stat.st_size == 0:
        result["problems"].append("asset_file_empty")
    if expected_size is not None:
        result["size_checked"] = True
        if stat.st_size != expected_size:
            result["problems"].append("asset_size_mismatch")

    actual_sha256 = hashed.get(inode)
    hash_reused = actual_sha256 is not None
    if actual_sha256 is None:
        try:
            actual_sha256 = file_sha256(candidate)
        except OSError:
            result["problems"].append("asset_file_unreadable")
        else:
            hashed[inode] = actual_sha256
    if actual_sha256 is not None:
        result["sha256_checked"] = True
        if expected_sha256 is not None and actual_sha256.lower() != expected_sha256.lower():
            result["problems"].append("asset_sha256_mismatch")

    if expected_mime:
        expected_magic = MIME_TO_MAGIC.get(expected_mime.strip().lower())
        if expected_magic is None:
            result["problems"].append("asset_mime_unknown")
        else:
            result["mime_checked"] = True
            try:
                actual_magic = _asset_magic(candidate)
            except OSError:
                result["problems"].append("asset_file_unreadable")
            else:
                if actual_magic != expected_magic:
                    result["problems"].append("asset_mime_mismatch")
    result["state"] = "pass" if not result["problems"] else "fail"
    return result, inode, hash_reused


def _database_identity(path: Path, artifact_sha256: str | None) -> dict[str, Any]:
    stat = path.stat()
    return {
        "database": str(path.resolve()),
        "database_bytes": stat.st_size,
        "database_mtime_ns": stat.st_mtime_ns,
        "artifact_sha256": artifact_sha256,
    }


def validate_shard(
    database: Path,
    asset_root: Path,
    rowid_start: int,
    rowid_end: int,
    *,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate rows in ``[rowid_start, rowid_end)`` without mutating inputs."""
    if rowid_start < 1 or rowid_end <= rowid_start:
        raise ValueError("rowid range must satisfy 1 <= start < end")
    database = database.expanduser().resolve(strict=True)
    root = asset_root.expanduser().resolve(strict=False)
    started = time.monotonic()
    uri = f"file:{database}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    summary: dict[str, Any] = {
        "rows": 0,
        "pass": 0,
        "fail": 0,
        "paths_checked": 0,
        "files_checked": 0,
        "inode_reused": 0,
        "missing": 0,
        "empty": 0,
        "size_mismatch": 0,
        "hash_mismatch": 0,
        "mime_mismatch": 0,
        "unsafe": 0,
        "invalid_binding": 0,
        "invalid_fields": 0,
        "bad_sample": [],
    }
    errors: list[dict[str, Any]] = []
    hashed: dict[tuple[int, int], str] = {}
    try:
        total_rows = int(connection.execute("SELECT COUNT(*) FROM catalog_assets").fetchone()[0])
        min_rowid, max_rowid = connection.execute(
            "SELECT MIN(rowid), MAX(rowid) FROM catalog_assets"
        ).fetchone()
        if min_rowid is None or max_rowid is None:
            raise ValueError("catalog_assets is empty")
        identity = _database_identity(database, artifact_sha256)
        query = """
            SELECT rowid AS asset_rowid, asset_key, material_code, system_node_key,
                   asset_type, object_key, source_sha256, size_bytes, status, mime_type
            FROM catalog_assets
            WHERE rowid >= ? AND rowid < ?
            ORDER BY rowid
        """
        for row in connection.execute(query, (rowid_start, rowid_end)):
            summary["rows"] += 1
            problems = _field_problems(row)
            if "unsafe_asset_object_key" in problems:
                summary["unsafe"] += 1
            if "asset_binding_count" in problems:
                summary["invalid_binding"] += 1
            if any(
                item
                in problems
                for item in (
                    "empty_asset_key",
                    "invalid_asset_type",
                    "invalid_asset_status",
                    "invalid_asset_sha256",
                    "invalid_asset_size",
                    "publishable_fields",
                )
            ):
                summary["invalid_fields"] += 1

            check = {
                "state": "not_checked",
                "problems": [],
            }
            if not _unsafe_object_key(row["object_key"]):
                check, inode, hash_reused = _check_asset_file(
                    root=root,
                    object_key=str(row["object_key"]),
                    expected_size=_integer(row["size_bytes"]),
                    expected_sha256=(
                        str(row["source_sha256"]) if row["source_sha256"] else None
                    ),
                    expected_mime=(str(row["mime_type"]) if row["mime_type"] else None),
                    hashed=hashed,
                )
                summary["paths_checked"] += 1
                file_problem_map = {
                    "asset_file_missing": "missing",
                    "asset_file_empty": "empty",
                    "asset_size_mismatch": "size_mismatch",
                    "asset_sha256_mismatch": "hash_mismatch",
                    "asset_mime_mismatch": "mime_mismatch",
                    "asset_file_unreadable": "missing",
                    "asset_path_escape": "unsafe",
                }
                for item in check["problems"]:
                    if item in file_problem_map:
                        summary[file_problem_map[item]] += 1
                if inode is not None:
                    if check.get("sha256_checked") and hash_reused:
                        summary["inode_reused"] += 1
                    elif check.get("sha256_checked"):
                        summary["files_checked"] += 1
            else:
                check = {"state": "fail", "problems": ["asset_path_escape"]}

            problems.extend(str(item) for item in check.get("problems", []))
            if problems:
                summary["fail"] += 1
                error = {
                    "rowid": row["asset_rowid"],
                    "asset_key": row["asset_key"],
                    "problems": sorted(set(problems)),
                }
                if len(errors) < 100:
                    errors.append(error)
                if len(summary["bad_sample"]) < 20:
                    summary["bad_sample"].append(error)
            else:
                summary["pass"] += 1
        if summary["rows"] == 0:
            errors.append(
                {
                    "rowid_start": rowid_start,
                    "rowid_end": rowid_end,
                    "problems": ["no_rows_in_range"],
                }
            )
    finally:
        connection.close()

    return {
        "tool": "validate_catalog_assets_shard",
        "schema_version": "limeauto-catalog-assets-shard.v1",
        **identity,
        "asset_root": str(root),
        "rowid_start": rowid_start,
        "rowid_end": rowid_end,
        "database_min_rowid": int(min_rowid),
        "database_max_rowid": int(max_rowid),
        "database_total_rows": total_rows,
        "asset_summary": summary,
        "blocking_errors": errors,
        "blocking_error_count": summary["fail"] + int(summary["rows"] == 0),
        "status": "pass" if not errors and summary["rows"] == summary["pass"] else "fail",
        "read_only": True,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--asset-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rowid-start", required=True, type=int)
    parser.add_argument("--rowid-end", required=True, type=int, help="exclusive")
    parser.add_argument("--artifact-sha256")
    args = parser.parse_args(argv)
    try:
        report = validate_shard(
            args.database,
            args.asset_root,
            args.rowid_start,
            args.rowid_end,
            artifact_sha256=args.artifact_sha256,
        )
        atomic_write_json(args.output, report)
    except Exception as exc:
        print(f"validate_catalog_assets_shard failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.output),
                "rowid_start": report["rowid_start"],
                "rowid_end": report["rowid_end"],
                "rows": report["asset_summary"]["rows"],
                "blocking_error_count": len(report["blocking_errors"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
