#!/usr/bin/env python3
"""Audit the read-only qpren PostgreSQL source used by the catalog adapter.

The script never writes to qpren.  It records live row counts, table columns
and sizes, explicit referential-integrity orphan counts, and bounded local file
checks for ``tasks.raw_path`` and ``images.local_path`` (when those columns
exist and file checks are enabled).

Usage examples are documented in ``tools/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # psycopg is optional at import time only for humans reading this file.
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - depends on the local environment
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]

CORE_TABLES = ("model_versions", "epc_nodes", "occurrences", "parts", "images")
MAX_JSON_BYTES = 16 * 1024 * 1024
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write UTF-8 JSON to ``path`` atomically with ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
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
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_only_evidence(connection: Any) -> dict[str, Any]:
    """Collect lightweight server/session read-only evidence."""
    version_row = connection.execute("SELECT version() AS server_version").fetchone()
    database_row = connection.execute("SELECT current_database() AS current_database").fetchone()
    current_user_row = connection.execute("SELECT current_user AS current_user").fetchone()
    session_row = connection.execute("SHOW transaction_read_only").fetchone()
    default_row = connection.execute(
        "SELECT current_setting('default_transaction_read_only') AS default_transaction_read_only"
    ).fetchone()

    return {
        "connection_options": READ_ONLY_OPTIONS,
        "server_version": version_row["server_version"],
        "current_database": database_row["current_database"],
        "current_user": current_user_row["current_user"],
        "transaction_read_only": session_row["transaction_read_only"],
        "default_transaction_read_only": default_row["default_transaction_read_only"],
    }


def _table_count(connection: Any, table_name: str) -> int:
    query = sql.SQL("SELECT COUNT(*) AS row_count FROM {}").format(sql.Identifier(table_name))
    row = connection.execute(query).fetchone()
    return int(row["row_count"])


def _table_columns(connection: Any, table_name: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT column_name, data_type, is_nullable, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    ).fetchall()
    return [dict(row) for row in rows]


def _table_size_bytes(connection: Any, table_name: str) -> int:
    # ``regclass`` expects a SQL literal containing the relation name; an
    # Identifier would be parsed as a column reference ("table"::regclass).
    query = sql.SQL("SELECT pg_total_relation_size({}::regclass) AS size_bytes").format(
        sql.Literal(table_name)
    )
    return int(connection.execute(query).fetchone()["size_bytes"])


def audit_core_tables(connection: Any) -> dict[str, Any]:
    """Record columns, counts, and sizes for the five catalog core tables."""
    tables: dict[str, Any] = {}
    table_errors: list[dict[str, Any]] = []
    for table_name in CORE_TABLES:
        entry: dict[str, Any] = {"columns": []}
        try:
            entry["columns"] = _table_columns(connection, table_name)
        except Exception as exc:
            table_errors.append(
                {
                    "table": table_name,
                    "operation": "columns",
                    "message": str(exc),
                }
            )
        try:
            entry["row_count"] = _table_count(connection, table_name)
        except Exception as exc:
            table_errors.append(
                {
                    "table": table_name,
                    "operation": "row_count",
                    "message": str(exc),
                }
            )
        try:
            entry["size_bytes"] = _table_size_bytes(connection, table_name)
        except Exception as exc:
            table_errors.append(
                {
                    "table": table_name,
                    "operation": "size_bytes",
                    "message": str(exc),
                }
            )
        tables[table_name] = entry
    return {"tables": tables, "errors": table_errors}


ORPHAN_CHECKS: tuple[dict[str, str], ...] = (
    {
        "name": "occurrence_to_part",
        "sql": """
            SELECT COUNT(*) AS orphan_count
            FROM occurrences o
            WHERE NOT EXISTS (
                SELECT 1 FROM parts p WHERE p.material_code = o.material_code
            )
        """,
    },
    {
        "name": "occurrence_to_model_versions",
        "sql": """
            SELECT COUNT(*) AS orphan_count
            FROM occurrences o
            WHERE NOT EXISTS (
                SELECT 1 FROM model_versions mv
                WHERE mv.series_code = o.series_code
                  AND mv.model_code = o.model_code
            )
        """,
    },
    {
        "name": "occurrence_to_epc_nodes",
        "sql": """
            SELECT COUNT(*) AS orphan_count
            FROM occurrences o
            WHERE NOT EXISTS (
                SELECT 1 FROM epc_nodes en
                WHERE en.series_code = o.series_code
                  AND en.model_code = o.model_code
                  AND en.obj_code = o.obj_code
            )
        """,
    },
    {
        "name": "material_details_to_parts",
        "sql": """
            SELECT COUNT(*) AS orphan_count
            FROM material_details md
            WHERE NOT EXISTS (
                SELECT 1 FROM parts p WHERE p.material_code = md.material_code
            )
        """,
    },
    {
        "name": "images_to_parts",
        "sql": """
            SELECT COUNT(*) AS orphan_count
            FROM images i
            WHERE i.material_code IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM parts p WHERE p.material_code = i.material_code
            )
        """,
    },
)


def audit_image_asset_counts(connection: Any) -> dict[str, int]:
    """Record image totals and material binding counts independently of orphans."""
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS image_count,
            COUNT(*) FILTER (WHERE material_code IS NOT NULL) AS material_bound_count,
            COUNT(*) FILTER (WHERE material_code IS NULL) AS null_material_code_count
        FROM images
        """
    ).fetchone()
    return {
        "image_count": int(row["image_count"]),
        "material_bound_count": int(row["material_bound_count"]),
        "null_material_code_count": int(row["null_material_code_count"]),
    }


