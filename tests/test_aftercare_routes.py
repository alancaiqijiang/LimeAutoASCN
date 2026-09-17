from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app import aftercare_db
from app.aftercare_routes import create_staff_password
from app.main import app

VIN = "LGXCD6CD4P0123458"
PASSWORD = "LimeAuto-Route-Test-2026!"


def test_ops_staff_workflow_retains_corrections_and_gates_logout(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "route-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="route-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )

    client = TestClient(app)
    assert client.get("/ops/login").status_code == 200
    response = client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/vehicles"
    csrf = client.cookies.get("limeauto_aftercare_csrf")
    assert csrf

    vehicles_page = client.get("/ops/vehicles")
    assert vehicles_page.status_code == 200
    assert ">车辆登记</a>" in vehicles_page.text
    assert ">Vehicle lookup</a>" not in vehicles_page.text
    assert '<a class="brand" href="/ops/vehicles"' in vehicles_page.text
    assert '<form class="nav-logout" action="/ops/logout" method="post">' in vehicles_page.text
    assert f'name="csrf_token" value="{csrf}"' in vehicles_page.text

    response = client.post(
        "/ops/vehicles",
        data={
            "vin": "not-a-vin",
            "brand": "BYD & Co",
            "model_label": "Dolphin <long range>",
            "note": "Needs <manual> review",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert "VIN 必须为 17 位有效字符" in response.text
    assert 'value="BYD &amp; Co"' in response.text
    assert 'value="Dolphin &lt;long range&gt;"' in response.text
    assert "Needs &lt;manual&gt; review" in response.text

    response = client.post(
        "/ops/vehicles",
        data={
            "vin": "LGXCD6CD4P-0123 458",
            "brand": "BYD",
            "model_label": "Manual description",
            "note": "Internal record",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/vehicles/1"
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "····3458" in detail.text
    assert "维保历史" in detail.text
    assert VIN not in str(detail.url)

    response = client.post(
        "/ops/vehicles/1/maintenance",
        data={
            "maintenance_date": "2026-02-30",
            "maintenance_type": "inspection",
            "summary": "Oil & filter <check>",
            "odometer_km": "1234",
            "service_provider": "Garage & Co",
            "source_reference": "job <42>",
            "note": "Keep <invoice>",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert "维保日期须为 YYYY-MM-DD" in response.text
    assert 'value="2026-02-30"' in response.text
    assert 'value="1234"' in response.text
    assert 'value="Garage &amp; Co"' in response.text
    assert 'value="job &lt;42&gt;"' in response.text
    assert "Oil &amp; filter &lt;check&gt;" in response.text
    assert "Keep &lt;invoice&gt;" in response.text

    response = client.post(
        "/ops/vehicles/1/maintenance",
        data={
            "maintenance_date": "2026-08-28",
            "maintenance_type": "inspection",
            "summary": "PDI check",
            "odometer_km": "100",
            "service_provider": "Workshop",
            "source_reference": "doc-1",
            "note": "Initial record",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = client.post(
        "/ops/vehicles/1/maintenance/1",
        data={
            "maintenance_date": "2026-13-01",
            "maintenance_type": "repair",
            "summary": "Corrected & <summary>",
            "odometer_km": "2222",
            "service_provider": "Repair & Co",
            "source_reference": "updated <job>",
            "note": "Review <invoice>",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert "维保日期须为 YYYY-MM-DD" in response.text
    assert 'value="2026-13-01"' in response.text
    assert 'value="2222"' in response.text
    assert 'value="Repair &amp; Co"' in response.text
    assert 'value="updated &lt;job&gt;"' in response.text
    assert "Corrected &amp; &lt;summary&gt;" in response.text
    assert "Review &lt;invoice&gt;" in response.text

    response = client.post(
        "/ops/vehicles/1/maintenance/1",
        data={
            "maintenance_date": "2026-08-29",
            "maintenance_type": "repair",
            "summary": "Corrected repair",
            "odometer_km": "2222",
            "service_provider": "Repair Workshop",
            "source_reference": "updated-job",
            "note": "Reviewed",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    search = client.get("/ops/vehicles", params={"q": "3458"})
    assert search.status_code == 200
    assert "VIN ····3458" in search.text
    assert 'name="q" value="3458"' in search.text

    full_vin_search = client.get("/ops/vehicles", params={"q": VIN})
    assert full_vin_search.status_code == 200
    assert "VIN ····3458" in full_vin_search.text

    edit = client.get("/ops/vehicles/1/maintenance/1/edit")
    assert edit.status_code == 200
    assert 'value="2026-08-29"' in edit.text
    assert "Corrected repair" in edit.text

    response = client.post("/ops/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/login"
    gated = client.get("/ops/vehicles", follow_redirects=False)
    assert gated.status_code == 303
    assert parse_qs(urlsplit(gated.headers["location"]).query)["next"] == ["/ops/vehicles"]

    with aftercare_db.connect(db_path, hmac_secret="route-test-secret") as db:
        record = db.execute("SELECT * FROM maintenance_records").fetchone()
        assert record["summary"] == "Corrected repair"
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'vehicle_entitlements'"
        ).fetchone()[0] == 0


def test_oversized_maintenance_form_returns_error_and_retains_submitted_fields(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "boundary-route-secret")
    with aftercare_db.connect(db_path, hmac_secret="boundary-route-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
        aftercare_db.create_or_get_vehicle(
            db, VIN, brand="BYD", model_label="Dolphin", hmac_secret="boundary-route-secret"
        )

    client = TestClient(app)
    login = client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    assert login.status_code == 303
    csrf = client.cookies.get("limeauto_aftercare_csrf")
    assert csrf

    response = client.post(
        "/ops/vehicles/1/maintenance",
        data={
            "maintenance_date": "2026-08-29",
            "maintenance_type": "inspection",
            "summary": "Oversized odometer review",
            "odometer_km": str(2**63),
            "service_provider": "Workshop",
            "source_reference": "job-1001",
            "note": "Retain this note",
            "csrf_token": csrf,
        },
    )

    assert response.status_code == 200
    assert response.status_code != 500
    assert "里程数超出可保存范围" in response.text
    assert 'value="2026-08-29"' in response.text
    assert '<option value="inspection" selected>检查</option>' in response.text
    assert "Oversized odometer review" in response.text
    assert f'value="{2**63}"' in response.text
    assert 'value="Workshop"' in response.text
    assert 'value="job-1001"' in response.text
    assert "Retain this note" in response.text

    with aftercare_db.connect(db_path, hmac_secret="boundary-route-secret") as db:
        assert db.execute("SELECT COUNT(*) FROM maintenance_records").fetchone()[0] == 0


def test_ops_login_preserves_safe_next_and_rejects_external_next(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "next-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="next-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )

    client = TestClient(app)
    gated = client.get("/ops/vehicles", params={"q": "3458"}, follow_redirects=False)
    assert gated.status_code == 303
    login_url = gated.headers["location"]
    assert parse_qs(urlsplit(login_url).query)["next"] == ["/ops/vehicles?q=3458"]

    login_page = client.get(login_url)
    assert login_page.status_code == 200
    assert 'name="next_url" value="/ops/vehicles?q=3458"' in login_page.text

    response = client.post(
        "/ops/login",
        data={
            "email": "operator@example.test",
            "password": PASSWORD,
            "next_url": "/ops/vehicles?q=3458",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/vehicles?q=3458"

    second_client = TestClient(app)
    external_page = second_client.get("/ops/login?next=https://evil.example/steal")
    assert external_page.status_code == 200
    assert 'name="next_url" value="/ops/vehicles"' in external_page.text
    response = second_client.post(
        "/ops/login",
        data={
            "email": "operator@example.test",
            "password": PASSWORD,
            "next_url": "https://evil.example/steal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/vehicles"


def test_ops_writes_require_explicit_csrf(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "csrf-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="csrf-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
    client = TestClient(app)
    response = client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/ops/vehicles",
        data={"vin": VIN},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_legacy_public_vin_route_remains_closed():
    response = TestClient(app).get("/vin")
    assert response.status_code == 410


def test_ops_vehicle_register_paginates_for_staff_and_preserves_query(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "pagination-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="pagination-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
        for number in range(125):
            aftercare_db.create_or_get_vehicle(
                db,
                f"LGXCD6CD4P{number:07d}",
                brand="BYD",
                model_label=f"Model {number}",
                hmac_secret="pagination-test-secret",
            )

    client = TestClient(app)
    response = client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303

    first = client.get("/ops/vehicles")
    assert first.status_code == 200
    assert first.text.count('class="saved-vehicle"') == 100
    assert "····0124" in first.text
    assert "····0000" not in first.text
    assert 'href="/ops/vehicles?page=2&amp;q="' in first.text

    second = client.get("/ops/vehicles?page=2")
    assert second.status_code == 200
    assert second.text.count('class="saved-vehicle"') == 25
    assert "····0000" in second.text
    assert 'href="/ops/vehicles?page=1&amp;q="' in second.text
    assert "Next" not in second.text

    filtered_first = client.get("/ops/vehicles", params={"q": "BYD"})
    assert filtered_first.status_code == 200
    assert filtered_first.text.count('class="saved-vehicle"') == 100
    assert 'href="/ops/vehicles?page=2&amp;q=BYD"' in filtered_first.text

    filtered_second = client.get("/ops/vehicles", params={"page": 2, "q": "BYD"})
    assert filtered_second.status_code == 200
    assert filtered_second.text.count('class="saved-vehicle"') == 25
    assert 'href="/ops/vehicles?page=1&amp;q=BYD"' in filtered_second.text
    assert "····0000" in filtered_second.text

    detail = client.get("/ops/vehicles/125")
    assert detail.status_code == 200
    assert 'href="/ops/vehicles"' in detail.text


def test_ops_maintenance_history_shows_provider_and_source(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "history-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="history-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, VIN, hmac_secret="history-test-secret"
        )
        aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle["id"],
            maintenance_date="2026-08-29",
            maintenance_type="inspection",
            summary="Receiving check",
            service_provider="Dubai workshop",
            source_reference="job-1001",
        )

    client = TestClient(app)
    response = client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get("/ops/vehicles/1")
    assert detail.status_code == 200
    assert "Dubai workshop" in detail.text
    assert "job-1001" in detail.text


def test_overview_ledger_filters_and_keeps_query_on_pages(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "overview-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="overview-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
        aftercare_db.create_or_get_vehicle(
            db, VIN, brand="Hyundai", hmac_secret="overview-test-secret"
        )
        aftercare_db.create_or_get_vehicle(
            db,
            "LGXCD6CD4P0123459",
            brand="BYD",
            model_label="Seal",
            model_year=2024,
            hmac_secret="overview-test-secret",
        )
        aftercare_db.create_or_get_vehicle(
            db,
            "LGXCD6CD4P0123460",
            brand="BYD",
            model_label="Han",
            hmac_secret="overview-test-secret",
        )

    client = TestClient(app)
    assert client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    ).status_code == 303

    monkeypatch.setattr("app.aftercare_routes.OVERVIEW_PAGE_SIZE", 1)

    blank = client.get("/ops/overview")
    assert blank.status_code == 200
    assert "车辆总览" in blank.text
    assert 'name="brand"' in blank.text
    assert "未登记" in blank.text
    assert "出厂价" not in blank.text
    assert "Hyundai" in blank.text or "BYD" in blank.text

    filtered = client.get("/ops/overview", params={"brand": "Hyundai"})
    assert filtered.status_code == 200
    assert "Hyundai" in filtered.text
    assert "BYD" not in filtered.text
    assert 'name="brand" value="Hyundai"' in filtered.text
    assert 'href="/ops/vehicles/1"' in filtered.text

    empty = client.get("/ops/overview", params={"brand": "Hyundai", "model_year": "2024"})
    assert empty.status_code == 200
    assert "没有符合筛选条件的车辆" in empty.text

    invalid = client.get("/ops/overview", params={"model_year": "18"})
    assert invalid.status_code == 200
    assert "请检查年份或日期筛选后再查询" in invalid.text
    assert 'name="model_year" value="18"' in invalid.text
    assert "Hyundai" not in invalid.text

    paged = client.get("/ops/overview")
    assert paged.status_code == 200
    assert "rel=\"next\"" in paged.text
    next_link = [part for part in paged.text.split("href=") if "page=2" in part]
    assert next_link
    href = next_link[0].split('"', 2)[1].replace("&amp;", "&")
    parsed = parse_qs(urlsplit(href).query)
    assert parsed.get("page") == ["2"]

    filtered_page = client.get("/ops/overview", params={"brand": "BYD"})
    assert filtered_page.status_code == 200
    assert "BYD" in filtered_page.text
    assert "Hyundai" not in filtered_page.text
    assert 'rel="next"' in filtered_page.text
    next_filtered = [part for part in filtered_page.text.split("href=") if "page=2" in part][0]
    href = next_filtered.split('"', 2)[1].replace("&amp;", "&")
    parsed = parse_qs(urlsplit(href).query)
    assert parsed.get("page") == ["2"]
    assert parsed.get("brand") == ["BYD"]



def test_unauthenticated_login_brand_stays_on_login():
    login = TestClient(app).get("/ops/login")
    assert login.status_code == 200
    assert 'class="brand" href="/ops/login"' in login.text
    assert 'src="/static/lime-logo.png"' in login.text
    assert "AFTERCARE" in login.text
    assert "brand-mark" not in login.text


def test_maintenance_type_renders_localized_label_not_raw_enum(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "label-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="label-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, VIN, brand="BYD", hmac_secret="label-test-secret"
        )
        aftercare_db.create_maintenance_record(
            db,
            vehicle_id=vehicle["id"],
            maintenance_date="2026-09-08",
            maintenance_type="inspection",
            summary="PDI check",
        )
        db.commit()

    client = TestClient(app)
    client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    zh = client.get(f"/ops/vehicles/{vehicle['id']}?lang=zh")
    assert zh.status_code == 200
    assert "<td>检查</td>" in zh.text
    assert "<td>inspection</td>" not in zh.text

    en = client.get(f"/ops/vehicles/{vehicle['id']}?lang=en")
    assert en.status_code == 200
    assert "<td>inspection</td>" in en.text


def test_maintenance_form_exposes_server_side_maxlength_bounds(tmp_path, monkeypatch):
    db_path = tmp_path / "aftercare.sqlite3"
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", "bound-test-secret")
    with aftercare_db.connect(db_path, hmac_secret="bound-test-secret") as db:
        aftercare_db.create_staff_user(
            db,
            email="operator@example.test",
            password_hash=create_staff_password(PASSWORD),
        )
        vehicle, _ = aftercare_db.create_or_get_vehicle(
            db, VIN, brand="BYD", hmac_secret="bound-test-secret"
        )
        db.commit()

    client = TestClient(app)
    client.post(
        "/ops/login",
        data={"email": "operator@example.test", "password": PASSWORD},
        follow_redirects=False,
    )
    form = client.get(f"/ops/vehicles/{vehicle['id']}/maintenance/new")
    assert form.status_code == 200
    assert f'name="summary" rows="3" maxlength="{aftercare_db.MAINTENANCE_SUMMARY_LIMIT}"' in form.text
    assert f'name="service_provider" maxlength="{aftercare_db.MAINTENANCE_PROVIDER_LIMIT}"' in form.text
    assert f'name="source_reference" maxlength="{aftercare_db.MAINTENANCE_REFERENCE_LIMIT}"' in form.text
    assert f'name="note" rows="3" maxlength="{aftercare_db.MAINTENANCE_NOTE_LIMIT}"' in form.text

    csrf = client.cookies.get("limeauto_aftercare_csrf")
    oversized = client.post(
        f"/ops/vehicles/{vehicle['id']}/maintenance",
        data={
            "maintenance_date": "2026-09-08",
            "maintenance_type": "repair",
            "summary": "x" * (aftercare_db.MAINTENANCE_SUMMARY_LIMIT + 1),
            "csrf_token": csrf,
        },
    )
    assert oversized.status_code == 200
    assert "文本内容超出可保存长度" in oversized.text
    with aftercare_db.connect(db_path, hmac_secret="bound-test-secret") as db:
        assert db.execute("SELECT COUNT(*) FROM maintenance_records").fetchone()[0] == 0
