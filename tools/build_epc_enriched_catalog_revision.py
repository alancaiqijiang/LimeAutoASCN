#!/usr/bin/env python3
"""Build an EPC-enriched draft revision from raw-tree node bindings.

The current material-backfill draft is copied into a new release.  EPC image
rows are bound to release system nodes using raw tree model scope + node path +
obj_code, while qpren PostgreSQL is queried through an explicitly read-only
session.  The previous drafts and their asset roots are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sqlite3
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

APP_ROOT = Path(os.getenv("LIMEAUTO_EPC_APP_ROOT", "."))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.catalog_contract import _make_node_key, make_path_key  # noqa: E402

try:
    import psycopg  # type: ignore[import-not-found]
    from psycopg.rows import dict_row  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - runtime-specific
    psycopg = None
    dict_row = None

CURRENT_DB = Path(
    os.getenv(
        "LIMEAUTO_EPC_CURRENT_DB",
        "artifacts/releases/limeauto-material-backfill.sqlite",
    )
)
CURRENT_ROOT = Path(
    os.getenv(
        "LIMEAUTO_EPC_CURRENT_ROOT",
        "artifacts/catalog-assets-material-backfill",
    )
)
SOURCE_ROOT = Path(
    os.getenv(
        "LIMEAUTO_EPC_SOURCE_ROOT",
        "artifacts/qpren-source/var/lib/qpren",
    )
)
RAW_TREE = SOURCE_ROOT / "raw" / "tree"
OUTPUT_DB = Path(
    os.getenv(
        "LIMEAUTO_EPC_OUTPUT_DB",
        "artifacts/releases/limeauto-epc-enriched.sqlite",
    )
)
OUTPUT_ROOT = Path(
    os.getenv(
        "LIMEAUTO_EPC_OUTPUT_ROOT",
        "artifacts/catalog-assets-epc-enriched",
    )
)
OUTPUT_REPORT = Path(
    os.getenv(
        "LIMEAUTO_EPC_OUTPUT_REPORT",
        "reports/limeauto-epc-enriched.json",
    )
)
RELEASE_NO = os.getenv("LIMEAUTO_EPC_RELEASE_NO", "limeauto-epc-enriched-20260830")
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
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


def under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def source_path(local_path: str) -> Path:
    root = SOURCE_ROOT.resolve(strict=False)
    raw = Path(local_path)
    try:
        relative = raw.relative_to(Path("/var/lib/qpren"))
    except ValueError as exc:
        raise RuntimeError(f"unsupported qpren local path: {local_path}") from exc
    candidate = (root / relative).resolve(strict=False)
    if not under(root, candidate):
        raise RuntimeError(f"qpren local path escapes source root: {local_path}")
    return candidate


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text)


def _supplemental_row_error(index: int, reason: str) -> RuntimeError:
    # Do not include the URL or any other manifest value in an error.  Captured
    # URLs may contain credentials, tokens, or other sensitive query values.
    return RuntimeError(f"supplemental manifest row {index}: {reason}")


def _supplemental_file_path(root: Path, local_relative_path: Any) -> Path:
    if not isinstance(local_relative_path, str) or not local_relative_path:
        raise RuntimeError("supplemental local_relative_path is missing")
    if "\x00" in local_relative_path or "\\" in local_relative_path:
        raise RuntimeError("supplemental local_relative_path is not a safe POSIX path")
    relative = Path(local_relative_path)
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
        raise RuntimeError("supplemental local_relative_path escapes manifest root")
    candidate = root / relative
    probe = root
    for part in relative.parts:
        probe /= part
        if probe.is_symlink():
            raise RuntimeError("supplemental local_relative_path contains a symlink")
    resolved = candidate.resolve(strict=False)
    if not under(root, resolved):
        raise RuntimeError("supplemental local_relative_path escapes manifest root")
    return candidate


def load_supplemental_manifest(
    manifest_path: Path | str,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Load and validate a captured supplemental EPC thumbnail manifest.

    The manifest is evidence only: accepted rows are independently checked
    against their URL hash, local regular file, size, content hash, GIF magic,
    and MIME.  Only the manifest parent is permitted as the source root.
    """
    manifest = Path(manifest_path).expanduser()
    if not manifest.is_absolute():
        manifest = Path.cwd() / manifest
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError("supplemental manifest is not a regular file")
    try:
        root = manifest.parent.resolve(strict=True)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("supplemental manifest cannot be read as JSON") from exc
    if not root.is_dir() or not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("supplemental manifest results is not a list")

    accepted_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    accepted_count = 0
    rejected_count = 0
    duplicate_count = 0
    rejected_by_reason: Counter[str] = Counter()
    source_paths: set[str] = set()
    for index, row in enumerate(payload["results"]):
        if not isinstance(row, dict):
            rejected_count += 1
            rejected_by_reason["malformed_row"] += 1
            continue
        if row.get("accepted") is not True:
            rejected_count += 1
            rejected_by_reason["not_accepted"] += 1
            continue
        if "kind" in row and str(row.get("kind") or "").strip() != "epc_thumbnail":
            rejected_count += 1
            rejected_by_reason["wrong_kind"] += 1
            continue

        url = row.get("url")
        if not isinstance(url, str) or not url:
            raise _supplemental_row_error(index, "url is missing")
        url_sha256 = row.get("url_sha256")
        if not _is_sha256(url_sha256) or _sha256_text(url) != str(url_sha256).strip().lower():
            raise _supplemental_row_error(index, "url hash mismatch")
        body_sha256 = row.get("body_sha256")
        if not _is_sha256(body_sha256):
            raise _supplemental_row_error(index, "body hash is invalid")
        byte_count = row.get("byte_count")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count <= 0:
            raise _supplemental_row_error(index, "byte count is invalid")
        content_type = row.get("content_type", row.get("mime_type"))
        if not isinstance(content_type, str) or content_type.strip().lower() != "image/gif":
            raise _supplemental_row_error(index, "MIME mismatch")
        try:
            source_file = _supplemental_file_path(root, row.get("local_relative_path"))
            file_stat = os.lstat(source_file)
        except FileNotFoundError as exc:
            raise _supplemental_row_error(index, "source file is missing") from exc
        except RuntimeError as exc:
            raise _supplemental_row_error(index, str(exc)) from exc
        except OSError as exc:
            raise _supplemental_row_error(index, "source file cannot be inspected") from exc
        if stat.S_ISLNK(file_stat.st_mode):
            raise _supplemental_row_error(index, "source file is a symlink")
        if not stat.S_ISREG(file_stat.st_mode):
            raise _supplemental_row_error(index, "source file is not regular")
        if file_stat.st_size != byte_count:
            raise _supplemental_row_error(index, "size mismatch")
        try:
            actual_body_sha256 = sha256_file(source_file).lower()
            detected = detect_media_format(source_file, "epc_thumbnail")
        except OSError as exc:
            raise _supplemental_row_error(index, "source file cannot be validated") from exc
        if actual_body_sha256 != str(body_sha256).strip().lower():
            raise _supplemental_row_error(index, "body hash mismatch")
        if detected != (".gif", "image/gif"):
            raise _supplemental_row_error(index, "GIF magic mismatch")

        relative = source_file.relative_to(root).as_posix()
        key = ("epc_thumbnail", url)
        supplemental_row = {
            "url": url,
            "kind": "epc_thumbnail",
            "name": source_file.name,
            "local_path": relative,
            "sha256": str(body_sha256).strip().lower(),
            "size_bytes": byte_count,
            "status": "done",
            # Internal-only provenance used for later validation/materialization.
            "_supplemental_file": source_file,
            "_supplemental_root": root,
        }
        accepted_count += 1
        source_paths.add(relative)
        previous = accepted_by_key.get(key)
        if previous is not None:
            duplicate_count += 1
            if (
                previous["sha256"] != supplemental_row["sha256"]
                or previous["size_bytes"] != supplemental_row["size_bytes"]
            ):
                raise _supplemental_row_error(index, "duplicate URL has conflicting content")
            continue
        accepted_by_key[key] = supplemental_row

    return accepted_by_key, {
        "manifest_path": manifest.name,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "duplicate_count": duplicate_count,
        "source_file_count": len(source_paths),
        "accepted_unique_count": len(accepted_by_key),
        "rejected_by_reason": dict(rejected_by_reason),
    }


