from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.catalog_release import CatalogReleaseStore
from tests.staff_session import catalog_staff_client

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"
RELEASE_NO = "media-release-1"
ASSET_KEY = "material_image:" + "a" * 64
MATERIAL_CODE = "M-1"
CONTENT = b"\xff\xd8\xff\xe0" + b"catalog-media-fixture"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()
OBJECT_KEY = f"release/{RELEASE_NO}/assets/material_image/{CONTENT_SHA}.jpg"


def make_release(
    tmp_path: Path,
    *,
    status: str = "done",
    object_key: str = OBJECT_KEY,
    source_sha256: str | None = CONTENT_SHA,
    write_file: bool = True,
) -> tuple[CatalogReleaseStore, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "media-release.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        """
        INSERT INTO catalog_releases(
          release_id, release_no, source_snapshot_fingerprint,
          source_counts_json, validation_summary_json
        ) VALUES ('r-media', ?, 'fingerprint', '{}', '{}')
        """,
        (RELEASE_NO,),
    )
    connection.execute(
        """
        INSERT INTO catalog_parts(release_id, material_code, display_name_source, description)
        VALUES ('r-media', ?, 'Fixture material', 'Fixture material')
        """,
        (MATERIAL_CODE,),
    )
    connection.execute(
        """
        INSERT INTO catalog_assets(
          release_id, asset_key, material_code, asset_type, object_key,
          source_sha256, size_bytes, mime_type, status
        ) VALUES ('r-media', ?, ?, 'material_image', ?, ?, ?, 'image/jpeg', ?)
        """,
        (ASSET_KEY, MATERIAL_CODE, object_key, source_sha256, len(CONTENT), status),
    )
    connection.commit()
    connection.close()

    root = tmp_path / "asset-root"
    if write_file:
        target = root / object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENT)
    return CatalogReleaseStore(database, release_no=RELEASE_NO, allow_draft=True), root


def enabled_client(monkeypatch: pytest.MonkeyPatch, store: CatalogReleaseStore) -> TestClient:
    # Media is part of the staff-only catalog surface, so the fixture signs in for real.
    return catalog_staff_client(monkeypatch, store)


def media_url() -> str:
    return main.catalog_media_url(RELEASE_NO, ASSET_KEY)


def test_catalog_media_serves_valid_asset_with_safe_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, root = make_release(tmp_path)
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))
    response = enabled_client(monkeypatch, store).get(media_url())

    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert str(root) not in response.text
    assert "local_path" not in response.text
    assert "qpren" not in response.text.lower()


def test_catalog_media_serves_bmp_when_magic_and_mime_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, root = make_release(tmp_path)
    content = b"BM" + b"bmp-fixture"
    object_key = f"release/{RELEASE_NO}/assets/material_image/{hashlib.sha256(content).hexdigest()}.bmp"
    monkeypatch.setattr(
        store,
        "asset_by_key",
        lambda _: {
            "asset_key": ASSET_KEY,
            "asset_type": "material_image",
            "object_key": object_key,
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "mime_type": "image/bmp",
            "status": "done",
        },
    )
    target = root / object_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))

    response = enabled_client(monkeypatch, store).get(media_url())

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "image/bmp"


def test_catalog_media_rejects_mismatched_magic_and_declared_mime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, root = make_release(tmp_path)
    content = b"BM" + b"bmp-fixture"
    monkeypatch.setattr(
        store,
        "asset_by_key",
        lambda _: {
            "asset_key": ASSET_KEY,
            "asset_type": "material_image",
            "object_key": OBJECT_KEY,
            "source_sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "mime_type": "image/jpeg",
            "status": "done",
        },
    )
    target = root / OBJECT_KEY
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))

    assert enabled_client(monkeypatch, store).get(media_url()).status_code == 404


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda: "/media/catalog/not-the-release/" + main.release_url_token(ASSET_KEY),
        lambda: "/media/catalog/" + main.release_url_token(RELEASE_NO) + "/raw-asset-key",
        lambda: "/media/catalog/" + main.release_url_token(RELEASE_NO) + "/" + main.release_url_token("missing"),
    ],
)
def test_catalog_media_rejects_wrong_or_noncanonical_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path_factory
) -> None:
    store, root = make_release(tmp_path)
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))
    response = enabled_client(monkeypatch, store).get(path_factory())
    assert response.status_code == 404
    assert response.text == '{"detail":"Catalog media unavailable"}'


@pytest.mark.parametrize("status", ["pending", "failed", "missing"])
def test_catalog_media_rejects_nonpublishable_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
) -> None:
    store, root = make_release(tmp_path, status=status)
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))
    response = enabled_client(monkeypatch, store).get(media_url())
    assert response.status_code == 404


def test_catalog_media_rejects_missing_tampered_and_unverifiable_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, root = make_release(tmp_path, write_file=False)
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))
    client = enabled_client(monkeypatch, store)
    assert client.get(media_url()).status_code == 404

    target = root / OBJECT_KEY
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"tampered")
    assert client.get(media_url()).status_code == 404

    null_hash_store, null_hash_root = make_release(
        tmp_path / "null-hash", source_sha256=None
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(null_hash_root))
    assert enabled_client(monkeypatch, null_hash_store).get(media_url()).status_code == 404


def test_catalog_media_rejects_unsafe_object_key_without_path_disclosure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, root = make_release(tmp_path)
    monkeypatch.setattr(
        store,
        "asset_by_key",
        lambda _: {
            "asset_key": ASSET_KEY,
            "asset_type": "material_image",
            "object_key": "../outside.jpg",
            "source_sha256": CONTENT_SHA,
            "size_bytes": len(CONTENT),
            "mime_type": "image/jpeg",
            "status": "done",
        },
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))
    response = enabled_client(monkeypatch, store).get(media_url())
    assert response.status_code == 404
    assert "outside.jpg" not in response.text
    assert str(root) not in response.text


def test_catalog_media_is_closed_with_catalog_when_browse_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, root = make_release(tmp_path)
    monkeypatch.setattr(main, "get_release_store", lambda: store)
    monkeypatch.setattr(main, "CATALOG_BROWSE_ENABLED", False)
    monkeypatch.setattr(main, "CATALOG_CLOSED_PREFIXES", main.CATALOG_CLOSED_PREFIXES)
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(root))
    response = TestClient(main.app).get(media_url())
    assert response.status_code == 410
    assert response.headers["x-content-type-options"] == "nosniff"


def test_pending_asset_does_not_create_catalog_media_url() -> None:
    view = main.release_part_view(
        {
            "material_code": MATERIAL_CODE,
            "material_asset_key": ASSET_KEY,
            "material_asset_status": "pending",
            "material_asset_type": "material_image",
        },
        "S",
        "M",
        "N",
        release_no=RELEASE_NO,
    )
    assert view["media_url"] is None


def test_catalog_object_key_resolver_rejects_escape_and_unconfigured_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LIMEAUTO_CATALOG_ASSET_ROOT", raising=False)
    assert main.resolve_catalog_asset_path(OBJECT_KEY) is None
    monkeypatch.setenv("LIMEAUTO_CATALOG_ASSET_ROOT", str(tmp_path))
    assert main.resolve_catalog_asset_path("../outside.jpg") is None
    assert main.resolve_catalog_asset_path("/absolute.jpg") is None
    assert main.resolve_catalog_asset_path("release/x/assets/a..jpg") is None
