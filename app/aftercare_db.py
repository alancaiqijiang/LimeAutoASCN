"""LimeAuto internal aftercare business database.

This module is deliberately separate from the immutable catalog release. It
stores staff-operated vehicle and maintenance facts plus an optional,
human-selected catalog navigation shortcut. It does not calculate policy
eligibility or vehicle fitment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "aftercare.sqlite3"
HMAC_SECRET_ENV = "LIMEAUTO_AFTERCARE_HMAC_SECRET"
_TEST_HMAC_SECRET = "limeauto-aftercare-test-only-not-for-production"
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
STAFF_ROLES = ("operator", "admin")
STAFF_STATUSES = ("active", "suspended")
MAINTENANCE_TYPES = ("maintenance", "inspection", "repair", "replacement", "other")
MAINTENANCE_ITEM_TYPES = MAINTENANCE_TYPES + ("adjustment", "recommendation")
MAINTENANCE_ITEM_STATUSES = ("completed", "recommended", "deferred", "not_completed")
MAINTENANCE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SQLITE_INTEGER_MIN = -(2**63)
SQLITE_INTEGER_MAX = 2**63 - 1
DEFAULT_MEDIA_ROOT = ROOT / "data" / "aftercare-media"
MEDIA_ROOT_ENV = "LIMEAUTO_AFTERCARE_MEDIA_ROOT"
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
ATTACHMENT_MAX_RECORD_BYTES = 50 * 1024 * 1024
ATTACHMENT_MAX_FILES = 20
ATTACHMENT_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
VEHICLE_LIFECYCLE_COLUMNS = (
    ("model_year", "INTEGER"),
    ("color", "TEXT"),
    ("delivery_date", "TEXT"),
    ("registration_date", "TEXT"),
    ("departure_date", "TEXT"),
    ("license_plate_date", "TEXT"),
)
MAINTENANCE_EXTRA_COLUMNS = (
    ("complaint", "TEXT"),
    ("correction", "TEXT"),
)
_NEW_ATTACHMENT_FILES: dict[int, list[Path]] = {}
_DELETED_ATTACHMENT_FILES: dict[int, list[Path]] = {}


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS staff_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'admin')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin_normalized TEXT NOT NULL UNIQUE,
    vin_hmac TEXT NOT NULL UNIQUE,
    vin_last4 TEXT NOT NULL CHECK (length(vin_last4) = 4),
    brand TEXT,
    model_label TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    model_year INTEGER,
    color TEXT,
    delivery_date TEXT,
    registration_date TEXT,
    departure_date TEXT,
    license_plate_date TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL,
    maintenance_date TEXT NOT NULL,
    odometer_km INTEGER,
    maintenance_type TEXT NOT NULL CHECK (
        maintenance_type IN ('maintenance', 'inspection', 'repair', 'replacement', 'other')
    ),
    summary TEXT NOT NULL,
    service_provider TEXT,
    source_reference TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    complaint TEXT,
    correction TEXT,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS maintenance_record_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maintenance_record_id INTEGER NOT NULL,
    sequence_no INTEGER NOT NULL DEFAULT 1,
    item_type TEXT NOT NULL CHECK (
        item_type IN (
            'maintenance', 'inspection', 'repair', 'replacement', 'adjustment', 'recommendation'
        )
    ),
    item_name TEXT NOT NULL,
    material_code TEXT,
    quantity INTEGER,
    item_status TEXT NOT NULL DEFAULT 'completed' CHECK (
        item_status IN ('completed', 'recommended', 'deferred', 'not_completed')
    ),
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (maintenance_record_id) REFERENCES maintenance_records(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS maintenance_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maintenance_record_id INTEGER NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    original_filename TEXT,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256 TEXT NOT NULL,
    uploaded_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (maintenance_record_id) REFERENCES maintenance_records(id) ON DELETE CASCADE,
    FOREIGN KEY (uploaded_by) REFERENCES staff_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS vehicle_catalog_shortcuts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL,
    series_code TEXT NOT NULL,
    model_code TEXT NOT NULL,
    confirmation_note TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    created_by INTEGER,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES staff_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (actor_user_id) REFERENCES staff_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS staff_sessions (
    token_hash TEXT PRIMARY KEY,
    staff_user_id INTEGER NOT NULL,
    csrf_token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY (staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS maintenance_vehicle_date_idx
    ON maintenance_records (vehicle_id, maintenance_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS shortcut_vehicle_history_idx
    ON vehicle_catalog_shortcuts (vehicle_id, created_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS shortcut_one_current_idx
    ON vehicle_catalog_shortcuts (vehicle_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS audit_target_idx
    ON audit_events (target_type, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS staff_sessions_expiry_idx
    ON staff_sessions (expires_at);
CREATE INDEX IF NOT EXISTS maintenance_items_record_idx
    ON maintenance_record_items (maintenance_record_id, sequence_no, id);
CREATE INDEX IF NOT EXISTS maintenance_attachments_record_idx
    ON maintenance_attachments (maintenance_record_id, id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_vin(value: str) -> str:
    """Normalize a VIN and reject malformed values before any database write."""

    if not isinstance(value, str):
        raise ValueError("VIN must be text")
    normalized = re.sub(r"[\s-]+", "", value).upper()
    if not VIN_RE.fullmatch(normalized):
        raise ValueError("VIN must contain 17 valid characters")
    return normalized


def _hmac_secret(secret: str | bytes | None = None) -> bytes:
    if secret is not None:
        return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    configured = os.getenv(HMAC_SECRET_ENV)
    if configured:
        return configured.encode("utf-8")
    # Pytest sets this variable for every test invocation. This fallback makes
    # isolated unit tests deterministic but is never used by a normal process.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return _TEST_HMAC_SECRET.encode("utf-8")
    raise RuntimeError(f"{HMAC_SECRET_ENV} must be configured")


def vin_hmac(value: str, secret: str | bytes | None = None) -> str:
    normalized = normalize_vin(value)
    return hmac.new(_hmac_secret(secret), normalized.encode("ascii"), hashlib.sha256).hexdigest()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(item) for item in rows]


def initialize_schema(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    _ensure_columns(db, "vehicles", VEHICLE_LIFECYCLE_COLUMNS)
    _ensure_columns(db, "maintenance_records", MAINTENANCE_EXTRA_COLUMNS)


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(
    db: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    existing = _table_columns(db, table)
    for name, declaration in columns:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def media_root(path: str | Path | None = None) -> Path:
    raw = path or os.getenv(MEDIA_ROOT_ENV) or DEFAULT_MEDIA_ROOT
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def connect(
    path: str | Path | None = None,
    *,
    hmac_secret: str | bytes | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open an isolated aftercare database and initialize it idempotently.

    ``hmac_secret`` is accepted for callers that want to validate a secret at
    connection setup; query helpers may receive the same value explicitly.
    The database connection itself never stores the secret.
    """

    if hmac_secret is not None:
        _hmac_secret(hmac_secret)
    db_path = Path(path or os.getenv("LIMEAUTO_AFTERCARE_DB_PATH") or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db_id = id(db)
    _NEW_ATTACHMENT_FILES[db_id] = []
    _DELETED_ATTACHMENT_FILES[db_id] = []
    initialize_schema(db)
    try:
        yield db
        db.commit()
        for stored in _DELETED_ATTACHMENT_FILES.pop(db_id, []):
            stored.unlink(missing_ok=True)
    except Exception:
        db.rollback()
        for stored in _NEW_ATTACHMENT_FILES.pop(db_id, []):
            stored.unlink(missing_ok=True)
        raise
    finally:
        _NEW_ATTACHMENT_FILES.pop(db_id, None)
        _DELETED_ATTACHMENT_FILES.pop(db_id, None)
        db.close()


def create_staff_user(
    db: sqlite3.Connection,
    *,
    email: str,
    password_hash: str,
    role: str = "operator",
    status: str = "active",
) -> dict[str, Any]:
    email = email.strip()
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("Invalid staff email")
    if role not in STAFF_ROLES or status not in STAFF_STATUSES:
        raise ValueError("Invalid staff role or status")
    now = utc_now()
    cursor = db.execute(
        """
        INSERT INTO staff_users(email, password_hash, role, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (email, password_hash, role, status, now, now),
    )
    return staff_user_by_id(db, int(cursor.lastrowid))  # type: ignore[return-value]


def staff_user_by_id(db: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    return _row(db.execute("SELECT * FROM staff_users WHERE id = ?", (user_id,)).fetchone())


def staff_user_by_email(db: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    return _row(
        db.execute("SELECT * FROM staff_users WHERE email = ? COLLATE NOCASE", (email.strip(),)).fetchone()
    )


def update_staff_login(db: sqlite3.Connection, user_id: int) -> None:
    now = utc_now()
    db.execute(
        "UPDATE staff_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
        (now, now, user_id),
    )


def create_staff_session(
    db: sqlite3.Connection,
    *,
    staff_user_id: int,
    token_hash: str,
    csrf_token_hash: str,
    expires_at: str,
) -> None:
    if staff_user_by_id(db, staff_user_id) is None:
        raise ValueError("Staff user does not exist")
    now = utc_now()
    db.execute(
        """
        INSERT INTO staff_sessions(
            token_hash, staff_user_id, csrf_token_hash, expires_at, created_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (token_hash, staff_user_id, csrf_token_hash, expires_at, now, now),
    )


def staff_user_by_session(
    db: sqlite3.Connection,
    *,
    token_hash: str,
    now: str,
) -> dict[str, Any] | None:
    row = db.execute(
        """
        SELECT su.*
        FROM staff_sessions ss
        JOIN staff_users su ON su.id = ss.staff_user_id
        WHERE ss.token_hash = ? AND ss.expires_at > ? AND su.status = 'active'
        """,
        (token_hash, now),
    ).fetchone()
    if row is None:
        db.execute("DELETE FROM staff_sessions WHERE token_hash = ?", (token_hash,))
        return None
    db.execute(
        "UPDATE staff_sessions SET last_seen_at = ? WHERE token_hash = ?",
        (now, token_hash),
    )
    return _row(row)


def csrf_hash_for_staff_session(db: sqlite3.Connection, token_hash: str) -> str | None:
    row = db.execute(
        "SELECT csrf_token_hash FROM staff_sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    return str(row["csrf_token_hash"]) if row else None


def delete_staff_session(db: sqlite3.Connection, token_hash: str) -> None:
    db.execute("DELETE FROM staff_sessions WHERE token_hash = ?", (token_hash,))


def delete_staff_user_sessions(db: sqlite3.Connection, staff_user_id: int) -> None:
    db.execute("DELETE FROM staff_sessions WHERE staff_user_id = ?", (staff_user_id,))


def _vehicle_lifecycle_values(
    *,
    model_year: int | None = None,
    color: str | None = None,
    delivery_date: str | None = None,
    registration_date: str | None = None,
    departure_date: str | None = None,
    license_plate_date: str | None = None,
) -> dict[str, Any]:
    return {
        "model_year": validate_model_year(model_year),
        "color": validate_optional_text(color, limit=80),
        "delivery_date": validate_optional_date(delivery_date, field="Delivery date"),
        "registration_date": validate_optional_date(registration_date, field="Registration date"),
        "departure_date": validate_optional_date(departure_date, field="Departure date"),
        "license_plate_date": validate_optional_date(license_plate_date, field="License plate date"),
    }


def create_or_get_vehicle(
    db: sqlite3.Connection,
    vin: str,
    *,
    brand: str | None = None,
    model_label: str | None = None,
    note: str | None = None,
    model_year: int | None = None,
    color: str | None = None,
    delivery_date: str | None = None,
    registration_date: str | None = None,
    departure_date: str | None = None,
    license_plate_date: str | None = None,
    hmac_secret: str | bytes | None = None,
) -> tuple[dict[str, Any], bool]:
    normalized = normalize_vin(vin)
    digest = vin_hmac(normalized, hmac_secret)
    existing = vehicle_by_hmac(db, digest)
    if existing is not None:
        return existing, False
    now = utc_now()
    brand = validate_optional_text(brand, limit=120)
    model_label = validate_optional_text(model_label, limit=200)
    note = validate_optional_text(note, limit=2000)
    lifecycle = _vehicle_lifecycle_values(
        model_year=model_year,
        color=color,
        delivery_date=delivery_date,
        registration_date=registration_date,
        departure_date=departure_date,
        license_plate_date=license_plate_date,
    )
    try:
        cursor = db.execute(
            """
            INSERT INTO vehicles(
                vin_normalized, vin_hmac, vin_last4, brand, model_label, note,
                model_year, color, delivery_date, registration_date, departure_date,
                license_plate_date, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized,
                digest,
                normalized[-4:],
                brand,
                model_label,
                note,
                lifecycle["model_year"],
                lifecycle["color"],
                lifecycle["delivery_date"],
                lifecycle["registration_date"],
                lifecycle["departure_date"],
                lifecycle["license_plate_date"],
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        raced = vehicle_by_hmac(db, digest) or _row(
            db.execute(
                "SELECT * FROM vehicles WHERE vin_normalized = ?", (normalized,)
            ).fetchone()
        )
        if raced is None:
            raise
        return raced, False
    return vehicle_by_id(db, int(cursor.lastrowid)), True  # type: ignore[return-value]


def update_vehicle(
    db: sqlite3.Connection,
    vehicle_id: int,
    *,
    brand: str | None = None,
    model_label: str | None = None,
    note: str | None = None,
    model_year: int | None = None,
    color: str | None = None,
    delivery_date: str | None = None,
    registration_date: str | None = None,
    departure_date: str | None = None,
    license_plate_date: str | None = None,
) -> dict[str, Any] | None:
    now = utc_now()
    brand = validate_optional_text(brand, limit=120)
    model_label = validate_optional_text(model_label, limit=200)
    note = validate_optional_text(note, limit=2000)
    lifecycle = _vehicle_lifecycle_values(
        model_year=model_year,
        color=color,
        delivery_date=delivery_date,
        registration_date=registration_date,
        departure_date=departure_date,
        license_plate_date=license_plate_date,
    )
    db.execute(
        """
        UPDATE vehicles
        SET brand = ?, model_label = ?, note = ?, model_year = ?, color = ?,
            delivery_date = ?, registration_date = ?, departure_date = ?,
            license_plate_date = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            brand,
            model_label,
            note,
            lifecycle["model_year"],
            lifecycle["color"],
            lifecycle["delivery_date"],
            lifecycle["registration_date"],
            lifecycle["departure_date"],
            lifecycle["license_plate_date"],
            now,
            vehicle_id,
        ),
    )
    return vehicle_by_id(db, vehicle_id)


def vehicle_by_id(db: sqlite3.Connection, vehicle_id: int) -> dict[str, Any] | None:
    return _row(db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone())


def vehicle_by_hmac(db: sqlite3.Connection, digest: str) -> dict[str, Any] | None:
    return _row(db.execute("SELECT * FROM vehicles WHERE vin_hmac = ?", (digest,)).fetchone())


def vehicle_by_vin(
    db: sqlite3.Connection,
    vin: str,
    *,
    hmac_secret: str | bytes | None = None,
) -> dict[str, Any] | None:
    return vehicle_by_hmac(db, vin_hmac(vin, hmac_secret))


def list_vehicles(db: sqlite3.Connection, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500 or offset < 0:
        raise ValueError("Invalid vehicle pagination")
    return _rows(
        db.execute(
            "SELECT * FROM vehicles ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    )


OVERVIEW_TEXT_FIELDS = ("model_label", "brand", "color")
OVERVIEW_DATE_FIELDS = (
    "delivery_date",
    "registration_date",
    "departure_date",
    "license_plate_date",
)
OVERVIEW_FILTER_FIELDS = (*OVERVIEW_TEXT_FIELDS, "model_year", *OVERVIEW_DATE_FIELDS)


def normalize_overview_filters(
    *,
    model_label: str = "",
    brand: str = "",
    color: str = "",
    model_year: str | int | None = "",
    delivery_date: str = "",
    registration_date: str = "",
    departure_date: str = "",
    license_plate_date: str = "",
) -> dict[str, Any]:
    """Return ledger filters. Empty means any value, including 未登记."""

    filters: dict[str, Any] = {
        "model_label": validate_optional_text(model_label, limit=200) or "",
        "brand": validate_optional_text(brand, limit=120) or "",
        "color": validate_optional_text(color, limit=80) or "",
        "delivery_date": "",
        "registration_date": "",
        "departure_date": "",
        "license_plate_date": "",
        "model_year": "",
    }
    year_text = "" if model_year is None else str(model_year).strip()
    if year_text:
        try:
            filters["model_year"] = str(validate_model_year(int(year_text)))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid model year filter") from exc
    for field in OVERVIEW_DATE_FIELDS:
        raw = {
            "delivery_date": delivery_date,
            "registration_date": registration_date,
            "departure_date": departure_date,
            "license_plate_date": license_plate_date,
        }[field]
        if not str(raw).strip():
            continue
        label = {
            "delivery_date": "Delivery date",
            "registration_date": "Registration date",
            "departure_date": "Departure date",
            "license_plate_date": "License plate date",
        }[field]
        filters[field] = validate_optional_date(str(raw), field=label) or ""
    return filters


def _overview_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for field in OVERVIEW_TEXT_FIELDS:
        value = filters.get(field) or ""
        if not value:
            continue
        clauses.append(f"v.{field} LIKE ? ESCAPE '\\' COLLATE NOCASE")
        params.append(_like_pattern(value))
    year = filters.get("model_year") or ""
    if year:
        clauses.append("v.model_year = ?")
        params.append(int(year))
    for field in OVERVIEW_DATE_FIELDS:
        value = filters.get(field) or ""
        if not value:
            continue
        clauses.append(f"v.{field} = ?")
        params.append(value)
    if not clauses:
        return "", []
    return "WHERE " + " AND ".join(clauses), params


def list_overview_vehicles(
    db: sqlite3.Connection,
    filters: dict[str, Any] | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500 or offset < 0:
        raise ValueError("Invalid vehicle pagination")
    where, params = _overview_where(filters or {})
    return _rows(
        db.execute(
            f"""
            SELECT * FROM vehicles v
            {where}
            ORDER BY v.updated_at DESC, v.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    )


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def validate_maintenance_date(value: str) -> str:
    """Return a trimmed ISO date or reject an invalid maintenance date."""

    if not isinstance(value, str):
        raise ValueError("Maintenance date must be a valid ISO date (YYYY-MM-DD)")
    normalized = value.strip()
    if not MAINTENANCE_DATE_RE.fullmatch(normalized):
        raise ValueError("Maintenance date must be a valid ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Maintenance date must be a valid ISO date (YYYY-MM-DD)") from exc
    return normalized


def validate_optional_date(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a valid ISO date (YYYY-MM-DD)")
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return validate_maintenance_date(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO date (YYYY-MM-DD)") from exc


def validate_model_year(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1900 or value > 2100:
        raise ValueError("Model year must be between 1900 and 2100")
    return value


def validate_optional_text(value: str | None, *, limit: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Text fields must be strings")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise ValueError("Text field is too long")
    return normalized


# Maintenance free-text bounds. Kept next to the validators so the form
# `maxlength` attributes and the server-side limits cannot drift apart.
MAINTENANCE_SUMMARY_LIMIT = 2000
MAINTENANCE_PROVIDER_LIMIT = 200
MAINTENANCE_REFERENCE_LIMIT = 200
MAINTENANCE_NOTE_LIMIT = 2000


def _validated_maintenance_text(
    summary: str,
    service_provider: str | None,
    source_reference: str | None,
    note: str | None,
    complaint: str | None,
    correction: str | None,
) -> dict[str, str | None]:
    if not summary.strip():
        raise ValueError("Maintenance summary is required")
    return {
        "summary": validate_optional_text(summary, limit=MAINTENANCE_SUMMARY_LIMIT) or "",
        "service_provider": validate_optional_text(
            service_provider, limit=MAINTENANCE_PROVIDER_LIMIT
        ),
        "source_reference": validate_optional_text(
            source_reference, limit=MAINTENANCE_REFERENCE_LIMIT
        ),
        "note": validate_optional_text(note, limit=MAINTENANCE_NOTE_LIMIT),
        "complaint": validate_optional_text(complaint, limit=MAINTENANCE_NOTE_LIMIT),
        "correction": validate_optional_text(correction, limit=MAINTENANCE_NOTE_LIMIT),
    }


def validate_odometer_km(value: int | None) -> int | None:
    """Return an odometer integer that can be stored by SQLite."""

    if value is None:
        return None
    if value < 0:
        raise ValueError("Odometer cannot be negative")
    if value < SQLITE_INTEGER_MIN or value > SQLITE_INTEGER_MAX:
        raise ValueError("Odometer exceeds SQLite INTEGER range")
    return value


def search_vehicles(
    db: sqlite3.Connection,
    query: str,
    *,
    hmac_secret: str | bytes | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return list_vehicles(db, limit=limit, offset=offset)
    if limit < 1 or limit > 500:
        raise ValueError("Invalid vehicle limit")
    if offset < 0:
        raise ValueError("Invalid vehicle offset")
    candidates: list[dict[str, Any]] = []
    try:
        exact = vehicle_by_vin(db, query, hmac_secret=hmac_secret)
    except ValueError:
        exact = None
    if exact is not None:
        # Exact VIN matches retain their existing priority in the result set.
        # Once the first page has consumed that match, offset the broad
        # search by one so later pages continue through the combined results.
        if offset == 0:
            candidates.append(exact)
        broad_offset = max(0, offset - 1)
    else:
        broad_offset = offset
    pattern = _like_pattern(query)
    where = """
        v.brand LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR v.model_label LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR v.note LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR v.vin_last4 LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR m.summary LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR m.service_provider LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR m.source_reference LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR m.note LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR m.complaint LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR m.correction LIKE ? ESCAPE '\\' COLLATE NOCASE
           OR EXISTS (
               SELECT 1 FROM maintenance_record_items i
               WHERE i.maintenance_record_id = m.id
                 AND (i.item_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                      OR i.material_code LIKE ? ESCAPE '\\' COLLATE NOCASE
                      OR i.note LIKE ? ESCAPE '\\' COLLATE NOCASE)
           )
    """
    params: list[Any] = [pattern] * 13
    if exact is not None:
        # The exact match is already represented as the first logical result;
        # exclude it from the broad search so it cannot be duplicated across
        # pages and so the existing exact-match priority remains intact.
        where += " AND v.id != ?"
        params.append(exact["id"])
    rows = db.execute(
        f"""
        SELECT DISTINCT v.*
        FROM vehicles v
        LEFT JOIN maintenance_records m ON m.vehicle_id = v.id
        WHERE ({where})
        ORDER BY v.updated_at DESC, v.id DESC
        LIMIT ?
        OFFSET ?
        """,
        (*params, limit + (1 if exact is not None else 0), broad_offset),
    ).fetchall()
    seen = {item["id"] for item in candidates}
    candidates.extend(dict(item) for item in rows if item["id"] not in seen)
    return _attach_search_matches(db, candidates, pattern, limit)


# Match disclosure rows shown under a vehicle search hit. Bounded so a
# vehicle with many records cannot flood the results page.
SEARCH_MATCH_LIMIT = 3


def _attach_search_matches(
    db: sqlite3.Connection,
    vehicles: list[dict[str, Any]],
    pattern: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not vehicles or len(vehicles) > limit:
        return vehicles
    match_clause = """
            SELECT i.maintenance_record_id AS record_id, m.maintenance_date, m.summary,
                   i.item_name, i.material_code, i.item_status, NULL AS complaint, NULL AS correction
            FROM maintenance_record_items i
            JOIN maintenance_records m ON m.id = i.maintenance_record_id
            WHERE m.vehicle_id = ?
              AND (i.item_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR i.material_code LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR i.note LIKE ? ESCAPE '\\' COLLATE NOCASE)
        UNION ALL
            SELECT m.id AS record_id, m.maintenance_date, m.summary, NULL AS item_name,
                   NULL AS material_code, NULL AS item_status, m.complaint, m.correction
            FROM maintenance_records m
            WHERE m.vehicle_id = ?
              AND (m.summary LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR m.service_provider LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR m.source_reference LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR m.note LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR m.complaint LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR m.correction LIKE ? ESCAPE '\\' COLLATE NOCASE)
    """
    for vehicle in vehicles:
        matches: list[dict[str, Any]] = []
        seen_records: set[int] = set()
        rows = db.execute(
            f"{match_clause} ORDER BY maintenance_date DESC, record_id DESC LIMIT ?",
            (vehicle["id"], *([pattern] * 3), vehicle["id"], *([pattern] * 6), SEARCH_MATCH_LIMIT * 2 + 1),
        ).fetchall()
        for row in rows:
            if int(row["record_id"]) in seen_records:
                continue
            seen_records.add(int(row["record_id"]))
            matches.append(dict(row))
            if len(matches) >= SEARCH_MATCH_LIMIT:
                break
        if matches:
            vehicle["match_lines"] = matches
    return vehicles


def create_maintenance_record(
    db: sqlite3.Connection,
    *,
    vehicle_id: int,
    maintenance_date: str,
    maintenance_type: str,
    summary: str,
    odometer_km: int | None = None,
    service_provider: str | None = None,
    source_reference: str | None = None,
    note: str | None = None,
    complaint: str | None = None,
    correction: str | None = None,
    items: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    maintenance_date = validate_maintenance_date(maintenance_date)
    if vehicle_by_id(db, vehicle_id) is None:
        raise ValueError("Vehicle does not exist")
    if maintenance_type not in MAINTENANCE_TYPES:
        raise ValueError("Invalid maintenance type")
    text = _validated_maintenance_text(
        summary, service_provider, source_reference, note, complaint, correction
    )
    odometer_km = validate_odometer_km(odometer_km)
    now = utc_now()
    cursor = db.execute(
        """
        INSERT INTO maintenance_records(
            vehicle_id, maintenance_date, odometer_km, maintenance_type, summary,
            service_provider, source_reference, note, complaint, correction,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vehicle_id,
            maintenance_date,
            odometer_km,
            maintenance_type,
            text["summary"],
            text["service_provider"],
            text["source_reference"],
            text["note"],
            text["complaint"],
            text["correction"],
            now,
            now,
        ),
    )
    record_id = cursor.lastrowid
    if record_id is None:
        raise RuntimeError("Failed to create maintenance record")
    replace_maintenance_items(db, int(record_id), items or [])
    return maintenance_by_id(db, int(record_id))  # type: ignore[return-value]


def maintenance_by_id(
    db: sqlite3.Connection,
    record_id: int,
    *,
    vehicle_id: int | None = None,
) -> dict[str, Any] | None:
    if vehicle_id is None:
        row = db.execute("SELECT * FROM maintenance_records WHERE id = ?", (record_id,)).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM maintenance_records WHERE id = ? AND vehicle_id = ?",
            (record_id, vehicle_id),
        ).fetchone()
    record = _row(row)
    if record is None:
        return None
    return _attach_maintenance_children(db, record)


def list_maintenance_records(
    db: sqlite3.Connection,
    vehicle_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if vehicle_by_id(db, vehicle_id) is None:
        raise ValueError("Vehicle does not exist")
    if limit < 1 or limit > 500 or offset < 0:
        raise ValueError("Invalid maintenance pagination")
    rows = _rows(
        db.execute(
            """
            SELECT m.*,
                   (
                       SELECT COUNT(*) FROM maintenance_record_items i
                       WHERE i.maintenance_record_id = m.id
                   ) AS item_count,
                   (
                       SELECT COUNT(*) FROM maintenance_attachments a
                       WHERE a.maintenance_record_id = m.id
                   ) AS attachment_count
            FROM maintenance_records m
            WHERE m.vehicle_id = ?
            ORDER BY m.maintenance_date DESC, m.id DESC
            LIMIT ? OFFSET ?
            """,
            (vehicle_id, limit, offset),
        ).fetchall()
    )
    return [_attach_maintenance_children(db, row) for row in rows]


def update_maintenance_record(
    db: sqlite3.Connection,
    record_id: int,
    *,
    maintenance_date: str,
    maintenance_type: str,
    summary: str,
    odometer_km: int | None = None,
    service_provider: str | None = None,
    source_reference: str | None = None,
    note: str | None = None,
    complaint: str | None = None,
    correction: str | None = None,
    items: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    maintenance_date = validate_maintenance_date(maintenance_date)
    if maintenance_type not in MAINTENANCE_TYPES or not summary.strip():
        raise ValueError("Invalid maintenance record")
    text = _validated_maintenance_text(
        summary, service_provider, source_reference, note, complaint, correction
    )
    odometer_km = validate_odometer_km(odometer_km)
    db.execute(
        """
        UPDATE maintenance_records
        SET maintenance_date = ?, odometer_km = ?, maintenance_type = ?, summary = ?,
            service_provider = ?, source_reference = ?, note = ?, complaint = ?,
            correction = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            maintenance_date,
            odometer_km,
            maintenance_type,
            text["summary"],
            text["service_provider"],
            text["source_reference"],
            text["note"],
            text["complaint"],
            text["correction"],
            utc_now(),
            record_id,
        ),
    )
    if items is not None:
        replace_maintenance_items(db, record_id, items)
    return maintenance_by_id(db, record_id)


def _attach_maintenance_children(
    db: sqlite3.Connection, record: dict[str, Any]
) -> dict[str, Any]:
    record_id = int(record["id"])
    record["items"] = list_maintenance_items(db, record_id)
    record["attachments"] = list_maintenance_attachments(db, record_id)
    record["item_count"] = int(record.get("item_count") or len(record["items"]))
    record["attachment_count"] = int(record.get("attachment_count") or len(record["attachments"]))
    return record


def _normalize_maintenance_item(item: Mapping[str, Any], sequence_no: int) -> dict[str, Any]:
    item_type = str(item.get("item_type") or "").strip()
    item_name = str(item.get("item_name") or "").strip()
    item_status = str(item.get("item_status") or "completed").strip()
    if item_type not in MAINTENANCE_ITEM_TYPES:
        raise ValueError("Invalid maintenance item type")
    if not item_name:
        raise ValueError("Maintenance item name is required")
    if item_status not in MAINTENANCE_ITEM_STATUSES:
        raise ValueError("Invalid maintenance item status")
    quantity = item.get("quantity")
    if quantity is not None and quantity != "":
        try:
            quantity = int(quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError("Item quantity must be an integer") from exc
        if quantity < 0:
            raise ValueError("Item quantity cannot be negative")
        if quantity > SQLITE_INTEGER_MAX:
            raise ValueError("Item quantity exceeds SQLite INTEGER range")
    else:
        quantity = None
    return {
        "sequence_no": sequence_no,
        "item_type": item_type,
        "item_name": item_name[:200],
        "material_code": validate_optional_text(item.get("material_code"), limit=80),
        "quantity": quantity,
        "item_status": item_status,
        "note": validate_optional_text(item.get("note"), limit=500),
    }


def list_maintenance_items(db: sqlite3.Connection, record_id: int) -> list[dict[str, Any]]:
    return _rows(
        db.execute(
            """
            SELECT * FROM maintenance_record_items
            WHERE maintenance_record_id = ?
            ORDER BY sequence_no, id
            """,
            (record_id,),
        ).fetchall()
    )


def replace_maintenance_items(
    db: sqlite3.Connection,
    record_id: int,
    items: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if maintenance_by_id_raw(db, record_id) is None:
        raise ValueError("Maintenance record does not exist")
    normalized = [
        _normalize_maintenance_item(item, index)
        for index, item in enumerate(items, start=1)
        if str(item.get("item_name") or "").strip()
    ]
    db.execute("DELETE FROM maintenance_record_items WHERE maintenance_record_id = ?", (record_id,))
    now = utc_now()
    for item in normalized:
        db.execute(
            """
            INSERT INTO maintenance_record_items(
                maintenance_record_id, sequence_no, item_type, item_name, material_code,
                quantity, item_status, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                item["sequence_no"],
                item["item_type"],
                item["item_name"],
                item["material_code"],
                item["quantity"],
                item["item_status"],
                item["note"],
                now,
                now,
            ),
        )
    return list_maintenance_items(db, record_id)


def maintenance_by_id_raw(
    db: sqlite3.Connection, record_id: int, *, vehicle_id: int | None = None
) -> dict[str, Any] | None:
    if vehicle_id is None:
        row = db.execute("SELECT * FROM maintenance_records WHERE id = ?", (record_id,)).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM maintenance_records WHERE id = ? AND vehicle_id = ?",
            (record_id, vehicle_id),
        ).fetchone()
    return _row(row)


def list_maintenance_attachments(db: sqlite3.Connection, record_id: int) -> list[dict[str, Any]]:
    return _rows(
        db.execute(
            """
            SELECT * FROM maintenance_attachments
            WHERE maintenance_record_id = ?
            ORDER BY id
            """,
            (record_id,),
        ).fetchall()
    )


def attachment_by_id(
    db: sqlite3.Connection,
    attachment_id: int,
    *,
    record_id: int | None = None,
    vehicle_id: int | None = None,
) -> dict[str, Any] | None:
    if vehicle_id is not None:
        query = """
            SELECT a.*
            FROM maintenance_attachments a
            JOIN maintenance_records m ON m.id = a.maintenance_record_id
            WHERE a.id = ? AND m.vehicle_id = ?
            """
        params: tuple[Any, ...] = (attachment_id, vehicle_id)
        if record_id is not None:
            query += " AND a.maintenance_record_id = ?"
            params = (attachment_id, vehicle_id, record_id)
        row = db.execute(query, params).fetchone()
    elif record_id is not None:
        row = db.execute(
            "SELECT * FROM maintenance_attachments WHERE id = ? AND maintenance_record_id = ?",
            (attachment_id, record_id),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM maintenance_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    return _row(row)


def detect_image_mime(data: bytes) -> str | None:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_attachment_path(object_key: str, *, root: Path | None = None) -> Path | None:
    if not object_key or ".." in object_key.split("/") or object_key.startswith("/"):
        return None
    base = root or media_root()
    path = (base / object_key).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return None
    return path


def save_maintenance_attachment(
    db: sqlite3.Connection,
    *,
    record_id: int,
    data: bytes,
    original_filename: str | None = None,
    uploaded_by: int | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    if maintenance_by_id_raw(db, record_id) is None:
        raise ValueError("Maintenance record does not exist")
    if not data:
        raise ValueError("Attachment is empty")
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise ValueError("Attachment exceeds 10 MB")
    mime_type = detect_image_mime(data)
    if mime_type not in ATTACHMENT_MIME_TYPES:
        raise ValueError("Attachment must be a JPEG, PNG, or WEBP image")
    existing = list_maintenance_attachments(db, record_id)
    if len(existing) >= ATTACHMENT_MAX_FILES:
        raise ValueError("Too many attachments on this record")
    used = sum(int(item["size_bytes"]) for item in existing)
    if used + len(data) > ATTACHMENT_MAX_RECORD_BYTES:
        raise ValueError("Attachments exceed 50 MB for this record")
    digest = hashlib.sha256(data).hexdigest()
    extension = ATTACHMENT_MIME_TYPES[mime_type]
    object_key = f"{record_id}/{secrets.token_hex(16)}{extension}"
    base = root or media_root()
    path = resolve_attachment_path(object_key, root=base)
    if path is None:
        raise ValueError("Invalid attachment path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _NEW_ATTACHMENT_FILES.setdefault(id(db), []).append(path)
    filename = validate_optional_text(original_filename, limit=180)
    try:
        cursor = db.execute(
            """
            INSERT INTO maintenance_attachments(
                maintenance_record_id, object_key, original_filename, mime_type,
                size_bytes, sha256, uploaded_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                object_key,
                filename,
                mime_type,
                len(data),
                digest,
                uploaded_by,
                utc_now(),
            ),
        )
    except Exception:
        path.unlink(missing_ok=True)
        tracked = _NEW_ATTACHMENT_FILES.get(id(db))
        if tracked and path in tracked:
            tracked.remove(path)
        raise
    return attachment_by_id(db, int(cursor.lastrowid))  # type: ignore[return-value]


def delete_maintenance_attachment(
    db: sqlite3.Connection,
    attachment_id: int,
    *,
    record_id: int | None = None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    attachment = attachment_by_id(db, attachment_id, record_id=record_id)
    if attachment is None:
        return None
    db.execute("DELETE FROM maintenance_attachments WHERE id = ?", (attachment_id,))
    path = resolve_attachment_path(str(attachment["object_key"]), root=root)
    if path is not None:
        _DELETED_ATTACHMENT_FILES.setdefault(id(db), []).append(path)
    return attachment


def current_catalog_shortcut(
    db: sqlite3.Connection,
    vehicle_id: int,
) -> dict[str, Any] | None:
    return _row(
        db.execute(
            "SELECT * FROM vehicle_catalog_shortcuts WHERE vehicle_id = ? AND is_current = 1",
            (vehicle_id,),
        ).fetchone()
    )


def list_catalog_shortcuts(db: sqlite3.Connection, vehicle_id: int) -> list[dict[str, Any]]:
    return _rows(
        db.execute(
            """
            SELECT * FROM vehicle_catalog_shortcuts
            WHERE vehicle_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (vehicle_id,),
        ).fetchall()
    )


def set_catalog_shortcut(
    db: sqlite3.Connection,
    *,
    vehicle_id: int,
    series_code: str,
    model_code: str,
    confirmation_note: str | None = None,
    created_by: int | None = None,
) -> dict[str, Any]:
    if vehicle_by_id(db, vehicle_id) is None:
        raise ValueError("Vehicle does not exist")
    if not series_code.strip() or not model_code.strip():
        raise ValueError("Catalog series and model are required")
    if created_by is not None and staff_user_by_id(db, created_by) is None:
        raise ValueError("Staff user does not exist")
    now = utc_now()
    note = validate_optional_text(confirmation_note, limit=500)
    db.execute(
        """
        UPDATE vehicle_catalog_shortcuts
        SET is_current = 0, superseded_at = ?
        WHERE vehicle_id = ? AND is_current = 1
        """,
        (now, vehicle_id),
    )
    try:
        cursor = db.execute(
            """
            INSERT INTO vehicle_catalog_shortcuts(
                vehicle_id, series_code, model_code, confirmation_note,
                is_current, created_by, created_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (vehicle_id, series_code.strip(), model_code.strip(), note, created_by, now),
        )
    except sqlite3.IntegrityError:
        current = current_catalog_shortcut(db, vehicle_id)
        if current is None:
            raise
        return current
    return _row(
        db.execute("SELECT * FROM vehicle_catalog_shortcuts WHERE id = ?", (cursor.lastrowid,)).fetchone()
    )  # type: ignore[return-value]


def remove_catalog_shortcut(db: sqlite3.Connection, vehicle_id: int) -> bool:
    cursor = db.execute(
        """
        UPDATE vehicle_catalog_shortcuts
        SET is_current = 0, superseded_at = ?
        WHERE vehicle_id = ? AND is_current = 1
        """,
        (utc_now(), vehicle_id),
    )
    return cursor.rowcount > 0


def append_audit_event(
    db: sqlite3.Connection,
    *,
    actor_user_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if actor_user_id is not None and staff_user_by_id(db, actor_user_id) is None:
        raise ValueError("Staff user does not exist")
    if not action.strip():
        raise ValueError("Audit action is required")
    encoded = json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cursor = db.execute(
        """
        INSERT INTO audit_events(
            actor_user_id, action, target_type, target_id, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (actor_user_id, action.strip(), target_type, target_id, encoded, utc_now()),
    )
    return _row(db.execute("SELECT * FROM audit_events WHERE id = ?", (cursor.lastrowid,)).fetchone())  # type: ignore[return-value]


def list_audit_events(db: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500:
        raise ValueError("Invalid audit limit")
    return _rows(
        db.execute(
            "SELECT * FROM audit_events ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    )


__all__ = [
    "DEFAULT_DB_PATH",
    "HMAC_SECRET_ENV",
    "MAINTENANCE_TYPES",
    "SCHEMA",
    "append_audit_event",
    "connect",
    "create_or_get_vehicle",
    "create_maintenance_record",
    "create_staff_user",
    "current_catalog_shortcut",
    "initialize_schema",
    "list_audit_events",
    "list_catalog_shortcuts",
    "list_maintenance_records",
    "list_overview_vehicles",
    "list_vehicles",
    "normalize_overview_filters",
    "maintenance_by_id",
    "normalize_vin",
    "validate_maintenance_date",
    "validate_odometer_km",
    "SQLITE_INTEGER_MAX",
    "SQLITE_INTEGER_MIN",
    "remove_catalog_shortcut",
    "search_vehicles",
    "set_catalog_shortcut",
    "staff_user_by_email",
    "staff_user_by_id",
    "update_maintenance_record",
    "update_staff_login",
    "update_vehicle",
    "vehicle_by_hmac",
    "vehicle_by_id",
    "vehicle_by_vin",
    "vin_hmac",
]
