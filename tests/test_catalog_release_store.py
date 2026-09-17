from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from app.catalog_release import (
    RELEASE_MAX_PARTS_LIMIT,
    RELEASE_MAX_PARTS_OFFSET,
    CatalogReleaseError,
    CatalogReleaseStore,
    _material_code_keys,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"


def make_release(tmp_path: Path) -> Path:
    path = tmp_path / "release.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        """
        INSERT INTO catalog_releases (
            release_id, release_no, source_snapshot_fingerprint,
            source_counts_json, validation_summary_json
        ) VALUES ('r1', 'release-1', 'fingerprint', '{}', '{}')
        """
    )
    connection.execute(
        """
        INSERT INTO release_models (
            release_id, series_code, model_code, series_name_source,
            model_name_source
        ) VALUES ('r1', 'S', 'M', 'Series', 'Model')
        """
    )
    connection.executemany(
        """
        INSERT INTO system_nodes (
            release_id, node_key, series_code, model_code, source_obj_code,
            path_key, parent_key, node_path_source, name_source, display_name,
            depth, path_variant_count, child_count, direct_part_count
        ) VALUES ('r1', ?, 'S', 'M', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("source:S:M:Engine:OBJ-1", "OBJ-1", "Engine", None, "Engine", "Engine", "Engine", 1, 2, 1, 1),
            ("source:S:M:Engine:OBJ-2", "OBJ-2", "Engine", None, "Engine", "Engine", "Engine", 1, 2, 1, 1),
            ("source:S:M:Engine>Cooling:OBJ-3", "OBJ-3", "Engine>Cooling", "Engine", "Engine>Cooling", "Cooling", "Cooling", 2, 1, 1, 1),
        ],
    )
    connection.execute(
        "INSERT INTO catalog_parts (release_id, material_code, display_name_source, description) VALUES ('r1', 'P1', 'Part one', 'Part one')"
    )
    connection.execute(
        "INSERT INTO catalog_parts (release_id, material_code, display_name_source, description) VALUES ('r1', 'P2', 'Part two', 'Part two')"
    )
    connection.executemany(
        """
        INSERT INTO fitments (
            release_id, source_occurrence_key, series_code, model_code,
            node_key, material_code, callout, quantity, quantity_raw,
            fitment_level, review_status
        ) VALUES ('r1', ?, 'S', 'M', ?, ?, ?, ?, ?, 'reference_only', 'pending')
        """,
        [
            ("occ-1", "source:S:M:Engine:OBJ-1", "P1", "1", 1, "1"),
            ("occ-2", "source:S:M:Engine:OBJ-2", "P2", "2", None, "2-4"),
            ("occ-3", "source:S:M:Engine>Cooling:OBJ-3", "P1", "3", 1, "1"),
        ],
    )
    connection.execute(
        """
        INSERT INTO catalog_assets (
            release_id, asset_key, material_code, asset_type,
            object_key, source_sha256, size_bytes, mime_type, status
        ) VALUES ('r1', 'material_image:one', 'P1', 'material_image',
                  'release/release-1/assets/material_image/one.jpg',
                  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                  10, 'image/jpeg', 'done')
        """
    )
    connection.commit()
    connection.close()
    return path


def test_release_store_queries_are_release_and_context_scoped(tmp_path: Path) -> None:
    path = make_release(tmp_path)
    store = CatalogReleaseStore(path, release_no="release-1", allow_draft=True)

    assert store.release_metadata()["status"] == "draft"
    assert store.catalog_counts()["fitments"] == 3
    assert store.list_series()[0]["series_code"] == "S"
    assert store.list_models()[0]["model_code"] == "M"
    assert store.models_for_series("S")[0]["model_code"] == "M"
    assert len(store.root_nodes_for_model("S", "M")) == 2

    children = store.children_for_node("S", "M", "source:S:M:Engine:OBJ-1")
    assert [node["node_key"] for node in children] == ["source:S:M:Engine>Cooling:OBJ-3"]

    page = store.parts_for_node("S", "M", "source:S:M:Engine:OBJ-1", limit=1)
    assert page["total"] == 1
    assert page["has_next"] is False
    assert page["items"][0]["material_code"] == "P1"
    assert page["items"][0]["material_object_key"].startswith("release/")
    assert "local_path" not in page["items"][0]

    context = store.part_context("S", "M", "source:S:M:Engine:OBJ-1", "P1")
    assert context and context["source_occurrence_key"] == "occ-1"
    assert store.part_context("S", "M", "source:S:M:Engine:OBJ-1", "P2") is None
    assert store.asset_for_part("P1")["asset_type"] == "material_image"


