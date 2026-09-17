"""LimeAuto Aftercare application.

The runtime boundary is intentionally small:

* authenticated internal aftercare work under ``/ops``;
* immutable, read-only catalog browsing under ``/catalog``;
* verified local catalog media;
* liveness and readiness probes.

Catalog browsing is closed unless explicitly enabled.  The runtime reads only
the local catalog release and never performs VIN decoding, fitment, policy,
pricing, stock, customer, request, cart, order, or payment work.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .aftercare_routes import (
    StaffSessionUnavailable,
    staff_login_url,
    staff_session_user,
)
from .aftercare_routes import router as aftercare_router
from .i18n import (
    LANG_COOKIE,
    LANG_COOKIE_MAX_AGE,
    SUPPORTED_LANGS,
    bind_lang,
    current_lang,
    reset_lang,
    resolve_lang,
    template_context,
)
from .catalog_release import (
    DISPLAY_ASSET_TYPES,
    PUBLISHABLE_ASSET_STATUSES,
    RELEASE_DEFAULT_PARTS_LIMIT,
    CatalogReleaseError,
    CatalogReleaseStore,
    catalog_model_display_name,
    catalog_node_display_name,
    catalog_part_display_name,
    catalog_series_display_name,
    normalize_parts_pagination,
)
from .catalog_translation import catalog_translation_status, catalog_translation_store

APP_DIR = Path(__file__).resolve().parent

# Upper bound on the source strings one English query may resolve to. The
# measured exhaustive maximum over the corpus is ~20.5k sources (the 2-gram
# "se"), which queries in ~1.1s; the cap only guards against a pathological
# future corpus, it does not truncate any real query today.
ENGLISH_SEARCH_SOURCE_LIMIT = 30000


def _has_cjk(value: str) -> bool:
    """True when the query needs the source-text path instead of the overlay."""
    return any("\u3400" <= char <= "\u9fff" for char in value)


app = FastAPI(
    title="LimeAuto Aftercare",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

CATALOG_BROWSE_ENABLED = os.getenv("LIMEAUTO_CATALOG_BROWSE", "0") == "1"
CATALOG_CLOSED_PREFIXES = ("/catalog", "/media/catalog", "/media/catalog-model")
# Page routes redirect a signed-out visitor to the staff login; media routes do not, because
# an <img> request has nowhere to follow a redirect to. Both are denied without a session.
CATALOG_PAGE_PREFIXES = ("/catalog",)
CATALOG_MEDIA_PREFIXES = ("/media/catalog", "/media/catalog-model")
REMOVED_SURFACE_PREFIXES = (
    "/vin",
    "/vehicle",
    "/search",
    "/account",
    "/admin",
    "/requests",
    "/cart",
    "/checkout",
    "/orders",
    "/payment",
    "/api",
)


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """True for the prefix itself and for its subtree, never for a longer sibling name."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


CATALOG_MEDIA_MIME_TYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/webp",
    }
)
CATALOG_MIME_MAGIC = {
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}
CATALOG_OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)
# private, not public: these bytes are only served to a signed-in staff session, so a shared
# cache must never hand them to someone else. The content is still immutable per release.
CATALOG_MEDIA_CACHE_CONTROL = "private, max-age=31536000, immutable"
SECURITY_RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "font-src 'self' data:; connect-src 'self'; object-src 'none'"
    ),
}


@app.middleware("http")
async def language_context(request: Request, call_next):
    lang = resolve_lang(request)
    request.state.lang = lang
    token = bind_lang(lang)
    try:
        response = await call_next(request)
    finally:
        reset_lang(token)
    if request.query_params.get("lang") in SUPPORTED_LANGS:
        secure = os.getenv("LIMEAUTO_AFTERCARE_COOKIE_SECURE", "0") == "1"
        response.set_cookie(
            LANG_COOKIE,
            lang,
            max_age=LANG_COOKIE_MAX_AGE,
            httponly=False,
            samesite="lax",
            secure=secure,
            path="/",
        )
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_RESPONSE_HEADERS)
    return response


