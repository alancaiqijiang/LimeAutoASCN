from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"

EXPECTED_INDEX_COLUMNS = ("release_id", "material_code", "asset_type", "status")


def _index_columns(connection: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    return tuple(row[2] for row in sorted(rows, key=lambda row: row[0]))


def test_schema_defines_material_index_on_catalog_assets(tmp_path: Path) -> None:
    path = tmp_path / "release.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='catalog_assets'"
        )
    }
    assert "catalog_assets_material_idx" in names
    assert (
        _index_columns(connection, "catalog_assets_material_idx")
        == EXPECTED_INDEX_COLUMNS
    )


def test_parts_for_node_material_lookup_uses_material_index(tmp_path: Path) -> None:
    """The first-asset correlated subquery must search by (release_id, material_code)."""

    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    plan = connection.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT a.asset_key FROM catalog_assets a
        WHERE a.release_id = 'r1'
          AND a.material_code = 'P1'
          AND a.asset_type IN ('material_image', 'epc_drawing', 'thumbnail')
          AND a.status IN ('ready', 'done', 'available')
        ORDER BY CASE a.asset_type WHEN 'material_image' THEN 0 ELSE 1 END, a.asset_key
        LIMIT 1
        """
    ).fetchall()
    detail = " | ".join(row[3] for row in plan)
    assert "catalog_assets_material_idx" in detail, detail