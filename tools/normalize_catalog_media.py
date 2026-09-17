#!/usr/bin/env python3
"""Normalize catalog media MIME declarations and filename extensions.

The input release database and asset root are read-only.  A new SQLite artifact
and a new hard-link asset root are produced, so a draft can be repaired without
mutating the source candidate.  Every referenced file is checked for regular
file type, non-zero size, declared size, SHA-256, and binary magic before it is
linked into the output tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)
MIME_EXTENSION = {
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_mime(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(4096)
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    text = header.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n").lower()
    if text.startswith("<?xml") or text.startswith("<svg"):
        return "image/svg+xml"
    return "unknown"


def safe_key(value: Any) -> str:
    if not isinstance(value, str) or not OBJECT_KEY_RE.fullmatch(value):
        raise ValueError(f"unsafe object key: {value!r}")
    if ".." in value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe object key: {value!r}")
    return value


def copy_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve()}?mode=ro&immutable=1"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=120)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection, pages=4096, sleep=0.01)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def normalize(
    database: Path,
    asset_root: Path,
    output_database: Path,
    output_asset_root: Path,
    report_path: Path,
) -> dict[str, Any]:
    database = database.expanduser().resolve(strict=True)
    asset_root = asset_root.expanduser().resolve(strict=True)
    output_database = output_database.expanduser().resolve(strict=False)
    output_asset_root = output_asset_root.expanduser().resolve(strict=False)
    report_path = report_path.expanduser().resolve(strict=False)
    if output_database == database or output_asset_root == asset_root:
        raise ValueError("output must be separate from input")
    if output_database.exists() or output_asset_root.exists():
        raise ValueError("output database or asset root already exists")
    output_database.parent.mkdir(parents=True, exist_ok=True)
    output_asset_root.parent.mkdir(parents=True, exist_ok=True)

    uri = f"file:{database}?mode=ro&immutable=1"
    source_connection = sqlite3.connect(uri, uri=True, timeout=120)
    source_connection.row_factory = sqlite3.Row
    rows = source_connection.execute(
        """
        SELECT release_id, asset_key, asset_type, object_key,
               source_sha256, size_bytes, mime_type, status
        FROM catalog_assets ORDER BY asset_key, object_key
        """
    ).fetchall()
    releases = source_connection.execute("SELECT * FROM catalog_releases").fetchall()
    if len(releases) != 1:
        source_connection.close()
        raise ValueError(f"expected one release row, got {len(releases)}")
    release = dict(releases[0])
    source_connection.close()

    temporary_database = output_database.with_name(f".{output_database.name}.{uuid.uuid4().hex}.tmp")
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{output_asset_root.name}.{uuid.uuid4().hex}.", dir=str(output_asset_root.parent)))
    source_cache: dict[Path, tuple[int, str, str]] = {}
    destination_sources: dict[Path, Path] = {}
    updates: list[dict[str, Any]] = []
    counts = {"rows": 0, "linked": 0, "material_image": 0, "epc_drawing": 0, "thumbnail": 0}
    normalized_count = 0
    try:
        copy_sqlite(database, temporary_database)
        for row in rows:
            counts["rows"] += 1
            old_key = safe_key(row["object_key"])
            source = (asset_root / old_key).resolve(strict=False)
            try:
                source.relative_to(asset_root)
            except ValueError as exc:
                raise ValueError(f"asset path escapes root: {old_key}") from exc
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"asset is not a regular file: {source}")
            expected_size = row["size_bytes"]
            if expected_size is None or isinstance(expected_size, bool) or int(expected_size) < 1:
                raise ValueError(f"invalid asset size: {row['asset_key']}")
            expected_size = int(expected_size)
            cached = source_cache.get(source)
            if cached is None:
                actual_size = source.stat().st_size
                actual_sha = sha256(source)
                actual_mime = detect_mime(source)
                cached = (actual_size, actual_sha, actual_mime)
                source_cache[source] = cached
            actual_size, actual_sha, actual_mime = cached
            if actual_size != expected_size:
                raise ValueError(f"size mismatch for {source}: {actual_size} != {expected_size}")
            expected_sha = str(row["source_sha256"] or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or actual_sha != expected_sha:
                raise ValueError(f"SHA-256 mismatch for {source}")
            if actual_mime not in MIME_EXTENSION:
                raise ValueError(f"unsupported media magic for {source}")
            new_key = str(Path(old_key).with_suffix(MIME_EXTENSION[actual_mime]))
            new_mime = actual_mime
            if new_key != old_key or str(row["mime_type"] or "").lower() != new_mime:
                normalized_count += 1
                updates.append({
                    "release_id": str(row["release_id"]),
                    "asset_key": str(row["asset_key"]),
                    "old_object_key": old_key,
                    "new_object_key": new_key,
                    "old_mime_type": row["mime_type"],
                    "new_mime_type": new_mime,
                    "source_sha256": expected_sha,
                })
            destination = (temporary_root / new_key).resolve(strict=False)
            destination.relative_to(temporary_root)
            previous = destination_sources.get(destination)
            if previous is not None:
                if previous != source and not os.path.samefile(previous, source):
                    raise ValueError(f"destination collision: {new_key}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, destination)
            destination_sources[destination] = source
            counts["linked"] += 1
            counts[str(row["asset_type"])] = counts.get(str(row["asset_type"]), 0) + 1
            if counts["rows"] % 50000 == 0:
                print(json.dumps({"progress": counts["rows"], "linked": counts["linked"], "normalized": normalized_count}), flush=True)

        connection = sqlite3.connect(temporary_database)
        try:
            connection.execute("BEGIN")
            for update in updates:
                cursor = connection.execute(
                    """
                    UPDATE catalog_assets
                    SET object_key = ?, mime_type = ?
                    WHERE release_id = ? AND asset_key = ? AND object_key = ?
                    """,
                    (
                        update["new_object_key"], update["new_mime_type"],
                        update["release_id"], update["asset_key"], update["old_object_key"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"asset row update failed: {update['asset_key']}")
            summary = json.loads(release.get("validation_summary_json") or "{}")
            if not isinstance(summary, dict):
                summary = {}
            summary["media_normalization"] = {
                "rows_checked": counts["rows"],
                "rows_normalized": normalized_count,
                "source_files_hashed": len(source_cache),
                "generated_by": "normalize_catalog_media",
            }
            connection.execute(
                "UPDATE catalog_releases SET validation_summary_json = ?, notes = ? WHERE release_id = ?",
                (
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    "Draft revision with media MIME/extension normalization; not published.",
                    release["release_id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        for directory in temporary_root.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        os.replace(temporary_database, output_database)
        os.replace(temporary_root, output_asset_root)
        temporary_database = None
        temporary_root = None
        result = {
            "status": "pass",
            "source_database": str(database),
            "output_database": str(output_database),
            "source_asset_root": str(asset_root),
            "output_asset_root": str(output_asset_root),
            "release_no": release["release_no"],
            "counts": counts,
            "normalized_rows": normalized_count,
            "source_files_hashed": len(source_cache),
            "physical_files": len(destination_sources),
            "symlink_files": 0,
            "updates": updates,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "output_database_bytes": output_database.stat().st_size,
            "output_database_sha256": sha256(output_database),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return result
    finally:
        if temporary_database is not None:
            temporary_database.unlink(missing_ok=True)
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--asset-root", required=True, type=Path)
    parser.add_argument("--output-database", required=True, type=Path)
    parser.add_argument("--output-asset-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = normalize(
            args.database, args.asset_root, args.output_database,
            args.output_asset_root, args.report,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"normalize_catalog_media failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": result["status"],
        "release_no": result["release_no"],
        "normalized_rows": result["normalized_rows"],
        "physical_files": result["physical_files"],
        "output_database": result["output_database"],
        "output_asset_root": result["output_asset_root"],
        "report": str(args.report),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