def _qpren_content_conflicts(
    qpren_row: dict[str, Any], supplemental_row: dict[str, Any]
) -> bool:
    supplemental_sha = str(supplemental_row["sha256"]).strip().lower()
    qpren_sha = str(qpren_row.get("sha256") or "").strip().lower()
    if qpren_sha and qpren_sha != supplemental_sha:
        return True
    qpren_size = qpren_row.get("size_bytes")
    if qpren_size is not None:
        try:
            if int(qpren_size) != int(supplemental_row["size_bytes"]):
                return True
        except (TypeError, ValueError):
            return True
    if qpren_sha:
        return False
    # A legacy qpren row may omit its hash.  If its read-only source file is
    # available, use the actual bytes to detect a collision; never replace the
    # qpren row merely because its metadata is incomplete.
    try:
        local_path = qpren_row.get("local_path")
        if not local_path:
            return False
        candidate = source_path(str(local_path))
        if candidate.is_symlink() or not candidate.is_file():
            return False
        if candidate.stat().st_size != int(supplemental_row["size_bytes"]):
            return True
        return sha256_file(candidate).lower() != supplemental_sha
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def merge_image_rows(
    qpren_rows: dict[tuple[str, str], dict[str, Any]],
    supplemental_rows: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    """Merge supplemental rows without ever replacing a qpren row."""
    merged = dict(qpren_rows)
    qpren_duplicate_count = 0
    added_count = 0
    for key in sorted(supplemental_rows):
        row = supplemental_rows[key]
        existing = qpren_rows.get(key)
        if existing is not None:
            qpren_duplicate_count += 1
            if _qpren_content_conflicts(existing, row):
                raise RuntimeError("supplemental duplicate URL conflicts with qpren content")
            continue
        merged[key] = row
        added_count += 1
    return merged, {
        "qpren_duplicate_count": qpren_duplicate_count,
        "added_count": added_count,
    }


def source_file_for_row(row: dict[str, Any]) -> Path:
    """Resolve a qpren or supplemental row to its verified source location."""
    supplemental_root = row.get("_supplemental_root")
    local_path = row.get("local_path")
    if local_path is None:
        local_path = row["source_path"]
    if supplemental_root is not None:
        root = Path(supplemental_root).resolve(strict=True)
        return _supplemental_file_path(root, local_path)
    return source_path(str(local_path))


def canonical_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or "/null" in text or text.rstrip().endswith("/"):
        return None
    if text.startswith("/"):
        return "https://www.qpren.cn" + text
    return text


def load_current_release() -> dict[str, Any]:
    if not CURRENT_DB.is_file() or not CURRENT_ROOT.is_dir():
        raise RuntimeError("current material revision or asset root is missing")
    connection = sqlite3.connect(f"file:{CURRENT_DB.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("current release quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("current release foreign_key_check failed")
        release_rows = connection.execute("SELECT * FROM catalog_releases").fetchall()
        if len(release_rows) != 1:
            raise RuntimeError("current release must contain one release row")
        release = dict(release_rows[0])
        nodes = [
            dict(row)
            for row in connection.execute(
                """
                SELECT series_code, model_code, source_obj_code, path_key, node_key
                FROM system_nodes
                WHERE is_derived = 0 AND source_obj_code IS NOT NULL
                """
            )
        ]
        assets = [
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
        for row in assets:
            if row["asset_type"] != "material_image":
                continue
            original_object_key = str(row["object_key"])
            detected = detect_media_format(CURRENT_ROOT / original_object_key, "material_image")
            if detected is None:
                raise RuntimeError(f"unsupported material bytes: {original_object_key}")
            extension, mime_type = detected
            row["_source_object_key"] = original_object_key
            row["object_key"] = str(Path(original_object_key).with_suffix(extension))
            row["mime_type"] = mime_type
        model_assets = []
        if connection.execute(
            """
            SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_model_assets'
            """
        ).fetchone():
            model_assets = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM catalog_model_assets ORDER BY asset_key"
                )
            ]
        return {
            "release": release,
            "nodes": nodes,
            "assets": assets,
            "model_assets": model_assets,
            "counts": {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "release_models",
                    "system_nodes",
                    "catalog_parts",
                    "fitments",
                    "catalog_assets",
                )
            },
            "sha256": sha256_file(CURRENT_DB),
            "bytes": CURRENT_DB.stat().st_size,
        }
    finally:
        connection.close()