def audit_orphan_checks(connection: Any) -> dict[str, Any]:
    """Run explicit orphan checks and always return an explicit count/error."""
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for check in ORPHAN_CHECKS:
        try:
            row = connection.execute(check["sql"]).fetchone()
            count = int(row["orphan_count"])
            checks.append({"name": check["name"], "orphan_count": count, "status": "ok"})
            if count != 0:
                errors.append(
                    {
                        "name": check["name"],
                        "orphan_count": count,
                        "message": "orphan count is nonzero",
                    }
                )
        except Exception as exc:
            checks.append(
                {
                    "name": check["name"],
                    "orphan_count": None,
                    "status": "error",
                    "message": str(exc),
                }
            )
            errors.append({"name": check["name"], "message": str(exc)})
    return {"checks": checks, "errors": errors}


def _column_names(connection: Any, table_name: str) -> set[str]:
    rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        (table_name,),
    ).fetchall()
    return {str(row["column_name"]) for row in rows}


def _candidate_size_column(columns: set[str]) -> str | None:
    for candidate in ("raw_size", "file_size", "size", "raw_bytes", "file_bytes"):
        if candidate in columns:
            return candidate
    return None


def _path_candidate(raw_path: Any, asset_root: Path | None) -> Path | None:
    if raw_path is None:
        return None
    text = str(raw_path).strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute() and asset_root is not None:
            candidate = asset_root / candidate
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _size_as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_file_is_valid(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return False
        with path.open("rb") as handle:
            json.load(handle)
        return True
    except (OSError, ValueError):
        return False


def _file_check(
    connection: Any,
    *,
    table_name: str,
    path_column: str,
    asset_root: Path | None,
    report_json: bool,
) -> dict[str, Any]:
    columns = _column_names(connection, table_name)
    if path_column not in columns:
        return {
            "enabled": True,
            "skipped_reason": f"column {path_column!r} not found in {table_name}",
        }

    size_column = _candidate_size_column(columns)
    query_columns: list[Any] = [sql.Identifier(path_column)]
    if size_column is not None:
        query_columns.append(sql.Identifier(size_column))

    total_row = connection.execute(
        sql.SQL("SELECT COUNT(*) AS total FROM {}").format(sql.Identifier(table_name))
    ).fetchone()
    total = int(total_row["total"])
    empty_row = connection.execute(
        sql.SQL("SELECT COUNT(*) AS empty_count FROM {} WHERE {} IS NULL OR btrim({}) = ''").format(
            sql.Identifier(table_name),
            sql.Identifier(path_column),
            sql.Identifier(path_column),
        )
    ).fetchone()
    empty = int(empty_row["empty_count"])

    missing = 0
    valid = 0
    size_mismatch = 0
    json_checked = 0
    invalid_json = 0

    select_sql = sql.SQL("SELECT {} FROM {} WHERE {} IS NOT NULL AND btrim({}) <> ''").format(
        sql.SQL(", ").join(query_columns),
        sql.Identifier(table_name),
        sql.Identifier(path_column),
        sql.Identifier(path_column),
    )

    with connection.cursor(name=f"audit_{table_name}_files") as cursor:
        cursor.execute(select_sql)
        for row in cursor:
            raw_path = row[0]
            size_meta = row[1] if size_column is not None else None
            path = _path_candidate(raw_path, asset_root)
            if path is None or not path.is_file():
                missing += 1
                continue
            valid += 1
            expected_size = _size_as_int(size_meta)
            if expected_size is not None:
                try:
                    actual_size = path.stat().st_size
                except OSError:
                    actual_size = -1
                if actual_size != expected_size:
                    size_mismatch += 1
            if report_json and path.name.lower().endswith(".json"):
                json_checked += 1
                if not _json_file_is_valid(path):
                    invalid_json += 1

    checked = total - empty
    return {
        "enabled": True,
        "path_column": path_column,
        "size_column": size_column,
        "reference_count": total,
        "empty": empty,
        "checked": checked,
        "missing": missing,
        "valid": valid,
        "size_mismatch": size_mismatch,
        "json_checked": json_checked,
        "invalid_json": invalid_json,
    }


def audit_file_checks(connection: Any, asset_root: Path | None, enabled: bool) -> dict[str, Any]:
    """Stream local file references without loading the whole table in memory."""
    if not enabled:
        return {"enabled": False, "reason": "--skip-files"}

    results: dict[str, Any] = {
        "enabled": True,
        "asset_root": str(asset_root) if asset_root is not None else None,
        "tables": {},
    }
    try:
        results["tables"]["tasks"] = _file_check(
            connection,
            table_name="tasks",
            path_column="raw_path",
            asset_root=asset_root,
            report_json=True,
        )
    except Exception as exc:
        results["tables"]["tasks"] = {
            "enabled": True,
            "status": "error",
            "message": str(exc),
        }

    try:
        results["tables"]["images"] = _file_check(
            connection,
            table_name="images",
            path_column="local_path",
            asset_root=asset_root,
            report_json=False,
        )
    except Exception as exc:
        results["tables"]["images"] = {
            "enabled": True,
            "status": "error",
            "message": str(exc),
        }

    return results


def _error_records(items: Iterable[dict[str, Any]], *, kind: str) -> list[dict[str, Any]]:
    return [{"kind": kind, **item} for item in items]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("LIMEAUTO_QPREN_READ_DSN", ""))
    parser.add_argument("--output", required=True, help="Destination JSON path")
    parser.add_argument(
        "--asset-root",
        default=None,
        help="Base directory for relative task/image paths; absolute paths remain unchanged",
    )
    parser.add_argument(
        "--skip-files",
        action="store_true",
        help="Disable local filesystem checks",
    )
    args = parser.parse_args(argv)

    if psycopg is None or sql is None:
        print(
            "psycopg[binary] is required. Install project requirements and retry.",
            file=sys.stderr,
        )
        return 2

    if not args.dsn:
        print(
            "--dsn is required when LIMEAUTO_QPREN_READ_DSN is not set",
            file=sys.stderr,
        )
        return 2

    asset_root = Path(args.asset_root).expanduser().resolve() if args.asset_root else None
    output_path = Path(args.output)
    file_checks_enabled = not args.skip_files

    try:
        with psycopg.connect(
            args.dsn,
            connect_timeout=15,
            row_factory=dict_row,
            options=READ_ONLY_OPTIONS,
        ) as connection:
            connection.autocommit = True
            evidence = read_only_evidence(connection)
            core = audit_core_tables(connection)
            orphans = audit_orphan_checks(connection)
            image_asset_counts = audit_image_asset_counts(connection)
            files = audit_file_checks(connection, asset_root, file_checks_enabled)
            payload = {
                "tool": "audit_qpren_source",
                "generated_at": utc_now(),
                "database": evidence,
                "core_tables": core,
                "orphan_checks": orphans,
                "image_asset_counts": image_asset_counts,
                "file_checks": files,
                "blocking_errors": [
                    *_error_records(core["errors"], kind="core_table_audit_error"),
                    *_error_records(orphans["errors"], kind="orphan_integrity_error"),
                ],
            }
    except Exception as exc:
        print(f"audit_qpren_source failed: {exc}", file=sys.stderr)
        return 2

    atomic_write_json(output_path, payload)

    if payload["blocking_errors"]:
        print(
            f"Wrote {output_path} but audit found "
            f"{len(payload['blocking_errors'])} blocking database error(s)",
            file=sys.stderr,
        )
        return 1

    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
