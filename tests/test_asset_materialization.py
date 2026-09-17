from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import tools.materialize_catalog_assets as materializer
from tools.build_catalog_release import asset_identity
from tools.materialize_catalog_assets import (
    AssetSpec,
    ReleaseManifest,
    materialize_assets,
    read_release_manifest,
    resolve_source_path,
    verify_read_only_session,
)


def _fixture(
    tmp_path: Path,
    *,
    content: bytes = b"n6-image-bytes",
    local_path: str = "/var/lib/qpren/images/material-1.jpg",
    url: str = "https://private.example.invalid/images/material-1.jpg",
    expected_content: bytes | None = None,
) -> tuple[ReleaseManifest, list[dict[str, object]], Path, Path, bytes, str]:
    release_no = "rayah-n6-test"
    source_root = tmp_path / "source-root"
    output_root = tmp_path / "output-root"
    source_path = source_root / "images" / "material-1.jpg"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(content)

    expected = content if expected_content is None else expected_content
    expected_sha256 = hashlib.sha256(expected).hexdigest()
    identity = asset_identity("rayah-n6-test", "material", url, expected_sha256)
    asset = AssetSpec(
        asset_key=identity["asset_key"],
        material_code="MAT-1",
        system_node_key=None,
        asset_type=identity["asset_type"],
        object_key=identity["object_key"],
        source_sha256=expected_sha256,
        size_bytes=len(expected),
        mime_type="image/jpeg",
        status="ready",
    )
    manifest = ReleaseManifest(
        release_no=release_no,
        release_id="release-id-1",
        assets=(asset,),
        node_material_codes={},
    )
    rows: list[dict[str, object]] = [
        {
            "url": url,
            "kind": "material",
            "material_code": "MAT-1",
            "name": "material-1.jpg",
            "local_path": local_path,
            "sha256": expected_sha256,
            "size_bytes": len(expected),
            "status": "ready",
        }
    ]
    return manifest, rows, source_root, output_root, expected, expected_sha256


def _destination(manifest: ReleaseManifest, output_root: Path) -> Path:
    return output_root / manifest.assets[0].object_key


def test_success_copies_to_exact_object_key(tmp_path: Path) -> None:
    manifest, rows, source_root, output_root, expected, _ = _fixture(tmp_path)

    report = materialize_assets(manifest, rows, source_root, output_root)

    assert report["status"] == "materialized"
    assert report["counts"] == {"expected": 1, "materialized": 1, "reused": 0, "failed": 0}
    assert _destination(manifest, output_root).read_bytes() == expected
    assert (
        output_root / "release" / manifest.release_no
    ).is_dir()
    assert report["assets"][0]["object_key"] == manifest.assets[0].object_key


def test_existing_complete_release_is_reused_without_source_rows(tmp_path: Path) -> None:
    manifest, rows, source_root, output_root, expected, _ = _fixture(tmp_path)
    first = materialize_assets(manifest, rows, source_root, output_root)
    assert first["status"] == "materialized"

    source_path = source_root / "images" / "material-1.jpg"
    source_path.unlink()
    second = materialize_assets(manifest, [], source_root, output_root)

    assert second["status"] == "reused"
    assert second["counts"] == {"expected": 1, "materialized": 0, "reused": 1, "failed": 0}
    assert _destination(manifest, output_root).read_bytes() == expected


def test_missing_source_fails_without_final_directory(tmp_path: Path) -> None:
    manifest, rows, source_root, output_root, _, _ = _fixture(tmp_path)
    (source_root / "images" / "material-1.jpg").unlink()

    report = materialize_assets(manifest, rows, source_root, output_root)

    assert report["status"] == "failed"
    assert {error["kind"] for error in report["errors"]} == {"source_file_missing"}
    assert not (output_root / "release" / manifest.release_no).exists()


@pytest.mark.parametrize(
    ("content", "expected_content", "expected_kind"),
    [
        (b"short", b"longer expected bytes", "source_size_mismatch"),
        (b"wrong-hash-bytes", b"right-hash-bytes", "source_hash_mismatch"),
    ],
)
def test_source_size_and_hash_mismatch_fail(
    tmp_path: Path,
    content: bytes,
    expected_content: bytes,
    expected_kind: str,
) -> None:
    manifest, rows, source_root, output_root, _, _ = _fixture(
        tmp_path, content=content, expected_content=expected_content
    )

    report = materialize_assets(manifest, rows, source_root, output_root)

    assert report["status"] == "failed"
    assert {error["kind"] for error in report["errors"]} == {expected_kind}
    assert not (output_root / "release" / manifest.release_no).exists()


def test_source_prefix_traversal_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")

    manifest, rows, source_root, output_root, _, _ = _fixture(tmp_path / "traversal")
    traversal_rows = [{**rows[0], "local_path": "/var/lib/qpren/../outside.jpg"}]
    traversal_report = materialize_assets(
        manifest, traversal_rows, source_root, output_root
    )
    assert {error["kind"] for error in traversal_report["errors"]} == {
        "source_path_escape"
    }

    manifest, rows, source_root, output_root, _, _ = _fixture(tmp_path / "symlink")
    linked_source = source_root / "images" / "material-1.jpg"
    linked_source.unlink()
    linked_source.symlink_to(outside)
    symlink_report = materialize_assets(manifest, rows, source_root, output_root)
    assert {error["kind"] for error in symlink_report["errors"]} == {
        "source_path_escape"
    }

    prefix_rows = [{**rows[0], "local_path": "/etc/passwd"}]
    prefix_report = materialize_assets(manifest, prefix_rows, source_root, output_root)
    assert {error["kind"] for error in prefix_report["errors"]} == {
        "source_path_escape"
    }


