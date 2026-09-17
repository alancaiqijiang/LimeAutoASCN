from __future__ import annotations

import sqlite3
from pathlib import Path

from app.catalog_release import (
    catalog_model_display_name,
    catalog_node_display_name,
    catalog_part_display_name,
    catalog_series_display_name,
)
from app.catalog_translation import (
    CatalogTranslationStore,
    catalog_translation_status,
    catalog_translation_store,
    stable_term_id,
)
from app.i18n import bind_lang, reset_lang

FINGERPRINT = "f" * 64


def make_release(path: Path, fingerprint: str = FINGERPRINT) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE catalog_releases (source_snapshot_fingerprint TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO catalog_releases VALUES (?)", (fingerprint,))
    connection.commit()
    connection.close()


def make_overlay(
    path: Path,
    rows: list[tuple[str, str, str, str, str, str]],
    fingerprint: str = FINGERPRINT,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE translation_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE translation_terms (
            term_id TEXT NOT NULL,
            term_kind TEXT NOT NULL,
            source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            lang TEXT NOT NULL,
            status TEXT NOT NULL,
            source_snapshot_fingerprint TEXT NOT NULL,
            PRIMARY KEY (term_id, lang)
        );
        """
    )
    connection.execute(
        "INSERT INTO translation_meta VALUES ('source_snapshot_fingerprint', ?)",
        (fingerprint,),
    )
    connection.executemany(
        """
        INSERT INTO translation_terms (
            term_id, term_kind, source_text, translated_text, lang, status,
            source_snapshot_fingerprint
        ) VALUES (?, ?, ?, ?, 'en', ?, ?)
        """,
        rows,
    )
    connection.commit()
    connection.close()


def test_stable_term_id_matches_manifest_algorithm() -> None:
    assert stable_term_id("part", "门锁") == "part:63579e05e34021a82ba2ae4a"


def test_store_requires_published_row_and_matching_fingerprint(tmp_path: Path) -> None:
    overlay = tmp_path / "translation.sqlite"
    source = "左前门锁"
    term_id = stable_term_id("part", source)
    make_overlay(
        overlay,
        [
            (term_id, "part", source, "Left Front Door Lock", "published", FINGERPRINT),
            (
                stable_term_id("part", "待审核"),
                "part",
                "待审核",
                "Pending",
                "luna_reviewed",
                FINGERPRINT,
            ),
        ],
    )
    store = CatalogTranslationStore(overlay, expected_fingerprint=FINGERPRINT)
    assert store.lookup("part", source) == "Left Front Door Lock"
    assert store.lookup("part", "待审核") is None

    mismatched = CatalogTranslationStore(overlay, expected_fingerprint="a" * 64)
    assert mismatched.lookup("part", source) is None


def test_runtime_display_uses_overlay_only_for_english_and_matching_release(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "release.sqlite"
    overlay = tmp_path / "translation.sqlite"
    make_release(release)
    entries = [
        (stable_term_id("series", "宋PLUS"), "series", "宋PLUS", "Song PLUS", "published", FINGERPRINT),
        (stable_term_id("model", "尊贵型"), "model", "尊贵型", "Premium Edition", "published", FINGERPRINT),
        (stable_term_id("node", "发动机"), "node", "发动机", "Engine", "published", FINGERPRINT),
        (stable_term_id("part", "左前门锁"), "part", "左前门锁", "Left Front Door Lock", "published", FINGERPRINT),
    ]
    make_overlay(overlay, entries)
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    token = bind_lang("en")
    try:
        assert catalog_series_display_name("宋PLUS") == "Song PLUS"
        assert catalog_model_display_name("尊贵型", "M1") == "Premium Edition"
        assert catalog_node_display_name({"name_source": "发动机", "display_name": "发动机"}) == "Engine"
        assert catalog_part_display_name({"description": "左前门锁", "material_code": "P1"}) == "Left Front Door Lock"
    finally:
        reset_lang(token)

    token = bind_lang("zh")
    try:
        assert catalog_series_display_name("宋PLUS") == "宋PLUS"
    finally:
        reset_lang(token)


def test_unpublished_rows_and_empty_text_fall_back_to_source(tmp_path: Path, monkeypatch) -> None:
    """The reviewed candidate overlay carries no published rows, so it must not show English.

    A row that is merely reviewed (or whose English is blank) has to keep the
    source string: a half-translated page is worse than an untranslated one.
    """
    release = tmp_path / "release.sqlite"
    overlay = tmp_path / "translation.sqlite"
    make_release(release)
    make_overlay(
        overlay,
        [
            (stable_term_id("part", "待人工"), "part", "待人工", "Pending Human", "needs_human", FINGERPRINT),
            (
                stable_term_id("part", "主进程"),
                "part",
                "主进程",
                "Main Process",
                "main_process_reviewed",
                FINGERPRINT,
            ),
            (stable_term_id("part", "空译文"), "part", "空译文", "   ", "published", FINGERPRINT),
        ],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    token = bind_lang("en")
    try:
        assert catalog_part_display_name({"description": "待人工", "material_code": "P1"}) == "待人工"
        assert catalog_part_display_name({"description": "主进程", "material_code": "P2"}) == "主进程"
        assert catalog_part_display_name({"description": "空译文", "material_code": "P3"}) == "空译文"
    finally:
        reset_lang(token)


def test_same_source_text_per_kind_keeps_its_own_rendering(tmp_path: Path, monkeypatch) -> None:
    """98 source strings are shared across kinds and 16 render differently.

    ``主轴组件`` is a system node (Main Shaft Assembly) and also a material
    (Main Shaft Component). Binding by source text alone would serve the wrong
    label on one of the two surfaces.
    """
    release = tmp_path / "release.sqlite"
    overlay = tmp_path / "translation.sqlite"
    make_release(release)
    make_overlay(
        overlay,
        [
            (
                stable_term_id("node", "主轴组件"),
                "node",
                "主轴组件",
                "Main Shaft Assembly",
                "published",
                FINGERPRINT,
            ),
            (
                stable_term_id("part", "主轴组件"),
                "part",
                "主轴组件",
                "Main Shaft Component",
                "published",
                FINGERPRINT,
            ),
        ],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    token = bind_lang("en")
    try:
        assert catalog_node_display_name({"name_source": "主轴组件", "display_name": "主轴组件"}) == (
            "Main Shaft Assembly"
        )
        assert catalog_part_display_name({"description": "主轴组件", "material_code": "P1"}) == (
            "Main Shaft Component"
        )
    finally:
        reset_lang(token)


def test_missing_row_and_missing_env_fall_back_to_source(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "release.sqlite"
    overlay = tmp_path / "translation.sqlite"
    make_release(release)
    make_overlay(
        overlay,
        [
            (
                stable_term_id("part", "已知件"),
                "part",
                "已知件",
                "Known Part",
                "published",
                FINGERPRINT,
            )
        ],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    token = bind_lang("en")
    try:
        monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
        assert catalog_part_display_name({"description": "已知件", "material_code": "P1"}) == "Known Part"
        # rows the runtime artifact does not carry keep the source name
        assert catalog_part_display_name({"description": "未收录", "material_code": "P2"}) == "未收录"
        # no configured overlay at all -> source name
        monkeypatch.delenv("LIMEAUTO_CATALOG_TRANSLATION_PATH")
        assert catalog_part_display_name({"description": "已知件", "material_code": "P1"}) == "已知件"
    finally:
        reset_lang(token)


def test_runtime_falls_back_when_release_fingerprint_changes(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "release.sqlite"
    overlay = tmp_path / "translation.sqlite"
    make_release(release, fingerprint="a" * 64)
    source = "发动机"
    make_overlay(
        overlay,
        [
            (
                stable_term_id("node", source),
                "node",
                source,
                "Engine",
                "published",
                FINGERPRINT,
            )
        ],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    token = bind_lang("en")
    try:
        assert catalog_node_display_name({"name_source": source, "display_name": source}) == source
    finally:
        reset_lang(token)


# --------------------------------------------------------------- import visibility


def test_translation_status_is_disabled_without_configuration(monkeypatch) -> None:
    monkeypatch.delenv("LIMEAUTO_CATALOG_RELEASE_PATH", raising=False)
    monkeypatch.delenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", raising=False)
    assert catalog_translation_status() == "disabled"


def test_translation_status_is_matched_when_the_fingerprints_agree(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "release.sqlite"
    overlay = tmp_path / "translation.sqlite"
    make_release(release)
    make_overlay(
        overlay,
        [(stable_term_id("part", "左前门锁"), "part", "左前门锁", "Left Front Door Lock",
          "published", FINGERPRINT)],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    assert catalog_translation_status() == "matched"


def test_translation_status_reports_a_release_update_as_mismatched(
    tmp_path: Path, monkeypatch
) -> None:
    """The state that must be visible: a rebuilt release with the old glossary.

    Every English label falls back to Chinese here, so the operator has to be able to see
    it without reading the catalog by hand.
    """
    release = tmp_path / "release.sqlite"
    overlay = tmp_path / "translation.sqlite"
    make_release(release, fingerprint="b" * 64)  # release was rebuilt
    make_overlay(
        overlay,
        [(stable_term_id("part", "左前门锁"), "part", "左前门锁", "Left Front Door Lock",
          "published", FINGERPRINT)],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    assert catalog_translation_status() == "mismatched"


def test_translation_status_is_unavailable_when_the_glossary_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "release.sqlite"
    make_release(release)
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(tmp_path / "absent.sqlite"))
    assert catalog_translation_status() == "unavailable"


def test_translation_status_is_unavailable_when_the_release_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    overlay = tmp_path / "translation.sqlite"
    make_overlay(
        overlay,
        [(stable_term_id("part", "左前门锁"), "part", "左前门锁", "Left Front Door Lock",
          "published", FINGERPRINT)],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(tmp_path / "absent.sqlite"))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    assert catalog_translation_status() == "unavailable"
