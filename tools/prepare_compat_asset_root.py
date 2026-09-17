#!/usr/bin/env python3
"""Prepare a compatibility asset root from validated material hashes.

This is an offline build/deploy helper. It reads qpren in a PostgreSQL
read-only transaction and reads the old release in SQLite read-only mode. It
does not change either source, the old release, or any current pointer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - requires the optional build dependency
    psycopg = None
    dict_row = None

QPREN_PREFIX = Path("/var/lib/qpren")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def magic(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(64)
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return "unknown"


def source_path(source_root: Path, local_path: Any) -> Path | None:
    if not isinstance(local_path, str) or "\x00" in local_path:
        return None
    try:
        relative = Path(local_path).relative_to(QPREN_PREFIX)
        candidate = (source_root / relative).resolve(strict=False)
        candidate.relative_to(source_root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate


def read_source_hashes(dsn: str, source_root: Path) -> dict[str, Path]:
    if psycopg is None or dict_row is None:
        raise RuntimeError("psycopg is required for this offline helper")
    candidates: dict[str, list[Path]] = {}
    with psycopg.connect(
        dsn, connect_timeout=20, row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    ) as connection:
        connection.autocommit = False
        try:
            evidence = connection.execute(
                "SELECT current_setting('transaction_read_only') AS tx, "
                "current_setting('default_transaction_read_only') AS default_tx"
            ).fetchone()
            if evidence["tx"] != "on" or evidence["default_tx"] != "on":
                raise RuntimeError("qpren connection is not explicitly read-only")
            connection.execute("BEGIN READ ONLY")
            rows = connection.execute(
                """
                SELECT local_path, sha256, size_bytes
                FROM images
                WHERE kind = 'material' AND sha256 IS NOT NULL
                ORDER BY sha256, local_path
                """
            ).fetchall()
            for row in rows:
                digest = str(row["sha256"] or "").strip().lower()
                path = source_path(source_root, row["local_path"])
                if not SHA256_RE.fullmatch(digest) or path is None:
                    continue
                try:
                    stat = path.stat()
                    expected_size = row["size_bytes"]
                    valid_size = expected_size is None or int(expected_size) == stat.st_size
                    valid_file = path.is_file() and stat.st_size > 0
                    valid_magic = magic(path) != "unknown"
                    valid_hash = valid_file and sha256(path) == digest
                except (OSError, TypeError, ValueError, OverflowError):
                    continue
                if valid_size and valid_file and valid_magic and valid_hash:
                    candidates.setdefault(digest, []).append(path)
        finally:
            connection.rollback()
    return {digest: sorted(paths, key=str)[0] for digest, paths in candidates.items()}


def read_release_assets(database: Path) -> list[dict[str, Any]]:
    if not database.is_file():
        raise RuntimeError(f"old release is missing: {database}")
    connection = sqlite3.connect(
        f"file:{database.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("old release quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("old release foreign_key_check failed")
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT asset_key, object_key, source_sha256, size_bytes, mime_type
                FROM catalog_assets
                WHERE asset_type = 'material_image'
                ORDER BY asset_key, object_key
                """
            )
        ]
    finally:
        connection.close()


def prepare(
    database: Path, source_root: Path, output_root: Path, dsn: str
) -> dict[str, Any]:
    source_root = source_root.expanduser().resolve(strict=False)
    output_root = output_root.expanduser().resolve(strict=False)
    if output_root == source_root or output_root.is_relative_to(source_root):
        raise RuntimeError("output root must be separate from source root")
    if output_root.exists():
        raise RuntimeError(f"output root already exists: {output_root}")
    assets = read_release_assets(database)
    source_by_hash = read_source_hashes(dsn, source_root)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=str(output_root.parent)))
    linked = 0
    missing: list[dict[str, Any]] = []
    try:
        for asset in assets:
            digest = str(asset.get("source_sha256") or "").lower()
            if not SHA256_RE.fullmatch(digest):
                missing.append({"asset_key": asset.get("asset_key"), "reason": "invalid_hash"})
                continue
            source = source_by_hash.get(digest)
            object_key = asset.get("object_key")
            if source is None:
                missing.append({"asset_key": asset.get("asset_key"), "reason": "source_missing"})
                continue
            if not isinstance(object_key, str) or not OBJECT_KEY_RE.fullmatch(object_key) or ".." in object_key:
                missing.append({"asset_key": asset.get("asset_key"), "reason": "unsafe_object_key"})
                continue
            try:
                destination = (temporary / object_key).resolve(strict=False)
                destination.relative_to(temporary)
                expected_size = asset.get("size_bytes")
                if expected_size is not None and int(expected_size) != source.stat().st_size:
                    raise ValueError("size_mismatch")
                expected_mime = str(asset.get("mime_type") or "").lower()
                actual_mime = magic(source)
                if expected_mime and expected_mime != actual_mime:
                    raise ValueError("mime_mismatch")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise ValueError("destination_collision")
                os.link(source, destination)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                missing.append({"asset_key": asset.get("asset_key"), "reason": str(exc)})
                continue
            linked += 1
        if missing:
            raise RuntimeError(json.dumps({"missing": len(missing), "sample": missing[:10]}))
        for directory in temporary.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        os.replace(temporary, output_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "old_asset_rows": len(assets),
        "qpren_material_hash_rows": len(source_by_hash),
        "linked": linked,
        "missing": 0,
        "root": str(output_root),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-db", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.old_db, args.source_root, args.output_root, args.dsn)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
