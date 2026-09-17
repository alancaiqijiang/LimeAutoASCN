#!/usr/bin/env python3
"""Create a new draft catalog revision from the validated material backfills.

This is an additive overlay builder.  It never writes qpren PostgreSQL, never
changes the previous release, and never promotes a release.  Existing release
objects are hard-linked into a new immutable asset root; newly validated
material files are hard-linked from the qpren source tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

OLD_DB = Path(os.getenv("LIMEAUTO_MATERIAL_OLD_DB", "artifacts/releases/limeauto-base.sqlite"))
OLD_ASSET_ROOT = Path(os.getenv("LIMEAUTO_MATERIAL_OLD_ASSET_ROOT", "artifacts/catalog-assets-base"))
SOURCE_ROOT = Path(os.getenv("LIMEAUTO_MATERIAL_SOURCE_ROOT", "artifacts/qpren-source/var/lib/qpren"))
KNOWN_MANIFEST = Path(os.getenv("LIMEAUTO_MATERIAL_KNOWN_MANIFEST", "reports/material-image-backfill-known.json"))
NEW_MANIFEST = Path(os.getenv("LIMEAUTO_MATERIAL_NEW_MANIFEST", "reports/material-image-backfill-new.json"))
RAW_DIR = Path(os.getenv("LIMEAUTO_MATERIAL_RAW_DIR", "artifacts/raw-material"))
OUTPUT_DB = Path(os.getenv("LIMEAUTO_MATERIAL_OUTPUT_DB", "artifacts/releases/limeauto-material-backfill.sqlite"))
OUTPUT_ASSET_ROOT = Path(os.getenv("LIMEAUTO_MATERIAL_OUTPUT_ASSET_ROOT", "artifacts/catalog-assets-material-backfill"))
OUTPUT_REPORT = Path(os.getenv("LIMEAUTO_MATERIAL_OUTPUT_REPORT", "reports/limeauto-material-backfill.json"))
RELEASE_NO = os.getenv("LIMEAUTO_MATERIAL_RELEASE_NO", "limeauto-material-backfill-20260830")

SUCCESS_STATUSES = {"downloaded", "exists"}
EXCLUDED_STATUSES = {"empty_source", "invalid_local"}
MIME_BY_MAGIC = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "bmp": "image/bmp",
}


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temporary = Path(name)
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


def copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            dst.write(chunk)
            digest.update(chunk)
            total += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    shutil.copystat(source, destination)
    return total, digest.hexdigest()


def under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def source_path(path_value: str) -> Path:
    candidate = Path(path_value).expanduser().resolve(strict=False)
    root = SOURCE_ROOT.resolve(strict=False)
    if not under(root, candidate):
        raise BuildError(f"source path escapes qpren root: {path_value}")
    return candidate


def magic(path: Path) -> str:
    head = path.read_bytes()[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in {
        b"avif", b"avis"
    }:
        return "avif"
    return "unknown"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"invalid JSON input: {path}: {exc}") from exc


def load_raw_details() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    status = Counter()
    for path in sorted(RAW_DIR.glob("*.json")):
        code = path.stem
        payload = load_json(path)
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("code") != 200 or not isinstance(data, dict):
            status[f"code_{payload.get('code')}"] += 1
            raise BuildError(f"raw detail is not a successful object: {path}")
        returned_code = str(data.get("materialCode") or "").strip()
        if returned_code and returned_code != code:
            raise BuildError(f"raw material code mismatch: {path}")
        name = str(data.get("materialName") or "").strip()
        if not name:
            raise BuildError(f"raw material name is empty: {path}")
        file_list = payload.get("fileList")
        if not isinstance(file_list, list):
            file_list = []
        details[code] = {
            "material_name": name,
            "file_list_count": len(file_list),
            "raw_sha256": sha256_file(path),
        }
        hashes[code] = details[code]["raw_sha256"]
        status["available_with_image" if file_list else "available_empty"] += 1
    if len(details) != 3156:
        raise BuildError(f"expected 3156 raw detail files, found {len(details)}")
    return details, {
        "files": len(details),
        "status": dict(status),
        "raw_index_sha256": canonical_sha(sorted(hashes.items())),
        "image_file_list_rows": sum(v["file_list_count"] for v in details.values()),
    }


def load_image_candidates(path: Path, provenance: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = load_json(path)
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise BuildError(f"manifest results is not a list: {path}")
    status = Counter()
    accepted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise BuildError(f"manifest row is not an object: {path}")
        row_status = str(row.get("status") or "")
        status[row_status] += 1
        code = str(row.get("material_code") or "").strip()
        if not code:
            raise BuildError(f"manifest row has no material code: {path}")
        if row_status in EXCLUDED_STATUSES:
            excluded.append({"material_code": code, "status": row_status})
            continue
        if row_status not in SUCCESS_STATUSES:
            raise BuildError(f"unexpected manifest status {row_status!r} in {path}")
        path_value = row.get("path")
        if not path_value:
            raise BuildError(f"successful row has no local path: {path}")
        local = source_path(str(path_value))
        if not local.is_file():
            raise BuildError(f"successful row file is missing: {local}")
        actual_size = local.stat().st_size
        try:
            expected_size = int(row.get("bytes"))
        except (TypeError, ValueError) as exc:
            raise BuildError(f"invalid byte count for {code}: {path}") from exc
        expected_sha = str(row.get("sha256") or "").strip().lower()
        if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
            raise BuildError(f"invalid SHA-256 for {code}: {path}")
        if actual_size <= 0 or actual_size != expected_size:
            raise BuildError(f"size mismatch for {local}: {actual_size} != {expected_size}")
        actual_sha = sha256_file(local)
        if actual_sha != expected_sha:
            raise BuildError(f"hash mismatch for {local}: {actual_sha} != {expected_sha}")
        actual_magic = magic(local)
        if actual_magic not in MIME_BY_MAGIC:
            raise BuildError(f"unsupported or unknown image bytes: {local}")
        declared_magic = str(row.get("magic") or "").strip().lower()
        if declared_magic and declared_magic not in {"unknown", actual_magic}:
            raise BuildError(f"manifest magic mismatch for {local}: {declared_magic}")
        accepted.append(
            {
                "material_code": code,
                "sha256": expected_sha,
                "size_bytes": actual_size,
                "mime_type": MIME_BY_MAGIC[actual_magic],
                "path": str(local),
                "provenance": provenance,
            }
        )
    return accepted, {
        "path": str(path),
        "sha256": sha256_file(path),
        "rows": len(rows),
        "status": dict(status),
        "accepted_rows": len(accepted),
        "excluded_rows": excluded,
    }


def read_old_release() -> dict[str, Any]:
    if not OLD_DB.is_file():
        raise BuildError(f"old release is missing: {OLD_DB}")
    if not OLD_ASSET_ROOT.is_dir():
        raise BuildError(f"old asset root is missing: {OLD_ASSET_ROOT}")
    uri = f"file:{OLD_DB.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BuildError("old release quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BuildError("old release foreign_key_check failed")
        releases = connection.execute("SELECT * FROM catalog_releases").fetchall()
        if len(releases) != 1:
            raise BuildError("old release must contain exactly one release row")
        release = dict(releases[0])
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "release_models",
                "system_nodes",
                "catalog_parts",
                "fitments",
                "catalog_assets",
            )
        }
        asset_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT asset_key, material_code, system_node_key, asset_type,
                       object_key, source_sha256, size_bytes, mime_type, status
                FROM catalog_assets
                ORDER BY asset_key, object_key
                """
            )
        ]
        model_asset_rows: list[dict[str, Any]] = []
        has_model_assets = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='catalog_model_assets'
            """
        ).fetchone()
        if has_model_assets:
            model_asset_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM catalog_model_assets ORDER BY asset_key"
                )
            ]
        old_parts = {
            str(row["material_code"]): dict(row)
            for row in connection.execute("SELECT * FROM catalog_parts")
        }
        old_sha_materials: dict[str, set[str]] = defaultdict(set)
        for row in asset_rows:
            if row["source_sha256"]:
                old_sha_materials[str(row["source_sha256"]).lower()].add(
                    str(row["material_code"] or "")
                )
        return {
            "release": release,
            "counts": counts,
            "asset_rows": asset_rows,
            "model_asset_rows": model_asset_rows,
            "old_parts": old_parts,
            "old_sha_materials": old_sha_materials,
            "old_db_sha256": sha256_file(OLD_DB),
            "old_db_bytes": OLD_DB.stat().st_size,
        }
    finally:
        connection.close()


def collect_bindings(
    manifests: list[tuple[Path, str]],
    old_sha_materials: dict[str, set[str]],
    old_asset_keys: set[str],
    old_material_asset_codes: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    accepted_by_sha_material: dict[tuple[str, str], dict[str, Any]] = {}
    manifest_reports = []
    excluded = Counter()
    for path, provenance in manifests:
        accepted, report = load_image_candidates(path, provenance)
        manifest_reports.append(report)
        excluded.update(item["status"] for item in report["excluded_rows"])
        for row in accepted:
            key = (row["sha256"], row["material_code"])
            previous = accepted_by_sha_material.get(key)
            if previous is not None:
                if previous["size_bytes"] != row["size_bytes"] or previous["path"] != row["path"]:
                    raise BuildError(f"conflicting duplicate candidate: {key}")
                previous["provenance"] = ",".join(
                    sorted(set(previous["provenance"].split(",")) | {provenance})
                )
            else:
                accepted_by_sha_material[key] = row

    new_rows: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_existing_material_codes: set[str] = set()
    for (source_sha, material_code), row in sorted(accepted_by_sha_material.items()):
        if material_code in old_material_asset_codes:
            skipped_existing += 1
            skipped_existing_material_codes.add(material_code)
            continue
        existing_materials = old_sha_materials.get(source_sha, set())
        if material_code in existing_materials:
            skipped_existing += 1
            skipped_existing_material_codes.add(material_code)
            continue
        if source_sha not in old_sha_materials and len(
            [key for key in accepted_by_sha_material if key[0] == source_sha]
        ) == 1:
            identity = source_sha
            asset_key = f"material_image:{identity}"
            object_name = f"{identity}.jpg"
        else:
            material_component = quote(material_code, safe="-._~")
            identity = f"{source_sha}-material-{material_component}"
            asset_key = f"material_image:{identity}"
            object_name = f"{identity}.jpg"
        if asset_key in old_asset_keys or any(
            item["asset_key"] == asset_key for item in new_rows
        ):
            raise BuildError(f"asset key collision: {asset_key}")
        new_rows.append(
            {
                **row,
                "asset_key": asset_key,
                "object_key": (
                    f"release/{RELEASE_NO}/assets/material_image/{object_name}"
                ),
                "asset_type": "material_image",
                "status": "done",
            }
        )
    return new_rows, {
        "manifest_reports": manifest_reports,
        "accepted_unique_sha_material": len(accepted_by_sha_material),
        "accepted_unique_sha": len({key[0] for key in accepted_by_sha_material}),
        "skipped_already_bound": skipped_existing,
        "skipped_existing_material_codes": len(skipped_existing_material_codes),
        "new_asset_rows": len(new_rows),
        "excluded_statuses": dict(excluded),
    }


def rewrite_object_key(object_key: str, old_release_no: str) -> str:
    old_prefix = f"release/{old_release_no}/"
    if not object_key.startswith(old_prefix):
        raise BuildError(f"old object key has unexpected release prefix: {object_key}")
    return f"release/{RELEASE_NO}/" + object_key[len(old_prefix) :]


def prepare_database(
    old: dict[str, Any],
    details: dict[str, dict[str, Any]],
    new_asset_rows: list[dict[str, Any]],
    overlay_fingerprint: str,
    overlay_summary: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if OUTPUT_DB.exists():
        raise BuildError(f"output artifact already exists: {OUTPUT_DB}")
    OUTPUT_DB.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{OUTPUT_DB.name}.", suffix=".tmp", dir=str(OUTPUT_DB.parent)
    )
    os.close(fd)
    temporary = Path(name)
    try:
        copied_bytes, copied_sha = copy_and_hash(OLD_DB, temporary)
        if copied_sha != old["old_db_sha256"] or copied_bytes != old["old_db_bytes"]:
            raise BuildError("old release changed while copying")
        connection = sqlite3.connect(temporary)
        try:
            # This is a disposable copy.  Disable rollback journaling while
            # rewriting the release-scoped foreign-key keys; the original
            # release remains untouched and the copy is removed on failure.
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA foreign_keys=OFF")
            old_release = old["release"]
            old_id = str(old_release["release_id"])
            new_id = str(uuid.uuid4())
            base_counts = old["counts"]
            validation_summary = {
                "base_release_no": old_release["release_no"],
                "base_release_sha256": old["old_db_sha256"],
                "overlay_fingerprint": overlay_fingerprint,
                "overlay": overlay_summary,
                "release_counts_before_validation": base_counts,
            }
            connection.execute("BEGIN")
            for table in (
                "release_models",
                "system_nodes",
                "catalog_parts",
                "fitments",
                "catalog_assets",
            ):
                connection.execute(
                    f"UPDATE {table} SET release_id=? WHERE release_id=?",
                    (new_id, old_id),
                )
            has_model_assets = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='catalog_model_assets'
                """
            ).fetchone()
            if has_model_assets:
                connection.execute(
                    "UPDATE catalog_model_assets SET release_id=? WHERE release_id=?",
                    (new_id, old_id),
                )
            old_prefix = f"release/{old_release['release_no']}/"
            new_prefix = f"release/{RELEASE_NO}/"
            prefix_position = len(old_prefix) + 1
            connection.execute(
                """
                UPDATE catalog_assets
                SET object_key=? || substr(object_key, ?)
                WHERE release_id=? AND object_key LIKE ?
                """,
                (new_prefix, prefix_position, new_id, old_prefix + "%"),
            )
            if has_model_assets:
                connection.execute(
                    """
                    UPDATE catalog_model_assets
                    SET object_key=? || substr(object_key, ?)
                    WHERE release_id=? AND object_key LIKE ?
                    """,
                    (new_prefix, prefix_position, new_id, old_prefix + "%"),
                )
            connection.execute(
                """
                UPDATE catalog_releases
                SET release_id=?, release_no=?, source_snapshot=?,
                    source_snapshot_fingerprint=?, validation_summary_json=?,
                    status='draft', notes=?
                WHERE release_id=?
                """,
                (
                    new_id,
                    RELEASE_NO,
                    f"base={old_release['release_no']};overlay={overlay_fingerprint}",
                    overlay_fingerprint,
                    json.dumps(validation_summary, ensure_ascii=False, sort_keys=True),
                    "Draft revision from validated material backfills; not published.",
                    old_id,
                ),
            )
            updated_names = 0
            updated_status = 0
            update_rows = []
            for code, detail in sorted(details.items()):
                previous = old["old_parts"].get(code)
                if previous is None:
                    raise BuildError(f"raw detail material is absent from release: {code}")
                if str(previous.get("description") or "") != detail["material_name"]:
                    updated_names += 1
                if str(previous.get("source_detail_status") or "") != "available":
                    updated_status += 1
                update_rows.append(
                    (detail["material_name"], detail["material_name"], "available", new_id, code)
                )
            connection.executemany(
                """
                UPDATE catalog_parts
                SET display_name_source=?, description=?, source_detail_status=?
                WHERE release_id=? AND material_code=?
                """,
                update_rows,
            )
            asset_rows = []
            for row in new_asset_rows:
                asset_rows.append(
                    (
                        new_id,
                        row["asset_key"],
                        row["material_code"],
                        None,
                        row["asset_type"],
                        row["object_key"],
                        row["sha256"],
                        row["size_bytes"],
                        row["mime_type"],
                        row["status"],
                    )
                )
            connection.executemany(
                """
                INSERT INTO catalog_assets(
                    release_id, asset_key, material_code, system_node_key,
                    asset_type, object_key, source_sha256, size_bytes,
                    mime_type, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                asset_rows,
            )
            connection.commit()
            connection.execute("PRAGMA synchronous=FULL")
            journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise BuildError(f"failed to restore SQLite journal mode: {journal_mode}")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BuildError("foreign_key_check failed after overlay")
            final_counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE release_id=?", (new_id,)
                    ).fetchone()[0]
                )
                for table in (
                    "release_models",
                    "system_nodes",
                    "catalog_parts",
                    "fitments",
                    "catalog_assets",
                )
            }
            if final_counts["catalog_assets"] != old["counts"]["catalog_assets"] + len(
                new_asset_rows
            ):
                raise BuildError("final asset count is not base plus new rows")
            return temporary, {
                "release_id": new_id,
                "updated_part_names": updated_names,
                "updated_part_statuses": updated_status,
                "counts": final_counts,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def link_one(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise BuildError(f"asset source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise BuildError(f"asset destination already exists: {destination}")
    os.link(source, destination)


def prepare_asset_root(
    old: dict[str, Any], new_asset_rows: list[dict[str, Any]]
) -> tuple[Path, dict[str, Any]]:
    if OUTPUT_ASSET_ROOT.exists():
        raise BuildError(f"output asset root already exists: {OUTPUT_ASSET_ROOT}")
    OUTPUT_ASSET_ROOT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_ASSET_ROOT.with_name(
        f".{OUTPUT_ASSET_ROOT.name}.{uuid.uuid4().hex}.tmp"
    )
    temporary.mkdir(parents=True)
    old_count = 0
    for row in old["asset_rows"]:
        old_key = str(row["object_key"])
        new_key = rewrite_object_key(old_key, str(old["release"]["release_no"]))
        source = (OLD_ASSET_ROOT / old_key).resolve(strict=False)
        if not under(OLD_ASSET_ROOT.resolve(), source):
            raise BuildError(f"old asset escapes root: {old_key}")
        destination = temporary / new_key
        link_one(source, destination)
        old_count += 1
    model_count = 0
    for row in old["model_asset_rows"]:
        old_key = str(row["object_key"])
        new_key = rewrite_object_key(old_key, str(old["release"]["release_no"]))
        source = (OLD_ASSET_ROOT / old_key).resolve(strict=False)
        if not under(OLD_ASSET_ROOT.resolve(), source):
            raise BuildError(f"old model asset escapes root: {old_key}")
        link_one(source, temporary / new_key)
        model_count += 1
    new_count = 0
    for row in new_asset_rows:
        source = source_path(row["path"])
        link_one(source, temporary / row["object_key"])
        new_count += 1
    for directory in temporary.rglob("*"):
        if directory.is_dir():
            os.chmod(directory, 0o755)
    return temporary, {
        "old_catalog_assets_linked": old_count,
        "old_model_assets_linked": model_count,
        "new_material_assets_linked": new_count,
        "root": str(OUTPUT_ASSET_ROOT),
    }


def main(argv: list[str] | None = None) -> int:
    global OLD_DB, OLD_ASSET_ROOT, SOURCE_ROOT, KNOWN_MANIFEST, NEW_MANIFEST
    global RAW_DIR, OUTPUT_DB, OUTPUT_ASSET_ROOT, OUTPUT_REPORT, RELEASE_NO

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-db", type=Path, default=OLD_DB)
    parser.add_argument("--old-asset-root", type=Path, default=OLD_ASSET_ROOT)
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--known-manifest", type=Path, default=KNOWN_MANIFEST)
    parser.add_argument("--new-manifest", type=Path, default=NEW_MANIFEST)
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--output-db", type=Path, default=OUTPUT_DB)
    parser.add_argument("--output-asset-root", type=Path, default=OUTPUT_ASSET_ROOT)
    parser.add_argument("--output-report", type=Path, default=OUTPUT_REPORT)
    parser.add_argument("--release-no", default=RELEASE_NO)
    args = parser.parse_args(argv)
    OLD_DB = args.old_db
    OLD_ASSET_ROOT = args.old_asset_root
    SOURCE_ROOT = args.source_root
    KNOWN_MANIFEST = args.known_manifest
    NEW_MANIFEST = args.new_manifest
    RAW_DIR = args.raw_dir
    OUTPUT_DB = args.output_db
    OUTPUT_ASSET_ROOT = args.output_asset_root
    OUTPUT_REPORT = args.output_report
    RELEASE_NO = args.release_no

    if not all(path.exists() for path in (KNOWN_MANIFEST, NEW_MANIFEST, RAW_DIR)):
        raise BuildError("one or more backfill inputs are missing")
    old = read_old_release()
    details, raw_summary = load_raw_details()
    new_asset_rows, candidate_summary = collect_bindings(
        [(KNOWN_MANIFEST, "known_release_mapping_gap"), (NEW_MANIFEST, "missing_raw_backfill")],
        old["old_sha_materials"],
        {str(row["asset_key"]) for row in old["asset_rows"]},
        {
            str(row["material_code"])
            for row in old["asset_rows"]
            if row["asset_type"] == "material_image" and row["material_code"] is not None
        },
    )
    overlay_summary = {
        "raw_details": raw_summary,
        "candidate_images": candidate_summary,
        "base_release_counts": old["counts"],
        "known_manifest_sha256": sha256_file(KNOWN_MANIFEST),
        "new_manifest_sha256": sha256_file(NEW_MANIFEST),
    }
    overlay_fingerprint = canonical_sha(overlay_summary)
    temporary_db: Path | None = None
    temporary_root: Path | None = None
    try:
        temporary_db, db_summary = prepare_database(
            old, details, new_asset_rows, overlay_fingerprint, overlay_summary
        )
        temporary_root, root_summary = prepare_asset_root(old, new_asset_rows)
        os.replace(temporary_db, OUTPUT_DB)
        temporary_db = None
        os.replace(temporary_root, OUTPUT_ASSET_ROOT)
        temporary_root = None
        report = {
            "tool": "build_material_backfill_revision",
            "release_no": RELEASE_NO,
            "release_id": db_summary["release_id"],
            "status": "draft",
            "published": False,
            "current_pointer_changed": False,
            "base_release": {
                "release_no": old["release"]["release_no"],
                "release_id": old["release"]["release_id"],
                "artifact": str(OLD_DB),
                "bytes": old["old_db_bytes"],
                "sha256": old["old_db_sha256"],
                "counts": old["counts"],
            },
            "overlay_fingerprint": overlay_fingerprint,
            "overlay": overlay_summary,
            "database": {
                "artifact": str(OUTPUT_DB),
                "bytes": OUTPUT_DB.stat().st_size,
                "sha256": sha256_file(OUTPUT_DB),
                "counts": db_summary["counts"],
                "updated_part_names": db_summary["updated_part_names"],
                "updated_part_statuses": db_summary["updated_part_statuses"],
            },
            "assets": {
                **root_summary,
                "artifact_root": str(OUTPUT_ASSET_ROOT),
            },
            "validation": {
                "catalog_release_validator": "pending",
                "media_http_smoke": "pending",
                "page_chain": "pending",
                "server_upload": "not_started",
            },
        }
        atomic_json(OUTPUT_REPORT, report)
        print(json.dumps({
            "status": report["status"],
            "release_no": RELEASE_NO,
            "artifact": str(OUTPUT_DB),
            "asset_root": str(OUTPUT_ASSET_ROOT),
            "report": str(OUTPUT_REPORT),
            "new_asset_rows": candidate_summary["new_asset_rows"],
            "final_asset_count": db_summary["counts"]["catalog_assets"],
        }, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if temporary_db is not None:
            temporary_db.unlink(missing_ok=True)
        if temporary_root is not None:
            # Keep an interrupted hard-link tree for forensic recovery; it is
            # not a release and is never referenced by the application.
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"build_material_backfill_revision failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
