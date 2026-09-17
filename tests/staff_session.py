"""Signed-in staff clients for the tests that exercise the catalog.

The catalog is behind the staff session gate, so a catalog test needs a real session in a
real (temporary) aftercare database: the lookup hashes the cookie token and compares it
with a stored session row, so a planted cookie would prove nothing about the gate.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import aftercare_db, main
from app.aftercare_routes import create_staff_password

STAFF_EMAIL = "operator@example.test"
STAFF_PASSWORD = "LimeAuto-Route-Test-2026!"
STAFF_SECRET = "route-test-secret"


def aftercare_db_path_for(store) -> Path:
    """Put the session database next to the release the test just built."""
    return Path(store.database_path).parent / "aftercare.sqlite3"


def create_staff_account(monkeypatch, db_path: Path, *, secret: str = STAFF_SECRET) -> Path:
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_DB_PATH", str(db_path))
    monkeypatch.setenv("LIMEAUTO_AFTERCARE_HMAC_SECRET", secret)
    with aftercare_db.connect(db_path, hmac_secret=secret) as db:
        aftercare_db.create_staff_user(
            db,
            email=STAFF_EMAIL,
            password_hash=create_staff_password(STAFF_PASSWORD),
        )
    return db_path


def staff_client(monkeypatch, *, db_path: Path, secret: str = STAFF_SECRET) -> TestClient:
    """A client holding a session created through the real login route."""
    create_staff_account(monkeypatch, db_path, secret=secret)
    client = TestClient(main.app)
    response = client.post(
        "/ops/login",
        data={"email": STAFF_EMAIL, "password": STAFF_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


def catalog_staff_client(monkeypatch, store, *, db_path: Path | None = None) -> TestClient:
    """A signed-in client with the catalog open and the release store patched in."""
    monkeypatch.setattr(main, "get_release_store", lambda: store)
    monkeypatch.setattr(main, "CATALOG_BROWSE_ENABLED", True)
    return staff_client(monkeypatch, db_path=db_path or aftercare_db_path_for(store))


def anonymous_client(monkeypatch, store, *, db_path: Path | None = None) -> TestClient:
    """An unsigned client with the catalog open, for gate tests."""
    monkeypatch.setattr(main, "get_release_store", lambda: store)
    monkeypatch.setattr(main, "CATALOG_BROWSE_ENABLED", True)
    create_staff_account(monkeypatch, db_path or aftercare_db_path_for(store))
    return TestClient(main.app)
