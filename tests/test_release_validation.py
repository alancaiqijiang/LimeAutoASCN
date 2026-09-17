from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

from tools.validate_catalog_release import validate_release

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"


def make_valid_release(tmp_path: Path) -> Path:
    path = tmp_path / "release.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        """
        INSERT INTO catalog_releases (
            release_id, release_no, source_snapshot_fingerprint,
            source_counts_json, validation_summary_json
        ) VALUES ('r1', 'release-1', 'a' || printf('%064d', 0), '{}', '{}')
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
    connection.execute(
        """
        INSERT INTO system_nodes (
            release_id, node_key, series_code, model_code, source_obj_code,
            path_key, parent_key, node_path_source, name_source, display_name,
            depth, path_variant_count, direct_part_count
        ) VALUES ('r1', 'source:S:M:Engine:OBJ-1', 'S', 'M', 'OBJ-1',
                  'Engine', NULL, 'Engine', 'Engine', 'Engine', 1, 1, 1)
        """
    )
    connection.execute(
        "INSERT INTO catalog_parts (release_id, material_code, description) VALUES ('r1', 'P1', 'Part')"
    )
    connection.execute(
        """
        INSERT INTO fitments (
            release_id, source_occurrence_key, series_code, model_code,
            node_key, material_code, quantity, quantity_raw, fitment_level,
            review_status
        ) VALUES ('r1', 'occ-1', 'S', 'M', 'source:S:M:Engine:OBJ-1',
                  'P1', 1, '1', 'reference_only', 'pending')
        """
    )
    connection.commit()
    connection.close()
    return path


def test_valid_release_passes_read_only_validation(tmp_path: Path) -> None:
    report = validate_release(make_valid_release(tmp_path), "release-1")
    assert report["status"] == "pass"
    assert report["blocking_errors"] == []
    assert report["read_only"] is True
    assert report["published"] is False
    assert any(warning["kind"] == "asset_root_not_provided" for warning in report["warnings"])


