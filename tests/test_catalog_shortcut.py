from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app import aftercare_db, main
from app.aftercare_routes import create_staff_password
from app.main import app

PASSWORD = "LimeAuto-Shortcut-Test-2026!"


def make_release(path):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE catalog_releases (release_id TEXT, release_no TEXT, status TEXT);
        CREATE TABLE release_models (
            release_id TEXT, series_code TEXT, model_code TEXT,
            series_name_source TEXT, model_name_source TEXT, publish_status TEXT
        );
        INSERT INTO catalog_releases VALUES ('r1', 'limeauto-test', 'validated');
        INSERT INTO release_models VALUES ('r1', 'HYE', 'HYEE-PZ02', '海豹08EV', '海豹08EV 右舵', 'published');
        """
    )
    db.commit()
    db.close()


def logged_client(db_path, release_path):
    import os
    os.environ["LIMEAUTO_AFTERCARE_DB_PATH"] = str(db_path)
    os.environ["LIMEAUTO_AFTERCARE_HMAC_SECRET"] = "shortcut-secret"
    os.environ["LIMEAUTO_CATALOG_RELEASE_PATH"] = str(release_path)
    with aftercare_db.connect(db_path, hmac_secret="shortcut-secret") as db:
        user = aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, "LGXCD6CD4P0123458", hmac_secret="shortcut-secret"
        )
    client = TestClient(app)
    response = client.post(
        "/ops/login",
        data={"email": user["email"], "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client, vehicle["id"]


def test_manual_catalog_shortcut_saves_and_targets_model_page(tmp_path, monkeypatch):
    release = tmp_path / "release.sqlite"
    make_release(release)
    db_path = tmp_path / "aftercare.sqlite"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "shortcut-secret")
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    with aftercare_db.connect(db_path, hmac_secret="shortcut-secret") as db:
        user = aftercare_db.create_staff_user(
            db, email="operator@example.test", password_hash=create_staff_password(PASSWORD)
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, "LGXCD6CD4P0123458", hmac_secret="shortcut-secret"
        )
    client = TestClient(app)
    assert client.post(
        "/ops/login", data={"email": user["email"], "password": PASSWORD}, follow_redirects=False
    ).status_code == 303
    csrf = client.cookies.get("limeauto_aftercare_csrf")
    response = client.post(
        f"/ops/vehicles/{vehicle['id']}/catalog-shortcut",
        data={
            "series_code": "HYE",
            "model_code": "HYEE-PZ02",
            "confirmation_note": "Manual catalog reference",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(f"/ops/vehicles/{vehicle['id']}")
    assert detail.status_code == 200
    assert f"/catalog/{main.release_url_token('HYE')}/{main.release_url_token('HYEE-PZ02')}" in detail.text
    assert "海豹08EV · 海豹08EV 右舵" in detail.text
    assert "<code>HYE / HYEE-PZ02</code>" in detail.text
    assert "Manual catalog reference" in detail.text


def test_shortcut_form_does_not_offer_draft_release(tmp_path, monkeypatch):
    release = tmp_path / "draft.sqlite"
    db = sqlite3.connect(release)
    db.executescript(
        """
        CREATE TABLE catalog_releases (release_id TEXT, release_no TEXT, status TEXT);
        CREATE TABLE release_models (
            release_id TEXT, series_code TEXT, model_code TEXT,
            series_name_source TEXT, model_name_source TEXT, publish_status TEXT
        );
        INSERT INTO catalog_releases VALUES ('r1', 'limeauto-draft', 'draft');
        """
    )
    db.commit()
    db.close()
    db_path = tmp_path / "aftercare.sqlite"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "draft-secret")
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.delenv("LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT", raising=False)
    with aftercare_db.connect(db_path, hmac_secret="draft-secret") as db:
        user = aftercare_db.create_staff_user(
            db, email="operator@example.test", password_hash=create_staff_password(PASSWORD)
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, "LGXCD6CD4P0123458", hmac_secret="draft-secret"
        )
    client = TestClient(app)
    assert client.post(
        "/ops/login", data={"email": user["email"], "password": PASSWORD}, follow_redirects=False
    ).status_code == 303
    response = client.get(f"/ops/vehicles/{vehicle['id']}/catalog-shortcut")
    assert response.status_code == 200
    assert "目录暂不可用" in response.text


def test_shortcut_flag_allows_draft_release_to_be_shown_and_saved(tmp_path, monkeypatch):
    release = tmp_path / "draft.sqlite"
    db = sqlite3.connect(release)
    db.executescript(
        """
        CREATE TABLE catalog_releases (release_id TEXT, release_no TEXT, status TEXT);
        CREATE TABLE release_models (
            release_id TEXT, series_code TEXT, model_code TEXT,
            series_name_source TEXT, model_name_source TEXT, publish_status TEXT
        );
        INSERT INTO catalog_releases VALUES ('r1', 'limeauto-draft', 'draft');
        INSERT INTO release_models VALUES ('r1', 'HYE', 'HYEE-PZ02', '海豹08EV', '海豹08EV 右舵', 'published');
        """
    )
    db.commit()
    db.close()
    db_path = tmp_path / "aftercare.sqlite"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "draft-secret")
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT", "1")
    with aftercare_db.connect(db_path, hmac_secret="draft-secret") as db:
        user = aftercare_db.create_staff_user(
            db, email="operator@example.test", password_hash=create_staff_password(PASSWORD)
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, "LGXCD6CD4P0123458", hmac_secret="draft-secret"
        )
    client = TestClient(app)
    assert client.post(
        "/ops/login", data={"email": user["email"], "password": PASSWORD}, follow_redirects=False
    ).status_code == 303
    csrf = client.cookies.get("limeauto_aftercare_csrf")
    form = client.get(f"/ops/vehicles/{vehicle['id']}/catalog-shortcut?series=HYE")
    assert form.status_code == 200
    assert "海豹08EV 右舵" in form.text
    assert "onchange=" not in form.text
    assert 'id="catalog-shortcut-models"' not in form.text
    assert "HYEE-PZ02" in form.text
    assert "显示该车系车型" in form.text
    response = client.post(
        f"/ops/vehicles/{vehicle['id']}/catalog-shortcut",
        data={
            "series_code": "HYE",
            "model_code": "HYEE-PZ02",
            "confirmation_note": "Draft catalog reference",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(f"/ops/vehicles/{vehicle['id']}")
    assert detail.status_code == 200
    assert "HYE / HYEE-PZ02" in detail.text
    assert "Draft catalog reference" in detail.text


def test_aftercare_shows_catalog_entry_when_browse_enabled(tmp_path, monkeypatch):
    release = tmp_path / "release.sqlite"
    make_release(release)
    db_path = tmp_path / "aftercare.sqlite"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "shortcut-secret")
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_BROWSE", "1")
    with aftercare_db.connect(db_path, hmac_secret="shortcut-secret") as db:
        user = aftercare_db.create_staff_user(
            db, email="operator@example.test", password_hash=create_staff_password(PASSWORD)
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, "LGXCD6CD4P0123458", hmac_secret="shortcut-secret"
        )
    client = TestClient(app)
    assert client.post(
        "/ops/login", data={"email": user["email"], "password": PASSWORD}, follow_redirects=False
    ).status_code == 303
    vehicles = client.get("/ops/vehicles")
    assert vehicles.status_code == 200
    assert 'href="/catalog"' in vehicles.text
    assert "打开车型目录" in vehicles.text
    assert "车型目录" in vehicles.text
    detail = client.get(f"/ops/vehicles/{vehicle['id']}")
    assert detail.status_code == 200
    assert 'href="/catalog"' in detail.text
    assert "打开车型目录" in detail.text
    form = client.get(f"/ops/vehicles/{vehicle['id']}/catalog-shortcut")
    assert form.status_code == 200
    assert 'href="/catalog"' in form.text
    assert "onchange=" not in form.text
    assert '"series_code": "HYE"' not in form.text
