from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.main import app


EXPECTED_CSP = (
    "default-src 'self'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "font-src 'self' data:; connect-src 'self'; object-src 'none'"
)
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "app" / "templates"
HTTP_URL_RE = re.compile(r"(?i)(?:https?:)?//[^\s\"'<>]+")


class _TemplateAttributeScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.external_urls: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name not in {"href", "src", "action"} or value is None:
                continue
            for candidate in HTTP_URL_RE.findall(value):
                parsed = urlsplit(candidate)
                if parsed.scheme in {"http", "https"} or candidate.startswith("//"):
                    self.external_urls.append((tag, name, candidate))


def test_runtime_headers_and_template_urls_are_self_only() -> None:
    response = TestClient(app).get("/health/live")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"

    base_text = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
    css_text = (TEMPLATE_DIR.parent / "static" / "site.css").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in base_text
    assert "fonts.gstatic.com" not in base_text
    assert "qpren" not in base_text.lower()
    assert "google fonts" not in css_text.lower()
    assert "atlas aftercare" not in css_text.lower()
    assert "@import" not in css_text.lower()
    assert ".catalog-epc-asset-frame" in css_text
    assert ".catalog-epc-asset-frame img" in css_text
    assert "min-width: 0" in css_text
    assert "min-height: 0" in css_text
    assert "overflow: hidden" in css_text
    assert ".catalog-image-zoom" in css_text
    assert "img[data-catalog-image]" in css_text
    js_text = (TEMPLATE_DIR.parent / "static" / "site.js").read_text(encoding="utf-8")
    assert "catalog-epc-asset-placeholder" in js_text
    assert "catalog-series-thumbnail-empty" in js_text
    assert "catalog-series-heading-placeholder" in js_text
    assert 'closest(".catalog-epc-asset-frame, .catalog-media-frame")' in js_text
    assert "dblclick" in js_text
    assert "openZoom" in js_text
    assert 'data-zoom-open' in base_text
    assert "catalog-nav-legend" in base_text
    assert "catalog-nav-note" in base_text
    assert "catalog-nav-static" not in base_text
    assert ".catalog-nav-legend" in css_text
    assert ".catalog-nav-note" in css_text
    assert "pointer-events: none" in css_text

    for template_path in sorted(TEMPLATE_DIR.glob("*.html")):
        scanner = _TemplateAttributeScanner()
        scanner.feed(template_path.read_text(encoding="utf-8"))
        assert not scanner.external_urls, (template_path, scanner.external_urls)


def test_catalog_templates_use_display_names_not_identifier_fallbacks() -> None:
    catalog_templates = {
        "release_catalog.html": "display_series_name",
        "release_series.html": "series_display_name",
        "release_model.html": "display_model_name",
        "release_node.html": "display_node_name",
        "release_part_detail.html": "display_part_name",
        "release_search.html": "display_part_name",
    }
    forbidden_fallbacks = (
        "series_name_source or series_code",
        "model_name_source or model_code",
        "display_name or name_source",
        "display_name or path_key",
        "description or display_name_source",
        "series_name or series_code",
        "model_name or model_name_source",
    )
    for filename, display_field in catalog_templates.items():
        text = (TEMPLATE_DIR / filename).read_text(encoding="utf-8")
        assert display_field in text, filename
        assert not any(pattern in text for pattern in forbidden_fallbacks), filename


    client = TestClient(app)

    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "catalog": "disabled"}

    catalog = client.get("/catalog")
    assert catalog.status_code == 410
    assert catalog.json()["code"] == "catalog_browse_disabled"


