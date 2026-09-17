#!/usr/bin/env python3
"""Build an isolated, draft SQLite catalog release from read-only qpren PostgreSQL.

This node deliberately creates only a draft artifact.  It never writes to qpren,
does not publish a release, and does not maintain a current-release pointer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.catalog_contract import build_catalog_report, parse_quantity  # noqa: E402

try:  # Optional for unit tests that only import pure helpers.
    import psycopg  # type: ignore[import-not-found]
    from psycopg.rows import dict_row  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on environment
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

SCHEMA_PATH = ROOT / "release_schema" / "catalog_release.sql"
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
BATCH_SIZE = 5_000
ASSET_KIND_MAP = {
    "material": ("material_image", ".jpg"),
    "epc_svg": ("epc_drawing", ".svg"),
    "epc_thumbnail": ("thumbnail", ".jpg"),
}
ASSET_STATUSES = frozenset(
    {"pending", "ready", "done", "available", "failed", "missing"}
)
QPREN_ASSET_PREFIX = Path("/var/lib/qpren")
RELEASE_COUNT_TABLES = (
    "release_models",
    "system_nodes",
    "catalog_parts",
    "fitments",
    "catalog_assets",
)
ASSET_INSERT_SQL = """
    INSERT OR IGNORE INTO catalog_assets (
        release_id, asset_key, material_code, system_node_key,
        asset_type, object_key, source_sha256, size_bytes,
        mime_type, status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_model_filter(value: str) -> tuple[str, str]:
    """Parse the public ``SERIES/MODEL`` selector without accepting ambiguity."""
    pieces = value.split("/")
    if len(pieces) != 2 or not all(piece.strip() for piece in pieces):
        raise ValueError("model selector must use the form SERIES/MODEL")
    return pieces[0].strip(), pieces[1].strip()


def safe_asset_type(kind: str | None) -> tuple[str, str]:
    """Map a qpren image kind to a release type and safe extension."""
    return ASSET_KIND_MAP.get(str(kind or "").strip(), ("other", ".bin"))


def _safe_component(value: str) -> str:
    return quote(str(value), safe="-._~")


def _normalize_source_sha256(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def _normalize_asset_size(value: Any) -> int | None:
    """Return a schema-compatible non-negative source size, if supplied."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid asset size")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("invalid asset size")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid asset size") from exc
    if normalized < 0:
        raise ValueError("invalid asset size")
    return normalized


def _normalize_asset_status(value: Any) -> str | None:
    """Return a release-schema asset status, or ``None`` when it is invalid."""
    normalized = "pending" if value is None else str(value).strip().lower()
    if not normalized:
        normalized = "pending"
    return normalized if normalized in ASSET_STATUSES else None


def asset_identity(
    release_no: str,
    kind: str | None,
    url: str,
    source_sha256: str | None,
) -> dict[str, Any]:
    """Return deterministic asset/object keys without exposing the source URL."""
    asset_type, extension = safe_asset_type(kind)
    source_hash = _normalize_source_sha256(source_sha256)
    if source_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("invalid source sha256")
    identity_hash = source_hash or hashlib.sha256(url.encode("utf-8")).hexdigest()
    release_part = _safe_component(release_no)
    key = f"{asset_type}:{identity_hash}"
    return {
        "asset_key": key,
        "object_key": f"release/{release_part}/assets/{asset_type}/{identity_hash}{extension}",
        "source_sha256": source_hash,
        "asset_type": asset_type,
        "identity_hash": identity_hash,
    }


def resolve_asset_path(asset_root: Path, local_path: str | None) -> Path:
    """Map the known qpren source prefix into root without permitting escapes."""
    if not local_path or "\x00" in str(local_path):
        raise ValueError("asset local_path is missing")
    source = Path(str(local_path))
    if not source.is_absolute():
        raise ValueError("asset local_path is outside the supported qpren prefix")

    try:
        relative = source.relative_to(QPREN_ASSET_PREFIX)
    except ValueError as exc:
        raise ValueError("asset local_path is outside the supported qpren prefix") from exc
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("asset local_path escapes the qpren prefix")

    root = Path(asset_root).expanduser().resolve(strict=False)
    try:
        candidate = (root / relative).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("asset path cannot be resolved safely") from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("asset path escapes asset root") from exc
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_asset_file(
    asset_root: Path,
    local_path: str | None,
    expected_size: Any,
    expected_sha256: Any = None,
) -> str:
    """Validate one mapped source file; return a report-friendly check status."""
    if not local_path:
        return "missing"
    try:
        path = resolve_asset_path(asset_root, local_path)
    except ValueError:
        return "path_escape"
    try:
        if not path.is_file():
            return "missing"
        actual_size = path.stat().st_size
    except (OSError, RuntimeError):
        return "missing"
    if actual_size <= 0:
        return "empty"
    if expected_size is not None:
        try:
            expected = int(expected_size)
        except (TypeError, ValueError, OverflowError):
            return "size_mismatch"
        if expected != actual_size:
            return "size_mismatch"
    normalized_sha256 = _normalize_source_sha256(expected_sha256)
    if normalized_sha256 is None:
        return "hash_missing"
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
        return "invalid_hash"
    try:
        actual_sha256 = _file_sha256(path)
    except OSError:
        return "missing"
    if actual_sha256 != normalized_sha256:
        return "hash_mismatch"
    return "ok"


def canonical_fingerprint(payload: Any) -> str:
    """Hash canonical JSON so reordered source mappings produce one fingerprint."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def read_only_evidence(connection: Any) -> dict[str, Any]:
    version = connection.execute("SELECT version() AS value").fetchone()["value"]
    database = connection.execute("SELECT current_database() AS value").fetchone()["value"]
    user = connection.execute("SELECT current_user AS value").fetchone()["value"]
    transaction_read_only = connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
    default_read_only = connection.execute(
        "SELECT current_setting('default_transaction_read_only') AS value"
    ).fetchone()["value"]
    return {
        "connection_options": READ_ONLY_OPTIONS,
        "server_version": version,
        "current_database": database,
        "current_user": user,
        "transaction_read_only": transaction_read_only,
        "default_transaction_read_only": default_read_only,
    }


def _model_predicate(filters: Sequence[tuple[str, str]], alias: str = "") -> tuple[str, list[str]]:
    if not filters:
        return "", []
    prefix = f"{alias}." if alias else ""
    pieces = [f"({prefix}series_code = %s AND {prefix}model_code = %s)" for _ in filters]
    params = [value for pair in filters for value in pair]
    return "WHERE " + " OR ".join(pieces), params


def query_model_rows(connection: Any, filters: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    where, params = _model_predicate(filters)
    return [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT series_code, model_code, series_name, model_name
            FROM model_versions
            {where}
            ORDER BY series_code, model_code
            """,
            params,
        ).fetchall()
    ]


def query_node_rows(connection: Any, filters: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    where, params = _model_predicate(filters, alias="en")
    rows = connection.execute(
        f"""
        SELECT en.series_code, en.model_code, en.obj_code, en.tree_key, en.node_path,
               COALESCE(
                   NULLIF(regexp_replace(COALESCE(en.node_path, ''), '^.* > ', ''), ''),
                   NULLIF(en.tree_key, ''), en.obj_code
               ) AS node_name,
               COUNT(o.id)::int AS direct_part_count
        FROM epc_nodes en
        LEFT JOIN occurrences o
          ON o.series_code = en.series_code
         AND o.model_code = en.model_code
         AND o.obj_code = en.obj_code
        {where}
        GROUP BY en.id, en.series_code, en.model_code, en.obj_code, en.tree_key, en.node_path
        ORDER BY en.series_code, en.model_code, en.node_path, en.obj_code
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def query_count(connection: Any, table_name: str, filters: Sequence[tuple[str, str]]) -> int:
    # Table names here are fixed constants selected by this module, never user input.
    where, params = _model_predicate(filters)
    row = connection.execute(f"SELECT COUNT(*) AS value FROM {table_name} {where}", params).fetchone()
    return int(row["value"])


def query_scoped_material_count(
    connection: Any, table_name: str, filters: Sequence[tuple[str, str]]
) -> int:
    """Count material-keyed tables through the selected occurrences."""
    where, params = _model_predicate(filters, alias="o")
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS value
        FROM {table_name} t
        WHERE t.material_code IS NOT NULL
          AND t.material_code IN (
            SELECT DISTINCT o.material_code FROM occurrences o {where}
        )
        """,
        params,
    ).fetchone()
    return int(row["value"])


@contextmanager
def streaming_query(
    connection: Any,
    query: str,
    params: Sequence[Any],
    *,
    name: str,
) -> Iterator[Any]:
    """Yield a server-side cursor in one read-only transaction."""
    previous_autocommit = connection.autocommit
    connection.autocommit = False
    try:
        with connection.cursor(name=name) as cursor:
            cursor.execute(query, params)
            yield cursor
        connection.rollback()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.autocommit = previous_autocommit


def iter_occurrences(
    connection: Any,
    filters: Sequence[tuple[str, str]],
) -> Iterator[dict[str, Any]]:
    where, params = _model_predicate(filters)
    with streaming_query(
        connection,
        f"""
        SELECT occurrence_key, series_code, model_code, obj_code, material_code,
               callout, qty, manual_code, description, note
        FROM occurrences
        {where}
        ORDER BY id
        """,
        params,
        name=f"release_occurrences_{uuid.uuid4().hex}",
    ) as cursor:
        while True:
            rows = cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break
            yield from (dict(row) for row in rows)


def iter_images(connection: Any, material_codes: set[str] | None) -> Iterator[dict[str, Any]]:
    params: list[Any] = []
    if material_codes is None:
        where = ""
    elif not material_codes:
        return
    else:
        where = "WHERE material_code = ANY(%s)"
        params = [sorted(material_codes)]
    with streaming_query(
        connection,
        f"""
        SELECT url, kind, material_code, name, local_path, sha256, size_bytes, status
        FROM images
        {where}
        ORDER BY url, kind
        """,
        params,
        name=f"release_images_{uuid.uuid4().hex}",
    ) as cursor:
        while True:
            rows = cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break
            yield from (dict(row) for row in rows)


def material_detail_statuses(connection: Any, material_codes: set[str]) -> dict[str, str]:
    if not material_codes:
        return {}
    columns = {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'material_details'
            """
        ).fetchall()
    }
    if "status" not in columns:
        return {}
    rows = connection.execute(
        """
        SELECT material_code, status
        FROM material_details
        WHERE material_code = ANY(%s)
        """,
        [sorted(material_codes)],
    ).fetchall()
    return {str(row["material_code"]): str(row["status"] or "unavailable") for row in rows}


def _sqlite_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TEMP TABLE staged_occurrences (
            source_occurrence_key TEXT PRIMARY KEY,
            series_code TEXT NOT NULL,
            model_code TEXT NOT NULL,
            node_key TEXT NOT NULL,
            material_code TEXT NOT NULL,
            callout TEXT,
            quantity INTEGER,
            quantity_raw TEXT,
            manual_code TEXT,
            fitment_note TEXT
        )
        """
    )
    return connection


def _asset_mime(asset_type: str, name: Any, url: str) -> str:
    if asset_type == "epc_drawing":
        return "image/svg+xml"
    return mimetypes.guess_type(str(name or url))[0] or "image/jpeg"


def insert_asset_batch(connection: Any, rows: Sequence[Sequence[Any]]) -> None:
    """Insert a batch while allowing deterministic duplicate asset identities."""
    if rows:
        connection.executemany(ASSET_INSERT_SQL, rows)


def query_release_counts(connection: Any, release_id: str) -> dict[str, int]:
    """Count only the rows belonging to this draft release after all inserts."""
    return {
        table: int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE release_id = ?", (release_id,)
            ).fetchone()[0]
        )
        for table in RELEASE_COUNT_TABLES
    }


def build_release(
    connection: Any,
    *,
    output: Path,
    release_no: str,
    scope: str,
    filters: Sequence[tuple[str, str]],
    report_path: Path,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    if scope == "allowlist" and not filters:
        raise ValueError("--scope allowlist requires at least one --model")
    if scope == "all" and filters:
        raise ValueError("--model is only valid with --scope allowlist")

    evidence = read_only_evidence(connection)
    selected_filters = list(filters)
    model_rows = query_model_rows(connection, selected_filters)
    requested_models = set(selected_filters)
    actual_models = {
        (str(row["series_code"]), str(row["model_code"])) for row in model_rows
    }
    missing_models = sorted(requested_models - actual_models)
    node_rows = query_node_rows(connection, selected_filters)
    contract = build_catalog_report(node_rows)
    blocking_errors: list[dict[str, Any]] = list(contract["blocking_errors"])
    if missing_models:
        blocking_errors.append(
            {
                "kind": "requested_model_missing",
                "models": [list(pair) for pair in missing_models],
            }
        )

    scoped_material_count = query_scoped_material_count if scope == "allowlist" else query_count
    source_counts = {
        "total": {
            name: query_count(connection, name, [])
            for name in (
                "model_versions",
                "epc_nodes",
                "occurrences",
                "parts",
                "material_details",
                "images",
            )
        },
        "scoped": {
            "model_versions": query_count(connection, "model_versions", selected_filters),
            "epc_nodes": query_count(connection, "epc_nodes", selected_filters),
            "occurrences": query_count(connection, "occurrences", selected_filters),
            "parts": scoped_material_count(connection, "parts", selected_filters),
            "material_details": scoped_material_count(
                connection, "material_details", selected_filters
            ),
            "images": scoped_material_count(connection, "images", selected_filters),
        },
    }
    scope_descriptor = {
        "scope": scope,
        "models": [list(pair) for pair in selected_filters],
        "source_counts": source_counts,
        "database": evidence,
    }
    source_snapshot_fingerprint = canonical_fingerprint(scope_descriptor)
    release_id = str(uuid.uuid4())
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent))
    os.close(fd)
    temporary_output = Path(temporary_name)
    sqlite_connection: sqlite3.Connection | None = None
    report: dict[str, Any]
    try:
        sqlite_connection = _sqlite_connection(temporary_output)
        sqlite_connection.execute(
            """
            INSERT INTO catalog_releases (
                release_id, release_no, source_system, source_snapshot,
                source_snapshot_fingerprint, source_counts_json,
                validation_summary_json, status, notes
            ) VALUES (?, ?, 'qpren', ?, ?, ?, '{}', 'draft', ?)
            """,
            (
                release_id,
                release_no,
                source_snapshot_fingerprint,
                source_snapshot_fingerprint,
                json.dumps(source_counts, ensure_ascii=False, sort_keys=True),
                "Draft only; not published.",
            ),
        )
        sqlite_connection.executemany(
            """
            INSERT INTO release_models (
                release_id, series_code, model_code, series_name_source,
                model_name_source, model_name, publish_status
            ) VALUES (?, ?, ?, ?, ?, ?, 'published')
            """,
            [
                (
                    release_id,
                    str(row["series_code"]),
                    str(row["model_code"]),
                    row.get("series_name"),
                    row.get("model_name"),
                    row.get("model_name"),
                )
                for row in model_rows
            ],
        )

        source_node_map: dict[tuple[str, str, str], str] = {}
        for node in contract["nodes"]:
            sqlite_connection.execute(
                """
                INSERT INTO system_nodes (
                    release_id, node_key, series_code, model_code, source_obj_code,
                    source_tree_key, path_key, parent_key, node_path_source,
                    name_source, display_name, depth, is_derived,
                    path_variant_count, child_count, direct_part_count,
                    descendant_part_count, publish_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published')
                """,
                (
                    release_id,
                    node["node_key"],
                    node["series_code"],
                    node["model_code"],
                    node["obj_code"],
                    node.get("tree_key"),
                    node["path_key"],
                    node["parent_key"],
                    node["node_path"],
                    node["display_name"],
                    node["display_name"],
                    node["depth"],
                    int(bool(node["is_derived"])),
                    node.get("path_variant_count", 0),
                    node["child_count"],
                    node["direct_part_count"],
                    node["descendant_part_count"],
                ),
            )
            if not node["is_derived"]:
                key = (node["series_code"], node["model_code"], str(node["obj_code"]))
                if key in source_node_map:
                    blocking_errors.append({"kind": "duplicate_source_node_key", "key": list(key)})
                source_node_map[key] = str(node["node_key"])

        material_codes: set[str] = set()
        staged_count = 0
        staged_insert_sql = """
            INSERT INTO staged_occurrences (
                source_occurrence_key, series_code, model_code, node_key,
                material_code, callout, quantity, quantity_raw, manual_code, fitment_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        staged_batch: list[tuple[Any, ...]] = []
        for occurrence in iter_occurrences(connection, selected_filters):
            scope_key = (
                str(occurrence["series_code"]),
                str(occurrence["model_code"]),
                str(occurrence["obj_code"]),
            )
            node_key = source_node_map.get(scope_key)
            if node_key is None:
                blocking_errors.append({"kind": "occurrence_node_missing", "key": list(scope_key)})
                continue
            occurrence_key = str(occurrence["occurrence_key"])
            material_code = str(occurrence["material_code"])
            quantity = parse_quantity(occurrence.get("qty"))
            staged_batch.append(
                (
                    occurrence_key,
                    str(occurrence["series_code"]),
                    str(occurrence["model_code"]),
                    node_key,
                    material_code,
                    occurrence.get("callout"),
                    quantity["quantity"],
                    quantity["quantity_raw"],
                    occurrence.get("manual_code"),
                    occurrence.get("note"),
                )
            )
            material_codes.add(material_code)
            if len(staged_batch) >= BATCH_SIZE:
                sqlite_connection.executemany(staged_insert_sql, staged_batch)
                staged_count += len(staged_batch)
                staged_batch.clear()
        if staged_batch:
            sqlite_connection.executemany(staged_insert_sql, staged_batch)
            staged_count += len(staged_batch)

        detail_status = material_detail_statuses(connection, material_codes)
        if material_codes:
            parts_rows = connection.execute(
                """
                SELECT material_code, first_description
                FROM parts
                WHERE material_code = ANY(%s)
                ORDER BY material_code
                """,
                [sorted(material_codes)],
            ).fetchall()
        else:
            parts_rows = []
        found_parts = {str(row["material_code"]) for row in parts_rows}
        missing_parts = sorted(material_codes - found_parts)
        if missing_parts:
            blocking_errors.append(
                {
                    "kind": "staged_part_missing",
                    "count": len(missing_parts),
                    "sample": missing_parts[:20],
                }
            )
        sqlite_connection.executemany(
            """
            INSERT INTO catalog_parts (
                release_id, material_code, display_name_source, description,
                source_detail_status, publish_status
            ) VALUES (?, ?, ?, ?, ?, 'reference_only')
            """,
            [
                (
                    release_id,
                    str(row["material_code"]),
                    row.get("first_description"),
                    row.get("first_description"),
                    detail_status.get(str(row["material_code"]), "unavailable"),
                )
                for row in parts_rows
            ],
        )
        sqlite_connection.execute(
            """
            INSERT INTO fitments (
                release_id, source_occurrence_key, series_code, model_code,
                node_key, material_code, callout, quantity, quantity_raw,
                manual_code, fitment_note, fitment_level, review_status
            )
            SELECT ?, source_occurrence_key, series_code, model_code, node_key,
                   material_code, callout, quantity, quantity_raw, manual_code,
                   fitment_note, 'reference_only', 'pending'
            FROM staged_occurrences
            """,
            (release_id,),
        )

        unbound_image_count = 0
        asset_check_summary = {
            "source_rows": 0,
            "manifest_candidates": 0,
            "checked": 0,
            "ok": 0,
            "missing": 0,
            "empty": 0,
            "size_mismatch": 0,
            "path_escape": 0,
            "hash_missing": 0,
            "hash_mismatch": 0,
            "invalid_hash": 0,
            "invalid_size": 0,
            "invalid_status": 0,
        }
        image_batch: list[tuple[Any, ...]] = []
        for image in iter_images(connection, material_codes if scope == "allowlist" else None):
            if image.get("material_code") is None:
                unbound_image_count += 1
                continue
            material_code = str(image["material_code"])
            if material_code not in found_parts:
                continue
            asset_check_summary["source_rows"] += 1
            source_sha256 = _normalize_source_sha256(image.get("sha256"))
            if source_sha256 is None:
                asset_check_summary["hash_missing"] += 1
            try:
                identity = asset_identity(
                    release_no,
                    image.get("kind"),
                    str(image.get("url") or ""),
                    source_sha256,
                )
            except ValueError as exc:
                asset_check_summary["invalid_hash"] += 1
                blocking_errors.append(
                    {
                        "kind": "invalid_source_sha256",
                        "material_code": material_code,
                        "asset_kind": image.get("kind"),
                        "identity_hash": hashlib.sha256(
                            str(image.get("url") or "").encode("utf-8")
                        ).hexdigest(),
                        "message": str(exc),
                    }
                )
                continue
            try:
                size_bytes = _normalize_asset_size(image.get("size_bytes"))
            except ValueError as exc:
                asset_check_summary["invalid_size"] += 1
                blocking_errors.append(
                    {
                        "kind": "invalid_asset_size",
                        "material_code": material_code,
                        "asset_kind": image.get("kind"),
                        "size_bytes": image.get("size_bytes"),
                        "message": str(exc),
                    }
                )
                continue
            asset_status = _normalize_asset_status(image.get("status"))
            if asset_status is None:
                asset_check_summary["invalid_status"] += 1
                blocking_errors.append(
                    {
                        "kind": "invalid_asset_status",
                        "material_code": material_code,
                        "asset_kind": image.get("kind"),
                        "status": image.get("status"),
                    }
                )
                continue
            asset_check_summary["manifest_candidates"] += 1
            if asset_root is not None:
                asset_check_summary["checked"] += 1
                check_status = check_asset_file(
                    asset_root,
                    image.get("local_path"),
                    size_bytes,
                    source_sha256,
                )
                if check_status != "hash_missing":
                    asset_check_summary[check_status] += 1
                if check_status != "ok":
                    blocking_errors.append(
                        {
                            "kind": "source_asset_check_failed",
                            "material_code": material_code,
                            "asset_kind": image.get("kind"),
                            "status": check_status,
                        }
                    )
            image_batch.append(
                (
                    release_id,
                    identity["asset_key"],
                    material_code,
                    None,
                    identity["asset_type"],
                    identity["object_key"],
                    identity["source_sha256"],
                    size_bytes,
                    _asset_mime(identity["asset_type"], image.get("name"), str(image.get("url") or "")),
                    asset_status,
                )
            )
            if len(image_batch) >= BATCH_SIZE:
                insert_asset_batch(sqlite_connection, image_batch)
                image_batch.clear()
        if image_batch:
            insert_asset_batch(sqlite_connection, image_batch)
        release_counts = query_release_counts(sqlite_connection, release_id)
        asset_manifest_count = release_counts["catalog_assets"]
        asset_check_summary["deduplicated"] = max(
            0, asset_check_summary["manifest_candidates"] - asset_manifest_count
        )
        sqlite_connection.execute(
            "UPDATE catalog_releases SET validation_summary_json = ? WHERE release_id = ?",
            (
                json.dumps(
                    {
                        "blocking_error_count": len(blocking_errors),
                        "contract_summary": contract["summary"],
                        "release_counts": release_counts,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                release_id,
            ),
        )
        sqlite_connection.commit()
        sqlite_connection.close()
        sqlite_connection = None

        report = {
            "tool": "build_catalog_release",
            "generated_at": utc_now(),
            "release_id": release_id,
            "release_no": release_no,
            "status": "draft" if not blocking_errors else "failed",
            "published": False,
            "current_pointer_changed": False,
            "artifact": str(output),
            "report": str(report_path),
            "scope": scope,
            "model_filters": [list(pair) for pair in selected_filters],
            "asset_root": str(asset_root) if asset_root else None,
            "database": evidence,
            "source_counts": source_counts,
            "source_snapshot_fingerprint": source_snapshot_fingerprint,
            "contract_summary": contract["summary"],
            "structural_conflict_count": len(contract.get("structural_conflicts", [])),
            "structural_conflict_sample": contract.get("structural_conflicts", [])[:25],
            "warnings": contract["warnings"],
            "blocking_errors": blocking_errors,
            "staged_occurrence_count": staged_count,
            "unbound_image_count": unbound_image_count,
            "asset_manifest_count": asset_manifest_count,
            "source_asset_check_summary": asset_check_summary,
            "release_counts": release_counts,
        }
        if asset_root is None:
            report["warnings"] = [
                *report["warnings"],
                {
                    "kind": "asset_root_not_provided",
                    "message": "source asset files were not checked",
                },
            ]
        if blocking_errors:
            temporary_output.unlink(missing_ok=True)
        else:
            os.replace(temporary_output, output)
        atomic_write_json(report_path, report)
        return report
    except Exception as exc:
        if sqlite_connection is not None:
            sqlite_connection.rollback()
            sqlite_connection.close()
        temporary_output.unlink(missing_ok=True)
        report = {
            "tool": "build_catalog_release",
            "generated_at": utc_now(),
            "release_no": release_no,
            "status": "failed",
            "published": False,
            "current_pointer_changed": False,
            "scope": scope,
            "model_filters": [list(pair) for pair in selected_filters],
            "blocking_errors": [{"kind": "builder_exception", "message": str(exc)}],
        }
        atomic_write_json(report_path, report)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("LIMEAUTO_QPREN_READ_DSN", ""))
    parser.add_argument("--output", required=True, help="Atomic SQLite draft artifact path")
    parser.add_argument("--report", required=True, help="Atomic JSON report path")
    parser.add_argument("--release-no", required=True)
    parser.add_argument("--scope", choices=("all", "allowlist"), default="allowlist")
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--asset-root", default=None)
    args = parser.parse_args(argv)

    if psycopg is None or dict_row is None:
        print("psycopg[binary] is required", file=sys.stderr)
        return 2
    if not args.dsn:
        print("--dsn is required when LIMEAUTO_QPREN_READ_DSN is not set", file=sys.stderr)
        return 2
    try:
        filters = [parse_model_filter(value) for value in args.model]
        asset_root = Path(args.asset_root).expanduser().resolve() if args.asset_root else None
        with psycopg.connect(
            args.dsn,
            connect_timeout=20,
            row_factory=dict_row,
            options=READ_ONLY_OPTIONS,
        ) as connection:
            connection.autocommit = True
            report = build_release(
                connection,
                output=Path(args.output),
                release_no=args.release_no,
                scope=args.scope,
                filters=filters,
                report_path=Path(args.report),
                asset_root=asset_root,
            )
    except (ValueError, OSError, sqlite3.Error, psycopg.Error if psycopg else Exception) as exc:  # type: ignore[misc]
        print(f"build_catalog_release failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"build_catalog_release failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "status": report["status"],
        "release_no": report["release_no"],
        "artifact": report.get("artifact"),
        "report": report.get("report"),
        "release_counts": report.get("release_counts"),
        "blocking_error_count": len(report.get("blocking_errors", [])),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "draft" and not report.get("blocking_errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