def test_output_root_equal_or_nested_in_source_root_is_rejected(tmp_path: Path) -> None:
    manifest, rows, source_root, _, _, _ = _fixture(tmp_path)

    equal_report = materialize_assets(manifest, rows, source_root, source_root)
    nested_output = source_root / "nested-output"
    nested_report = materialize_assets(manifest, rows, source_root, nested_output)

    assert equal_report["errors"] == [{"kind": "output_root_boundary"}]
    assert nested_report["errors"] == [{"kind": "output_root_boundary"}]


def test_atomic_copy_failure_leaves_no_final_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, rows, source_root, output_root, _, _ = _fixture(tmp_path)

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(materializer, "_copy_atomically", fail_copy)
    report = materialize_assets(manifest, rows, source_root, output_root)

    assert report["status"] == "failed"
    assert report["errors"] == [{"kind": "atomic_materialization_failed"}]
    assert not (output_root / "release" / manifest.release_no).exists()


def test_existing_invalid_release_is_not_overwritten(tmp_path: Path) -> None:
    manifest, rows, source_root, output_root, _, _ = _fixture(tmp_path)
    final_dir = output_root / "release" / manifest.release_no
    final_dir.mkdir(parents=True)
    destination = output_root / manifest.assets[0].object_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"not-the-release")

    report = materialize_assets(manifest, rows, source_root, output_root)

    assert report["status"] == "failed"
    assert report["errors"][0]["kind"] == "existing_size_mismatch"
    assert destination.read_bytes() == b"not-the-release"


def test_report_has_no_source_metadata_or_connection_string(tmp_path: Path) -> None:
    manifest, rows, source_root, output_root, _, _ = _fixture(tmp_path)
    report = materialize_assets(manifest, rows, source_root, output_root)
    encoded = json.dumps(report, ensure_ascii=False)

    assert "private.example.invalid" not in encoded
    assert "/var/lib/qpren/images/material-1.jpg" not in encoded
    assert "local_path" not in encoded
    assert "source-root" not in encoded
    assert "postgresql://qpren:secret@db.example/qpren" not in encoded
    assert manifest.assets[0].object_key in encoded


def test_read_release_manifest_uses_sqlite_read_only_and_keeps_node_binding_generic(
    tmp_path: Path,
) -> None:
    database = tmp_path / "release.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE catalog_releases (release_id TEXT, release_no TEXT);
        CREATE TABLE catalog_assets (
            release_id TEXT, asset_key TEXT, material_code TEXT,
            system_node_key TEXT, asset_type TEXT, object_key TEXT,
            source_sha256 TEXT, size_bytes INTEGER, mime_type TEXT, status TEXT
        );
        CREATE TABLE fitments (release_id TEXT, node_key TEXT, material_code TEXT);
        """
    )
    connection.execute(
        "INSERT INTO catalog_releases VALUES ('r1', 'rayah-n6-test')"
    )
    connection.execute(
        """
        INSERT INTO catalog_assets VALUES
        ('r1', 'asset-1', NULL, 'node-1', 'other',
         'release/rayah-n6-test/assets/other/asset.bin', ?, 4, 'application/octet-stream', 'ready')
        """,
        ("a" * 64,),
    )
    connection.execute("INSERT INTO fitments VALUES ('r1', 'node-1', 'MAT-1')")
    connection.commit()
    connection.close()

    manifest = read_release_manifest(database)

    assert manifest.release_no == "rayah-n6-test"
    assert manifest.material_codes == frozenset({"MAT-1"})
    with pytest.raises(sqlite3.OperationalError):
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            connection.execute("UPDATE catalog_releases SET release_no = 'changed'")
        finally:
            connection.close()


def test_read_only_session_requires_both_settings() -> None:
    class Result:
        def __init__(self, row: dict[str, str]) -> None:
            self.row = row

        def fetchone(self) -> dict[str, str]:
            return self.row

    class Connection:
        def __init__(self, transaction: str, default: str) -> None:
            self.transaction = transaction
            self.default = default
            self.queries: list[str] = []

        def execute(self, query: str) -> Result:
            self.queries.append(query)
            if query.startswith("SHOW"):
                return Result({"transaction_read_only": self.transaction})
            return Result({"setting": self.default})

    connection = Connection("on", "on")
    verify_read_only_session(connection)
    assert connection.queries == [
        "SHOW transaction_read_only",
        "SELECT current_setting('default_transaction_read_only') AS setting",
    ]
    with pytest.raises(materializer.MaterializerError):
        verify_read_only_session(Connection("off", "on"))


def test_duplicate_identity_uses_first_valid_candidate_deterministically(tmp_path: Path) -> None:
    manifest, rows, source_root, output_root, expected, _ = _fixture(tmp_path)
    duplicate = {**rows[0], "local_path": "/var/lib/qpren/images/aaa-missing.jpg"}

    report = materialize_assets(manifest, [duplicate, rows[0]], source_root, output_root)

    assert report["status"] == "materialized"
    assert _destination(manifest, output_root).read_bytes() == expected