def test_removed_public_surfaces_are_explicitly_closed() -> None:
    client = TestClient(app)
    for path in (
        "/vin",
        "/vehicle/demo",
        "/search/models",
        "/account",
        "/admin",
        "/requests",
        "/cart",
        "/checkout",
        "/orders",
        "/payment",
        "/api/capabilities",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 410, path
        assert response.json()["code"] == "legacy_surface_removed", path


def test_root_routes_to_internal_aftercare() -> None:
    response = TestClient(app).get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/vehicles"


def test_aftercare_brand_uses_local_lime_logo() -> None:
    logo = TEMPLATE_DIR.parent / "static" / "lime-logo.png"
    assert logo.is_file()
    assert logo.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    base_text = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
    css_text = (TEMPLATE_DIR.parent / "static" / "site.css").read_text(encoding="utf-8")
    assert 'src="/static/lime-logo.png"' in base_text
    assert "brand-mark" not in base_text
    assert "#c08a2e" not in css_text
    assert "#c99436" not in css_text
    assert "#eef4f7" not in css_text
    assert "#5a9aaf" not in css_text
    assert "#c24d24" not in css_text
    assert "auth-retro-gear" not in css_text
    assert "radial-gradient(circle 2.2px" not in css_text
    assert "border-width: 0 0 22px 22px" in css_text
    assert "--paper: #faf7f4" in css_text
    assert "v=20260910-search-fix1" in base_text
    assert ".site-header .lang-switch a.active" in css_text
    assert ".site-header .lang-switch a.active { color: inherit; }" not in css_text
    assert "ui-retro" in base_text
    assert ".catalog-card-meta" in css_text
    assert "data-catalog-locator" in base_text
    assert "<summary>{{ t.catalog }}</summary>" not in base_text
    assert ".catalog-locator" in css_text
    assert ".catalog-series-card[hidden], .catalog-model-card[hidden], .catalog-locator-list a[hidden]" in css_text
    catalog_home = (TEMPLATE_DIR / "release_catalog.html").read_text(encoding="utf-8")
    assert 'class="catalog-card-meta"' in catalog_home
    assert catalog_home.index("catalog-card-index") < catalog_home.index("catalog-count-badge")
    maintenance_form = (TEMPLATE_DIR / "aftercare_maintenance_form.html").read_text(encoding="utf-8")
    assert 'class="account-page account-maintenance"' in maintenance_form
    assert "account-narrow" not in maintenance_form
    assert 'class="maintenance-item-card" data-item-row' in maintenance_form
    assert "form-section-grid" in maintenance_form
    assert 'name="item_type"' in maintenance_form
    detail_template = (TEMPLATE_DIR / "aftercare_maintenance_detail.html").read_text(encoding="utf-8")
    assert "maintenance-detail-head" in detail_template
    assert "maintenance-status" in detail_template
    vehicles_template = (TEMPLATE_DIR / "aftercare_vehicles.html").read_text(encoding="utf-8")
    assert "vehicle-match-lines" in vehicles_template
    assert ".maintenance-item-card" in css_text
    assert ".maintenance-status-recommended" in css_text
    assert ".vehicle-match-lines" in css_text
    assert ".maintenance-detail-head" in css_text
    assert ".account-page.account-maintenance {\n  max-width: 900px;\n  width: 100%;\n}" in css_text
    login = TestClient(app).get("/ops/login")
    assert login.status_code == 200
    assert "/static/lime-logo.png" in login.text
    asset = TestClient(app).get("/static/lime-logo.png")
    assert asset.status_code == 200
    assert "image/png" in asset.headers["content-type"]


def test_catalog_drawer_and_image_preview_contracts() -> None:
    """Guard the mobile drawer and image-preview wiring regressions."""
    js_text = (TEMPLATE_DIR.parent / "static" / "site.js").read_text(encoding="utf-8")
    css_text = (TEMPLATE_DIR.parent / "static" / "site.css").read_text(encoding="utf-8")
    base_text = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")

    # The toggle must start collapsed and have a real close affordance in the
    # drawer, otherwise the open drawer covers its own toggle on small screens.
    assert 'aria-expanded="false"' in base_text
    assert 'class="catalog-sidebar-close"' in base_text
    assert ".catalog-sidebar-close" in css_text
    assert ".catalog-sidebar-toggle { position: relative; z-index: 11; }" in css_text
    assert 'event.key === "Escape" && sidebarOpen()' in js_text
    assert "catalog-sidebar-close" in js_text

    # Series-card thumbnails live inside a link: they must navigate, not be
    # swallowed, while framed previews still open the zoom viewer.
    assert "if (!previewFrame(image)) return;" in js_text
    assert '.maintenance-photo-row, .maintenance-existing-photo' in js_text
    assert '.catalog-series-thumbnail' not in js_text.split("previewFrame = (image) =>")[1].split(";")[0]
