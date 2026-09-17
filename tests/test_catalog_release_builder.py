from pathlib import Path
import hashlib
import sqlite3

import tools.build_catalog_release as builder
from tools.build_catalog_release import (
    asset_identity,
    build_release,
    check_asset_file,
    canonical_fingerprint,
    insert_asset_batch,
    parse_model_filter,
    query_release_counts,
    query_scoped_material_count,
    resolve_asset_path,
    safe_asset_type,
)


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"
)


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return connection


def _insert_release_fixture(
    connection: sqlite3.Connection,
    *,
    release_id: str = "release-1",
    node_key: str = "source:S:M:Engine:OBJ-1",
    source_occurrence_key: str = "occurrence-1",
    include_fitment: bool = True,
) -> None:
    connection.execute(
        "INSERT INTO catalog_releases (release_id, release_no) VALUES (?, ?)",
        (release_id, f"release-no-{release_id}"),
    )
    connection.execute(
        """
        INSERT INTO release_models (release_id, series_code, model_code)
        VALUES (?, 'S', 'M')
        """,
        (release_id,),
    )
    connection.execute(
        """
        INSERT INTO system_nodes (
            release_id, node_key, series_code, model_code, source_obj_code,
            path_key, parent_key, display_name, is_derived, path_variant_count
        )
        VALUES (?, ?, 'S', 'M', 'OBJ-1', 'Engine', NULL, 'Engine', 0, 1)
        """,
        (release_id, node_key),
    )
    connection.execute(
        """
        INSERT INTO catalog_parts (release_id, material_code, description)
        VALUES (?, 'MAT-1', 'Tiny fixture part')
        """,
        (release_id,),
    )
    if include_fitment:
        connection.execute(
            """
            INSERT INTO fitments (
                release_id, source_occurrence_key, series_code, model_code,
                node_key, material_code, callout, quantity, quantity_raw,
                manual_code, fitment_note, fitment_level, review_status
            )
            VALUES (?, ?, 'S', 'M', ?, 'MAT-1', '1', NULL, '1.5',
                    'MAN-1', 'raw quantity retained', 'node', 'pending')
            """,
            (release_id, source_occurrence_key, node_key),
        )


def _expect_integrity_error(operation) -> None:
    try:
        operation()
    except sqlite3.IntegrityError:
        return
    raise AssertionError("expected sqlite3.IntegrityError")


def test_schema_initializes() -> None:
    connection = _connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        table_names = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        assert table_names == {
            "catalog_releases",
            "release_models",
            "system_nodes",
            "catalog_parts",
            "fitments",
            "catalog_assets",
        }
    finally:
        connection.close()


def test_duplicate_release_node_key_is_rejected() -> None:
    connection = _connect()
    try:
        _insert_release_fixture(connection)
        _expect_integrity_error(
            lambda: connection.execute(
                """
                INSERT INTO system_nodes (
                    release_id, node_key, series_code, model_code, path_key
                )
                VALUES ('release-1', 'source:S:M:Engine:OBJ-1', 'S', 'M', 'Engine')
                """
            )
        )
    finally:
        connection.close()


def test_duplicate_release_source_occurrence_key_is_rejected() -> None:
    connection = _connect()
    try:
        _insert_release_fixture(connection)
        _expect_integrity_error(
            lambda: connection.execute(
                """
                INSERT INTO fitments (
                    release_id, source_occurrence_key, series_code, model_code,
                    node_key, material_code
                )
                VALUES ('release-1', 'occurrence-1', 'S', 'M',
                        'source:S:M:Engine:OBJ-1', 'MAT-1')
                """
            )
        )
    finally:
        connection.close()


def test_same_display_path_retains_two_source_node_variants() -> None:
    connection = _connect()
    try:
        _insert_release_fixture(connection, include_fitment=False)
        connection.execute(
            """
            INSERT INTO system_nodes (
                release_id, node_key, series_code, model_code, source_obj_code,
                path_key, parent_key, display_name, is_derived, path_variant_count
            )
            VALUES ('release-1', 'source:S:M:Engine:OBJ-2', 'S', 'M', 'OBJ-2',
                    'Engine', NULL, 'Engine', 0, 2)
            """
        )

        rows = connection.execute(
            """
            SELECT node_key, source_obj_code
            FROM system_nodes
            WHERE release_id = 'release-1' AND path_key = 'Engine'
            ORDER BY node_key
            """
        ).fetchall()
        assert rows == [
            ("source:S:M:Engine:OBJ-1", "OBJ-1"),
            ("source:S:M:Engine:OBJ-2", "OBJ-2"),
        ]
    finally:
        connection.close()


def test_fitment_quantity_raw_is_retained() -> None:
    connection = _connect()
    try:
        _insert_release_fixture(connection)
        assert connection.execute(
            "SELECT quantity, quantity_raw FROM fitments WHERE release_id = 'release-1'"
        ).fetchone() == (None, "1.5")
    finally:
        connection.close()


