"""The catalog answers only to a signed-in staff session.

The gate is the only thing standing between the internal catalog and the internet, so these
tests cover both directions: what a signed-out visitor gets, and what a signed-in one gets.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import main
from app.catalog_release import CatalogReleaseStore
from tests.staff_session import (
    STAFF_EMAIL,
    STAFF_PASSWORD,
    anonymous_client,
    catalog_staff_client,
    create_staff_account,
    staff_client,
)
from tests.test_catalog_routes import HYE_MODEL, HYE_SERIES, make_release

PAGE_ROUTES = (
    "/catalog",
    "/catalog/search",
    "/catalog/search-models",
    f"/catalog/{HYE_SERIES}",
    f"/catalog/{HYE_SERIES}/{HYE_MODEL}",
)
MEDIA_ROUTES = (
    "/media/catalog/token/token",
    "/media/catalog-model/token/token",
)


def test_catalog_page_sends_a_signed_out_visitor_to_the_staff_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = anonymous_client(monkeypatch, make_release(tmp_path))

    for route in PAGE_ROUTES:
        response = client.get(route, follow_redirects=False)
        assert response.status_code == 303, route
        location = response.headers["location"]
        assert location.startswith("/ops/login?next="), route
        assert parse_qs(urlsplit(location).query)["next"] == [route]
        # A redirect must not become a way to read the page it points at.
        assert "release" not in response.text.lower()


def test_catalog_redirect_keeps_the_query_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = anonymous_client(monkeypatch, make_release(tmp_path))

    response = client.get(
        "/catalog/search", params={"material_name": "brake", "lang": "en"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["next"] == [
        "/catalog/search?material_name=brake&lang=en"
    ]

    # Following the redirect to the login page and signing in returns to the same place.
    login = client.get(response.headers["location"])
    assert login.status_code == 200
    assert 'name="next_url" value="/catalog/search?material_name=brake&amp;lang=en"' in login.text


def test_catalog_media_refuses_a_signed_out_visitor_without_redirecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = anonymous_client(monkeypatch, make_release(tmp_path))

    for route in MEDIA_ROUTES:
        response = client.get(route)
        assert response.status_code == 403, route
        assert response.json()["code"] == "staff_session_required"
        assert "location" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"


def test_signed_in_staff_reach_the_catalog_and_its_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = make_release(tmp_path, with_node_assets=True)
    client = catalog_staff_client(monkeypatch, store)

    assert client.get("/catalog").status_code == 200
    assert client.get(f"/catalog/{HYE_SERIES}/{HYE_MODEL}").status_code == 200
    # Media keeps its own 404 for an unknown key instead of the gate's 403: the point is
    # that the session got past the gate and the handler answered.
    assert client.get("/media/catalog/token/token").status_code == 404


def test_sign_in_session_cookie_covers_the_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = make_release(tmp_path)
    db_path = tmp_path / "aftercare.sqlite3"
    create_staff_account(monkeypatch, db_path)
    monkeypatch.setattr(main, "get_release_store", lambda: store)
    monkeypatch.setattr(main, "CATALOG_BROWSE_ENABLED", True)
    client = TestClient(main.app)

    response = client.post(
        "/ops/login",
        data={"email": STAFF_EMAIL, "password": STAFF_PASSWORD},
        follow_redirects=False,
    )

    # Path=/ops would leave the cookie unsent on every catalog request, which is exactly the
    # failure this gate would then report as "signed out" while staff were signed in.
    assert response.status_code == 303
    session_cookie = response.headers["set-cookie"]
    assert "limeauto_aftercare_session=" in session_cookie
    assert "Path=/;" in session_cookie
    assert client.get("/catalog").status_code == 200


def test_catalog_is_indistinguishable_from_absent_while_browse_is_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = make_release(tmp_path)
    monkeypatch.setattr(main, "get_release_store", lambda: store)
    monkeypatch.setattr(main, "CATALOG_BROWSE_ENABLED", False)
    client = TestClient(main.app)

    # Closed means 410 for everyone, signed in or not: no redirect, so a closed catalog does
    # not advertise that the staff login exists.
    for route in (*PAGE_ROUTES, *MEDIA_ROUTES):
        response = client.get(route)
        assert response.status_code == 410, route
        assert response.json()["code"] == "catalog_browse_disabled"
        assert "location" not in response.headers


def test_broken_session_store_is_reported_as_such_not_as_signed_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = make_release(tmp_path)
    monkeypatch.setattr(main, "get_release_store", lambda: store)
    monkeypatch.setattr(main, "CATALOG_BROWSE_ENABLED", True)
    # A directory where the database should be: the session store cannot be read at all.
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(tmp_path))
    client = TestClient(main.app)

    # A visitor carrying a session cannot be served and is not told "signed out": staff sent
    # to a login page that cannot read its own store would see a wrong-password loop instead
    # of the broken deployment that it is.
    client.cookies.set("limeauto_aftercare_session", "some-session-token")
    response = client.get("/catalog", follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["code"] == "staff_session_unavailable"

    # Without a cookie there is nothing to look up, so the ordinary redirect still applies.
    anonymous = TestClient(main.app)
    assert anonymous.get("/catalog", follow_redirects=False).status_code == 303


def test_signing_out_closes_the_catalog_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = make_release(tmp_path)
    client = catalog_staff_client(monkeypatch, store)
    assert client.get("/catalog").status_code == 200

    csrf = client.cookies.get("limeauto_aftercare_csrf") or ""
    assert client.post("/ops/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303

    response = client.get("/catalog", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/media/catalog/token/token").status_code == 403


def test_expired_or_unknown_session_cookie_does_not_open_the_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = anonymous_client(monkeypatch, make_release(tmp_path))
    client.cookies.set("limeauto_aftercare_session", "not-a-real-session-token")

    response = client.get("/catalog", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ops/login?next=")


def test_catalog_gate_does_not_widen_the_login_next_whitelist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = staff_client(monkeypatch, db_path=tmp_path / "aftercare.sqlite3")

    # /catalog became a legal return target because the gate produces it; anything that
    # leaves the site is still replaced with the default.
    external = client.post(
        "/ops/login",
        data={"email": STAFF_EMAIL, "password": STAFF_PASSWORD, "next_url": "https://example.test/x"},
        follow_redirects=False,
    )
    assert external.headers["location"] == "/ops/vehicles"
    assert (
        client.post(
            "/ops/login",
            data={"email": STAFF_EMAIL, "password": STAFF_PASSWORD, "next_url": "//example.test/x"},
            follow_redirects=False,
        ).headers["location"]
        == "/ops/vehicles"
    )
    catalog_next = client.post(
        "/ops/login",
        data={"email": STAFF_EMAIL, "password": STAFF_PASSWORD, "next_url": "/catalog/search?q=1"},
        follow_redirects=False,
    )
    assert catalog_next.headers["location"] == "/catalog/search?q=1"


def test_health_and_static_stay_reachable_without_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(main, "CATALOG_BROWSE_ENABLED", True)
    client = TestClient(main.app)

    # The deployment probes the health endpoints and the login page needs its own assets;
    # gating them would turn the gate itself into an outage.
    assert client.get("/health/live").status_code == 200
    assert client.get("/ops/login").status_code == 200
    assert client.get("/static/site.css").status_code in {200, 304}


def test_media_cache_is_not_shared_between_visitors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = make_release(tmp_path)
    client = catalog_staff_client(monkeypatch, store)

    # Session-scoped content must not be marked shareable, or a cache could hand it to a
    # signed-out caller after one staff member fetched it.
    assert "public" not in main.CATALOG_MEDIA_CACHE_CONTROL
    assert main.CATALOG_MEDIA_CACHE_CONTROL.startswith("private")


def test_a_new_catalog_route_cannot_be_added_outside_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate reads the path prefix, not a list of routes, so this holds by construction."""
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/catalog"):
            continue
        assert main._matches_prefix(path, main.CATALOG_CLOSED_PREFIXES), path
