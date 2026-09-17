#!/usr/bin/env python3
"""Stream-validate one immutable release artifact and its asset tree.

Usage: stream_validate_epc.py RELEASE_DB ASSET_ROOT REPORT LOG

This is an offline build/deploy check. It never writes to the release database
or to the asset tree. Paths, file sizes, SHA-256 values, MIME values, and file
magic are checked before a report is emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ASSET_TYPES = frozenset({"material_image", "epc_drawing", "thumbnail", "other"})
ASSET_STATUSES = frozenset(
    {"pending", "ready", "done", "available", "failed", "missing"}
)
IMAGE_MIME_TO_MAGIC = {
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def magic(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(4096)
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"BM"):
        return "bmp"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {
        b"avif",
        b"avis",
    }:
        return "avif"
    text = header.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n")
    if text.startswith("<?xml") or text.startswith("<svg"):
        return "svg"
    return "unknown"


def safe_asset_path(root: Path, object_key: Any) -> Path | None:
    if not isinstance(object_key, str) or not OBJECT_KEY_RE.fullmatch(object_key):
        return None
    if "\x00" in object_key or "\\" in object_key or ".." in object_key:
        return None
    try:
        candidate = (root / object_key).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def validate(database: Path, asset_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    root = asset_root.expanduser().resolve(strict=False)
    report: dict[str, Any] = {
        "status": "fail",
        "database": str(database),
        "database_bytes": database.stat().st_size,
        "quick_check": None,
        "foreign_key_errors": 0,
        "release": None,
        "counts": {},
        "asset_summary": {
            "rows": 0,
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
        },
    }
    summary = report["asset_summary"]
    hashed: dict[tuple[int, int], str] = {}
    uri = f"file:{database.resolve()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        report["quick_check"] = connection.execute("PRAGMA quick_check").fetchone()[0]
        report["foreign_key_errors"] = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        releases = connection.execute("SELECT * FROM catalog_releases").fetchall()
        report["release"] = dict(releases[0]) if len(releases) == 1 else None
        for table in (
            "release_models",
            "system_nodes",
            "catalog_parts",
            "fitments",
            "catalog_assets",
        ):
            report["counts"][table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        for row in connection.execute(
            """
            SELECT asset_key, material_code, system_node_key, asset_type,
                   object_key, source_sha256, size_bytes, status, mime_type
            FROM catalog_assets ORDER BY asset_key, object_key
            """
        ):
            summary["rows"] += 1
            problems: list[str] = []
            asset_type = str(row["asset_type"] or "")
            asset_status = str(row["status"] or "")
            if asset_type not in ASSET_TYPES or asset_status not in ASSET_STATUSES:
                summary["invalid_fields"] += 1
                problems.append("enum")
            if bool(row["material_code"]) == bool(row["system_node_key"]):
                summary["invalid_binding"] += 1
                problems.append("binding")
            source_sha = row["source_sha256"]
            if source_sha is not None and not SHA256_RE.fullmatch(str(source_sha)):
                summary["invalid_fields"] += 1
                problems.append("sha_field")
            mime_type = str(row["mime_type"] or "").strip().lower()
            expected_magic = IMAGE_MIME_TO_MAGIC.get(mime_type)
            if asset_type in {"material_image", "epc_drawing", "thumbnail"} and (
                asset_status in {"ready", "done", "available"}
                and (not expected_magic or source_sha is None or row["size_bytes"] is None)
            ):
                summary["invalid_fields"] += 1
                problems.append("publishable_fields")
            path = safe_asset_path(root, row["object_key"])
            if path is None:
                summary["unsafe"] += 1
                problems.append("unsafe_path")
            else:
                summary["paths_checked"] += 1
                try:
                    if not path.is_file():
                        summary["missing"] += 1
                        problems.append("missing")
                    else:
                        stat = path.stat()
                        if stat.st_size <= 0:
                            summary["empty"] += 1
                            problems.append("empty")
                        expected_size = row["size_bytes"]
                        if expected_size is not None:
                            try:
                                if isinstance(expected_size, bool) or int(expected_size) != stat.st_size:
                                    summary["size_mismatch"] += 1
                                    problems.append("size")
                            except (TypeError, ValueError, OverflowError):
                                summary["invalid_fields"] += 1
                                problems.append("size_field")
                        inode = (stat.st_dev, stat.st_ino)
                        actual_sha = hashed.get(inode)
                        if actual_sha is None:
                            actual_sha = sha256(path)
                            hashed[inode] = actual_sha
                            summary["files_checked"] += 1
                        else:
                            summary["inode_reused"] += 1
                        if source_sha is not None and actual_sha.lower() != str(source_sha).lower():
                            summary["hash_mismatch"] += 1
                            problems.append("hash")
                        actual_magic = magic(path)
                        if expected_magic and actual_magic != expected_magic:
                            summary["mime_mismatch"] += 1
                            problems.append("mime")
                except (OSError, RuntimeError, ValueError):
                    summary["missing"] += 1
                    problems.append("unreadable")
            if problems and len(summary["bad_sample"]) < 20:
                summary["bad_sample"].append(
                    {"asset_key": row["asset_key"], "problems": problems}
                )
    finally:
        connection.close()
    report["status"] = (
        "pass"
        if report["quick_check"] == "ok"
        and report["foreign_key_errors"] == 0
        and report["release"] is not None
        and all(
            summary[key] == 0
            for key in (
                "unsafe",
                "invalid_binding",
                "invalid_fields",
                "missing",
                "empty",
                "size_mismatch",
                "hash_mismatch",
                "mime_mismatch",
            )
        )
        else "fail"
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("asset_root", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    result = validate(args.database, args.asset_root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"status": result["status"], "asset_summary": result["asset_summary"]},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
