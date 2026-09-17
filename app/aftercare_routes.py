"""Internal LimeAuto Aftercare routes.

The router uses only ``app.aftercare_db`` and the immutable read-only catalog
release. It is the complete staff workflow under ``/ops``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import aftercare_db
from .catalog_release import (
    CatalogReleaseError,
    CatalogReleaseStore,
    catalog_model_display_name,
    catalog_series_display_name,
)
from .i18n import public_error, template_context, tr

APP_DIR = aftercare_db.ROOT / "app"
templates = Jinja2Templates(directory=APP_DIR / "templates")
router = APIRouter()
SESSION_COOKIE = "limeauto_aftercare_session"
CSRF_COOKIE = "limeauto_aftercare_csrf"
SESSION_TTL = timedelta(days=14)
DEFAULT_NEXT_URL = "/ops/vehicles"
# Paths a signed-out visitor may be sent back to after logging in. Local only: the
# catalog needs this because the catalog gate redirects to the staff login.
LOCAL_NEXT_PATH_PREFIXES = ("/ops", "/catalog")
# The session cookie is scoped to the whole application, not to /ops: the catalog and
# its media live outside /ops and must see the same session. A cookie scoped to /ops
# would silently make every signed-in catalog request look signed out.
SESSION_COOKIE_PATH = "/"
VEHICLE_PAGE_SIZE = 100
MAX_VEHICLE_PAGE = 1000
OVERVIEW_PAGE_SIZE = 100
_UNSET = object()
_DUMMY_PASSWORD_HASH: str | None = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def create_staff_password(value: str) -> str:
    """PBKDF2 format with recoverable base64 salt for runtime authentication."""
    import base64

    if len(value) < 10:
        raise ValueError("Password must contain at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 260_000)
    return "pbkdf2_sha256$260000${}${}".format(
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def check_password(value: str, encoded: str) -> bool:
    import base64

    try:
        algorithm, iterations_text, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256" or int(iterations_text) != 260_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(expected_text + "=" * (-len(expected_text) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 260_000)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _dummy_password_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = create_staff_password("limeauto-dummy-password-not-used")
    return _DUMMY_PASSWORD_HASH


def _db():
    try:
        return aftercare_db.connect()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Aftercare database is unavailable") from exc


def _user(request: Request) -> dict[str, Any] | None:
    cached = getattr(request.state, "aftercare_user", _UNSET)
    if cached is not _UNSET:
        return cached  # type: ignore[return-value]
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 256:
        request.state.aftercare_user = None
        return None
    with _db() as db:
        user = aftercare_db.staff_user_by_session(
            db, token_hash=_digest(token), now=_iso(_now())
        )
    request.state.aftercare_user = user
    return user


def _safe_next_url(value: str | None) -> str:
    """Allow only a local aftercare path, retaining its query string."""

    if not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value) or "\\" in value:
        return DEFAULT_NEXT_URL
    try:
        parsed = urlsplit(value)
    except ValueError:
        return DEFAULT_NEXT_URL
    if parsed.scheme or parsed.netloc or not any(
        parsed.path == prefix or parsed.path.startswith(prefix + "/")
        for prefix in LOCAL_NEXT_PATH_PREFIXES
    ):
        return DEFAULT_NEXT_URL
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _login_redirect_url(next_url: str | None) -> str:
    return f"/ops/login?next={quote(_safe_next_url(next_url), safe='/')}"


class StaffSessionUnavailable(RuntimeError):
    """The staff session store could not be read, so no session can be honoured.

    Kept distinct from "no session": a broken session store must not be reported as a
    signed-out visitor, which would send staff to a login page that cannot work.
    """


def staff_session_user(request: Request) -> dict[str, Any] | None:
    """Public session lookup for surfaces outside /ops (the catalog and its media).

    A session store that cannot be read (missing secret, unopenable file, broken schema)
    is reported as StaffSessionUnavailable instead of escaping as an unhandled 500, because
    the caller has to be able to tell "no session" from "cannot tell".
    """
    try:
        return _user(request)
    except HTTPException as exc:
        raise StaffSessionUnavailable(str(exc.detail)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise StaffSessionUnavailable(f"{type(exc).__name__}: {exc}") from exc


def staff_login_url(next_url: str | None) -> str:
    """Public form of the staff login redirect target, for surfaces outside /ops."""
    return _login_redirect_url(next_url)


def _vehicle_page_url(query: str, page: int) -> str:
    return "/ops/vehicles?" + urlencode({"page": page, "q": query})


def _overview_page_url(filters: dict[str, Any], page: int) -> str:
    params = {"page": page}
    for field in aftercare_db.OVERVIEW_FILTER_FIELDS:
        value = filters.get(field) or ""
        if value:
            params[field] = value
    return "/ops/overview?" + urlencode(params)


def _overview_filters_from_query(
    model_label: str,
    brand: str,
    color: str,
    model_year: str,
    delivery_date: str,
    registration_date: str,
    departure_date: str,
    license_plate_date: str,
) -> tuple[dict[str, Any], bool]:
    submitted = {
        "model_label": model_label,
        "brand": brand,
        "color": color,
        "model_year": model_year,
        "delivery_date": delivery_date,
        "registration_date": registration_date,
        "departure_date": departure_date,
        "license_plate_date": license_plate_date,
    }
    try:
        return aftercare_db.normalize_overview_filters(**submitted), False
    except ValueError:
        retained = aftercare_db.normalize_overview_filters()
        retained.update({field: str(value) for field, value in submitted.items()})
        return retained, True


def _require(request: Request) -> dict[str, Any] | RedirectResponse:
    user = _user(request)
    if user:
        return user
    next_url = request.url.path
    if request.url.query:
        next_url += f"?{request.url.query}"
    return RedirectResponse(url=_login_redirect_url(next_url), status_code=303)


def _csrf(request: Request, submitted: str) -> None:
    token = submitted or request.headers.get("x-csrf-token", "")
    session = request.cookies.get(SESSION_COOKIE, "")
    if not token or not session:
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    with _db() as db:
        expected = aftercare_db.csrf_hash_for_staff_session(db, _digest(session))
    if not expected or not hmac.compare_digest(_digest(token), expected):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _render(request: Request, name: str, **context):
    context.setdefault("active_path", request.url.path)
    context.setdefault("aftercare_user", _user(request))
    context.setdefault("aftercare_csrf_token", request.cookies.get(CSRF_COOKIE, ""))
    context.setdefault(
        "catalog_url_token",
        lambda value: urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("="),
    )
    context.setdefault("catalog_browse_enabled", _catalog_browse_enabled())
    context.update(template_context(request))
    return templates.TemplateResponse(request, name, context)


def _login_response(user: dict[str, Any], next_url: str | None = None) -> RedirectResponse:
    session = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    with _db() as db:
        aftercare_db.create_staff_session(
            db,
            staff_user_id=int(user["id"]),
            token_hash=_digest(session),
            csrf_token_hash=_digest(csrf),
            expires_at=_iso(_now() + SESSION_TTL),
        )
        aftercare_db.update_staff_login(db, int(user["id"]))
        aftercare_db.append_audit_event(
            db,
            actor_user_id=int(user["id"]),
            action="staff_login",
            target_type="staff_user",
            target_id=str(user["id"]),
        )
    response = RedirectResponse(url=_safe_next_url(next_url), status_code=303)
    secure = os.getenv("LIMEAUTO_AFTERCARE_COOKIE_SECURE", "0") == "1"
    response.set_cookie(SESSION_COOKIE, session, max_age=int(SESSION_TTL.total_seconds()), httponly=True, samesite="lax", secure=secure, path=SESSION_COOKIE_PATH)
    response.set_cookie(CSRF_COOKIE, csrf, max_age=int(SESSION_TTL.total_seconds()), httponly=False, samesite="lax", secure=secure, path=SESSION_COOKIE_PATH)
    return response


def _catalog_browse_enabled() -> bool:
    return os.getenv("LIMEAUTO_CATALOG_BROWSE", "0") == "1"


def _catalog_release_allow_draft() -> bool:
    return os.getenv("LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT", "0") == "1"


def _catalog_store() -> CatalogReleaseStore | None:
    try:
        return CatalogReleaseStore(allow_draft=_catalog_release_allow_draft())
    except (CatalogReleaseError, OSError, ValueError):
        return None


def _model_choice(row: dict) -> dict:
    return {
        "series_code": row.get("series_code"),
        "model_code": row.get("model_code"),
        "display_model_name": catalog_model_display_name(
            row.get("model_name_source"), row.get("model_code")
        ),
    }


def _release_models(series_code: str = "") -> tuple[list[dict], list[dict]]:
    store = _catalog_store()
    if store is None:
        return [], []
    try:
        series_rows = []
        for row in store.list_series():
            item = dict(row)
            item["display_series_name"] = catalog_series_display_name(item.get("series_name"))
            series_rows.append(item)
        selected = []
        if series_code:
            selected = [_model_choice(dict(row)) for row in store.models_for_series(series_code)]
        return series_rows, selected
    except (CatalogReleaseError, OSError, ValueError):
        return [], []


def _catalog_shortcut_view(
    shortcut: dict | None, store: CatalogReleaseStore | None
) -> dict | None:
    """Add display names to a stored code-only shortcut without changing it."""
    if not shortcut:
        return None
    item = dict(shortcut)
    item["display_series_name"] = catalog_series_display_name(None)
    item["display_model_name"] = catalog_model_display_name(None, item.get("model_code"))
    if store is None:
        return item
    try:
        model = store.model_by_codes(
            str(item.get("series_code", "")), str(item.get("model_code", ""))
        )
    except (CatalogReleaseError, OSError, ValueError):
        model = None
    if model:
        item["display_series_name"] = catalog_series_display_name(
            model.get("series_name_source")
        )
        item["display_model_name"] = catalog_model_display_name(
            model.get("model_name_source"), model.get("model_code")
        )
    return item


def _parse_model_year(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError("Model year must be between 1900 and 2100") from exc


def _vehicle_lifecycle_kwargs(
    *,
    model_year: str,
    color: str,
    delivery_date: str,
    registration_date: str,
    departure_date: str,
    license_plate_date: str,
) -> dict[str, Any]:
    return {
        "model_year": _parse_model_year(model_year),
        "color": color.strip() or None,
        "delivery_date": delivery_date.strip() or None,
        "registration_date": registration_date.strip() or None,
        "departure_date": departure_date.strip() or None,
        "license_plate_date": license_plate_date.strip() or None,
    }


def _pad_list(values: list[str], size: int) -> list[str]:
    if len(values) >= size:
        return values[:size]
    return values + [""] * (size - len(values))


def _as_list(values: list[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    return list(values)


def _item_form_rows(
    item_type: list[str] | str,
    item_name: list[str] | str,
    material_code: list[str] | str,
    quantity: list[str] | str,
    item_status: list[str] | str,
    item_note: list[str] | str,
) -> list[dict[str, str]]:
    names = _as_list(item_name)
    types = _as_list(item_type)
    codes = _as_list(material_code)
    quantities = _as_list(quantity)
    statuses = _as_list(item_status)
    notes = _as_list(item_note)
    size = max(
        (len(values) for values in (names, types, codes, quantities, statuses, notes)),
        default=0,
    )
    values = (
        _pad_list(types, size),
        _pad_list(names, size),
        _pad_list(codes, size),
        _pad_list(quantities, size),
        _pad_list(statuses, size),
        _pad_list(notes, size),
    )
    return [
        {
            "item_type": row[0] or "maintenance",
            "item_name": row[1],
            "material_code": row[2],
            "quantity": row[3],
            "item_status": row[4] or "completed",
            "note": row[5],
        }
        for row in zip(*values)
    ]


def _submitted_item_rows(
    item_type: list[str] | str,
    item_name: list[str] | str,
    material_code: list[str] | str,
    quantity: list[str] | str,
    item_status: list[str] | str,
    item_note: list[str] | str,
) -> list[dict[str, str]]:
    return [
        row
        for row in _item_form_rows(
            item_type, item_name, material_code, quantity, item_status, item_note
        )
        if any(
            (
                row["item_name"].strip(),
                row["material_code"].strip(),
                row["quantity"].strip(),
                row["note"].strip(),
            )
        )
    ]


def _items_from_form(
    item_type: list[str] | str,
    item_name: list[str] | str,
    material_code: list[str] | str,
    quantity: list[str] | str,
    item_status: list[str] | str,
    item_note: list[str] | str,
) -> list[dict[str, Any]]:
    items = []
    for row in _item_form_rows(
        item_type, item_name, material_code, quantity, item_status, item_note
    ):
        if not row["item_name"].strip():
            if any(
                (
                    row["material_code"].strip(),
                    row["quantity"].strip(),
                    row["note"].strip(),
                )
            ):
                raise ValueError("Maintenance item name is required")
            continue
        items.append(
            {
                "item_type": row["item_type"],
                "item_name": row["item_name"],
                "material_code": row["material_code"] or None,
                "quantity": row["quantity"] or None,
                "item_status": row["item_status"],
                "note": row["note"] or None,
            }
        )
    return items


def _iter_uploads(photos: list[UploadFile] | UploadFile | None):
    if photos is None:
        return
    if isinstance(photos, list):
        for photo in photos:
            yield photo
        return
    yield photos


def _read_photo_payloads(photos: list[UploadFile] | UploadFile | None) -> list[tuple[bytes, str | None]]:
    payloads: list[tuple[bytes, str | None]] = []
    limit = aftercare_db.ATTACHMENT_MAX_BYTES
    for photo in _iter_uploads(photos):
        filename = getattr(photo, "filename", None) or ""
        if not filename.strip():
            # The empty file input every browser submits on a form with an optional
            # upload: nobody picked a file, so there is nothing to store and nothing to
            # report.
            continue
        declared = getattr(photo, "size", None)
        if isinstance(declared, int) and declared > limit:
            raise ValueError("Attachment exceeds 10 MB")
        chunks = bytearray()
        while True:
            chunk = photo.file.read(64 * 1024)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise ValueError("Attachment exceeds 10 MB")
        if not chunks:
            # A named file that carries no bytes is a failed or interrupted upload. The
            # attachment store would reject it anyway (size_bytes must be positive), so
            # report it here instead of dropping the file and saving the record as if the
            # photo had been attached.
            raise ValueError("Attachment is empty")
        payloads.append((bytes(chunks), filename))
    return payloads


def _save_photo_payloads(
    db,
    record_id: int,
    payloads: list[tuple[bytes, str | None]],
    uploaded_by: int,
) -> None:
    for data, filename in payloads:
        aftercare_db.save_maintenance_attachment(
            db,
            record_id=record_id,
            data=data,
            original_filename=filename,
            uploaded_by=uploaded_by,
        )


@router.get("/ops/login")
def login(request: Request, next_url: str = Query("", alias="next")):
    next_url = _safe_next_url(next_url)
    if _user(request):
        return RedirectResponse(url=next_url, status_code=303)
    return _render(request, "aftercare_login.html", next_url=next_url)


@router.post("/ops/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next_url: str = Form(""),
):
    next_url = _safe_next_url(next_url)
    with _db() as db:
        user = aftercare_db.staff_user_by_email(db, email)
    if user:
        accepted = check_password(password, user["password_hash"]) and user["status"] == "active"
    else:
        check_password(password, _dummy_password_hash())
        accepted = False
    if accepted and user is not None:
        return _login_response(user, next_url)
    return _render(
        request,
        "aftercare_login.html",
        error=tr("invalid_credentials"),
        submitted_email=email.strip(),
        next_url=next_url,
    )


@router.post("/ops/logout")
def logout(request: Request, csrf_token: str = Form("")):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    _csrf(request, csrf_token)
    token = request.cookies.get(SESSION_COOKIE)
    with _db() as db:
        if token:
            aftercare_db.delete_staff_session(db, _digest(token))
    response = RedirectResponse(url="/ops/login", status_code=303)
    # Both paths: the session cookie used to be scoped to /ops, and browsers keep cookies
    # per path, so a browser that signed in before that change still holds one that only
    # a matching-path delete clears. The session row is already gone server-side, so a
    # lingering cookie is inert either way -- clearing it just avoids leaving the artifact.
    for path in (SESSION_COOKIE_PATH, "/ops"):
        response.delete_cookie(SESSION_COOKIE, path=path)
        response.delete_cookie(CSRF_COOKIE, path=path)
    return response


@router.get("/ops")
def ops_home():
    return RedirectResponse(url="/ops/vehicles", status_code=303)


@router.get("/ops/overview")
def vehicle_overview(
    request: Request,
    page: int = Query(1, ge=1, le=MAX_VEHICLE_PAGE),
    model_label: str = Query(""),
    brand: str = Query(""),
    color: str = Query(""),
    model_year: str = Query(""),
    delivery_date: str = Query(""),
    registration_date: str = Query(""),
    departure_date: str = Query(""),
    license_plate_date: str = Query(""),
):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    filters, filter_error = _overview_filters_from_query(
        model_label,
        brand,
        color,
        model_year,
        delivery_date,
        registration_date,
        departure_date,
        license_plate_date,
    )
    offset = (page - 1) * OVERVIEW_PAGE_SIZE
    with _db() as db:
        rows = [] if filter_error else aftercare_db.list_overview_vehicles(
            db, filters, limit=OVERVIEW_PAGE_SIZE + 1, offset=offset
        )
    has_next = len(rows) > OVERVIEW_PAGE_SIZE
    rows = rows[:OVERVIEW_PAGE_SIZE]
    filtered = any(filters.get(field) for field in aftercare_db.OVERVIEW_FILTER_FIELDS)
    previous_url = _overview_page_url(filters, page - 1) if page > 1 else None
    next_url = _overview_page_url(filters, page + 1) if has_next else None
    return _render(
        request,
        "aftercare_overview.html",
        vehicles=rows,
        filters=filters,
        filtered=filtered,
        filter_error=filter_error,
        page=page,
        previous_url=previous_url,
        next_url=next_url,
    )


@router.get("/ops/vehicles")
def vehicles(
    request: Request,
    q: str = "",
    page: int = Query(1, ge=1, le=MAX_VEHICLE_PAGE),
):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    query = q[:120]
    offset = (page - 1) * VEHICLE_PAGE_SIZE
    catalog_store = _catalog_store()
    with _db() as db:
        rows = aftercare_db.search_vehicles(
            db,
            query,
            limit=VEHICLE_PAGE_SIZE + 1,
            offset=offset,
        )
        has_next = len(rows) > VEHICLE_PAGE_SIZE
        rows = rows[:VEHICLE_PAGE_SIZE]
        for row in rows:
            row["shortcut"] = _catalog_shortcut_view(
                aftercare_db.current_catalog_shortcut(db, int(row["id"])),
                catalog_store,
            )
    return _render(
        request,
        "aftercare_vehicles.html",
        vehicles=rows,
        query=query,
        page=page,
        has_next=has_next,
        previous_url=_vehicle_page_url(query, page - 1) if page > 1 else None,
        next_url=_vehicle_page_url(query, page + 1) if has_next else None,
    )


@router.get("/ops/vehicles/new")
def vehicle_new(request: Request):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    return _render(request, "aftercare_vehicle_form.html", vehicle=None)


@router.post("/ops/vehicles")
def vehicle_create(
    request: Request,
    vin: str = Form(""),
    brand: str = Form(""),
    model_label: str = Form(""),
    note: str = Form(""),
    model_year: str = Form(""),
    color: str = Form(""),
    delivery_date: str = Form(""),
    registration_date: str = Form(""),
    departure_date: str = Form(""),
    license_plate_date: str = Form(""),
    csrf_token: str = Form(""),
):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    _csrf(request, csrf_token)
    submitted = {
        "vin": vin,
        "brand": brand,
        "model_label": model_label,
        "note": note,
        "model_year": model_year,
        "color": color,
        "delivery_date": delivery_date,
        "registration_date": registration_date,
        "departure_date": departure_date,
        "license_plate_date": license_plate_date,
    }
    try:
        lifecycle = _vehicle_lifecycle_kwargs(
            model_year=model_year,
            color=color,
            delivery_date=delivery_date,
            registration_date=registration_date,
            departure_date=departure_date,
            license_plate_date=license_plate_date,
        )
        with _db() as db:
            vehicle, created = aftercare_db.create_or_get_vehicle(
                db,
                vin,
                brand=brand.strip() or None,
                model_label=model_label.strip() or None,
                note=note.strip() or None,
                **lifecycle,
            )
            if not created:
                vehicle = aftercare_db.update_vehicle(
                    db,
                    int(vehicle["id"]),
                    brand=brand.strip() or vehicle["brand"],
                    model_label=model_label.strip() or vehicle["model_label"],
                    note=note.strip() or vehicle["note"],
                    model_year=lifecycle["model_year"] if model_year.strip() else vehicle.get("model_year"),
                    color=lifecycle["color"] if color.strip() else vehicle.get("color"),
                    delivery_date=lifecycle["delivery_date"] if delivery_date.strip() else vehicle.get("delivery_date"),
                    registration_date=lifecycle["registration_date"] if registration_date.strip() else vehicle.get("registration_date"),
                    departure_date=lifecycle["departure_date"] if departure_date.strip() else vehicle.get("departure_date"),
                    license_plate_date=lifecycle["license_plate_date"] if license_plate_date.strip() else vehicle.get("license_plate_date"),
                ) or vehicle
            aftercare_db.append_audit_event(db, actor_user_id=int(user["id"]), action="vehicle_created" if created else "vehicle_opened", target_type="vehicle", target_id=str(vehicle["id"]), metadata={"vin_last4": vehicle["vin_last4"], "created": created})
    except ValueError as exc:
        return _render(
            request,
            "aftercare_vehicle_form.html",
            vehicle=submitted,
            error=public_error(str(exc)),
        )
    return RedirectResponse(url=f"/ops/vehicles/{vehicle['id']}", status_code=303)


@router.get("/ops/vehicles/{vehicle_id}")
def vehicle_detail(request: Request, vehicle_id: int):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    with _db() as db:
        vehicle = aftercare_db.vehicle_by_id(db, vehicle_id)
        if vehicle is None:
            raise HTTPException(status_code=404, detail="Vehicle not found")
        records = aftercare_db.list_maintenance_records(db, vehicle_id)
        catalog_store = _catalog_store()
        shortcut = _catalog_shortcut_view(
            aftercare_db.current_catalog_shortcut(db, vehicle_id),
            catalog_store,
        )
    return _render(request, "aftercare_vehicle_detail.html", vehicle=vehicle, maintenance=records, shortcut=shortcut)


@router.post("/ops/vehicles/{vehicle_id}")
def vehicle_update(
    request: Request,
    vehicle_id: int,
    brand: str = Form(""),
    model_label: str = Form(""),
    note: str = Form(""),
    model_year: str = Form(""),
    color: str = Form(""),
    delivery_date: str = Form(""),
    registration_date: str = Form(""),
    departure_date: str = Form(""),
    license_plate_date: str = Form(""),
    csrf_token: str = Form(""),
):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    _csrf(request, csrf_token)
    try:
        lifecycle = _vehicle_lifecycle_kwargs(
            model_year=model_year,
            color=color,
            delivery_date=delivery_date,
            registration_date=registration_date,
            departure_date=departure_date,
            license_plate_date=license_plate_date,
        )
        with _db() as db:
            vehicle = aftercare_db.update_vehicle(
                db,
                vehicle_id,
                brand=brand.strip() or None,
                model_label=model_label.strip() or None,
                note=note.strip() or None,
                **lifecycle,
            )
            if vehicle is None:
                raise HTTPException(status_code=404, detail="Vehicle not found")
            aftercare_db.append_audit_event(db, actor_user_id=int(user["id"]), action="vehicle_updated", target_type="vehicle", target_id=str(vehicle_id))
    except ValueError as exc:
        with _db() as db:
            vehicle = aftercare_db.vehicle_by_id(db, vehicle_id)
            records = aftercare_db.list_maintenance_records(db, vehicle_id) if vehicle else []
            shortcut = _catalog_shortcut_view(
                aftercare_db.current_catalog_shortcut(db, vehicle_id) if vehicle else None,
                _catalog_store(),
            )
        if vehicle is None:
            raise HTTPException(status_code=404, detail="Vehicle not found")
        vehicle = {
            **vehicle,
            "brand": brand,
            "model_label": model_label,
            "note": note,
            "model_year": model_year,
            "color": color,
            "delivery_date": delivery_date,
            "registration_date": registration_date,
            "departure_date": departure_date,
            "license_plate_date": license_plate_date,
        }
        return _render(
            request,
            "aftercare_vehicle_detail.html",
            vehicle=vehicle,
            maintenance=records,
            shortcut=shortcut,
            error=public_error(str(exc)),
        )
    return RedirectResponse(url=f"/ops/vehicles/{vehicle_id}", status_code=303)


def _record_form(request: Request, vehicle_id: int, record: dict | None = None, error: str | None = None):
    with _db() as db:
        vehicle = aftercare_db.vehicle_by_id(db, vehicle_id)
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return _render(
        request,
        "aftercare_maintenance_form.html",
        vehicle=vehicle,
        record=record,
        error=error,
        text_limits={
            "summary": aftercare_db.MAINTENANCE_SUMMARY_LIMIT,
            "provider": aftercare_db.MAINTENANCE_PROVIDER_LIMIT,
            "reference": aftercare_db.MAINTENANCE_REFERENCE_LIMIT,
            "note": aftercare_db.MAINTENANCE_NOTE_LIMIT,
        },
    )


def _submitted_maintenance_record(
    *,
    record_id: int | None = None,
    maintenance_date: str,
    maintenance_type: str,
    summary: str,
    odometer_km: str,
    service_provider: str,
    source_reference: str,
    note: str,
    complaint: str = "",
    correction: str = "",
    items: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    record = {
        "maintenance_date": maintenance_date,
        "maintenance_type": maintenance_type,
        "summary": summary,
        "odometer_km": odometer_km,
        "service_provider": service_provider,
        "source_reference": source_reference,
        "note": note,
        "complaint": complaint,
        "correction": correction,
        "items": items or [],
        "attachments": attachments or [],
    }
    if record_id is not None:
        record["id"] = record_id
    return record


@router.get("/ops/vehicles/{vehicle_id}/maintenance/new")
def maintenance_new(request: Request, vehicle_id: int):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    return _record_form(request, vehicle_id)


@router.post("/ops/vehicles/{vehicle_id}/maintenance")
def maintenance_create(
    request: Request,
    vehicle_id: int,
    maintenance_date: str = Form(""),
    maintenance_type: str = Form("maintenance"),
    summary: str = Form(""),
    odometer_km: str = Form(""),
    service_provider: str = Form(""),
    source_reference: str = Form(""),
    note: str = Form(""),
    complaint: str = Form(""),
    correction: str = Form(""),
    item_type: list[str] = Form(default=[]),
    item_name: list[str] = Form(default=[]),
    material_code: list[str] = Form(default=[]),
    quantity: list[str] = Form(default=[]),
    item_status: list[str] = Form(default=[]),
    item_note: list[str] = Form(default=[]),
    photos: list[UploadFile] = File(default=[]),
    csrf_token: str = Form(""),
):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    _csrf(request, csrf_token)
    try:
        items = _items_from_form(item_type, item_name, material_code, quantity, item_status, item_note)
    except (ValueError, TypeError) as exc:
        return _record_form(
            request,
            vehicle_id,
            record=_submitted_maintenance_record(
                maintenance_date=maintenance_date,
                maintenance_type=maintenance_type,
                summary=summary,
                odometer_km=odometer_km,
                service_provider=service_provider,
                source_reference=source_reference,
                note=note,
                complaint=complaint,
                correction=correction,
                items=_submitted_item_rows(
                    item_type, item_name, material_code, quantity, item_status, item_note
                ),
            ),
            error=public_error(str(exc)),
        )
    try:
        payloads = _read_photo_payloads(photos)
        with _db() as db:
            record = aftercare_db.create_maintenance_record(
                db,
                vehicle_id=vehicle_id,
                maintenance_date=maintenance_date,
                maintenance_type=maintenance_type,
                summary=summary,
                odometer_km=int(odometer_km) if odometer_km.strip() else None,
                service_provider=service_provider.strip() or None,
                source_reference=source_reference.strip() or None,
                note=note.strip() or None,
                complaint=complaint.strip() or None,
                correction=correction.strip() or None,
                items=items,
            )
            _save_photo_payloads(db, int(record["id"]), payloads, int(user["id"]))
            aftercare_db.append_audit_event(db, actor_user_id=int(user["id"]), action="maintenance_record_created", target_type="maintenance_record", target_id=str(record["id"]), metadata={"vehicle_id": vehicle_id})
    except (ValueError, TypeError) as exc:
        return _record_form(
            request,
            vehicle_id,
            record=_submitted_maintenance_record(
                maintenance_date=maintenance_date,
                maintenance_type=maintenance_type,
                summary=summary,
                odometer_km=odometer_km,
                service_provider=service_provider,
                source_reference=source_reference,
                note=note,
                complaint=complaint,
                correction=correction,
                items=items,
            ),
            error=public_error(str(exc)),
        )
    return RedirectResponse(url=f"/ops/vehicles/{vehicle_id}", status_code=303)


@router.get("/ops/vehicles/{vehicle_id}/maintenance/{record_id}")
def maintenance_detail(request: Request, vehicle_id: int, record_id: int):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    with _db() as db:
        record = aftercare_db.maintenance_by_id(db, record_id, vehicle_id=vehicle_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Maintenance record not found")
        vehicle = aftercare_db.vehicle_by_id(db, vehicle_id)
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return _render(
        request,
        "aftercare_maintenance_detail.html",
        vehicle=vehicle,
        record=record,
    )


@router.get("/ops/vehicles/{vehicle_id}/maintenance/{record_id}/edit")
def maintenance_edit(request: Request, vehicle_id: int, record_id: int):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    with _db() as db:
        record = aftercare_db.maintenance_by_id(db, record_id, vehicle_id=vehicle_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Maintenance record not found")
    return _record_form(request, vehicle_id, record=record)


@router.post("/ops/vehicles/{vehicle_id}/maintenance/{record_id}")
def maintenance_update(
    request: Request,
    vehicle_id: int,
    record_id: int,
    maintenance_date: str = Form(""),
    maintenance_type: str = Form("maintenance"),
    summary: str = Form(""),
    odometer_km: str = Form(""),
    service_provider: str = Form(""),
    source_reference: str = Form(""),
    note: str = Form(""),
    complaint: str = Form(""),
    correction: str = Form(""),
    item_type: list[str] = Form(default=[]),
    item_name: list[str] = Form(default=[]),
    material_code: list[str] = Form(default=[]),
    quantity: list[str] = Form(default=[]),
    item_status: list[str] = Form(default=[]),
    item_note: list[str] = Form(default=[]),
    delete_attachment_ids: list[str] = Form(default=[]),
    photos: list[UploadFile] = File(default=[]),
    csrf_token: str = Form(""),
):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    _csrf(request, csrf_token)
    try:
        items = _items_from_form(item_type, item_name, material_code, quantity, item_status, item_note)
    except (ValueError, TypeError) as exc:
        with _db() as db:
            existing = aftercare_db.maintenance_by_id(db, record_id, vehicle_id=vehicle_id)
        return _record_form(
            request,
            vehicle_id,
            record=_submitted_maintenance_record(
                record_id=record_id,
                maintenance_date=maintenance_date,
                maintenance_type=maintenance_type,
                summary=summary,
                odometer_km=odometer_km,
                service_provider=service_provider,
                source_reference=source_reference,
                note=note,
                complaint=complaint,
                correction=correction,
                items=_submitted_item_rows(
                    item_type, item_name, material_code, quantity, item_status, item_note
                ),
                attachments=(existing or {}).get("attachments") or [],
            ),
            error=public_error(str(exc)),
        )
    try:
        payloads = _read_photo_payloads(photos)
        with _db() as db:
            if aftercare_db.maintenance_by_id_raw(db, record_id, vehicle_id=vehicle_id) is None:
                raise HTTPException(status_code=404, detail="Maintenance record not found")
            aftercare_db.update_maintenance_record(
                db,
                record_id,
                maintenance_date=maintenance_date,
                maintenance_type=maintenance_type,
                summary=summary,
                odometer_km=int(odometer_km) if odometer_km.strip() else None,
                service_provider=service_provider.strip() or None,
                source_reference=source_reference.strip() or None,
                note=note.strip() or None,
                complaint=complaint.strip() or None,
                correction=correction.strip() or None,
                items=items,
            )
            for raw_id in delete_attachment_ids:
                if not raw_id.strip():
                    continue
                aftercare_db.delete_maintenance_attachment(
                    db, int(raw_id), record_id=record_id
                )
            _save_photo_payloads(db, record_id, payloads, int(user["id"]))
            aftercare_db.append_audit_event(db, actor_user_id=int(user["id"]), action="maintenance_record_updated", target_type="maintenance_record", target_id=str(record_id), metadata={"vehicle_id": vehicle_id})
    except (ValueError, TypeError) as exc:
        with _db() as db:
            existing = aftercare_db.maintenance_by_id(db, record_id, vehicle_id=vehicle_id)
        return _record_form(
            request,
            vehicle_id,
            record=_submitted_maintenance_record(
                record_id=record_id,
                maintenance_date=maintenance_date,
                maintenance_type=maintenance_type,
                summary=summary,
                odometer_km=odometer_km,
                service_provider=service_provider,
                source_reference=source_reference,
                note=note,
                complaint=complaint,
                correction=correction,
                items=items,
                attachments=(existing or {}).get("attachments") or [],
            ),
            error=public_error(str(exc)),
        )
    return RedirectResponse(url=f"/ops/vehicles/{vehicle_id}", status_code=303)


@router.get("/ops/vehicles/{vehicle_id}/maintenance/{record_id}/attachments/{attachment_id}")
def maintenance_attachment(
    request: Request,
    vehicle_id: int,
    record_id: int,
    attachment_id: int,
):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    with _db() as db:
        attachment = aftercare_db.attachment_by_id(
            db, attachment_id, record_id=record_id, vehicle_id=vehicle_id
        )
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = aftercare_db.resolve_attachment_path(str(attachment["object_key"]))
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(
        path,
        media_type=str(attachment["mime_type"]),
        headers={"Cache-Control": "private, max-age=0, no-store"},
    )


@router.get("/ops/catalog-models")
def catalog_models(request: Request, series_code: str = Query("")):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    selected = series_code.strip()
    if not selected:
        return JSONResponse([])
    store = _catalog_store()
    if store is None:
        return JSONResponse([])
    try:
        rows = [_model_choice(dict(row)) for row in store.models_for_series(selected)]
    except (CatalogReleaseError, OSError, ValueError):
        return JSONResponse([])
    return JSONResponse(rows)


@router.get("/ops/vehicles/{vehicle_id}/catalog-shortcut")
def shortcut_form(request: Request, vehicle_id: int, series: str = "", error: str = ""):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    with _db() as db:
        vehicle = aftercare_db.vehicle_by_id(db, vehicle_id)
        catalog_store = _catalog_store()
        current = _catalog_shortcut_view(
            aftercare_db.current_catalog_shortcut(db, vehicle_id),
            catalog_store,
        )
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    selected = series or (current or {}).get("series_code", "")
    series_rows, models = _release_models(selected)
    return _render(
        request,
        "aftercare_catalog_shortcut_form.html",
        vehicle=vehicle,
        shortcut=current,
        series_rows=series_rows,
        models=models,
        selected_series=selected,
        error=tr("catalog_unavailable_or_missing") if error else "",
    )


@router.post("/ops/vehicles/{vehicle_id}/catalog-shortcut")
def shortcut_save(request: Request, vehicle_id: int, series_code: str = Form(""), model_code: str = Form(""), confirmation_note: str = Form(""), csrf_token: str = Form("")):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    _csrf(request, csrf_token)
    try:
        store = CatalogReleaseStore(allow_draft=_catalog_release_allow_draft())
        if not store.model_by_codes(series_code.strip(), model_code.strip()):
            raise ValueError("Catalog model is unavailable")
    except (CatalogReleaseError, OSError, ValueError):
        return RedirectResponse(url=f"/ops/vehicles/{vehicle_id}/catalog-shortcut?error=model", status_code=303)
    with _db() as db:
        if aftercare_db.vehicle_by_id(db, vehicle_id) is None:
            raise HTTPException(status_code=404, detail="Vehicle not found")
        aftercare_db.set_catalog_shortcut(db, vehicle_id=vehicle_id, series_code=series_code, model_code=model_code, confirmation_note=confirmation_note.strip() or None, created_by=int(user["id"]))
        aftercare_db.append_audit_event(db, actor_user_id=int(user["id"]), action="catalog_shortcut_set", target_type="vehicle", target_id=str(vehicle_id), metadata={"series_code": series_code.strip(), "model_code": model_code.strip()})
    return RedirectResponse(url=f"/ops/vehicles/{vehicle_id}", status_code=303)


@router.post("/ops/vehicles/{vehicle_id}/catalog-shortcut/remove")
def shortcut_remove(request: Request, vehicle_id: int, csrf_token: str = Form("")):
    user = _require(request)
    if isinstance(user, RedirectResponse):
        return user
    _csrf(request, csrf_token)
    with _db() as db:
        aftercare_db.remove_catalog_shortcut(db, vehicle_id)
        aftercare_db.append_audit_event(db, actor_user_id=int(user["id"]), action="catalog_shortcut_removed", target_type="vehicle", target_id=str(vehicle_id))
    return RedirectResponse(url=f"/ops/vehicles/{vehicle_id}", status_code=303)
