#!/usr/bin/env python3
"""Compare one SQLite release projection with read-only qpren evidence.

The comparison is evidence only. It never changes qpren, the release artifact,
a release status, or a current pointer. Source URLs and local paths are used
only transiently to reproduce asset identities and are never written to the
report.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_catalog_release import asset_identity  # noqa: E402

try:  # Optional for unit tests that exercise pure helpers only.
    import psycopg  # type: ignore[import-not-found]
    from psycopg.rows import dict_row  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - environment dependent
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
BATCH_SIZE = 5_000


class CompareError(RuntimeError):
    """Raised for a comparison precondition or source-query failure."""


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def parse_model_selector(value: str) -> tuple[str, str]:
    """Parse SERIES::MODEL (preferred) or the unambiguous last-slash form."""
    text = str(value).strip()
    if "::" in text:
        series_code, model_code = text.split("::", 1)
    elif "/" in text:
        series_code, model_code = text.rsplit("/", 1)
    else:
        raise ValueError("model selector must use SERIES::MODEL")
    if not series_code.strip() or not model_code.strip():
        raise ValueError("model selector must use non-empty series and model")
    return series_code.strip(), model_code.strip()


def classify_count(
    field: str,
    source_value: int,
    release_value: int,
    *,
    expected: bool = True,
    classification_if_different: str = "blocking_mismatch",
) -> dict[str, Any]:
    equal = int(source_value) == int(release_value)
    if equal:
        classification = "exact_projection"
    elif expected:
        classification = classification_if_different
    else:
        classification = "expected_projection"
    return {
        "field": field,
        "source": int(source_value),
        "release": int(release_value),
        "equal": equal,
        "classification": classification,
    }


def _model_predicate(filters: Sequence[tuple[str, str]], alias: str = "") -> tuple[str, list[str]]:
    if not filters:
        return "", []
    prefix = f"{alias}." if alias else ""
    pieces = [f"({prefix}series_code = %s AND {prefix}model_code = %s)" for _ in filters]
    return "WHERE " + " OR ".join(pieces), [value for pair in filters for value in pair]


def _row_value(row: Any, key: str, index: int) -> Any:
    """Read a value from either a psycopg mapping row or a tuple row."""
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None


def normalize_material_codes(material_codes: Iterable[Any]) -> frozenset[str]:
    """Normalize material codes for exact, Python-side membership checks."""
    normalized: set[str] = set()
    for value in material_codes:
        if value is None:
            continue
        code = str(value)
        if code.strip():
            normalized.add(code)
    return frozenset(normalized)


def filter_material_code_rows(
    rows: Iterable[Any], material_codes: Iterable[Any]
) -> Iterable[Any]:
    """Yield rows whose material code is an exact member of ``material_codes``."""
    codes = (
        material_codes
        if isinstance(material_codes, (set, frozenset))
        else normalize_material_codes(material_codes)
    )
    for row in rows:
        material_code = _row_value(row, "material_code", 0)
        if material_code is not None and str(material_code) in codes:
            yield row


@contextmanager
def _server_cursor(connection: Any, query: str) -> Iterator[Any]:
    """Execute a read-only query using a named cursor that is always closed."""
    cursor = connection.cursor(name=f"compare_rows_{uuid.uuid4().hex}")
    try:
        cursor.execute(query)
        yield cursor
    finally:
        cursor.close()


def _stream_matching_material_rows(
    connection: Any, query: str, material_codes: Iterable[Any]
) -> Iterable[Any]:
    """Stream a source table and filter material codes without SQL parameters."""
    codes = normalize_material_codes(material_codes)
    if not codes:
        return
    try:
        with _server_cursor(connection, query) as cursor:
            while True:
                batch = cursor.fetchmany(BATCH_SIZE)
                if not batch:
                    break
                yield from filter_material_code_rows(batch, codes)
    finally:
        connection.rollback()


def _count_source(connection: Any, table: str, filters: Sequence[tuple[str, str]]) -> int:
    where, params = _model_predicate(filters)
    row = connection.execute(
        f"SELECT COUNT(*) AS value FROM {table} {where}", params
    ).fetchone()
    return int(_row_value(row, "value", 0))


def _distinct_occurrence_materials(connection: Any, filters: Sequence[tuple[str, str]]) -> set[str]:
    where, params = _model_predicate(filters)
    rows = connection.execute(
        f"SELECT DISTINCT material_code FROM occurrences {where} "
        "AND material_code IS NOT NULL" if where else
        "SELECT DISTINCT material_code FROM occurrences WHERE material_code IS NOT NULL",
        params,
    ).fetchall()
    return {str(_row_value(row, "material_code", 0)) for row in rows}


def _source_parts(connection: Any, material_codes: set[str]) -> set[str]:
    rows = _stream_matching_material_rows(
        connection,
        "SELECT material_code FROM parts",
        material_codes,
    )
    return {str(_row_value(row, "material_code", 0)) for row in rows}


def _source_material_detail_count(connection: Any, material_codes: set[str]) -> int:
    return sum(
        1
        for _ in _stream_matching_material_rows(
            connection,
            "SELECT material_code FROM material_details",
            material_codes,
        )
    )


def _source_image_evidence(
    connection: Any,
    material_codes: set[str],
    release_no: str,
) -> dict[str, int]:
    identities: set[str] = set()
    rows = 0
    hash_missing = 0
    invalid_hash = 0
    for row in _stream_matching_material_rows(
        connection,
        "SELECT material_code, url, kind, sha256 FROM images",
        material_codes,
    ):
        rows += 1
        source_sha = _row_value(row, "sha256", 3)
        if source_sha is None or not str(source_sha).strip():
            hash_missing += 1
        try:
            identity = asset_identity(
                release_no,
                _row_value(row, "kind", 2),
                str(_row_value(row, "url", 1) or ""),
                source_sha,
            )
        except (TypeError, ValueError):
            invalid_hash += 1
            continue
        identities.add(str(identity["asset_key"]))
    return {
        "rows": rows,
        "identity_count": len(identities),
        "hash_missing": hash_missing,
        "invalid_hash": invalid_hash,
    }


def read_release(path: Path, release_no: str | None = None) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        releases = connection.execute("SELECT * FROM catalog_releases").fetchall()
        if len(releases) != 1:
            raise CompareError("release_metadata_invalid")
        release = dict(releases[0])
        if release_no is not None and release["release_no"] != release_no:
            raise CompareError("release_no_mismatch")
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
        per_model: dict[tuple[str, str], dict[str, int]] = {}
        models = connection.execute(
            "SELECT series_code, model_code FROM release_models ORDER BY series_code, model_code"
        ).fetchall()
        node_counts = {
            (str(row["series_code"]), str(row["model_code"])): {
                "system_nodes": int(row["system_nodes"]),
                "source_nodes": int(row["source_nodes"]),
            }
            for row in connection.execute(
                """
                SELECT series_code, model_code,
                       COUNT(*) AS system_nodes,
                       SUM(CASE WHEN is_derived = 0 THEN 1 ELSE 0 END) AS source_nodes
                FROM system_nodes
                WHERE release_id = ?
                GROUP BY series_code, model_code
                """,
                (release["release_id"],),
            ).fetchall()
        }
        fitment_counts = {
            (str(row["series_code"]), str(row["model_code"])): {
                "fitments": int(row["fitments"]),
                "material_count": int(row["material_count"]),
            }
            for row in connection.execute(
                """
                SELECT series_code, model_code,
                       COUNT(*) AS fitments,
                       COUNT(DISTINCT material_code) AS material_count
                FROM fitments
                WHERE release_id = ?
                GROUP BY series_code, model_code
                """,
                (release["release_id"],),
            ).fetchall()
        }
        asset_counts = {
            (str(row["series_code"]), str(row["model_code"])): int(row["asset_count"])
            for row in connection.execute(
                """
                SELECT f.series_code, f.model_code,
                       COUNT(DISTINCT a.asset_key) AS asset_count
                FROM (
                    SELECT DISTINCT release_id, series_code, model_code, material_code
                    FROM fitments
                    WHERE release_id = ?
                ) f
                JOIN catalog_assets a
                  ON a.release_id = f.release_id
                 AND a.material_code = f.material_code
                WHERE a.release_id = ?
                GROUP BY f.series_code, f.model_code
                """,
                (release["release_id"], release["release_id"]),
            ).fetchall()
        }
        for model in models:
            key = (str(model["series_code"]), str(model["model_code"]))
            per_model[key] = {
                **node_counts.get(key, {"system_nodes": 0, "source_nodes": 0}),
                **fitment_counts.get(key, {"fitments": 0, "material_count": 0}),
                "asset_count": asset_counts.get(key, 0),
            }
        return {
            "release": release,
            "counts": counts,
            "source_node_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM system_nodes WHERE is_derived=0"
                ).fetchone()[0]
            ),
            "derived_node_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM system_nodes WHERE is_derived=1"
                ).fetchone()[0]
            ),
            "per_model": per_model,
        }
    finally:
        connection.close()


def verify_read_only(connection: Any) -> None:
    transaction_row = connection.execute("SHOW transaction_read_only").fetchone()
    default_row = connection.execute(
        """
        SELECT current_setting('default_transaction_read_only')
            AS default_transaction_read_only
        """
    ).fetchone()
    transaction = str(_row_value(transaction_row, "transaction_read_only", 0)).strip().lower()
    default = str(
        _row_value(default_row, "default_transaction_read_only", 0)
    ).strip().lower()
    if transaction != "on" or default != "on":
        raise CompareError("read_only_session")


@contextmanager
def _qpren_connection(dsn: str) -> Iterator[Any]:
    """Open one explicitly read-only qpren connection and always roll it back."""
    if psycopg is None or dict_row is None:
        raise CompareError("psycopg_unavailable")
    with psycopg.connect(
        dsn,
        connect_timeout=20,
        row_factory=dict_row,
        options=READ_ONLY_OPTIONS,
    ) as connection:
        connection.autocommit = False
        try:
            verify_read_only(connection)
            yield connection
        finally:
            connection.rollback()


def compare_release(
    database: Path,
    dsn: str,
    filters: Sequence[tuple[str, str]],
    representatives: Sequence[tuple[str, str]],
    release_no: str | None = None,
) -> dict[str, Any]:
    release_data = read_release(database, release_no)
    release = release_data["release"]
    with _qpren_connection(dsn) as connection:
        material_codes = _distinct_occurrence_materials(connection, filters)
        found_parts = _source_parts(connection, material_codes)
        source = {
            "model_versions": _count_source(connection, "model_versions", filters),
            "epc_nodes": _count_source(connection, "epc_nodes", filters),
            "occurrences": _count_source(connection, "occurrences", filters),
            "parts": len(found_parts),
            "material_details": 0,
        }
        if found_parts:
            source["material_details"] = _source_material_detail_count(connection, found_parts)
        image_evidence = _source_image_evidence(connection, found_parts, str(release["release_no"]))
        source.update(
            {
                "images_material_bound": image_evidence["rows"],
                "image_identity_count": image_evidence["identity_count"],
                "image_hash_missing": image_evidence["hash_missing"],
                "image_invalid_hash": image_evidence["invalid_hash"],
            }
        )
        connection.rollback()

        representative_results: list[dict[str, Any]] = []
        for pair in representatives:
            model_exists = _count_source(connection, "model_versions", [pair])
            source_nodes = _count_source(connection, "epc_nodes", [pair])
            source_occurrences = _count_source(connection, "occurrences", [pair])
            model_materials = _distinct_occurrence_materials(connection, [pair])
            model_parts = len(_source_parts(connection, model_materials))
            artifact_model = release_data["per_model"].get(pair)
            representative_results.append(
                {
                    "series_code": pair[0],
                    "model_code": pair[1],
                    "source": {
                        "model_versions": model_exists,
                        "epc_nodes": source_nodes,
                        "occurrences": source_occurrences,
                        "parts": model_parts,
                    },
                    "release": artifact_model,
                    "status": "pass" if model_exists == 1 and artifact_model else "missing_scope",
                }
            )
            connection.rollback()

    release_counts = release_data["counts"]
    comparisons = [
        classify_count("models", source["model_versions"], release_counts["release_models"]),
        classify_count(
            "source_nodes",
            source["epc_nodes"],
            release_data["source_node_count"],
        ),
        classify_count("occurrences_fitments", source["occurrences"], release_counts["fitments"]),
        classify_count("parts", source["parts"], release_counts["catalog_parts"]),
        classify_count(
            "material_details",
            source["material_details"],
            source["material_details"],
            expected=False,
        ),
        classify_count(
            "image_identity_projection",
            source["image_identity_count"],
            release_counts["catalog_assets"],
            expected=True,
            classification_if_different="asset_projection_mismatch",
        ),
        {
            "field": "derived_navigation_nodes",
            "source": 0,
            "release": release_data["derived_node_count"],
            "equal": release_data["derived_node_count"] == 0,
            "classification": "derived_navigation",
        },
        {
            "field": "material_bound_image_rows",
            "source": source["images_material_bound"],
            "release": release_counts["catalog_assets"],
            "equal": False,
            "classification": "expected_projection_deduplicated",
        },
    ]
    blocking = [
        item
        for item in comparisons
        if item["classification"] in {"blocking_mismatch", "asset_projection_mismatch"}
        and not item["equal"]
    ]
    return {
        "tool": "compare_release_to_qpren",
        "release_no": release["release_no"],
        "release_status": release["status"],
        "scope_models": [list(pair) for pair in filters],
        "read_only": True,
        "source": source,
        "release_counts": release_counts,
        "comparisons": comparisons,
        "representatives": representative_results,
        "blocking_mismatches": blocking,
        "status": "pass" if not blocking else "fail",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--release-no")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Representative selector SERIES::MODEL; repeatable. Use :: for codes containing /.",
    )
    args = parser.parse_args(argv)
    try:
        filters = [parse_model_selector(value) for value in args.model]
        representatives = filters or [("HYE", "HYEE-PZ02"), ("SA2HG/K", "EV/中文%")]
        report = compare_release(args.database, args.dsn, filters, representatives, args.release_no)
        atomic_write_json(args.output, report)
    except Exception:
        report = {
            "tool": "compare_release_to_qpren",
            "status": "fail",
            "read_only": True,
            "blocking_mismatches": [
                {"kind": "comparison_failed", "reason": "comparison_failed"}
            ],
        }
        atomic_write_json(args.output, report)
        print(json.dumps({"status": "fail", "report": str(args.output)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "release_no": report["release_no"],
                "report": str(args.output),
                "blocking_mismatch_count": len(report["blocking_mismatches"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