def test_node_assets_are_scoped_and_filter_to_publishable_epc_types(tmp_path: Path) -> None:
    path = make_release(tmp_path)
    connection = sqlite3.connect(path)
    connection.executemany(
        """
        INSERT INTO catalog_assets (
            release_id, asset_key, system_node_key, asset_type,
            object_key, mime_type, status
        ) VALUES ('r1', ?, ?, ?, ?, 'image/svg+xml', ?)
        """,
        [
            (
                "node-drawing-done",
                "source:S:M:Engine:OBJ-1",
                "epc_drawing",
                "release/release-1/assets/node-drawing-done.svg",
                "done",
            ),
            (
                "node-drawing-ready",
                "source:S:M:Engine:OBJ-1",
                "epc_drawing",
                "release/release-1/assets/node-drawing-ready.svg",
                "ready",
            ),
            (
                "node-thumb-available",
                "source:S:M:Engine:OBJ-1",
                "thumbnail",
                "release/release-1/assets/node-thumb-available.png",
                "available",
            ),
            (
                "node-drawing-pending",
                "source:S:M:Engine:OBJ-1",
                "epc_drawing",
                "release/release-1/assets/node-drawing-pending.svg",
                "pending",
            ),
            (
                "node-other-done",
                "source:S:M:Engine:OBJ-1",
                "other",
                "release/release-1/assets/node-other-done.bin",
                "done",
            ),
            (
                "other-node-drawing",
                "source:S:M:Engine:OBJ-2",
                "epc_drawing",
                "release/release-1/assets/other-node-drawing.svg",
                "done",
            ),
        ],
    )
    connection.commit()
    connection.close()

    store = CatalogReleaseStore(path, release_no="release-1", allow_draft=True)
    assets = store.assets_for_node("S", "M", "source:S:M:Engine:OBJ-1")

    assert assets == [
        {"asset_key": "node-drawing-done", "asset_type": "epc_drawing"},
        {"asset_key": "node-drawing-ready", "asset_type": "epc_drawing"},
        {"asset_key": "node-thumb-available", "asset_type": "thumbnail"},
    ]
    assert store.assets_for_node("S", "M", "source:S:M:Engine:OBJ-2") == [
        {"asset_key": "other-node-drawing", "asset_type": "epc_drawing"}
    ]
    assert store.assets_for_node("OTHER", "M", "source:S:M:Engine:OBJ-1") == []
    assert all("object_key" not in asset for asset in assets)


def test_release_store_rejects_draft_without_explicit_development_flag(tmp_path: Path) -> None:
    with pytest.raises(CatalogReleaseError):
        CatalogReleaseStore(make_release(tmp_path))


def test_release_store_rejects_cross_model_node_context(tmp_path: Path) -> None:
    store = CatalogReleaseStore(make_release(tmp_path), allow_draft=True)
    assert store.node_by_key("OTHER", "M", "source:S:M:Engine:OBJ-1") is None
    assert store.part_context("OTHER", "M", "source:S:M:Engine:OBJ-1", "P1") is None


def test_release_store_bounds_pagination_before_sqlite(tmp_path: Path) -> None:
    store = CatalogReleaseStore(make_release(tmp_path), allow_draft=True)
    page = store.parts_for_node(
        "S",
        "M",
        "source:S:M:Engine:OBJ-1",
        limit=10**60,
        offset=10**60,
    )
    assert page["limit"] == RELEASE_MAX_PARTS_LIMIT
    assert page["offset"] == RELEASE_MAX_PARTS_OFFSET
    with pytest.raises(ValueError):
        store.parts_for_node("S", "M", "source:S:M:Engine:OBJ-1", limit="invalid")
    with pytest.raises(ValueError):
        store.parts_for_node("S", "M", "source:S:M:Engine:OBJ-1", offset=-1)


def test_search_parts_matches_code_then_name_without_short_query(tmp_path: Path) -> None:
    store = CatalogReleaseStore(make_release(tmp_path), release_no="release-1", allow_draft=True)
    by_code = store.search_parts(material_code="P1")
    assert by_code["total"] == 2
    assert {row["material_code"] for row in by_code["items"]} == {"P1"}

    by_name = store.search_parts(material_name="Part two")
    assert by_name["total"] == 1
    assert by_name["items"][0]["material_code"] == "P2"

    missing = store.search_parts(material_name="no-such-part")
    assert missing["total"] == 0
    assert missing["items"] == []

    with pytest.raises(ValueError):
        store.search_parts(material_name="P")

    assert store.search_parts(material_code="p1")["total"] == 2
    assert store.search_parts(material_code="  P1  ")["total"] == 2
    assert store.search_parts(material_code="P1", series_code="S")["total"] == 2
    assert store.search_parts(material_code="P1", series_code="NOPE")["total"] == 0
    assert store.search_parts(material_code="P1", material_name="Part two")["total"] == 0
    assert _material_code_keys("18370426-00") == ["18370426-00"]
    assert _material_code_keys("p1") == ["p1", "P1"]
    source = inspect.getsource(CatalogReleaseStore.search_parts)
    assert "f.material_code COLLATE" not in source
    assert "COLLATE NOCASE" in source