def read_qpren_images(dsn: str) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    if psycopg is None or dict_row is None:
        raise RuntimeError("psycopg is unavailable")
    rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with psycopg.connect(
        dsn,
        connect_timeout=20,
        row_factory=dict_row,
        options=READ_ONLY_OPTIONS,
    ) as connection:
        connection.autocommit = False
        try:
            evidence = connection.execute(
                """
                SELECT current_database() AS database, current_user AS user,
                       pg_backend_pid() AS pid,
                       current_setting('transaction_read_only') AS transaction_read_only,
                       current_setting('default_transaction_read_only')
                           AS default_transaction_read_only
                """
            ).fetchone()
            if evidence["transaction_read_only"] != "on" or evidence[
                "default_transaction_read_only"
            ] != "on":
                raise RuntimeError("qpren connection is not explicitly read-only")
            connection.execute("BEGIN READ ONLY")
            rows = connection.execute(
                """
                SELECT url, kind, name, local_path, sha256, size_bytes, status
                FROM images
                WHERE kind IN ('epc_svg', 'epc_thumbnail')
                ORDER BY kind, url, local_path
                """
            ).fetchall()
            for row in rows:
                key = (str(row["kind"]), str(row["url"]))
                rows_by_key[key].append(dict(row))
        finally:
            connection.rollback()
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_keys = 0
    for key, candidates in rows_by_key.items():
        if len(candidates) > 1:
            duplicate_keys += 1
        selected[key] = sorted(
            candidates,
            key=lambda row: (
                str(row.get("status") or ""),
                str(row.get("local_path") or ""),
                str(row.get("sha256") or ""),
            ),
            reverse=True,
        )[0]
    return selected, {
        "read_only": dict(evidence),
        "rows": len(rows_by_key),
        "duplicate_url_kind_keys": duplicate_keys,
        "kind_counts": dict(Counter(key[0] for key in rows_by_key)),
    }