@app.middleware("http")
async def catalog_access_gate(request: Request, call_next):
    """One place that decides who may read the catalog.

    Closed means 410 for everyone, signed in or not, so a closed catalog never advertises
    the staff entry. Open means a staff session is required: the catalog pages are internal
    (they expose the release, the asset keys and the material names), and the media routes
    are part of the same surface. Access is decided from the session, not from the path,
    so a new catalog route cannot be added unprotected by accident.
    """
    path = request.url.path
    if not _matches_prefix(path, CATALOG_CLOSED_PREFIXES):
        return await call_next(request)
    if not CATALOG_BROWSE_ENABLED:
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            headers=SECURITY_RESPONSE_HEADERS,
            content={
                "code": "catalog_browse_disabled",
                "message": "Catalog browsing is disabled.",
            },
        )
    try:
        user = staff_session_user(request)
    except StaffSessionUnavailable:
        # Reported as its own condition: redirecting to a login page whose session store
        # cannot be read would look like a wrong password instead of a broken deployment.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers=SECURITY_RESPONSE_HEADERS,
            content={
                "code": "staff_session_unavailable",
                "message": "Staff sessions cannot be read.",
            },
        )
    if user is not None:
        return await call_next(request)
    if _matches_prefix(path, CATALOG_MEDIA_PREFIXES):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            headers=SECURITY_RESPONSE_HEADERS,
            content={
                "code": "staff_session_required",
                "message": "A staff session is required.",
            },
        )
    next_url = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(
        url=staff_login_url(next_url),
        status_code=status.HTTP_303_SEE_OTHER,
        headers=SECURITY_RESPONSE_HEADERS,
    )


@app.middleware("http")
async def removed_surface_gate(request: Request, call_next):
    if _matches_prefix(request.url.path, REMOVED_SURFACE_PREFIXES):
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            headers=SECURITY_RESPONSE_HEADERS,
            content={
                "code": "legacy_surface_removed",
                "message": "This application surface is no longer available.",
            },
        )
    return await call_next(request)


def render(request: Request, name: str, **context: Any):
    context.setdefault("active_path", request.url.path)
    context.setdefault("catalog_browse_enabled", CATALOG_BROWSE_ENABLED)
    context.update(template_context(request))
    if str(request.url.path).startswith("/catalog"):
        _attach_catalog_locator(request, context)
    return templates.TemplateResponse(request, name, context)


def _attach_catalog_locator(request: Request, context: dict[str, Any]) -> None:
    """Fill breadcrumb locator options. Model list stays scoped to the current series."""

    path = str(request.url.path)
    if path.startswith("/catalog/search"):
        return
    series_code = ""
    model_code = ""
    model = context.get("model")
    if isinstance(model, dict):
        series_code = str(model.get("series_code") or "")
        model_code = str(model.get("model_code") or "")
    if not series_code:
        series_code = str(context.get("series_code") or "")
    series_label = ""
    series_token = ""
    model_label = ""
    model_token = ""
    series_rows: list[dict[str, Any]] = []
    model_rows: list[dict[str, Any]] = []
    try:
        store = get_release_store()
    except HTTPException:
        context["catalog_locator"] = {
            "series_code": series_code,
            "model_code": model_code,
            "series_label": "",
            "model_label": "",
            "series_token": "",
            "model_token": "",
            "series_rows": [],
            "model_rows": [],
        }
        return
    for item in store.list_series():
        code = str(item["series_code"])
        token = release_url_token(code)
        label = catalog_series_display_name(item.get("series_name"))
        series_rows.append(
            {
                "series_code": code,
                "url_code": token,
                "display_series_name": label,
            }
        )
        if code == series_code:
            series_label = label
            series_token = token
    if series_code:
        for item in store.models_for_series(series_code):
            code = str(item["model_code"])
            token = release_url_token(code)
            label = catalog_model_display_name(item.get("model_name_source"), item.get("model_code"))
            model_rows.append(
                {
                    "model_code": code,
                    "url_model_code": token,
                    "display_model_name": label,
                }
            )
            if code == model_code:
                model_label = label
                model_token = token
    context["catalog_locator"] = {
        "series_code": series_code,
        "model_code": model_code,
        "series_label": series_label,
        "model_label": model_label,
        "series_token": series_token,
        "model_token": model_token,
        "series_rows": series_rows,
        "model_rows": model_rows,
    }


def get_release_store() -> CatalogReleaseStore:
    """Open only the configured immutable release artifact."""

    try:
        return CatalogReleaseStore(
            allow_draft=os.getenv("LIMEAUTO_CATALOG_RELEASE_ALLOW_DRAFT", "0") == "1"
        )
    except (CatalogReleaseError, OSError, ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="Catalog release is unavailable") from exc


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> Response:
    if not CATALOG_BROWSE_ENABLED:
        return JSONResponse({"status": "ok", "catalog": "disabled"})
    try:
        store = get_release_store()
        metadata = store.release_metadata()
        return JSONResponse(
            {
                "status": "ok",
                "catalog": "enabled",
                "release_no": str(metadata["release_no"]),
                "release_status": str(metadata["status"]),
                # Surfaces a release/glossary fingerprint mismatch, which otherwise only
                # shows up as every English label quietly reverting to Chinese.
                "translation": catalog_translation_status(),
            }
        )
    except HTTPException as exc:
        return JSONResponse({"status": "not_ready", "detail": exc.detail}, status_code=503)


