from __future__ import annotations

from fastapi.testclient import TestClient

from app import aftercare_db
from app.aftercare_routes import _db, create_staff_password
from app.main import app

VIN = "LGXCD6CD4P0123458"
PASSWORD = "LimeAuto-Route-Test-2026!"


def _login(tmp_path, monkeypatch, secret: str):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", secret)
    with aftercare_db.connect(db_path, hmac_secret=secret) as db:
        aftercare_db.create_staff_user(
            db, email="operator@example.test", password_hash=create_staff_password(PASSWORD)
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, VIN, brand="BYD", hmac_secret=secret
        )
    client = TestClient(app)
    client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    return client, csrf_token(client), int(vehicle["id"])


def csrf_token(client: TestClient) -> str:
    csrf = client.cookies.get("limeauto_aftercare_csrf")
    assert csrf
    return csrf


def test_maintenance_detail_route_renders_item_status(tmp_path, monkeypatch):
    client, csrf, vehicle_id = _login(tmp_path, monkeypatch, "detail-test-secret")
    record = None
    with _db() as db:
        record = aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle_id,
            maintenance_date="2026-09-01",
            maintenance_type="repair",
            summary="Brake review",
            items=[
                {
                    "item_type": "replacement",
                    "item_name": "更换空调滤芯",
                    "material_code": "13475854-00",
                    "quantity": 1,
                    "item_status": "completed",
                    "note": None,
                },
                {
                    "item_type": "replacement",
                    "item_name": "更换制动片",
                    "material_code": None,
                    "quantity": None,
                    "item_status": "recommended",
                    "note": "下次进厂处理",
                },
            ],
        )
        record_id = int(record["id"])

    detail = client.get(f"/ops/vehicles/{vehicle_id}/maintenance/{record_id}")
    assert detail.status_code == 200
    assert "Brake review" in detail.text
    assert "更换空调滤芯" in detail.text
    assert "已完成" in detail.text
    assert "建议处理" in detail.text
    assert "13475854-00" in detail.text
    assert "record-status-raw" not in detail.text

    history = client.get(f"/ops/vehicles/{vehicle_id}")
    assert history.status_code == 200
    assert "更换制动片" in history.text
    assert "建议处理" in history.text

    missing = client.get(f"/ops/vehicles/{vehicle_id}/maintenance/99999")
    assert missing.status_code == 404

    unauth = TestClient(app).get(f"/ops/vehicles/{vehicle_id}/maintenance/{record_id}", follow_redirects=False)
    assert unauth.status_code == 303


def test_item_search_discloses_matched_record_lines(tmp_path, monkeypatch):
    client, csrf, vehicle_id = _login(tmp_path, monkeypatch, "search-match-secret")
    with _db() as db:
        aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle_id,
            maintenance_date="2026-09-01",
            maintenance_type="maintenance",
            summary="常规保养",
            items=[
                {
                    "item_type": "replacement",
                    "item_name": "更换空调滤芯",
                    "material_code": "13475854-00",
                    "quantity": 1,
                    "item_status": "recommended",
                    "note": None,
                }
            ],
        )
        aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle_id,
            maintenance_date="2026-08-01",
            maintenance_type="inspection",
            summary="仪表板异响排查",
        )

    page = client.get("/ops/vehicles", params={"q": "空调滤芯"})
    assert page.status_code == 200
    assert "命中项目" in page.text
    assert "更换空调滤芯" in page.text
    assert "建议处理" in page.text
    assert "/ops/vehicles/1/maintenance/1" in page.text

    page = client.get("/ops/vehicles", params={"q": "13475854-00"})
    assert page.status_code == 200
    assert "命中项目" in page.text

    page = client.get("/ops/vehicles", params={"q": "仪表板"})
    assert page.status_code == 200
    assert "命中记录" in page.text

    # Unrelated query discloses nothing.
    page = client.get("/ops/vehicles", params={"q": "BYD"})
    assert page.status_code == 200
    assert "命中项目" not in page.text
    assert "命中记录" not in page.text


def test_partially_filled_item_row_is_rejected_with_error(tmp_path, monkeypatch):
    client, csrf, vehicle_id = _login(tmp_path, monkeypatch, "half-item-secret")
    response = client.post(
        f"/ops/vehicles/{vehicle_id}/maintenance",
        data={
            "maintenance_date": "2026-09-02",
            "maintenance_type": "repair",
            "summary": "半填条目校验",
            "item_type": "replacement",
            "item_name": "",
            "material_code": "ABC-123",
            "quantity": "",
            "item_status": "completed",
            "item_note": "",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert "填写了配件、数量或备注的项目必须填写项目名称" in response.text
    assert 'value="ABC-123"' in response.text
    with _db() as db:
        assert db.execute("SELECT COUNT(*) FROM maintenance_records").fetchone()[0] == 0