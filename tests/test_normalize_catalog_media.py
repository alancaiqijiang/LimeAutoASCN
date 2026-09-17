from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from tools.normalize_catalog_media import detect_mime, normalize

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"


def test_detect_mime_recognizes_bmp() -> None:
    path = Path("/tmp/limeauto-bmp-mime-fixture")
    try:
        path.write_bytes(b"BM" + b"fixture")
        assert detect_mime(path) == "image/bmp"
    finally:
        path.unlink(missing_ok=True)


def test_normalize_creates_new_release_without_mutating_source(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    source_root = tmp_path / "source-assets"
    old_key = "release/old/assets/material_image/bmp-content.jpg"
    source_file = source_root / old_key
    content = b"BM" + b"bmp-fixture"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    connection = sqlite3.connect(source_db)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO catalog_releases (release_id, release_no, source_snapshot_fingerprint) VALUES ('r1', 'old', 'fp')"
    )
    connection.execute(
        "INSERT INTO release_models (release_id, series_code, model_code) VALUES ('r1', 'S', 'M')"
    )
    connection.execute(
        "INSERT INTO catalog_parts (release_id, material_code) VALUES ('r1', 'P')"
    )
    connection.execute(
        """
        INSERT INTO catalog_assets (
            release_id, asset_key, material_code, asset_type, object_key,
            source_sha256, size_bytes, mime_type, status
        ) VALUES ('r1', 'material_image:one', 'P', 'material_image', ?, ?, ?,
                  'image/jpeg', 'done')
        """,
        (old_key, digest, len(content)),
    )
    connection.commit()
    connection.close()

    output_db = tmp_path / "normalized.sqlite"
    output_root = tmp_path / "normalized-assets"
    report_path = tmp_path / "normalize.json"
    result = normalize(source_db, source_root, output_db, output_root, report_path)

    assert result["status"] == "pass"
    assert result["normalized_rows"] == 1
    assert result["physical_files"] == 1
    assert source_file.exists()
    assert not (source_root / "release/old/assets/material_image/bmp-content.bmp").exists()

    new_key = "release/old/assets/material_image/bmp-content.bmp"
    normalized_file = output_root / new_key
    assert normalized_file.is_file()
    assert normalized_file.stat().st_nlink >= 2
    with sqlite3.connect(output_db) as check:
        row = check.execute(
            "SELECT object_key, mime_type, source_sha256, size_bytes FROM catalog_assets"
        ).fetchone()
    assert row == (new_key, "image/bmp", digest, len(content))
    assert json.loads(report_path.read_text())["normalized_rows"] == 1