_RELEASE_URL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_RELEASE_LEGACY_ROUTE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def release_url_token(value: str) -> str:
    """Encode a catalog route value as an unpadded URL-safe token."""

    return base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("=")


def catalog_media_url(release_no: str, asset_key: str) -> str:
    return (
        f"/media/catalog/{release_url_token(release_no)}"
        f"/{release_url_token(asset_key)}"
    )


MODEL_THUMBNAIL_MAP_ENV = "LIMEAUTO_CATALOG_MODEL_THUMBNAIL_MAP"
MODEL_THUMBNAIL_ROOT_ENV = "LIMEAUTO_CATALOG_MODEL_THUMBNAIL_ROOT"


@lru_cache(maxsize=4)
def _read_model_thumbnail_map(path_text: str, mtime_ns: int, size: int) -> dict[str, Any]:
    """Read a verified local series-thumbnail map."""

    del mtime_ns, size
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    series = payload.get("series") if isinstance(payload, dict) else None
    return series if isinstance(series, dict) else {}


def model_thumbnail_entry(series_code: str) -> dict[str, Any] | None:
    map_text = os.getenv(MODEL_THUMBNAIL_MAP_ENV, "").strip()
    root_text = os.getenv(MODEL_THUMBNAIL_ROOT_ENV, "").strip()
    if not map_text or not root_text:
        return None
    try:
        map_path = Path(map_text).expanduser().resolve(strict=True)
        stat = map_path.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    entry = _read_model_thumbnail_map(
        str(map_path), stat.st_mtime_ns, stat.st_size
    ).get(str(series_code))
    if not isinstance(entry, dict):
        return None
    filename = entry.get("filename")
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9._~-]+", filename):
        return None
    return dict(entry)


def model_thumbnail_file(series_code: str) -> tuple[Path, dict[str, Any]] | None:
    entry = model_thumbnail_entry(series_code)
    root_text = os.getenv(MODEL_THUMBNAIL_ROOT_ENV, "").strip()
    if entry is None or not root_text:
        return None
    try:
        root = Path(root_text).expanduser().resolve(strict=True)
        image_root = (root / "images").resolve(strict=True)
        path = (image_root / str(entry["filename"])).resolve(strict=True)
        path.relative_to(image_root)
        if not path.is_file():
            return None
        expected_size = int(entry.get("bytes"))
        expected_sha256 = str(entry.get("sha256"))
        if expected_size < 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            return None
        if path.stat().st_size != expected_size:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), expected_sha256.lower()):
            return None
        mime_type = str(entry.get("mime_type") or "image/png").strip().lower()
        if CATALOG_MIME_MAGIC.get(mime_type) != catalog_asset_magic(path):
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return path, entry


def catalog_model_thumbnail_url(release_no: str, series_code: str) -> str | None:
    if model_thumbnail_entry(series_code) is None:
        return None
    return (
        f"/media/catalog-model/{release_url_token(release_no)}"
        f"/{release_url_token(series_code)}"
    )


def release_value_from_url(token: str) -> str | None:
    """Decode only canonical unpadded base64url catalog tokens."""

    if not isinstance(token, str) or not _RELEASE_URL_TOKEN_RE.fullmatch(token):
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        value = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return value if release_url_token(value) == token else None


def release_route_value(token: str) -> str | None:
    """Decode canonical tokens and retain narrow ASCII catalog URL support."""

    decoded = release_value_from_url(token)
    if decoded is not None:
        return decoded
    if isinstance(token, str) and _RELEASE_LEGACY_ROUTE_VALUE_RE.fullmatch(token):
        return token
    return None


def release_node_url_key(node_key: str) -> str:
    return release_url_token(node_key)


def release_node_key_from_url(token: str) -> str | None:
    return release_value_from_url(token)


def release_model_view(model: dict[str, Any]) -> dict[str, Any]:
    item = dict(model)
    item["url_series_code"] = release_url_token(str(model["series_code"]))
    item["url_model_code"] = release_url_token(str(model["model_code"]))
    item["display_series_name"] = catalog_series_display_name(model.get("series_name_source"))
    item["display_model_name"] = catalog_model_display_name(
        model.get("model_name_source"), model.get("model_code")
    )
    return item