def insert_asset(path: Path, *, asset_key: str = "asset-1", object_key: str = "images/a.bin",
                 material_code: str | None = "P1", system_node_key: str | None = None,
                 asset_type: str = "material_image", status: str = "ready",
                 size_bytes: int | None = None, source_sha256: str | None = None,
                 mime_type: str | None = None) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        """
        INSERT INTO catalog_assets (
            release_id, asset_key, material_code, system_node_key, asset_type,
            object_key, source_sha256, size_bytes, mime_type, status
        ) VALUES ('r1', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (asset_key, material_code, system_node_key, asset_type, object_key,
         source_sha256, size_bytes, mime_type, status),
    )
    connection.commit()
    connection.close()


def kinds(report: dict) -> set[str]:
    return {error["kind"] for error in report["blocking_errors"]}


def test_asset_binding_occurrence_and_empty_fields_are_blocking(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE fitments SET source_occurrence_key = ''")
    connection.commit()
    connection.close()
    insert_asset(path, asset_key="", object_key="", material_code="P1", asset_type="", status="")
    insert_asset(path, asset_key="both", material_code="P1", system_node_key="source:S:M:Engine:OBJ-1")
    insert_asset(path, asset_key="zero", material_code=None, system_node_key=None)
    report = validate_release(path)
    assert {"empty_source_occurrence_key", "empty_asset_field", "asset_binding_count"} <= kinds(report)


def test_illegal_object_keys_are_blocking(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    for index, object_key in enumerate(("/absolute", ".. /escape", "../escape", r"dir\\file", "https://example/a")):
        insert_asset(path, asset_key=f"bad-{index}", object_key=object_key)
    report = validate_release(path, asset_root=tmp_path)
    assert sum(error["kind"] == "unsafe_asset_object_key" for error in report["blocking_errors"]) == 5


def test_asset_root_checks_real_file(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    root = tmp_path / "assets"
    (root / "images").mkdir(parents=True)
    content = b"asset bytes"
    (root / "images/a.bin").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    insert_asset(path, size_bytes=len(content), source_sha256=digest)
    insert_asset(path, asset_key="missing", object_key="images/missing.bin")
    insert_asset(path, asset_key="size", size_bytes=len(content) + 1)
    insert_asset(path, asset_key="sha", source_sha256="0" * 64)
    insert_asset(path, asset_key="outside", object_key="../outside.bin")
    report = validate_release(path, asset_root=root)
    assert report["status"] == "fail"
    assert {"asset_file_missing", "asset_size_mismatch", "asset_sha256_mismatch", "unsafe_asset_object_key"} <= kinds(report)
    assert report["checks"]["assets"]["total"] == 5
    assert report["asset_checks"][0]["state"] == "pass"


def test_asset_root_rejects_mime_magic_mismatch(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    root = tmp_path / "assets"
    target = root / "images/bmp.jpg"
    target.parent.mkdir(parents=True)
    content = b"BM" + b"fixture"
    target.write_bytes(content)
    insert_asset(
        path,
        object_key="images/bmp.jpg",
        size_bytes=len(content),
        source_sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/jpeg",
    )

    report = validate_release(path, asset_root=root)

    assert report["status"] == "fail"
    assert "asset_mime_mismatch" in kinds(report)


def test_asset_evidence_limit_streams_all_assets_and_retains_failures(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    root = tmp_path / "assets"
    (root / "images").mkdir(parents=True)
    (root / "images/a.bin").write_bytes(b"asset a")
    (root / "images/b.bin").write_bytes(b"asset b")
    insert_asset(path, object_key="images/b.bin")
    insert_asset(path, asset_key="missing", object_key="images/missing.bin")

    report = validate_release(path, asset_root=root, asset_check_limit=1)

    assert report["checks"]["assets"]["total"] == 2
    assert report["checks"]["assets"]["pass"] == 1
    assert report["checks"]["assets"]["fail"] == 1
    assert report["checks"]["assets"]["not_checked"] == 0
    assert report["checks"]["assets"]["evidence_sample_limit"] == 1
    assert report["checks"]["assets"]["evidence_retained_count"] == 2
    assert {check["asset_key"] for check in report["asset_checks"]} == {"asset-1", "missing"}
    assert sum(check["state"] == "pass" for check in report["asset_checks"]) == 1
    assert report["asset_checks"][-1]["state"] == "fail"

    zero_limit_report = validate_release(path, asset_root=root, asset_check_limit=0)
    assert zero_limit_report["checks"]["assets"]["evidence_sample_limit"] == 0
    assert zero_limit_report["checks"]["assets"]["evidence_retained_count"] == 1
    assert [check["asset_key"] for check in zero_limit_report["asset_checks"]] == ["missing"]


def test_validated_release_requires_asset_root(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE catalog_releases SET status = 'validated'")
    connection.commit()
    connection.close()
    report = validate_release(path)
    assert "asset_root_required" in kinds(report)


def test_validated_release_rejects_non_publishable_asset_status(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    insert_asset(path, status="pending")
    connection = sqlite3.connect(path)
    connection.execute("UPDATE catalog_releases SET status = 'validated'")
    connection.commit()
    connection.close()
    report = validate_release(path)
    assert "asset_not_publishable" in kinds(report)


def test_missing_parent_is_blocking(tmp_path: Path) -> None:
    path = make_valid_release(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO system_nodes (
            release_id, node_key, series_code, model_code, source_obj_code,
            path_key, parent_key, node_path_source, name_source, display_name,
            depth, path_variant_count
        ) VALUES ('r1', 'source:S:M:Engine:Block:OBJ-2', 'S', 'M', 'OBJ-2',
                  'Engine>Block', 'Missing', 'Engine>Block', 'Block', 'Block', 2, 1)
        """
    )
    connection.commit()
    connection.close()
    report = validate_release(path)
    assert report["status"] == "fail"
    assert any(error["kind"] == "missing_parent_path" for error in report["blocking_errors"])


def test_report_is_json_serializable(tmp_path: Path) -> None:
    report = validate_release(make_valid_release(tmp_path))
    json.dumps(report, ensure_ascii=False)