def detect_media_format(path: Path, kind: str) -> tuple[str, str] | None:
    """Return the extension/MIME proven by the file header, or None."""
    with path.open("rb") as handle:
        header = handle.read(4096)
    if kind == "epc_svg":
        text = header.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n")
        if text.startswith("<?xml") or text.startswith("<svg"):
            return ".svg", "image/svg+xml"
        return None
    if kind not in {"epc_thumbnail", "material_image"}:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if header.startswith(b"BM"):
        return ".bmp", "image/bmp"
    return None


def verify_source_row(row: dict[str, Any], cache: dict[tuple[str, str], tuple[bool, str]]) -> tuple[bool, str]:
    key = (str(row["kind"]), str(row["url"]))
    if key in cache:
        return cache[key]
    try:
        path = source_file_for_row(row)
        if path.is_symlink() or not path.is_file():
            result = (False, "missing")
        elif path.stat().st_size <= 0:
            result = (False, "empty")
        elif row.get("size_bytes") is not None and int(row["size_bytes"]) != path.stat().st_size:
            result = (False, "size_mismatch")
        elif not str(row.get("sha256") or "").strip():
            result = (False, "hash_missing")
        elif sha256_file(path).lower() != str(row["sha256"]).lower():
            result = (False, "hash_mismatch")
        else:
            result = (True, "ok")
    except (OSError, RuntimeError, TypeError, ValueError):
        result = (False, "source_path_invalid")
    cache[key] = result
    return result


