#!/usr/bin/env python3
"""Validate a draft SQLite catalog release without modifying it.

The validator is intentionally independent of FastAPI and qpren.  It opens the
release artifact read-only, emits a JSON evidence report, and never changes the
release status or a current-release pointer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

ALLOWED_STATUSES = {"draft", "failed", "validated", "published", "retired"}
ALLOWED_ASSET_TYPES = {"material_image", "epc_drawing", "thumbnail", "other"}
ALLOWED_ASSET_STATUSES = {"pending", "ready", "done", "available", "failed", "missing"}
PUBLISHABLE_ASSET_STATUSES = {"ready", "done", "available"}
MIME_TO_MAGIC = {
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ASSET_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._~+%\-]*$")
OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(value: Any, field: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError) as exc:
        errors.append({"kind": "invalid_json_metadata", "field": field, "message": str(exc)})
        return {}
    if not isinstance(decoded, dict):
        errors.append({"kind": "metadata_not_object", "field": field})
        return {}
    return decoded


def _check_parent_graph(
    nodes: Iterable[sqlite3.Row],
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, str, int]]:
    """Stream nodes while retaining only graph and fitment context maps."""
    path_variant_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    parent_by_path: dict[tuple[str, str, str], str | None] = {}
    node_by_key: dict[tuple[str, str], tuple[str, str, int]] = {}
    for node in nodes:
        scope = (node["series_code"], node["model_code"], node["path_key"])
        path_variant_counts[scope] += 1
        parent = node["parent_key"]
        if scope not in parent_by_path:
            parent_by_path[scope] = parent
        elif parent_by_path[scope] != parent:
            errors.append({"kind": "parent_path_conflict", "scope": list(scope)})

        node_key = (node["release_id"], node["node_key"])
        node_by_key[node_key] = (
            node["series_code"],
            node["model_code"],
            int(node["direct_part_count"] or 0),
        )
        if node["is_derived"] and node["source_obj_code"] is not None:
            errors.append({"kind": "derived_source_obj_code", "node_key": node["node_key"]})
        if not node["is_derived"] and not str(node["source_obj_code"] or "").strip():
            errors.append({"kind": "source_obj_code_missing", "node_key": node["node_key"]})
        if int(node["path_variant_count"] or 0) < 0:
            errors.append({"kind": "negative_path_variant_count", "node_key": node["node_key"]})
        if int(node["direct_part_count"] or 0) < 0 or int(node["descendant_part_count"] or 0) < 0:
            errors.append({"kind": "negative_node_count", "node_key": node["node_key"]})

    for scope, parent in parent_by_path.items():
        if parent is None:
            continue
        parent_scope = (scope[0], scope[1], parent)
        if parent_scope not in path_variant_counts:
            errors.append(
                {
                    "kind": "missing_parent_path",
                    "series_code": scope[0],
                    "model_code": scope[1],
                    "path_key": scope[2],
                    "parent_key": parent,
                }
            )

    for start in parent_by_path:
        seen: set[tuple[str, str, str]] = set()
        current: tuple[str, str, str] | None = start
        while current is not None:
            if current in seen:
                errors.append({"kind": "parent_cycle", "scope": list(current)})
                break
            seen.add(current)
            parent = parent_by_path.get(current)
            current = (current[0], current[1], parent) if parent is not None else None

    for scope, variant_count in path_variant_counts.items():
        if variant_count > 1:
            warnings.append(
                {
                    "kind": "path_variant_group",
                    "series_code": scope[0],
                    "model_code": scope[1],
                    "path_key": scope[2],
                    "variant_count": variant_count,
                }
            )
    return node_by_key


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _asset_ref(asset: sqlite3.Row) -> dict[str, Any]:
    return {"asset_key": asset["asset_key"]}


def _asset_magic(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(4096)
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"BM"):
        return "bmp"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
        return "avif"
    text = header.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n").lower()
    if text.startswith("<?xml") or text.startswith("<svg"):
        return "svg"
    return "unknown"


def _asset_file_check(
    *,
    root: Path,
    object_key: str,
    expected_size: int | None,
    expected_sha256: str | None,
    expected_mime: str | None,
) -> dict[str, Any]:
    """Check one object key without ever resolving a path outside ``root``."""
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
        return result

    try:
        if not candidate.is_file():
            result["state"] = "fail"
            result["problems"].append("asset_file_missing")
            return result
        actual_size = candidate.stat().st_size
    except OSError:
        result["state"] = "fail"
        result["problems"].append("asset_file_unreadable")
        return result

    result["actual_size_bytes"] = actual_size
    if actual_size == 0:
        result["problems"].append("asset_file_empty")
    if expected_size is not None:
        result["size_checked"] = True
        if actual_size != expected_size:
            result["problems"].append("asset_size_mismatch")

    if expected_sha256 is not None:
        try:
            actual_sha256 = artifact_sha256(candidate)
        except OSError:
            result["problems"].append("asset_file_unreadable")
        else:
            result["sha256_checked"] = True
            if actual_sha256.lower() != expected_sha256.lower():
                result["problems"].append("asset_sha256_mismatch")

    if expected_mime:
        expected_magic = MIME_TO_MAGIC.get(str(expected_mime).strip().lower())
        if expected_magic is None:
            result["problems"].append("asset_mime_unknown")
        else:
            result["mime_checked"] = True
            if _asset_magic(candidate) != expected_magic:
                result["problems"].append("asset_mime_mismatch")

    result["state"] = "pass" if not result["problems"] else "fail"
    return result


def validate_release(
    path: Path,
    release_no: str | None = None,
    asset_root: Path | None = None,
    integrity_mode: str = "full",
    asset_check_limit: int | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable validation report for one SQLite artifact."""
    if integrity_mode not in {"full", "quick", "skip"}:
        raise ValueError("integrity_mode must be 'full', 'quick', or 'skip'")
    if asset_check_limit is not None and asset_check_limit < 0:
        raise ValueError("asset_check_limit must be non-negative or None")
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    mmap_size_raw = os.getenv("LIMEAUTO_SQLITE_MMAP_SIZE", "0").strip()
    if mmap_size_raw:
        try:
            mmap_size = int(mmap_size_raw)
        except ValueError as exc:
            connection.close()
            raise ValueError("LIMEAUTO_SQLITE_MMAP_SIZE must be an integer") from exc
        if mmap_size > 0:
            connection.execute(f"PRAGMA mmap_size={mmap_size}")
    cache_size_raw = os.getenv("LIMEAUTO_SQLITE_CACHE_SIZE_KIB", "0").strip()
    if cache_size_raw:
        try:
            cache_size_kib = int(cache_size_raw)
        except ValueError as exc:
            connection.close()
            raise ValueError("LIMEAUTO_SQLITE_CACHE_SIZE_KIB must be an integer") from exc
        if cache_size_kib > 0:
            connection.execute(f"PRAGMA cache_size=-{cache_size_kib}")
    try:
        if integrity_mode == "skip":
            integrity = "skipped"
        else:
            integrity_pragma = "integrity_check" if integrity_mode == "full" else "quick_check"
            integrity = connection.execute(f"PRAGMA {integrity_pragma}").fetchone()[0]
            if integrity != "ok":
                errors.append({"kind": "sqlite_integrity", "message": str(integrity)})
        foreign_row_count = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        if foreign_row_count:
            errors.append({"kind": "foreign_key_check", "count": foreign_row_count})

        release = next(connection.execute("SELECT * FROM catalog_releases"), None)
        release_row_count = int(
            connection.execute("SELECT COUNT(*) FROM catalog_releases").fetchone()[0]
        )
        if release_row_count != 1:
            errors.append({"kind": "release_row_count", "count": release_row_count})
        if release is None:
            release_data: dict[str, Any] = {}
        else:
            release_data = dict(release)
            if release["status"] not in ALLOWED_STATUSES:
                errors.append({"kind": "invalid_release_status", "status": release["status"]})
            for field in ("release_id", "release_no", "source_snapshot_fingerprint"):
                if not str(release[field] or "").strip():
                    errors.append({"kind": "missing_release_metadata", "field": field})
            if release_no is not None and release["release_no"] != release_no:
                errors.append(
                    {
                        "kind": "release_no_mismatch",
                        "expected": release_no,
                        "actual": release["release_no"],
                    }
                )
            _json_object(release["source_counts_json"], "source_counts_json", errors)
            _json_object(release["validation_summary_json"], "validation_summary_json", errors)

        table_names = (
            "release_models",
            "system_nodes",
            "catalog_parts",
            "fitments",
            "catalog_assets",
        )
        for table in ("catalog_releases", *table_names):
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        node_by_key = _check_parent_graph(
            connection.execute("SELECT * FROM system_nodes ORDER BY release_id,node_key"),
            errors,
            warnings,
        )

        fitment_counts: dict[tuple[str, str], int] = defaultdict(int)
        for fitment in connection.execute("SELECT * FROM fitments"):
            if not str(fitment["source_occurrence_key"] or "").strip():
                errors.append({"kind": "empty_source_occurrence_key"})
            key = (fitment["release_id"], fitment["node_key"])
            fitment_counts[key] += 1
            node = node_by_key.get(key)
            if node is None:
                errors.append({"kind": "fitment_node_missing", "node_key": fitment["node_key"]})
            elif (
                fitment["series_code"] != node[0]
                or fitment["model_code"] != node[1]
            ):
                errors.append(
                    {
                        "kind": "fitment_model_context_mismatch",
                        "source_occurrence_key": fitment["source_occurrence_key"],
                    }
                )
            if fitment["quantity"] is not None and fitment["quantity_raw"] is None:
                errors.append(
                    {
                        "kind": "quantity_raw_missing",
                        "source_occurrence_key": fitment["source_occurrence_key"],
                    }
                )

        for (release_id, node_key), (_, _, expected) in node_by_key.items():
            actual = fitment_counts.get((release_id, node_key), 0)
            if actual != expected:
                errors.append(
                    {
                        "kind": "direct_part_count_mismatch",
                        "node_key": node_key,
                        "expected": expected,
                        "actual": actual,
                    }
                )

        for part in connection.execute("SELECT * FROM catalog_parts"):
            if not str(part["material_code"] or "").strip():
                errors.append({"kind": "empty_material_code"})

        asset_checks: list[dict[str, Any]] = []
        asset_summary = {
            "total": 0,
            "pass": 0,
            "fail": 0,
            "not_checked": 0,
            "root_provided": asset_root is not None,
        }
        retained_pass_count = 0
        resolved_asset_root = asset_root.resolve() if asset_root is not None else None
        if asset_root is None:
            warnings.append({"kind": "asset_root_not_provided"})
            if release_data.get("status") in {"validated", "published"}:
                errors.append({"kind": "asset_root_required", "status": release_data.get("status")})
        for asset in connection.execute("SELECT * FROM catalog_assets"):
            asset_summary["total"] += 1
            asset_key = str(asset["asset_key"] or "")
            object_key = str(asset["object_key"] or "")
            asset_ref = {"asset_key": asset_key, "object_key": object_key}
            if not asset_key.strip():
                errors.append({"kind": "empty_asset_field", "field": "asset_key", **asset_ref})
            object_parts = object_key.split("/")
            parsed_object_key = urlsplit(object_key)
            unsafe_object_key = (
                not object_key.strip()
                or object_key.startswith(("/", "\\"))
                or object_key.startswith("//")
                or bool(parsed_object_key.scheme)
                or "\\" in object_key
                or ".." in object_parts
                or (len(object_key) >= 2 and object_key[1] == ":")
                or not OBJECT_KEY_RE.fullmatch(object_key)
            )
            if unsafe_object_key:
                errors.append({"kind": "unsafe_asset_object_key", **asset_ref})
            if not str(asset["asset_type"] or "").strip():
                errors.append({"kind": "empty_asset_field", "field": "asset_type", **asset_ref})
            if asset["asset_type"] not in ALLOWED_ASSET_TYPES:
                errors.append({"kind": "invalid_asset_type", "asset_key": asset["asset_key"]})
            if not str(asset["status"] or "").strip():
                errors.append({"kind": "empty_asset_field", "field": "status", **asset_ref})
            if asset["status"] not in ALLOWED_ASSET_STATUSES:
                errors.append({"kind": "invalid_asset_status", "asset_key": asset["asset_key"]})
            elif release_data.get("status") in {"validated", "published"} and asset["status"] not in PUBLISHABLE_ASSET_STATUSES:
                errors.append(
                    {
                        "kind": "asset_not_publishable",
                        "asset_key": asset["asset_key"],
                        "status": asset["status"],
                    }
                )
            if asset["source_sha256"] and not SHA256_RE.fullmatch(str(asset["source_sha256"])):
                errors.append({"kind": "invalid_asset_sha256", "asset_key": asset["asset_key"]})
            if asset["size_bytes"] is not None and int(asset["size_bytes"]) < 0:
                errors.append({"kind": "negative_asset_size", "asset_key": asset["asset_key"]})
            material_bound = bool(str(asset["material_code"] or "").strip())
            node_bound = bool(str(asset["system_node_key"] or "").strip())
            if material_bound == node_bound:
                errors.append({"kind": "asset_binding_count", "binding_count": int(material_bound) + int(node_bound), **asset_ref})
            check = {**asset_ref, "state": "not_checked", "problems": []}
            if asset_root is not None and not unsafe_object_key and object_key.strip():
                check = {**asset_ref, **_asset_file_check(
                    root=resolved_asset_root,
                    object_key=object_key,
                    expected_size=_integer(asset["size_bytes"]),
                    expected_sha256=str(asset["source_sha256"]) if asset["source_sha256"] else None,
                    expected_mime=str(asset["mime_type"]) if asset["mime_type"] else None,
                )}
                for problem in check["problems"]:
                    errors.append({"kind": problem, **asset_ref})
            asset_summary[check["state"]] = asset_summary.get(check["state"], 0) + 1
            retain_check = (
                asset_check_limit is None
                or check["state"] == "fail"
                or (check["state"] == "pass" and retained_pass_count < asset_check_limit)
            )
            if retain_check:
                asset_checks.append(check)
                if check["state"] == "pass":
                    retained_pass_count += 1

        asset_summary.update(
            {
                "evidence_sample_limit": asset_check_limit,
                "evidence_retained_count": len(asset_checks),
                "evidence_retention": (
                    "all"
                    if asset_check_limit is None
                    else "all_failures_and_first_passes"
                ),
            }
        )

        return {
            "tool": "validate_catalog_release",
            "artifact": str(path),
            "artifact_sha256": artifact_sha256(path),
            "release": release_data,
            "counts": counts,
            "checks": {
                "sqlite_integrity": integrity,
                "sqlite_integrity_mode": integrity_mode,
                "foreign_key_rows": foreign_row_count,
                "parent_graph": "checked",
                "fitment_context": "checked",
                "asset_keys": "checked",
                "assets": asset_summary,
            },
            "asset_checks": asset_checks,
            "warnings": warnings,
            "blocking_errors": errors,
            "status": "pass" if not errors else "fail",
            "read_only": True,
            "published": bool(release_data.get("status") == "published"),
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--release-no")
    parser.add_argument(
        "--asset-root",
        type=Path,
        help="Root directory containing catalog_assets.object_key files",
    )
    parser.add_argument(
        "--integrity-mode",
        choices=("full", "quick", "skip"),
        default="full",
        help="SQLite integrity scan; skip leaves page integrity unverified and is only a structural validation mode",
    )
    parser.add_argument(
        "--asset-check-limit",
        type=int,
        default=128,
        help="Retain at most this many passing asset checks in the report; failures are always retained (0 disables passing evidence)",
    )
    args = parser.parse_args(argv)
    try:
        report = validate_release(
            args.database,
            args.release_no,
            args.asset_root,
            args.integrity_mode,
            args.asset_check_limit,
        )
        atomic_write_json(args.output, report)
    except Exception as exc:
        print(f"validate_catalog_release failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "artifact": report["artifact"],
                "report": str(args.output),
                "blocking_error_count": len(report["blocking_errors"]),
                "warning_count": len(report["warnings"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
