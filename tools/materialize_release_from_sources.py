#!/usr/bin/env python3
"""Materialize a release asset root from local qpren source indexes.

The release database is read-only.  The qpren TSV index is a previously
captured read-only query result with columns ``kind, local_path, sha256,
size_bytes``.  Material assets may come from a validated historical material
root; EPC assets come from the indexed qpren image paths.  Output files are
hard links, not byte copies, and the root is committed atomically only after
all referenced rows pass size, SHA-256, MIME, magic, and path checks.
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
from collections import defaultdict
from pathlib import Path
from typing import Any

QPREN_PREFIX = Path("/var/lib/qpren")
OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)
MIME_TO_MAGIC = {
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
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def magic(path: Path) -> str:
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


def load_qpren_index(path: Path) -> dict[tuple[str, str], list[Path]]:
    result: dict[tuple[str, str], list[Path]] = defaultdict(list)
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 4:
                continue
            kind, local_path, digest, _size = fields
            if kind not in {"epc_svg", "epc_thumbnail"}:
                continue
            if not re.fullmatch(r"[0-9a-fA-F]{64}", digest or ""):
                continue
            try:
                source = Path(local_path)
                source.relative_to(QPREN_PREFIX)
            except ValueError:
                continue
            if source.is_file() and not source.is_symlink():
                result[(kind, digest.lower())].append(source)
    for candidates in result.values():
        candidates.sort(key=str)
    return result


def load_material_index(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        digest = path.stem.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        previous = result.get(digest)
        if previous is not None and not os.path.samefile(previous, path):
            raise ValueError(f"duplicate material hash with different files: {digest}")
        result[digest] = path
    return result


def safe_object_key(value: Any) -> str:
    if not isinstance(value, str) or not OBJECT_KEY_RE.fullmatch(value):
        raise ValueError(f"unsafe object key: {value!r}")
    if ".." in value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe object key: {value!r}")
    return value


def source_candidates(
    asset_type: str, digest: str, material_index: dict[str, Path], qpren_index: dict[tuple[str, str], list[Path]]
) -> list[Path]:
    if asset_type == "material_image":
        path = material_index.get(digest)
        return [path] if path is not None else []
    kind = {"epc_drawing": "epc_svg", "thumbnail": "epc_thumbnail"}.get(asset_type)
    return list(qpren_index.get((kind, digest), [])) if kind else []


def materialize(
    database: Path,
    qpren_index_path: Path,
    material_root: Path,
    output_root: Path,
    report_path: Path,
) -> dict[str, Any]:
    database = database.expanduser().resolve(strict=True)
    qpren_index_path = qpren_index_path.expanduser().resolve(strict=True)
    material_root = material_root.expanduser().resolve(strict=True)
    output_root = output_root.expanduser().resolve(strict=False)
    report_path = report_path.expanduser().resolve(strict=False)
    if output_root.exists():
        raise ValueError(f"output root already exists: {output_root}")
    if output_root == material_root or output_root.is_relative_to(material_root):
        raise ValueError("output root must be separate from material root")

    qpren_index = load_qpren_index(qpren_index_path)
    material_index = load_material_index(material_root)
    connection = sqlite3.connect(
        f"file:{database}?mode=ro&immutable=1", uri=True, timeout=120
    )
    connection.row_factory = sqlite3.Row
    try:
        releases = connection.execute("SELECT * FROM catalog_releases").fetchall()
        if len(releases) != 1:
            raise ValueError(f"expected one release row, got {len(releases)}")
        assets = connection.execute(
            """
            SELECT asset_key, asset_type, object_key, source_sha256,
                   size_bytes, mime_type, status
            FROM catalog_assets ORDER BY asset_type, asset_key
            """
        ).fetchall()
    finally:
        connection.close()

    temporary = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.{uuid.uuid4().hex}.", dir=str(output_root.parent)))
    source_cache: dict[Path, tuple[int, str, str]] = {}
    destination_sources: dict[Path, Path] = {}
    counts = {"rows": 0, "linked": 0, "material_image": 0, "epc_drawing": 0, "thumbnail": 0}
    normalized_status = defaultdict(int)
    try:
        for row in assets:
            counts["rows"] += 1
            asset_type = str(row["asset_type"])
            digest = str(row["source_sha256"] or "").lower()
            expected_size = int(row["size_bytes"]) if row["size_bytes"] is not None else -1
            expected_mime = str(row["mime_type"] or "").strip().lower()
            candidates = source_candidates(asset_type, digest, material_index, qpren_index)
            source = None
            failure = "source_not_found"
            for candidate in candidates:
                try:
                    if not candidate.is_file() or candidate.is_symlink():
                        failure = "source_not_regular"
                        continue
                    actual_size = candidate.stat().st_size
                    if expected_size < 1 or actual_size != expected_size:
                        failure = "size_mismatch"
                        continue
                    cached = source_cache.get(candidate)
                    if cached is None:
                        actual_sha = sha256(candidate)
                        actual_magic = magic(candidate)
                        cached = (actual_size, actual_sha, actual_magic)
                        source_cache[candidate] = cached
                    cached_size, actual_sha, actual_magic = cached
                    if cached_size != expected_size or actual_sha != digest:
                        failure = "sha256_or_size_mismatch"
                        continue
                    if MIME_TO_MAGIC.get(expected_mime) != actual_magic:
                        failure = f"mime:{actual_magic}!={expected_mime}"
                        continue
                    source = candidate
                    break
                except (OSError, RuntimeError, ValueError) as exc:
                    failure = str(exc)
            if source is None:
                raise ValueError(f"{row['asset_key']}: {failure}")
            object_key = safe_object_key(row["object_key"])
            destination = (temporary / object_key).resolve(strict=False)
            destination.relative_to(temporary)
            previous = destination_sources.get(destination)
            if previous is not None:
                if not os.path.samefile(previous, source):
                    raise ValueError(f"destination collision: {object_key}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, destination)
                destination_sources[destination] = source
                counts["linked"] += 1
            counts[asset_type] = counts.get(asset_type, 0) + 1
            normalized_status[expected_mime] += 1
            if counts["rows"] % 50000 == 0:
                print(json.dumps({"progress": counts["rows"], "linked": counts["linked"]}), flush=True)

        for directory in temporary.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        output_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, output_root)
        temporary = None
        physical = [path for path in output_root.rglob("*") if path.is_file()]
        result = {
            "status": "pass",
            "database": str(database),
            "asset_root": str(output_root),
            "rows": len(assets),
            "linked": len(destination_sources),
            "physical_files": len(physical),
            "symlink_files": sum(1 for path in output_root.rglob("*") if path.is_symlink()),
            "source_files_hashed": len(source_cache),
            "mime_counts": dict(sorted(normalized_status.items())),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return result
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--qpren-index", required=True, type=Path)
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = materialize(args.database, args.qpren_index, args.material_root, args.output_root, args.report)
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
        print(f"materialize_release_from_sources failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