def collect_epc_assets(
    nodes: list[dict[str, Any]],
    image_rows: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    node_map = {
        (
            str(row["series_code"]),
            str(row["model_code"]),
            str(row["source_obj_code"]),
            str(row["path_key"]),
        ): str(row["node_key"])
        for row in nodes
    }
    specs: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_models: set[tuple[str, str]] = set()
    raw_success = 0
    raw_concrete = Counter()
    raw_exact = Counter()
    raw_missing_row = Counter()
    unmatched_models = Counter()
    verified_source_cache: dict[tuple[str, str], tuple[bool, str]] = {}
    source_check = Counter()

    def walk(value: Any, model: tuple[str, str], path: tuple[str, ...]) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, model, path)
            return
        if not isinstance(value, dict):
            return
        label = str(value.get("grpName") or "").strip()
        current_path = path + (label,) if label else path
        obj_code = str(value.get("objCode") or "").strip()
        node_key = None
        if obj_code:
            path_key = make_path_key(current_path)
            node_key = node_map.get((*model, obj_code, path_key))
        if obj_code and node_key:
            for kind, field in (("epc_svg", "svgUrl"), ("epc_thumbnail", "thumbnailUrl")):
                url = canonical_url(value.get(field))
                if url is None:
                    continue
                raw_concrete[kind] += 1
                source_row = image_rows.get((kind, url))
                if source_row is None:
                    raw_missing_row[kind] += 1
                    if model == ("ENEA", "18457944-00"):
                        unmatched_models[kind] += 1
                    continue
                raw_exact[kind] += 1
                valid, check = verify_source_row(source_row, verified_source_cache)
                source_check[f"{kind}:{check}"] += 1
                if not valid:
                    continue
                source_file = source_file_for_row(source_row)
                detected = detect_media_format(source_file, kind)
                if detected is None:
                    source_check[f"{kind}:invalid_magic"] += 1
                    continue
                extension, mime_type = detected
                source_sha = str(source_row.get("sha256") or "").strip().lower()
                association_hash = hashlib.sha256(
                    f"{kind}|{url}|{node_key}".encode("utf-8")
                ).hexdigest()
                asset_type = "epc_drawing" if kind == "epc_svg" else "thumbnail"
                specs[(kind, url, node_key)] = {
                    "kind": kind,
                    "url": url,
                    "node_key": node_key,
                    "source_path": str(source_row["local_path"]),
                    "source_sha256": source_sha,
                    "size_bytes": int(source_row["size_bytes"]),
                    "mime_type": mime_type,
                    "_supplemental_root": source_row.get("_supplemental_root"),
                    "asset_key": f"{asset_type}:{association_hash}",
                    "asset_type": asset_type,
                    "object_key": f"release/{RELEASE_NO}/assets/{asset_type}/{source_sha}{extension}",
                    "status": "done",
                }
        for child in value.values():
            if isinstance(child, (dict, list)):
                walk(child, model, current_path)

    for path in sorted(RAW_TREE.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("code") != 200 or not isinstance(data, dict):
            continue
        vin_info = data.get("vinInfo") or {}
        model = (str(vin_info.get("seriesCode") or ""), str(vin_info.get("modelCode") or ""))
        if not model[0] or not model[1]:
            continue
        raw_success += 1
        raw_models.add(model)
        walk(data.get("epcTree"), model, ())

    specs_list = [specs[key] for key in sorted(specs)]
    return specs_list, {
        "raw_tree_success_files": raw_success,
        "raw_tree_models": len(raw_models),
        "raw_concrete_url_fields": dict(raw_concrete),
        "raw_url_fields_with_qpren_image_row": dict(raw_exact),
        "raw_url_fields_without_qpren_image_row": dict(raw_missing_row),
        "unmatched_e7_520智行版": dict(unmatched_models),
        "validated_source_files": dict(source_check),
        "invalid_media_magic": {key: value for key, value in source_check.items() if key.endswith(":invalid_magic")},
        "unique_bound_associations": dict(Counter(spec["kind"] for spec in specs_list)),
        "unique_bound_source_files": {
            kind: len({(spec["source_sha256"], spec["source_path"]) for spec in specs_list if spec["kind"] == kind})
            for kind in ("epc_svg", "epc_thumbnail")
        },
    }


def copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"asset is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"asset destination collision: {destination}")
    os.link(source, destination)


def copy_asset_root(current: dict[str, Any], specs: list[dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    if OUTPUT_ROOT.exists():
        raise RuntimeError(f"output asset root already exists: {OUTPUT_ROOT}")
    temporary = OUTPUT_ROOT.with_name(f".{OUTPUT_ROOT.name}.{uuid.uuid4().hex}.tmp")
    temporary.mkdir(parents=True)
    old_release_no = str(current["release"]["release_no"])
    old_prefix = f"release/{old_release_no}/"
    new_prefix = f"release/{RELEASE_NO}/"
    copied_existing = 0
    for row in current["assets"]:
        object_key = str(row["object_key"])
        if not object_key.startswith(old_prefix):
            raise RuntimeError(f"unexpected current object key: {object_key}")
        source_object_key = str(row.get("_source_object_key") or object_key)
        new_key = new_prefix + object_key[len(old_prefix) :]
        copy_regular_file(CURRENT_ROOT / source_object_key, temporary / new_key)
        copied_existing += 1
        if copied_existing % 5000 == 0:
            print(f"copied existing assets {copied_existing}/{len(current['assets'])}", flush=True)
    copied_model = 0
    for row in current["model_assets"]:
        object_key = str(row["object_key"])
        if not object_key.startswith(old_prefix):
            raise RuntimeError(f"unexpected current model object key: {object_key}")
        new_key = new_prefix + object_key[len(old_prefix) :]
        copy_regular_file(CURRENT_ROOT / object_key, temporary / new_key)
        copied_model += 1
    copied_object_keys: set[str] = set()
    for spec in specs:
        # One verified EPC image is reused by many model/node associations.
        # Keep one immutable object per content hash while retaining one
        # manifest row per node association in SQLite.
        if spec["object_key"] in copied_object_keys:
            continue
        copy_regular_file(source_file_for_row(spec), temporary / spec["object_key"])
        copied_object_keys.add(spec["object_key"])
    for directory in temporary.rglob("*"):
        if directory.is_dir():
            os.chmod(directory, 0o755)
    return temporary, {
        "existing_material_assets_copied": copied_existing,
        "existing_model_assets_copied": copied_model,
        "epc_source_files_copied": len(copied_object_keys),
        "output_root": str(OUTPUT_ROOT),
        "independent_copies": False,
        "materialized_with_hardlinks": True,
    }


def copy_database_and_add_assets(
    current: dict[str, Any],
    specs: list[dict[str, Any]],
    overlay_fingerprint: str,
    overlay_summary: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if OUTPUT_DB.exists():
        raise RuntimeError(f"output database already exists: {OUTPUT_DB}")
    OUTPUT_DB.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{OUTPUT_DB.name}.", suffix=".tmp", dir=str(OUTPUT_DB.parent))
    os.close(fd)
    temporary = Path(name)
    shutil.copyfile(CURRENT_DB, temporary)
    if temporary.stat().st_size != CURRENT_DB.stat().st_size:
        raise RuntimeError("current database copy size mismatch")
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA foreign_keys=OFF")
        current_release = current["release"]
        old_id = str(current_release["release_id"])
        new_id = str(uuid.uuid4())
        connection.execute("BEGIN")
        for table in (
            "release_models",
            "system_nodes",
            "catalog_parts",
            "fitments",
            "catalog_assets",
        ):
            connection.execute(f"UPDATE {table} SET release_id=? WHERE release_id=?", (new_id, old_id))
        has_model_assets = bool(current["model_assets"])
        if has_model_assets:
            connection.execute(
                "UPDATE catalog_model_assets SET release_id=? WHERE release_id=?", (new_id, old_id)
            )
        old_prefix = f"release/{current_release['release_no']}/"
        new_prefix = f"release/{RELEASE_NO}/"
        position = len(old_prefix) + 1
        for table in ("catalog_assets", "catalog_model_assets") if has_model_assets else ("catalog_assets",):
            connection.execute(
                f"""
                UPDATE {table}
                SET object_key=? || substr(object_key, ?)
                WHERE release_id=? AND object_key LIKE ?
                """,
                (new_prefix, position, new_id, old_prefix + "%"),
            )
        normalized_existing = 0
        for row in current["assets"]:
            if row["asset_type"] != "material_image":
                continue
            source_object_key = str(row.get("_source_object_key") or row["object_key"])
            corrected_object_key = str(row["object_key"])
            old_key = new_prefix + source_object_key[len(old_prefix) :]
            new_key = new_prefix + corrected_object_key[len(old_prefix) :]
            cur = connection.execute(
                "UPDATE catalog_assets SET object_key=?, mime_type=? WHERE release_id=? AND asset_key=? AND object_key=?",
                (new_key, row["mime_type"], new_id, row["asset_key"], old_key),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"failed to normalize existing material asset: {row['asset_key']}"
                )
            normalized_existing += 1
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
                f"base={current_release['release_no']};overlay={overlay_fingerprint}",
                overlay_fingerprint,
                json.dumps(
                    {
                        "base_release_no": current_release["release_no"],
                        "base_release_sha256": current["sha256"],
                        "overlay": overlay_summary,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "Draft revision with validated material and EPC node bindings; not published.",
                old_id,
            ),
        )
        rows = [
            (
                new_id,
                spec["asset_key"],
                None,
                spec["node_key"],
                spec["asset_type"],
                spec["object_key"],
                spec["source_sha256"],
                spec["size_bytes"],
                spec["mime_type"],
                spec["status"],
            )
            for spec in specs
        ]
        connection.executemany(
            """
            INSERT INTO catalog_assets(
                release_id, asset_key, material_code, system_node_key,
                asset_type, object_key, source_sha256, size_bytes,
                mime_type, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS catalog_assets_node_type_idx
            ON catalog_assets (release_id, system_node_key, asset_type, status)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS catalog_assets_material_idx
            ON catalog_assets (release_id, material_code, asset_type, status)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS system_nodes_parent_idx
            ON system_nodes (release_id, series_code, model_code, parent_key)
            """
        )
        connection.commit()
        connection.execute("PRAGMA synchronous=FULL")
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(journal_mode).lower() != "delete":
            raise RuntimeError(f"failed to restore SQLite journal mode: {journal_mode}")
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("foreign_key_check failed after EPC overlay")
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE release_id=?", (new_id,)).fetchone()[0])
            for table in (
                "release_models",
                "system_nodes",
                "catalog_parts",
                "fitments",
                "catalog_assets",
            )
        }
        return temporary, {"release_id": new_id, "counts": counts, "normalized_existing_assets": normalized_existing}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--supplemental-manifest", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    current = load_current_release()
    qpren_rows, qpren_summary = read_qpren_images(args.dsn)
    supplemental_overlay: dict[str, Any] | None = None
    if args.supplemental_manifest is None:
        image_rows = qpren_rows
    else:
        supplemental_rows, manifest_summary = load_supplemental_manifest(args.supplemental_manifest)
        image_rows, merge_summary = merge_image_rows(qpren_rows, supplemental_rows)
        source_counts = {
            "accepted": manifest_summary["accepted_count"],
            "rejected": manifest_summary["rejected_count"],
            "duplicate": manifest_summary["duplicate_count"] + merge_summary["qpren_duplicate_count"],
            "added": merge_summary["added_count"],
        }
        supplemental_overlay = {
            "manifest_path": manifest_summary["manifest_path"],
            "accepted_count": manifest_summary["accepted_count"],
            "rejected_count": manifest_summary["rejected_count"],
            "duplicate_count": source_counts["duplicate"],
            "added_count": source_counts["added"],
            "manifest_duplicate_count": manifest_summary["duplicate_count"],
            "qpren_duplicate_count": merge_summary["qpren_duplicate_count"],
            "source_file_count": manifest_summary["source_file_count"],
            "accepted_unique_count": manifest_summary["accepted_unique_count"],
            "rejected_by_reason": manifest_summary["rejected_by_reason"],
            "source_counts": source_counts,
        }
    specs, raw_summary = collect_epc_assets(current["nodes"], image_rows)
    if not specs:
        raise RuntimeError("no validated EPC node assets were collected")
    overlay_summary = {
        "base_release": {
            "release_no": current["release"]["release_no"],
            "artifact": str(CURRENT_DB),
            "bytes": current["bytes"],
            "sha256": current["sha256"],
            "counts": current["counts"],
        },
        "qpren_image_evidence": qpren_summary,
        "raw_tree_evidence": raw_summary,
        "epc_overlay_counts": dict(Counter(spec["asset_type"] for spec in specs)),
        "epc_overlay_asset_rows": len(specs),
        "epc_overlay_source_sha256": len({(spec["asset_type"], spec["source_sha256"]) for spec in specs}),
        "status": "only_validated_source_rows",
    }
    if supplemental_overlay is not None:
        overlay_summary["supplemental_manifest"] = supplemental_overlay
    overlay_fingerprint = canonical_sha(overlay_summary)
    temporary_db: Path | None = None
    temporary_root: Path | None = None
    try:
        temporary_db, database_summary = copy_database_and_add_assets(
            current, specs, overlay_fingerprint, overlay_summary
        )
        temporary_root, root_summary = copy_asset_root(current, specs)
        os.replace(temporary_db, OUTPUT_DB)
        temporary_db = None
        os.replace(temporary_root, OUTPUT_ROOT)
        temporary_root = None
        report = {
            "tool": "build_epc_enriched_catalog_revision",
            "release_no": RELEASE_NO,
            "release_id": database_summary["release_id"],
            "status": "draft",
            "published": False,
            "current_pointer_changed": False,
            "base_release": overlay_summary["base_release"],
            "overlay_fingerprint": overlay_fingerprint,
            "overlay": overlay_summary,
            "database": {
                "artifact": str(OUTPUT_DB),
                "bytes": OUTPUT_DB.stat().st_size,
                "sha256": sha256_file(OUTPUT_DB),
                "counts": database_summary["counts"],
            },
            "assets": root_summary,
            "validation": {
                "catalog_release_validator": "pending",
                "media_http_smoke": "pending",
                "page_chain": "pending",
                "server_upload": "not_started",
            },
        }
        if supplemental_overlay is not None:
            report["supplemental_manifest"] = supplemental_overlay
        atomic_json(OUTPUT_REPORT, report)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "release_no": RELEASE_NO,
                    "artifact": str(OUTPUT_DB),
                    "asset_root": str(OUTPUT_ROOT),
                    "report": str(OUTPUT_REPORT),
                    "epc_asset_rows": len(specs),
                    "counts": database_summary["counts"],
                    "unmatched_e7_520智行版": raw_summary["unmatched_e7_520智行版"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        if temporary_db is not None:
            temporary_db.unlink(missing_ok=True)
        # Do not remove an interrupted asset tree automatically; it is outside
        # the final release path and remains available for forensic cleanup.


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"build_epc_enriched_catalog_revision failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