def release_node_view(node: dict[str, Any]) -> dict[str, Any]:
    item = dict(node)
    item["url_key"] = release_node_url_key(str(node["node_key"]))
    item["display_node_name"] = catalog_node_display_name(node)
    return item


def release_catalog_tree(
    store: CatalogReleaseStore,
    series_code: str,
    model_code: str,
    current_node: dict[str, Any],
    ancestors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the model's full node tree, expanded along the current path.

    The per-model tree is small (observed: 3 levels, <=187 nodes per model),
    so every branch is rendered server-side; non-current branches ship
    collapsed via the ``expanded`` flag and are toggled client-side.
    """
    rows = store.model_nodes(series_code, model_code)
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for row in rows:
        parent_path = str(row["parent_key"]) if row.get("parent_key") else None
        if parent_path is None:
            roots.append(row)
        else:
            children_by_parent.setdefault(parent_path, []).append(row)
    expanded_keys = {
        str(item["node_key"])
        for item in [*ancestors, current_node]
        if item.get("node_key")
    }
    current_key = str(current_node["node_key"])

    def visit(raw_node: dict[str, Any], path_seen: frozenset[str]) -> dict[str, Any]:
        item = release_node_view(raw_node)
        key = str(raw_node["node_key"])
        path_key = str(raw_node["path_key"]) if raw_node.get("path_key") else ""
        children = [] if key in path_seen or not path_key else (
            children_by_parent.get(path_key, [])
        )
        item["selected"] = key == current_key
        item["expanded"] = bool(children) and key in expanded_keys
        item["children"] = [
            visit(child, path_seen | {key}) for child in children
        ]
        return item

    return [visit(root, frozenset()) for root in roots]


NODE_MEDIA_ASSET_TYPES = ("epc_drawing", "thumbnail")


def release_node_asset_slots(
    assets: list[dict[str, Any]], release_no: str, *, keep_empty: bool = False
) -> list[dict[str, Any]]:
    """Keep stable EPC media positions when a valid node has no image."""

    by_type: dict[str, list[dict[str, Any]]] = {
        asset_type: [] for asset_type in NODE_MEDIA_ASSET_TYPES
    }
    for asset in assets:
        asset_view = release_node_asset_view(asset, release_no)
        if asset_view is not None:
            by_type[str(asset_view["asset_type"])].append(asset_view)
    if not keep_empty:
        return [asset for asset_type in NODE_MEDIA_ASSET_TYPES for asset in by_type[asset_type]]

    slots: list[dict[str, Any]] = []
    for asset_type in NODE_MEDIA_ASSET_TYPES:
        slots.extend(
            by_type[asset_type]
            or [{"asset_type": asset_type, "media_url": None}]
        )
    return slots


def release_node_asset_view(
    asset: dict[str, Any], release_no: str
) -> dict[str, str] | None:
    asset_key = asset.get("asset_key")
    asset_type = asset.get("asset_type")
    if (
        not isinstance(asset_key, str)
        or not asset_key
        or asset_type not in {"epc_drawing", "thumbnail"}
    ):
        return None
    return {
        "asset_type": asset_type,
        "media_url": catalog_media_url(release_no, asset_key),
    }


def release_part_view(
    item: dict[str, Any],
    series_code: str,
    model_code: str,
    node_key: str,
    *,
    release_no: str | None = None,
) -> dict[str, Any]:
    result = dict(item)
    result["url_part_code"] = release_url_token(str(item["material_code"]))
    result["url_node_key"] = release_node_url_key(str(node_key))
    result["url_series_code"] = release_url_token(str(series_code))
    result["url_model_code"] = release_url_token(str(model_code))
    result["series_code"] = series_code
    result["model_code"] = model_code
    result["display_part_name"] = catalog_part_display_name(item)
    asset_key = item.get("material_asset_key")
    result["media_url"] = (
        catalog_media_url(release_no, str(asset_key))
        if release_no
        and asset_key
        and item.get("material_asset_status") in PUBLISHABLE_ASSET_STATUSES
        and item.get("material_asset_type") in DISPLAY_ASSET_TYPES
        else None
    )
    return result


def release_page_params(request: Request) -> tuple[int, int]:
    raw_limit = request.query_params.get("limit", str(RELEASE_DEFAULT_PARTS_LIMIT))
    raw_offset = request.query_params.get("offset", "0")
    if not re.fullmatch(r"-?[0-9]+", raw_limit) or not re.fullmatch(
        r"-?[0-9]+", raw_offset
    ):
        raise HTTPException(status_code=400, detail="Invalid catalog pagination")
    try:
        return normalize_parts_pagination(raw_limit, raw_offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid catalog pagination") from exc


def resolve_catalog_asset_path(object_key: object) -> Path | None:
    """Resolve an object key below the dedicated configured asset root."""

    root_text = os.getenv("LIMEAUTO_CATALOG_ASSET_ROOT", "")
    if not root_text or not isinstance(object_key, str):
        return None
    if (
        not object_key
        or "\x00" in object_key
        or "\\" in object_key
        or ".." in object_key
        or not CATALOG_OBJECT_KEY_RE.fullmatch(object_key)
    ):
        return None
    try:
        root = Path(root_text).expanduser().resolve(strict=False)
        candidate = (root / object_key).resolve(strict=False)
        candidate.relative_to(root)
        if not root.is_dir():
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate


def catalog_asset_magic(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(4096)
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"BM"):
        return "bmp"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {
        b"avif",
        b"avis",
    }:
        return "avif"
    text = header.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n").lower()
    if text.startswith("<?xml") or text.startswith("<svg"):
        return "svg"
    return "unknown"


def read_catalog_asset(asset: dict[str, Any]) -> tuple[Path, str] | None:
    """Validate asset type, MIME, path, size, and SHA-256 before streaming."""

    if asset.get("asset_type") not in DISPLAY_ASSET_TYPES:
        return None
    if asset.get("status") not in PUBLISHABLE_ASSET_STATUSES:
        return None
    mime_type = str(asset.get("mime_type") or "").strip().lower()
    if mime_type not in CATALOG_MEDIA_MIME_TYPES:
        return None
    source_sha256 = asset.get("source_sha256")
    if not isinstance(source_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", source_sha256
    ):
        return None
    expected_size = asset.get("size_bytes")
    if expected_size is not None:
        if isinstance(expected_size, bool):
            return None
        try:
            expected_size = int(expected_size)
        except (TypeError, ValueError, OverflowError):
            return None
        if expected_size < 0:
            return None
    path = resolve_catalog_asset_path(asset.get("object_key"))
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        actual_size = path.stat().st_size
        if actual_size <= 0 or (
            expected_size is not None and actual_size != expected_size
        ):
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, RuntimeError, ValueError):
        return None
    if not hmac.compare_digest(digest.hexdigest(), source_sha256.lower()):
        return None
    if CATALOG_MIME_MAGIC.get(mime_type) != catalog_asset_magic(path):
        return None
    return path, mime_type


@app.get("/media/catalog/{release_token}/{asset_token}")
def catalog_media(release_token: str, asset_token: str):
    store = get_release_store()
    try:
        release_no = str(store.release_metadata()["release_no"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    if release_value_from_url(release_token) != release_no:
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    asset_key = release_value_from_url(asset_token)
    if asset_key is None:
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    try:
        asset = store.asset_by_key(asset_key)
    except (CatalogReleaseError, OSError, sqlite3.Error):
        asset = None
    if asset is None:
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    checked = read_catalog_asset(asset)
    if checked is None:
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    path, mime_type = checked
    return FileResponse(
        path,
        media_type=mime_type,
        headers={"Cache-Control": CATALOG_MEDIA_CACHE_CONTROL},
    )


@app.get("/media/catalog-model/{release_token}/{series_token}")
def catalog_model_thumbnail(release_token: str, series_token: str):
    store = get_release_store()
    release = store.release_metadata()
    if release_value_from_url(release_token) != str(release["release_no"]):
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    series_code = release_value_from_url(series_token)
    if series_code is None or not store.models_for_series(series_code):
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    checked = model_thumbnail_file(series_code)
    if checked is None:
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    path, entry = checked
    mime_type = str(entry.get("mime_type") or "image/png").strip().lower()
    if mime_type not in CATALOG_MEDIA_MIME_TYPES:
        raise HTTPException(status_code=404, detail="Catalog media unavailable")
    return FileResponse(
        path,
        media_type=mime_type,
        headers={"Cache-Control": CATALOG_MEDIA_CACHE_CONTROL},
    )


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse(url="/ops/vehicles", status_code=303)


@app.get("/catalog")
def release_catalog_root(request: Request):
    store = get_release_store()
    release = store.release_metadata()
    release_no = str(release["release_no"])
    series = []
    for item in store.list_series():
        series_code = str(item["series_code"])
        series.append(
            dict(item)
            | {
                "display_series_name": catalog_series_display_name(item.get("series_name")),
                "url_code": release_url_token(series_code),
                "thumbnail_url": catalog_model_thumbnail_url(release_no, series_code),
            }
        )
    return render(request, "release_catalog.html", series=series, release=release)


@app.get("/catalog/search")
def catalog_search(
    request: Request,
    material_code: str = Query(""),
    material_name: str = Query(""),
    series_code: str = Query(""),
    model_code: str = Query(""),
):
    store = get_release_store()
    release = store.release_metadata()
    series_rows = []
    for item in store.list_series():
        series_rows.append(
            dict(item)
            | {
                "display_series_name": catalog_series_display_name(item.get("series_name")),
            }
        )
    selected_series = series_code.strip()
    model_rows = []
    if selected_series:
        for item in store.models_for_series(selected_series):
            model_rows.append(
                dict(item)
                | {
                    "display_model_name": catalog_model_display_name(
                        item.get("model_name_source"), item.get("model_code")
                    )
                }
            )
    submitted = bool(material_code.strip() or material_name.strip())
    results = None
    named = None
    search_error = None
    english_query = False
    if submitted:
        name_query = material_name.strip()
        english_by_kind: dict[str, tuple[str, ...]] = {}
        try:
            limit, offset = release_page_params(request)
            # A name search in English has to be resolved back to source strings
            # first: the release only stores source text. Chinese queries keep the
            # direct LIKE path so nothing changes for the default language.
            english_sources: tuple[str, ...] = ()
            if name_query and current_lang() == "en" and not _has_cjk(name_query):
                store_translation = catalog_translation_store()
                if store_translation is not None:
                    english_query = True
                    english_by_kind = {
                        kind: store_translation.resolve_sources(
                            kind, name_query, ENGLISH_SEARCH_SOURCE_LIMIT
                        )
                        # series/model/node resolve the same way the part level does, so an
                        # English operator can name a car series or a system, not just a part.
                        for kind in ("part", "series", "model", "node")
                    }
                    english_sources = english_by_kind.get("part", ())
            page = store.search_parts(
                material_code=material_code,
                material_name=material_name,
                series_code=series_code,
                model_code=model_code,
                english_sources=english_sources,
                limit=limit,
                offset=offset,
            )
        except ValueError:
            page = None
            search_error = "search_parts_hint"
        if name_query and len(name_query) >= _name_query_min_length(name_query):
            # Resolved separately from the parts search: that one requires two characters
            # (a 148k-row LIKE), which would otherwise hide a whole series behind a single
            # Chinese character -- 唐 is one character and "Tang" is four, so folding the
            # two together is a language-dependent gap, not a real limit.
            named = _named_entity_groups(
                store,
                name_query,
                english_by_kind,
                series_code=series_code,
                model_code=model_code,
            )
            if search_error == "search_parts_hint" and _named_groups_have_rows(named):
                search_error = None
        if page is not None and english_query and not page["total"]:
            # The name exists in English but renders from no source string the
            # release carries. Say so instead of showing a bare empty result —
            # unless the name did resolve to a series / model / system, in which
            # case the navigation groups above are the answer.
            if not _named_groups_have_rows(named):
                search_error = "search_english_no_match"
        if page is not None:
            items = []
            for item in page["items"]:
                view = dict(item)
                view["display_part_name"] = catalog_part_display_name(item)
                view["display_series_name"] = catalog_series_display_name(
                    item.get("series_name_source")
                )
                view["display_model_name"] = catalog_model_display_name(
                    item.get("model_name_source"), item.get("model_code")
                )
                view["display_node_name"] = catalog_node_display_name(item)
                view["url_series_code"] = release_url_token(str(item["series_code"]))
                view["url_model_code"] = release_url_token(str(item["model_code"]))
                view["url_node_key"] = release_node_url_key(str(item["node_key"]))
                view["url_part_code"] = release_url_token(str(item["material_code"]))
                items.append(view)
            results = dict(page)
            results["items"] = items
            if results["has_next"]:
                results["next_url"] = "/catalog/search?" + urlencode(
                    {
                        "material_code": material_code,
                        "material_name": material_name,
                        "series_code": series_code,
                        "model_code": model_code,
                        "limit": results["limit"],
                        "offset": results["offset"] + results["limit"],
                    }
                )
    return render(
        request,
        "release_search.html",
        release=release,
        series_rows=series_rows,
        model_rows=model_rows,
        material_code=material_code,
        material_name=material_name,
        series_code=series_code,
        model_code=model_code,
        results=results,
        named=named,
        search_error=search_error,
        submitted=submitted,
    )


def _named_groups_have_rows(named: dict[str, Any] | None) -> bool:
    if not named:
        return False
    return bool(named.get("series") or named.get("models") or named.get("nodes"))


def _name_query_min_length(query: str) -> int:
    """One CJK character is a whole word; one Latin character is not.

    唐 names a car series on its own, while a lone Latin letter like "P" only matches a
    node called "Empty" by accident. Requiring two characters for everything would hide a
    whole series behind a single Chinese character -- a gap that does not exist in English
    only because "Tang" happens to be four characters long.
    """
    return 1 if _has_cjk(query) else 2


def _named_entity_groups(
    store: Any,
    name_query: str,
    english_by_kind: dict[str, tuple[str, ...]],
    *,
    series_code: str = "",
    model_code: str = "",
) -> dict[str, Any]:
    """Series / model / system-node navigation results for a typed name.

    Same query for both languages: Chinese matches the stored source text with LIKE, and
    English matches the exact source strings the caller resolved from the runtime glossary.
    That is what makes the two operators able to do the same thing rather than merely
    similar things.
    """
    groups = store.search_named_entities(
        name=name_query,
        english_series=english_by_kind.get("series", ()),
        english_models=english_by_kind.get("model", ()),
        english_nodes=english_by_kind.get("node", ()),
        series_code=series_code,
        model_code=model_code,
    )
    for item in groups["series"]:
        item["display_series_name"] = catalog_series_display_name(item.get("series_name_source"))
        item["url"] = f"/catalog/{release_url_token(str(item['series_code']))}"
    for item in groups["models"]:
        item["display_model_name"] = catalog_model_display_name(
            item.get("model_name_source"), item.get("model_code")
        )
        item["display_series_name"] = catalog_series_display_name(item.get("series_name_source"))
        item["url"] = (
            f"/catalog/{release_url_token(str(item['series_code']))}"
            f"/{release_url_token(str(item['model_code']))}"
        )
    for item in groups["nodes"]:
        item["display_node_name"] = catalog_node_display_name(item)
        # Each node link states the vehicle it opens, so the label and the destination
        # cannot disagree.
        item["display_series_name"] = catalog_series_display_name(item.get("series_name_source"))
        item["display_model_name"] = catalog_model_display_name(
            item.get("model_name_source"), item.get("model_code")
        )
        item["url"] = (
            f"/catalog/{release_url_token(str(item['series_code']))}"
            f"/{release_url_token(str(item['model_code']))}"
            f"/node/{release_node_url_key(str(item['node_key']))}"
        )
    return groups


@app.get("/catalog/search-models")
def catalog_search_models(series_code: str = Query("")):
    store = get_release_store()
    selected = series_code.strip()
    if not selected:
        return JSONResponse([])
    rows = []
    for item in store.models_for_series(selected):
        model_code = str(item["model_code"])
        rows.append(
            {
                "model_code": model_code,
                "url_model_code": release_url_token(model_code),
                "url_series_code": release_url_token(selected),
                "display_model_name": catalog_model_display_name(
                    item.get("model_name_source"), item.get("model_code")
                ),
            }
        )
    return JSONResponse(rows)


@app.get("/catalog/{series_token}")
def release_series(request: Request, series_token: str):
    store = get_release_store()
    series_code = release_route_value(series_token)
    if series_code is None:
        raise HTTPException(status_code=404, detail="Series catalog not found")
    models = store.models_for_series(series_code)
    if not models:
        raise HTTPException(status_code=404, detail="Series catalog not found")
    release = store.release_metadata()
    return render(
        request,
        "release_series.html",
        series_code=series_code,
        series_display_name=catalog_series_display_name(
            models[0].get("series_name_source") if models else None
        ),
        series_url_code=release_url_token(series_code),
        series_thumbnail_url=catalog_model_thumbnail_url(
            str(release["release_no"]), series_code
        ),
        models=[release_model_view(model) for model in models],
        release=release,
    )


@app.get("/catalog/{series_token}/{model_token}")
def catalog(request: Request, series_token: str, model_token: str):
    store = get_release_store()
    series_code = release_route_value(series_token)
    model_code = release_route_value(model_token)
    if series_code is None or model_code is None:
        raise HTTPException(status_code=404, detail="Model catalog not found")
    model = store.model_by_codes(series_code, model_code)
    if not model:
        raise HTTPException(status_code=404, detail="Model catalog not found")
    return render(
        request,
        "release_model.html",
        model=release_model_view(model),
        nodes=[
            release_node_view(node)
            for node in store.root_nodes_for_model(series_code, model_code)
        ],
        release=store.release_metadata(),
    )


@app.get("/catalog/{series_token}/{model_token}/node/{node_token}")
def catalog_node(request: Request, series_token: str, model_token: str, node_token: str):
    store = get_release_store()
    release = store.release_metadata()
    series_code = release_route_value(series_token)
    model_code = release_route_value(model_token)
    raw_node_key = release_route_value(node_token)
    if series_code is None or model_code is None or raw_node_key is None:
        raise HTTPException(status_code=404, detail="Catalog node not found")
    limit, offset = release_page_params(request)
    model = store.model_by_codes(series_code, model_code)
    node = store.node_by_key(series_code, model_code, raw_node_key)
    if not model or not node:
        raise HTTPException(status_code=404, detail="Catalog node not found")
    release_no = str(release["release_no"])
    children = [
        release_node_view(child)
        for child in store.children_for_node(series_code, model_code, raw_node_key)
    ]
    parts_page = store.parts_for_node(
        series_code, model_code, raw_node_key, limit=limit, offset=offset
    )
    parts_page["items"] = [
        release_part_view(
            item,
            series_code,
            model_code,
            raw_node_key,
            release_no=release_no,
        )
        for item in parts_page["items"]
    ]
    node_assets_raw = store.assets_for_node(series_code, model_code, raw_node_key)
    try:
        node_part_count = int(node.get("direct_part_count") or 0) + int(
            node.get("descendant_part_count") or 0
        )
    except (TypeError, ValueError):
        node_part_count = 0
    node_has_related_catalog_data = bool(children) or node_part_count > 0 or bool(
        parts_page is not None and parts_page.get("total", 0)
    )
    # Verified across the whole release: group nodes (child_count>0) never own
    # EPC assets; drawings live on leaf subsystems only. Skip the media block
    # for group nodes instead of showing two permanently-empty slots.
    node_asset_slots = release_node_asset_slots(
        node_assets_raw,
        release_no,
        keep_empty=not children and node_has_related_catalog_data,
    )
    parent = store.parent_node(series_code, model_code, raw_node_key)
    ancestors = [
        release_node_view(item)
        for item in store.ancestor_nodes(series_code, model_code, raw_node_key)
    ]
    model_view = release_model_view(model)
    if parent:
        back_href = (
            f"/catalog/{model_view['url_series_code']}/{model_view['url_model_code']}"
            f"/node/{release_node_url_key(str(parent['node_key']))}"
        )
    else:
        back_href = f"/catalog/{model_view['url_series_code']}/{model_view['url_model_code']}"
    return render(
        request,
        "release_node.html",
        model=model_view,
        node=release_node_view(node),
        parent_node=release_node_view(parent) if parent else None,
        node_ancestors=ancestors,
        back_href=back_href,
        catalog_tree=release_catalog_tree(store, series_code, model_code, node, ancestors),
        node_asset_slots=node_asset_slots,
        children=children,
        parts_page=parts_page,
        release=release,
    )


@app.get("/catalog/{series_token}/{model_token}/node/{node_token}/part/{part_token}")
def part_detail(
    request: Request,
    series_token: str,
    model_token: str,
    node_token: str,
    part_token: str,
):
    store = get_release_store()
    release = store.release_metadata()
    series_code = release_route_value(series_token)
    model_code = release_route_value(model_token)
    raw_node_key = release_route_value(node_token)
    raw_part_code = release_route_value(part_token)
    if (
        series_code is None
        or model_code is None
        or raw_node_key is None
        or raw_part_code is None
    ):
        raise HTTPException(status_code=404, detail="Part context not found")
    model = store.model_by_codes(series_code, model_code)
    node = store.node_by_key(series_code, model_code, raw_node_key)
    part = store.part_context(series_code, model_code, raw_node_key, raw_part_code)
    if not model or not node or not part:
        raise HTTPException(status_code=404, detail="Part context not found")
    return render(
        request,
        "release_part_detail.html",
        model=release_model_view(model),
        node=release_node_view(node),
        part=release_part_view(
            part,
            series_code,
            model_code,
            raw_node_key,
            release_no=str(release["release_no"]),
        ),
        release=release,
    )


app.include_router(aftercare_router)
