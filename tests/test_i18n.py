from __future__ import annotations

from fastapi.testclient import TestClient

from app import main
from app.i18n import STRINGS, bind_lang, reset_lang, tr

from tests.test_catalog_routes import enabled_client, make_release


def test_i18n_tables_have_matching_keys() -> None:
    zh_keys = set(STRINGS["zh"])
    en_keys = set(STRINGS["en"]) - {"result_count_plural"}
    assert zh_keys == en_keys


def test_catalog_and_aftercare_switch_between_zh_and_en(monkeypatch, tmp_path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))

    zh = client.get("/catalog?lang=zh")
    assert zh.status_code == 200
    assert 'lang="zh-CN"' in zh.text
    assert "车型目录" in zh.text
    assert "首页" in zh.text
    assert "hreflang=\"en\"" in zh.text
    assert zh.cookies.get("limeauto_lang") == "zh"
    assert 'data-zoom-open="双击放大查看细节"' in zh.text
    assert "catalog-nav-legend" in zh.text
    assert "catalog-nav-note" in zh.text
    assert "catalog-nav-static" not in zh.text
    assert "catalog-nav-item" in zh.text

    en = client.get("/catalog?lang=en")
    assert en.status_code == 200
    assert 'lang="en"' in en.text
    assert ">Catalog</a>" in en.text or ">Catalog<" in en.text
    assert "Home" in en.text
    assert "Browse by vehicle" in en.text
    assert "EPC drawing" not in en.text
    assert en.cookies.get("limeauto_lang") == "en"
    assert 'data-zoom-open="Double-click to enlarge"' in en.text

    persisted = client.get("/catalog")
    assert persisted.status_code == 200
    assert 'lang="en"' in persisted.text
    assert "Home" in persisted.text

    # The login page itself needs a client with no session: the fixture signs in, and a
    # signed-in visitor is sent on to the staff area instead of seeing the form.
    anonymous = TestClient(main.app)
    login_en = anonymous.get("/ops/login?lang=en")
    assert login_en.status_code == 200
    assert "Staff sign in" in login_en.text
    assert "Email address" in login_en.text

    login_zh = anonymous.get("/ops/login?lang=zh")
    assert login_zh.status_code == 200
    assert "员工登录" in login_zh.text
    assert "邮箱" in login_zh.text


def test_translator_follows_bound_language() -> None:
    token = bind_lang("en")
    try:
        assert tr("missing_model_name") == "Model name pending"
        assert tr("catalog") == "Catalog"
    finally:
        reset_lang(token)
    assert tr("missing_model_name") == "车型名称待补充"