def test_foreign_keys_reject_unknown_node_key() -> None:
    connection = _connect()
    try:
        _insert_release_fixture(connection, include_fitment=False)
        _expect_integrity_error(
            lambda: connection.execute(
                """
                INSERT INTO fitments (
                    release_id, source_occurrence_key, series_code, model_code,
                    node_key, material_code
                )
                VALUES ('release-1', 'occurrence-missing-node', 'S', 'M',
                        'source:S:M:Engine:UNKNOWN', 'MAT-1')
                """
            )
        )
    finally:
        connection.close()


def test_builder_model_filter_and_asset_helpers_are_deterministic() -> None:
    assert parse_model_filter("S/M") == ("S", "M")
    try:
        parse_model_filter("S/M/extra")
    except ValueError:
        pass
    else:
        raise AssertionError("malformed model selector should fail")

    assert safe_asset_type("epc_svg") == ("epc_drawing", ".svg")
    identity = asset_identity("r/1", "material", "https://example.invalid/a.jpg", None)
    assert identity["asset_type"] == "material_image"
    assert "example.invalid" not in identity["object_key"]
    assert canonical_fingerprint({"b": 2, "a": [1, 2]}) == canonical_fingerprint(
        {"a": [1, 2], "b": 2}
    )


def test_asset_identity_preserves_missing_sha_and_rejects_invalid_sha() -> None:
    identity = asset_identity("r/1", "material", "https://example.invalid/a.jpg", None)
    assert identity["source_sha256"] is None
    expected_identity_hash = hashlib.sha256(
        b"https://example.invalid/a.jpg"
    ).hexdigest()
    assert identity["identity_hash"] == expected_identity_hash
    assert identity["asset_key"] == f"material_image:{expected_identity_hash}"
    assert identity["identity_hash"] == asset_identity(
        "r/1", "material", "https://example.invalid/a.jpg", None
    )["identity_hash"]
    assert asset_identity(
        "r/1", "material", "https://example.invalid/a.jpg", " "
    )["source_sha256"] is None
    provided_sha = "A" * 64
    provided_identity = asset_identity(
        "r/1", "material", "https://example.invalid/a.jpg", provided_sha
    )
    assert provided_identity["source_sha256"] == provided_sha.lower()
    assert provided_identity["identity_hash"] == provided_sha.lower()
    try:
        asset_identity("r/1", "material", "https://example.invalid/a.jpg", "not-a-sha")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid source sha256 should be rejected")


def test_asset_root_mapping_checks_files_and_rejects_escapes(tmp_path: Path) -> None:
    root = tmp_path / "qpren"
    asset = root / "images" / "part.jpg"
    asset.parent.mkdir(parents=True)
    content = b"1234"
    asset.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    assert resolve_asset_path(root, "/var/lib/qpren/images/part.jpg") == asset
    assert check_asset_file(root, "/var/lib/qpren/images/part.jpg", 4, digest) == "ok"
    assert check_asset_file(root, "/var/lib/qpren/images/part.jpg", 4, "0" * 64) == "hash_mismatch"
    assert check_asset_file(root, "/var/lib/qpren/images/part.jpg", 4) == "hash_missing"
    assert check_asset_file(root, "/var/lib/qpren/images/part.jpg", 3, digest) == "size_mismatch"
    assert check_asset_file(root, "/var/lib/qpren/images/missing.jpg", 4) == "missing"
    assert check_asset_file(root, None, 4) == "missing"
    empty = root / "images" / "empty.jpg"
    empty.touch()
    assert check_asset_file(root, "/var/lib/qpren/images/empty.jpg", 0) == "empty"
    for bad_path in (
        "/var/lib/qpren/../outside.jpg",
        "/var/lib/qpren-other/images/part.jpg",
        "/etc/passwd",
        "relative.jpg",
    ):
        assert check_asset_file(root, bad_path, 4) == "path_escape"
    escaped = root / "images" / "link.jpg"
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(content)
    escaped.symlink_to(outside)
    assert check_asset_file(root, "/var/lib/qpren/images/link.jpg", 4) == "path_escape"


class _CountConnection:
    def execute(self, query, params):
        self.query = query
        self.params = params
        return self

    def fetchone(self):
        return {"value": 7}


def test_scoped_material_count_uses_occurrence_material_subquery() -> None:
    connection = _CountConnection()
    assert query_scoped_material_count(connection, "images", [("S", "M")]) == 7
    assert "FROM images t" in connection.query
    assert "t.material_code IS NOT NULL" in connection.query
    assert "FROM occurrences o" in connection.query
    assert "o.material_code" in connection.query
    assert connection.params == ["S", "M"]


