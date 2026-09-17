#!/usr/bin/env python3
"""Profile qpren ``epc_nodes.node_path`` values into a JSON node contract report.

The tool is read-only by construction.  Every connection sets
``default_transaction_read_only=on`` in PostgreSQL connection options, and the
script contains no INSERT/UPDATE/DELETE/DDL statements.

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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.catalog_contract import build_catalog_report  # noqa: E402

try:  # psycopg is optional at import time only for humans reading this file.
    from psycopg import connect  # type: ignore[assignment]
    from psycopg.rows import dict_row  # type: ignore[assignment]
except ImportError:  # pragma: no cover - depends on the local environment
    connect = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


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


def parse_model_filter(value: str) -> tuple[str, str]:
    """Parse the CLI ``--model SERIES/MODEL`` value into two parameters."""
    pieces = value.split("/")
    if len(pieces) != 2 or not all(piece.strip() for piece in pieces):
        raise ValueError("--model must use the form SERIES/MODEL")
    return pieces[0].strip(), pieces[1].strip()


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
        "connection_options": "-c default_transaction_read_only=on",
        "server_version": version_row["server_version"],
        "current_database": database_row["current_database"],
        "current_user": current_user_row["current_user"],
        "transaction_read_only": session_row["transaction_read_only"],
        "default_transaction_read_only": default_row["default_transaction_read_only"],
    }


def query_profile_rows(connection: Any, model_filter: tuple[str, str] | None) -> list[dict[str, Any]]:
    """Query one ``epc_nodes`` row per source node with direct part counts."""
    where = ""
    params: list[str] = []
    if model_filter is not None:
        where = "WHERE en.series_code = %s AND en.model_code = %s"
        params.extend(model_filter)

    # The display name expression deliberately avoids trusting node_path as the
    # sole source; it falls back to tree_key and then obj_code.
    sql = f"""
        SELECT
            en.series_code,
            en.model_code,
            en.obj_code,
            en.tree_key,
            en.node_path,
            COALESCE(
                NULLIF(
                    regexp_replace(COALESCE(en.node_path, ''), '^.* > ', ''),
                    ''
                ),
                NULLIF(en.tree_key, ''),
                en.obj_code
            ) AS node_name,
            COUNT(o.id)::int AS direct_part_count
        FROM epc_nodes en
        LEFT JOIN occurrences o
          ON o.series_code = en.series_code
         AND o.model_code = en.model_code
         AND o.obj_code = en.obj_code
        {where}
        GROUP BY
            en.id,
            en.series_code,
            en.model_code,
            en.obj_code,
            en.tree_key,
            en.node_path
        ORDER BY en.series_code, en.model_code, en.node_path, en.obj_code
    """
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("LIMEAUTO_QPREN_READ_DSN", ""))
    parser.add_argument("--output", required=True, help="Destination JSON path")
    parser.add_argument(
        "--model",
        help="Optional SERIES/MODEL filter, for example HYE/HYEE-PZ02",
    )
    args = parser.parse_args(argv)

    if connect is None or dict_row is None:
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

    try:
        model_filter = parse_model_filter(args.model) if args.model else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_path = Path(args.output)
    try:
        with connect(
            args.dsn,
            connect_timeout=15,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on",
        ) as connection:
            connection.autocommit = True
            evidence = read_only_evidence(connection)
            rows = query_profile_rows(connection, model_filter)
            report = build_catalog_report(rows)
            payload = {
                "tool": "profile_qpren_nodes",
                "generated_at": utc_now(),
                "database": evidence,
                "source_row_count": len(rows),
                "model_filter": {
                    "series_code": model_filter[0],
                    "model_code": model_filter[1],
                }
                if model_filter
                else None,
                "contract_summary": report["summary"],
                "warnings": report["warnings"],
                "structural_conflicts": report.get("structural_conflicts", []),
                "blocking_errors": report["blocking_errors"],
                "nodes": report["nodes"],
            }
    except Exception as exc:
        print(f"profile_qpren_nodes failed: {exc}", file=sys.stderr)
        return 2

    atomic_write_json(output_path, payload)

    if report["blocking_errors"]:
        print(
            f"Wrote {output_path} but catalog contract reported "
            f"{len(report['blocking_errors'])} blocking error(s)",
            file=sys.stderr,
        )
        return 1

    print(f"Wrote {output_path} ({len(rows)} source rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
