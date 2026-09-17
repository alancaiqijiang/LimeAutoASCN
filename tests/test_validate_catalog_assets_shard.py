from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from tools.merge_catalog_asset_shards import merge_reports
from tools.validate_catalog_assets_shard import validate_shard

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"


def make_release(tmp_path: Path, rows: list[tuple[str, str, int, str]]) -> tuple[Path, Path]:
    database = tmp_path / "release.sqlite"
    root = tmp_path / "assets"
    (root / "images").mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        """
        INSERT INTO catalog_releases (
            release_id, release_no, source_snapshot_fingerprint,
            source_counts_json, validation_summary_json
        ) VALUES ('r1', 'release-1', ?, '{}', '{}')
        """,
        ("a" * 64,),
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
        INSERT INTO catalog_parts (release_id, material_code, description)
        VALUES ('r1', 'P1', 'Part')
        """
    )
    content = b"shared asset bytes"
    (root / "images/shared.bin").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    for asset_key, object_key, size, asset_type in rows:
        connection.execute(
            """
            INSERT INTO catalog_assets (
                release_id, asset_key, material_code, system_node_key,
                asset_type, object_key, source_sha256, size_bytes, mime_type, status
            ) VALUES ('r1', ?, 'P1', NULL, ?, ?, ?, ?, NULL, 'ready')
            """,
            (asset_key, asset_type, object_key, digest, size),
        )
    connection.commit()
    connection.close()
    return database, root


def test_shard_checks_only_requested_rows_and_reuses_inode(tmp_path: Path) -> None:
    database, root = make_release(
        tmp_path,
        [
            ("a1", "images/shared.bin", len(b"shared asset bytes"), "other"),
            ("a2", "images/shared.bin", len(b"shared asset bytes"), "other"),
        ],
    )
    report = validate_shard(database, root, 1, 3, artifact_sha256="d" * 64)
    assert report["status"] == "pass"
    assert report["asset_summary"]["rows"] == 2
    assert report["asset_summary"]["pass"] == 2
    assert report["asset_summary"]["files_checked"] == 1
    assert report["asset_summary"]["inode_reused"] == 1
    assert report["database_total_rows"] == 2
    assert report["artifact_sha256"] == "d" * 64


def test_shard_reports_missing_file_without_scanning_other_rows(tmp_path: Path) -> None:
    database, root = make_release(
        tmp_path,
        [
            ("a1", "images/missing.bin", len(b"shared asset bytes"), "other"),
            ("a2", "images/shared.bin", len(b"shared asset bytes"), "other"),
        ],
    )
    report = validate_shard(database, root, 1, 2)
    assert report["status"] == "fail"
    assert report["asset_summary"]["rows"] == 1
    assert report["asset_summary"]["missing"] == 1
    assert report["blocking_error_count"] == 1
    assert report["asset_summary"]["pass"] == 0


def test_shard_rejects_empty_row_range(tmp_path: Path) -> None:
    database, root = make_release(
        tmp_path,
        [("a1", "images/shared.bin", len(b"shared asset bytes"), "other")],
    )
    report = validate_shard(database, root, 2, 3)
    assert report["status"] == "fail"
    assert report["asset_summary"]["rows"] == 0
    assert report["blocking_error_count"] == 1
    assert report["blocking_errors"][0]["problems"] == ["no_rows_in_range"]


def _shard(start: int, end: int, total: int = 2) -> dict:
    return {
        "schema_version": "limeauto-catalog-assets-shard.v1",
        "database": "/tmp/release.sqlite",
        "database_bytes": 100,
        "database_mtime_ns": 200,
        "artifact_sha256": "d" * 64,
        "asset_root": "/tmp/assets",
        "rowid_start": start,
        "rowid_end": end,
        "database_min_rowid": 1,
        "database_max_rowid": total,
        "database_total_rows": total,
        "asset_summary": {
            "rows": end - start,
            "pass": end - start,
            "fail": 0,
            "files_checked": end - start,
            "inode_reused": 0,
        },
        "blocking_errors": [],
        "blocking_error_count": 0,
        "status": "pass",
        "read_only": True,
    }


def test_merge_requires_contiguous_complete_coverage(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(_shard(1, 2)), encoding="utf-8")
    second.write_text(json.dumps(_shard(2, 3)), encoding="utf-8")
    result = merge_reports([first, second], require_complete=True)
    assert result["status"] == "pass"
    assert result["coverage_complete"] is True
    assert result["asset_summary"]["rows"] == 2
    assert result["range_errors"] == []


def test_merge_rejects_gap(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(_shard(1, 2, total=3)), encoding="utf-8")
    second.write_text(json.dumps(_shard(3, 4, total=3)), encoding="utf-8")
    result = merge_reports([first, second])
    assert result["status"] == "fail"
    assert any(error["kind"] == "gap" for error in result["range_errors"])