def test_release_asset_count_uses_final_rows_after_duplicate_identity_is_ignored() -> None:
    connection = _connect()
    try:
        _insert_release_fixture(connection, include_fitment=False)
        row = (
            "release-1",
            "material_image:identity-1",
            "MAT-1",
            None,
            "material_image",
            "release/r/assets/material_image/identity-1.jpg",
            None,
            4,
            "image/jpeg",
            "pending",
        )
        second_row = (*row[:1], "material_image:identity-2", *row[2:])
        insert_asset_batch(connection, [row, row, second_row])
        counts = query_release_counts(connection, "release-1")
        assert counts["catalog_assets"] == 2
    finally:
        connection.close()


class _Rows:
    def __init__(self, rows) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows


class _FakeSourceConnection:
    def execute(self, query, params=None):
        if "FROM parts" in query:
            return _Rows([{"material_code": "MAT-1", "first_description": "Fixture part"}])
        raise AssertionError(f"unexpected source query in pure builder test: {query}")


def test_build_report_uses_artifact_asset_count_and_keeps_missing_sha_null(
    tmp_path: Path, monkeypatch
) -> None:
    source = _FakeSourceConnection()
    monkeypatch.setattr(
        builder,
        "read_only_evidence",
        lambda connection: {"transaction_read_only": "on"},
    )
    monkeypatch.setattr(
        builder,
        "query_model_rows",
        lambda connection, filters: [
            {"series_code": "S", "model_code": "M", "series_name": "Series", "model_name": None}
        ],
    )
    monkeypatch.setattr(
        builder,
        "query_node_rows",
        lambda connection, filters: [
            {
                "series_code": "S",
                "model_code": "M",
                "obj_code": "OBJ-1",
                "tree_key": "Engine",
                "node_path": "Engine",
                "direct_part_count": 1,
            }
        ],
    )
    monkeypatch.setattr(builder, "query_count", lambda connection, table, filters: 1)
    monkeypatch.setattr(builder, "iter_occurrences", lambda connection, filters: iter([
        {
            "occurrence_key": "occ-1",
            "series_code": "S",
            "model_code": "M",
            "obj_code": "OBJ-1",
            "material_code": "MAT-1",
            "callout": "1",
            "qty": "1",
            "manual_code": None,
            "note": None,
        }
    ]))
    monkeypatch.setattr(builder, "material_detail_statuses", lambda connection, codes: {})

    duplicate_image = {
        "url": "https://example.invalid/a.jpg",
        "kind": "material",
        "material_code": "MAT-1",
        "name": "a.jpg",
        "local_path": "/var/lib/qpren/images/a.jpg",
        "sha256": None,
        "size_bytes": 4,
        "status": "pending",
    }
    monkeypatch.setattr(
        builder,
        "iter_images",
        lambda connection, material_codes: iter([duplicate_image, duplicate_image, {
            **duplicate_image,
            "material_code": None,
        }]),
    )

    output = tmp_path / "release.sqlite"
    report_path = tmp_path / "release.json"
    report = build_release(
        source,
        output=output,
        release_no="release-1",
        scope="all",
        filters=[],
        report_path=report_path,
    )

    assert report["status"] == "draft"
    assert report["asset_manifest_count"] == report["release_counts"]["catalog_assets"] == 1
    assert report["unbound_image_count"] == 1
    assert report["source_asset_check_summary"]["hash_missing"] == 2
    assert report["source_asset_check_summary"]["deduplicated"] == 1
    assert any(item["kind"] == "asset_root_not_provided" for item in report["warnings"])
    assert "/var/lib/qpren/images/a.jpg" not in report_path.read_text(encoding="utf-8")

    artifact = sqlite3.connect(output)
    try:
        assert artifact.execute(
            "SELECT COUNT(*), source_sha256 FROM catalog_assets"
        ).fetchone() == (1, None)
        assert artifact.execute("SELECT model_name FROM release_models").fetchone()[0] is None
    finally:
        artifact.close()

    checked_root = tmp_path / "qpren"
    (checked_root / "images").mkdir(parents=True)
    (checked_root / "images" / "a.jpg").write_bytes(b"1234")
    duplicate_image["sha256"] = "0" * 64
    checked_output = tmp_path / "checked-release.sqlite"
    checked_report_path = tmp_path / "checked-release.json"
    checked_report = build_release(
        source,
        output=checked_output,
        release_no="release-checked",
        scope="all",
        filters=[],
        report_path=checked_report_path,
        asset_root=checked_root,
    )

    assert checked_report["status"] == "failed"
    assert checked_report["source_asset_check_summary"]["hash_mismatch"] == 2
    assert checked_report["asset_manifest_count"] == checked_report["release_counts"]["catalog_assets"] == 1
    assert checked_report["source_asset_check_summary"]["deduplicated"] == 1
    assert any(
        error["kind"] == "source_asset_check_failed" and error["status"] == "hash_mismatch"
        for error in checked_report["blocking_errors"]
    )
    assert not checked_output.exists()
