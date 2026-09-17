from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.catalog_release import CatalogReleaseStore
from app.catalog_translation import stable_term_id
from tests.staff_session import catalog_staff_client

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "release_schema" / "catalog_release.sql"
HYE_SERIES = "HYE"
HYE_MODEL = "HYEE-PZ02"
SA_SERIES = "SA2HG/K"
SA_MODEL = "EV/中文%"
HYE_ROOT = "source:HYE:HYEE-PZ02:Engine:OBJ-1"
HYE_ROOT_VARIANT = "source:HYE:HYEE-PZ02:Engine:OBJ-2"
HYE_CHILD = "source:HYE:HYEE-PZ02:Engine>Cooling:OBJ-2"
HYE_EMPTY = "source:HYE:HYEE-PZ02:Empty:OBJ-EMPTY"
CODE_ONLY_SERIES = "CODE-SERIES"
CODE_ONLY_MODEL = "CODE-MODEL"
SA_NODE = "source:SA2HG/K:EV/中文%:Battery/高压:OBJ/1"
SA_SYSTEM_NODE = "source:SA2HG/K:EV/中文%:Engine:OBJ/SYS"
PADDED_PART = "P2"
PADDED_PART_SOURCE = "Padded part name"
PADDED_PART_RAW = "Padded part name\t\t"
SA_PART = "零件/10%/中文"


def make_release(
    tmp_path: Path,
    *,
    with_node_assets: bool = False,
    with_path_variant: bool = False,
    with_code_only_model: bool = False,
    with_shared_node_name: bool = False,
    with_padded_part_name: bool = False,
) -> CatalogReleaseStore:
    path = tmp_path / "route-release.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO catalog_releases (release_id, release_no, source_snapshot_fingerprint, source_counts_json, validation_summary_json) VALUES ('r1', 'route-1', 'fingerprint', '{}', '{}')"
    )
    nodes = [
        (HYE_ROOT, "HYE", "HYEE-PZ02", "OBJ-1", "Engine", None, "Engine", "Engine", "Engine", 1, 2 if with_path_variant else 1, 1, 0),
        (HYE_CHILD, "HYE", "HYEE-PZ02", "OBJ-2", "Engine>Cooling", "Engine", "Engine>Cooling", "Cooling", "Cooling", 2, 1, 0, 1),
        (HYE_EMPTY, "HYE", "HYEE-PZ02", "OBJ-EMPTY", "Empty", None, "Empty", "Empty", "Empty", 1, 1, 0, 0),
        (SA_NODE, SA_SERIES, SA_MODEL, "OBJ/1", "Battery/高压", None, "Battery/高压", "高压电池", "高压电池", 1, 1, 0, 1),
    ]
    if with_path_variant:
        nodes.append(
            (HYE_ROOT_VARIANT, "HYE", "HYEE-PZ02", "OBJ-2", "Engine", None, "Engine", "Engine", "Engine", 1, 2, 1, 0)
        )
    if with_shared_node_name:
        # The same system name in a second vehicle: the case where a name-only match must
        # not silently pick one of them.
        nodes.append(
            (SA_SYSTEM_NODE, SA_SERIES, SA_MODEL, "OBJ/SYS", "Engine", None, "Engine", "Engine", "Engine", 1, 1, 0, 1)
        )
    connection.executemany(
        """
        INSERT INTO release_models (
            release_id, series_code, model_code, series_name_source, model_name_source
        ) VALUES ('r1', ?, ?, ?, ?)
        """,
        [
            ("HYE", "HYEE-PZ02", "HYE series", "BYD Seal 08 EV"),
            ("SA2HG/K", "EV/中文%", "SA2HG/K series", "Slash model"),
            *(
                [(CODE_ONLY_SERIES, CODE_ONLY_MODEL, "Code series", CODE_ONLY_MODEL)]
                if with_code_only_model
                else []
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO system_nodes (
            release_id, node_key, series_code, model_code, source_obj_code,
            path_key, parent_key, node_path_source, name_source, display_name,
            depth, path_variant_count, child_count, direct_part_count
        ) VALUES ('r1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        nodes,
    )
    connection.executemany(
        """
        INSERT INTO catalog_parts (release_id, material_code, display_name_source, description)
        VALUES ('r1', ?, ?, ?)
        """,
        [
            ("P1", "Part one", "Part one"),
            (SA_PART, "Slash part", "Slash part"),
            *(
                [(PADDED_PART, PADDED_PART_RAW, PADDED_PART_RAW)]
                if with_padded_part_name
                else []
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO fitments (
            release_id, source_occurrence_key, series_code, model_code,
            node_key, material_code, callout, quantity, quantity_raw,
            fitment_level, review_status
        ) VALUES ('r1', 'occ-1', 'HYE', 'HYEE-PZ02', ?, 'P1', '1', 1, '1', 'reference_only', 'pending')
        """
        ,
        (HYE_CHILD,),
    )
    connection.execute(
        """
        INSERT INTO fitments (
            release_id, source_occurrence_key, series_code, model_code,
            node_key, material_code, callout, quantity, quantity_raw,
            fitment_level, review_status
        ) VALUES ('r1', 'occ-slash', ?, ?, ?, ?, 'S-1', 2, '2', 'reference_only', 'pending')
        """,
        (SA_SERIES, SA_MODEL, SA_NODE, SA_PART),
    )
    if with_shared_node_name:
        connection.execute(
            """
            INSERT INTO fitments (
                release_id, source_occurrence_key, series_code, model_code,
                node_key, material_code, callout, quantity, quantity_raw,
                fitment_level, review_status
            ) VALUES ('r1', 'occ-shared', ?, ?, ?, ?, 'E-1', 1, '1', 'reference_only', 'pending')
            """,
            (SA_SERIES, SA_MODEL, SA_SYSTEM_NODE, SA_PART),
        )
    if with_padded_part_name:
        connection.execute(
            """
            INSERT INTO fitments (
                release_id, source_occurrence_key, series_code, model_code,
                node_key, material_code, callout, quantity, quantity_raw,
                fitment_level, review_status
            ) VALUES ('r1', 'occ-padded', 'HYE', 'HYEE-PZ02', ?, ?, 'P-1', 1, '1', 'reference_only', 'pending')
            """,
            (HYE_CHILD, PADDED_PART),
        )
    if with_node_assets:
        connection.executemany(
            """
            INSERT INTO catalog_assets (
                release_id, asset_key, system_node_key, asset_type,
                object_key, mime_type, status
            ) VALUES ('r1', ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "node-epc-drawing",
                    HYE_ROOT,
                    "epc_drawing",
                    "release/route-1/assets/epc_drawing/node-drawing.svg",
                    "image/svg+xml",
                    "done",
                ),
                (
                    "node-epc-thumbnail",
                    HYE_ROOT,
                    "thumbnail",
                    "release/route-1/assets/thumbnail/node-thumbnail.png",
                    "image/png",
                    "ready",
                ),
            ],
        )
    connection.commit()
    connection.close()
    return CatalogReleaseStore(path, release_no="route-1", allow_draft=True)


def enabled_client(monkeypatch, store: CatalogReleaseStore) -> TestClient:
    # The catalog answers only to a staff session now, so the fixture signs in for real.
    return catalog_staff_client(monkeypatch, store)


def test_catalog_stays_closed_by_default() -> None:
    client = TestClient(main.app)
    response = client.get("/catalog")
    assert response.status_code == 410
    assert response.json()["code"] == "catalog_browse_disabled"
    assert client.get("/catalog/HYE/HYEE-PZ02").status_code == 410
    assert client.get("/api/catalog/parts/19789044-00").status_code == 410
    assert client.get("/media/catalog-model/x/y").status_code == 410


def test_catalog_first_route_chain(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))

    root = client.get("/catalog")
    assert root.status_code == 200
    assert "按车系浏览" in root.text
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    assert f"/catalog/{series_token}" in root.text

    series = client.get(f"/catalog/{series_token}")
    assert series.status_code == 200
    assert f"/catalog/{series_token}/{model_token}" in series.text

    model = client.get(f"/catalog/{series_token}/{model_token}")
    assert model.status_code == 200
    root_key = main.release_node_url_key(HYE_ROOT)
    assert f"/catalog/{series_token}/{model_token}/node/{root_key}" in model.text

    node = client.get(f"/catalog/{series_token}/{model_token}/node/{root_key}")
    assert node.status_code == 200
    child_key = main.release_node_url_key(HYE_CHILD)
    assert f"/catalog/{series_token}/{model_token}/node/{child_key}" in node.text
    assert f'href="/catalog/{series_token}/{model_token}/node/{root_key}" class="catalog-tree-item selected"' in node.text
    assert 'aria-expanded="true"' in node.text
    tree = node.text.split('<nav class="catalog-tree"', 1)[1].split('</nav>', 1)[0]
    assert tree.count('aria-current="page"') == 1
    empty_key = main.release_node_url_key(HYE_EMPTY)
    empty_link = f'href="/catalog/{series_token}/{model_token}/node/{empty_key}"'
    assert empty_link in node.text
    assert f'{empty_link} class="catalog-tree-item"' in node.text

    leaf = client.get(f"/catalog/{series_token}/{model_token}/node/{child_key}")
    assert leaf.status_code == 200
    assert f'href="/catalog/{series_token}/{model_token}/node/{root_key}" class="catalog-tree-item"' in leaf.text
    assert f'href="/catalog/{series_token}/{model_token}/node/{child_key}" class="catalog-tree-item selected"' in leaf.text
    leaf_tree = leaf.text.split('<nav class="catalog-tree"', 1)[1].split("</nav>", 1)[0]
    assert leaf_tree.count('aria-current="page"') == 1
    assert "Part one" in leaf.text
    part_token = main.release_url_token("P1")
    assert f"/catalog/{series_token}/{model_token}/node/{child_key}/part/{part_token}" in leaf.text

    # Sibling branches ship collapsed and are toggled in place by site.js.
    empty_page = client.get(f"/catalog/{series_token}/{model_token}/node/{empty_key}")
    assert empty_page.status_code == 200
    empty_tree = empty_page.text.split('<nav class="catalog-tree"', 1)[1].split("</nav>", 1)[0]
    assert '<div class="catalog-tree-children" hidden>' in empty_tree
    assert 'data-tree-toggle aria-expanded="false"' in empty_tree

    detail = client.get(
        f"/catalog/{series_token}/{model_token}/node/{child_key}/part/{part_token}"
    )
    assert detail.status_code == 200
    assert "参考物料" in detail.text
    assert "P1" in detail.text
    assert "仅供参考的目录" in detail.text
    for forbidden in ("local_path", "dsn", "price", "inventory", "ordering"):
        assert forbidden not in detail.text.lower()

    # Ordinary ASCII model/part URLs remain readable for existing bookmarks.
    assert client.get(f"/catalog/{HYE_SERIES}/{HYE_MODEL}").status_code == 200
    assert (
        client.get(
            f"/catalog/{HYE_SERIES}/{HYE_MODEL}/node/{root_key}/part/P1"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/catalog/{HYE_SERIES}/{HYE_MODEL}/node/{child_key}/part/P1"
        ).status_code
        == 200
    )


def test_catalog_breadcrumb_locator_lists_series_then_models(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path, with_code_only_model=True))
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    other_series = main.release_url_token(SA_SERIES)

    home = client.get("/catalog")
    assert home.status_code == 200
    assert 'data-catalog-locator' in home.text
    assert home.text.count("data-catalog-locator") == 2
    assert "<summary>车型目录</summary>" not in home.text
    assert f'href="/catalog/{series_token}"' in home.text
    assert f'href="/catalog/{other_series}"' in home.text
    assert "先选择车系" in home.text
    assert f'href="/catalog/{series_token}/{model_token}"' not in home.text

    series = client.get(f"/catalog/{series_token}")
    assert series.status_code == 200
    assert "HYE series" in series.text
    assert f'href="/catalog/{series_token}/{model_token}"' in series.text
    assert "Slash model" not in series.text

    model = client.get(f"/catalog/{series_token}/{model_token}")
    assert model.status_code == 200
    assert "BYD Seal 08 EV" in model.text
    assert f'href="/catalog/{series_token}/{model_token}"' in model.text
    assert "Slash model" not in model.text


def test_catalog_display_names_do_not_fall_back_to_codes(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(
        monkeypatch, make_release(tmp_path, with_code_only_model=True)
    )
    series_token = main.release_url_token(CODE_ONLY_SERIES)
    model_token = main.release_url_token(CODE_ONLY_MODEL)

    series = client.get(f"/catalog/{series_token}")
    assert series.status_code == 200
    assert "车型名称待补充" in series.text
    assert f"<code>{CODE_ONLY_MODEL}</code>" in series.text

    model = client.get(f"/catalog/{series_token}/{model_token}")
    assert model.status_code == 200
    assert f"<title>车型名称待补充 · LimeAuto</title>" in model.text
    assert "<h1>车型名称待补充</h1>" in model.text
    assert f"<code>{CODE_ONLY_MODEL}</code>" in model.text


def test_catalog_display_name_helpers_keep_codes_secondary() -> None:
    from app.catalog_release import (
        MISSING_MODEL_NAME,
        MISSING_NODE_NAME,
        MISSING_PART_NAME,
        catalog_model_display_name,
        catalog_node_display_name,
        catalog_part_display_name,
        catalog_series_display_name,
    )

    assert catalog_series_display_name("F0") == "F0"
    assert catalog_series_display_name("unknown") != "unknown"
    assert catalog_model_display_name(CODE_ONLY_MODEL, CODE_ONLY_MODEL) == MISSING_MODEL_NAME
    assert catalog_model_display_name("unknown", "MODEL-1") == MISSING_MODEL_NAME
    assert catalog_node_display_name(
        {"display_name": "OBJ-1", "name_source": "OBJ-1", "source_obj_code": "OBJ-1"}
    ) == MISSING_NODE_NAME
    assert catalog_part_display_name(
        {"material_code": "P1", "description": "P1", "display_name_source": "P1"}
    ) == MISSING_PART_NAME
    assert catalog_part_display_name(
        {"material_code": "P1", "description": "Readable part", "display_name_source": "P1"}
    ) == "Readable part"


def test_catalog_path_variants_are_labeled_in_lists_and_details(
    monkeypatch, tmp_path: Path
) -> None:
    client = enabled_client(
        monkeypatch, make_release(tmp_path, with_path_variant=True)
    )
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    model_url = f"/catalog/{series_token}/{model_token}"
    root_token = main.release_node_url_key(HYE_ROOT)
    variant_token = main.release_node_url_key(HYE_ROOT_VARIANT)

    model = client.get(model_url)

    assert model.status_code == 200
    assert "2 个来源变体" in model.text
    assert "来源编码：OBJ-1" in model.text
    assert "来源编码：OBJ-2" in model.text
    assert f'href="{model_url}/node/{root_token}"' in model.text
    assert f'href="{model_url}/node/{variant_token}"' in model.text
    assert "1 个来源变体" not in model.text

    variant_detail = client.get(f"{model_url}/node/{variant_token}")

    assert variant_detail.status_code == 200
    assert "2 个来源变体" in variant_detail.text

    ordinary_detail = client.get(
        f"{model_url}/node/{main.release_node_url_key(HYE_CHILD)}"
    )

    assert ordinary_detail.status_code == 200
    assert "1 个来源变体" not in ordinary_detail.text


def test_legacy_node_without_assets_keeps_existing_flow(
    monkeypatch, tmp_path: Path
) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    root_token = main.release_node_url_key(HYE_ROOT)

    response = client.get(f"/catalog/{series_token}/{model_token}/node/{root_token}")

    assert response.status_code == 200
    # Group nodes never own EPC drawings in this release; the media block is
    # removed for them instead of showing permanently-empty slots.
    assert "catalog-epc-reference" not in response.text
    assert "catalog-epc-asset-placeholder" not in response.text
    assert "继续选择子系统" in response.text


def test_catalog_node_without_assets_keeps_blank_media_slots_for_parts(
    monkeypatch, tmp_path: Path
) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    child_token = main.release_node_url_key(HYE_CHILD)

    response = client.get(f"/catalog/{series_token}/{model_token}/node/{child_token}")

    assert response.status_code == 200
    assert "catalog-epc-reference" in response.text
    assert response.text.count("catalog-epc-asset-placeholder") == 2
    assert "物料清单" in response.text
    assert "Part one" in response.text


def test_catalog_node_renders_opaque_epc_asset_urls(
    monkeypatch, tmp_path: Path
) -> None:
    client = enabled_client(
        monkeypatch, make_release(tmp_path, with_node_assets=True)
    )
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    root_token = main.release_node_url_key(HYE_ROOT)

    response = client.get(f"/catalog/{series_token}/{model_token}/node/{root_token}")

    assert response.status_code == 200
    assert main.catalog_media_url("route-1", "node-epc-drawing") in response.text
    assert main.catalog_media_url("route-1", "node-epc-thumbnail") in response.text
    assert "release/route-1/assets/epc_drawing/node-drawing.svg" not in response.text
    assert "release/route-1/assets/thumbnail/node-thumbnail.png" not in response.text
    assert "catalog-epc-reference" in response.text
    assert "EPC 图纸" in response.text
    assert "EPC 缩略图" in response.text
    assert "仅供参考" in response.text
    assert 'loading="lazy"' in response.text
    assert 'data-catalog-image' in response.text
    assert 'data-zoom-open="双击放大查看细节"' in response.text


@pytest.mark.parametrize(
    "raw_value",
    [
        "SA2HG/K",
        "EV/中文%",
        "零件/10%/中文",
        "节点/中文%",
    ],
)
def test_release_route_token_roundtrip(raw_value: str) -> None:
    token = main.release_url_token(raw_value)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert "/" not in token
    assert "%" not in token
    assert main.release_value_from_url(token) == raw_value


def test_slash_series_model_node_and_part_route_chain(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    series_token = main.release_url_token(SA_SERIES)
    model_token = main.release_url_token(SA_MODEL)
    node_token = main.release_node_url_key(SA_NODE)
    part_token = main.release_url_token(SA_PART)

    # The old percent-encoded slash is not a path-segment compatibility path.
    assert client.get("/catalog/SA2HG%2FK").status_code == 404

    series = client.get(f"/catalog/{series_token}")
    assert series.status_code == 200
    assert f'href="/catalog/{series_token}/{model_token}"' in series.text
    assert f'href="/catalog/{SA_SERIES}' not in series.text

    model = client.get(f"/catalog/{series_token}/{model_token}")
    assert model.status_code == 200
    assert f"/catalog/{series_token}/{model_token}/node/{node_token}" in model.text

    node = client.get(f"/catalog/{series_token}/{model_token}/node/{node_token}")
    assert node.status_code == 200
    assert "Slash part" in node.text
    assert f"/catalog/{series_token}/{model_token}/node/{node_token}/part/{part_token}" in node.text

    detail = client.get(
        f"/catalog/{series_token}/{model_token}/node/{node_token}/part/{part_token}"
    )
    assert detail.status_code == 200
    assert SA_PART in detail.text


def test_valid_empty_node_is_not_reported_as_unknown(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    empty_token = main.release_node_url_key(HYE_EMPTY)

    response = client.get(f"/catalog/{series_token}/{model_token}/node/{empty_token}")
    assert response.status_code == 200
    assert "该目录节点有效但暂无物料。" in response.text
    assert "目录节点不存在" not in response.text

    unknown_token = main.release_node_url_key("source:HYE:HYEE-PZ02:Unknown:OBJ")
    assert (
        client.get(f"/catalog/{series_token}/{model_token}/node/{unknown_token}").status_code
        == 404
    )
    assert (
        client.get(
            f"/catalog/{main.release_url_token(SA_SERIES)}/{model_token}/node/{empty_token}"
        ).status_code
        == 404
    )


@pytest.mark.parametrize("query", ["limit=not-a-number", "offset=not-a-number", "limit=-1", "offset=-1", "limit=0"])
def test_catalog_pagination_rejects_invalid_values(
    monkeypatch, tmp_path: Path, query: str
) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    child_token = main.release_node_url_key(HYE_CHILD)

    response = client.get(
        f"/catalog/{series_token}/{model_token}/node/{child_token}?{query}"
    )
    assert response.status_code == 400


def test_catalog_pagination_caps_huge_values_without_500(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    child_token = main.release_node_url_key(HYE_CHILD)

    response = client.get(
        f"/catalog/{series_token}/{model_token}/node/{child_token}"
        f"?limit={10**60}&offset={10**60}"
    )
    assert response.status_code == 200
    assert "该目录节点有效但暂无物料。" in response.text


def test_catalog_unknown_context_is_not_leaked(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    assert client.get("/catalog/UNKNOWN").status_code == 404
    assert client.get(f"/catalog/{main.release_url_token(HYE_SERIES)}/OTHER").status_code == 404
    assert (
        client.get(
            f"/catalog/{main.release_url_token(HYE_SERIES)}/{main.release_url_token(HYE_MODEL)}"
            "/node/not-a-real-node"
        ).status_code
        == 404
    )



def test_catalog_model_thumbnail_uses_aligned_local_asset(monkeypatch, tmp_path: Path) -> None:
    release = make_release(tmp_path)
    asset_root = tmp_path / "model-thumbnails"
    image_root = asset_root / "images"
    image_root.mkdir(parents=True)
    image = image_root / "hye.png"
    image_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    image.write_bytes(image_bytes)
    (asset_root / "series-map.json").write_text(
        json.dumps(
            {
                "schema_version": "limeauto-model-thumbnail-map.v1",
                "series": {
                    "HYE": {
                        "filename": image.name,
                        "source_name": "HYE series",
                        "source_url": "https://www.qpren.cn/prod-api/pc/file/cn/tis/file/carimgs/hye.png",
                        "sha256": hashlib.sha256(image_bytes).hexdigest(),
                        "bytes": len(image_bytes),
                        "mime_type": "image/png",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_MODEL_THUMBNAIL_MAP", str(asset_root / "series-map.json"))
    monkeypatch.setenv("LIMEAUTO_CATALOG_MODEL_THUMBNAIL_ROOT", str(asset_root))
    main._read_model_thumbnail_map.cache_clear()
    client = enabled_client(monkeypatch, release)
    release_token = main.release_url_token("route-1")
    series_token = main.release_url_token("HYE")
    media_url = f"/media/catalog-model/{release_token}/{series_token}"

    root = client.get("/catalog")
    assert root.status_code == 200
    assert media_url in root.text
    assert 'data-catalog-image' in root.text
    series = client.get(f"/catalog/{series_token}")
    assert series.status_code == 200
    assert media_url in series.text
    assert 'data-catalog-image' in series.text

    response = client.get(media_url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == image_bytes


def test_catalog_search_avoids_full_model_list_and_finds_parts(monkeypatch, tmp_path: Path) -> None:
    client = enabled_client(monkeypatch, make_release(tmp_path))
    blank = client.get("/catalog/search")
    assert blank.status_code == 200
    assert "先选择车系" in blank.text
    assert 'name="model_code"' in blank.text
    assert "HYEE-PZ02" not in blank.text
    assert "Slash model" not in blank.text
    assert "没有匹配的物料" not in blank.text

    series_only = client.get("/catalog/search", params={"series_code": HYE_SERIES})
    assert series_only.status_code == 200
    assert "BYD Seal 08 EV" in series_only.text
    assert "Slash model" not in series_only.text
    assert "没有匹配的物料" not in series_only.text

    models = client.get("/catalog/search-models", params={"series_code": HYE_SERIES})
    assert models.status_code == 200
    assert models.json() == [
        {
            "model_code": HYE_MODEL,
            "url_model_code": main.release_url_token(HYE_MODEL),
            "url_series_code": main.release_url_token(HYE_SERIES),
            "display_model_name": "BYD Seal 08 EV",
        }
    ]
    assert client.get("/catalog/search-models").json() == []

    named = client.get("/catalog/search", params={"material_name": "Part"})
    assert named.status_code == 200
    assert "P1" in named.text
    assert "Part one" in named.text
    assert "/part/" in named.text
    assert "出厂价" not in named.text
    assert "factory price" not in named.text.lower()

    coded = client.get("/catalog/search", params={"material_code": "P1"})
    assert coded.status_code == 200
    assert "P1" in coded.text

    lower = client.get("/catalog/search", params={"material_code": "p1"})
    assert lower.status_code == 200
    assert "P1" in lower.text

    missing = client.get("/catalog/search", params={"material_code": "NOPE"})
    assert missing.status_code == 200
    assert "没有匹配的物料" in missing.text

    short = client.get("/catalog/search", params={"material_name": "P"})
    assert short.status_code == 200
    assert "有编码时请优先填编码" in short.text


def test_catalog_search_in_english_matches_the_english_name(monkeypatch, tmp_path: Path) -> None:
    """lang=en must find a part by its English name, not only by its source text.

    The release carries source text only, so the English query is resolved to
    source strings first. Without that step an English search silently returns
    nothing while the page itself looks fully translated.
    """
    store = make_release(tmp_path)
    release_path = store.database_path
    overlay = tmp_path / "translation.sqlite"
    connection = sqlite3.connect(overlay)
    connection.executescript(
        """
        CREATE TABLE translation_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE translation_terms (
            term_id TEXT NOT NULL, term_kind TEXT NOT NULL, source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL, lang TEXT NOT NULL, status TEXT NOT NULL,
            source_snapshot_fingerprint TEXT NOT NULL,
            PRIMARY KEY (term_id, lang)
        );
        """
    )
    connection.execute(
        "INSERT INTO translation_meta VALUES ('source_snapshot_fingerprint', 'fingerprint')"
    )
    from app.catalog_translation import stable_term_id

    connection.executemany(
        "INSERT INTO translation_terms VALUES (?, 'part', ?, ?, 'en', 'published', 'fingerprint')",
        [
            (stable_term_id("part", "Part one"), "Part one", "Left Front Door Lock"),
            (stable_term_id("part", "Slash part"), "Slash part", "Rear Bumper Bracket"),
            (stable_term_id("part", "Pending part"), "Pending part", "Hidden Name"),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(release_path))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    client = enabled_client(monkeypatch, store)

    # English query finds the row and shows its English name.
    found = client.get("/catalog/search", params={"lang": "en", "material_name": "Door Lock"})
    assert found.status_code == 200
    assert "P1" in found.text
    assert "Left Front Door Lock" in found.text

    # Another row, matched case-insensitively and on a partial word.
    other = client.get("/catalog/search", params={"lang": "en", "material_name": "bumper"})
    assert other.status_code == 200
    assert "Rear Bumper Bracket" in other.text

    # An English name that renders from no source string says so, instead of
    # showing a bare "no matching parts" that reads like a data gap.
    absent = client.get(
        "/catalog/search", params={"lang": "en", "material_name": "Nonexistent Widget"}
    )
    assert absent.status_code == 200
    assert "Try the Chinese name or the part number" in absent.text

    # Chinese and part-number searches keep working unchanged in English mode.
    code = client.get("/catalog/search", params={"lang": "en", "material_code": "P1"})
    assert code.status_code == 200
    assert "P1" in code.text
    chinese = client.get("/catalog/search", params={"lang": "en", "material_name": "Part one"})
    assert chinese.status_code == 200
    assert "Part one" in chinese.text


def test_english_search_stays_off_without_an_overlay(monkeypatch, tmp_path: Path) -> None:
    """No overlay configured must not change search: source text only, as before."""
    store = make_release(tmp_path)
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(store.database_path))
    monkeypatch.delenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", raising=False)
    client = enabled_client(monkeypatch, store)

    response = client.get("/catalog/search", params={"lang": "en", "material_name": "Part one"})
    assert response.status_code == 200
    assert "P1" in response.text
    # the source string is shown, and no English-name claim is made
    assert "Part one" in response.text


def test_part_detail_localizes_source_detail_status(monkeypatch, tmp_path: Path) -> None:
    """The internal `source_detail_status` enum must not leak into the UI."""
    client = enabled_client(monkeypatch, make_release(tmp_path))
    series_token = main.release_url_token(HYE_SERIES)
    model_token = main.release_url_token(HYE_MODEL)
    child_key = main.release_node_url_key(HYE_CHILD)
    part_token = main.release_url_token("P1")
    url = f"/catalog/{series_token}/{model_token}/node/{child_key}/part/{part_token}"

    zh = client.get(url, params={"lang": "zh"})
    assert zh.status_code == 200
    assert '详情状态</dt><dd><span class="catalog-table-state">暂无详情' in zh.text
    assert 'catalog-table-state">unavailable' not in zh.text

    en = client.get(url, params={"lang": "en"})
    assert en.status_code == 200
    assert 'Detail status</dt><dd><span class="catalog-table-state">No details' in en.text
    assert 'catalog-table-state">unavailable' not in en.text


def test_readiness_reports_the_translation_snapshot_state(tmp_path, monkeypatch) -> None:
    """A release/glossary fingerprint mismatch must be observable from /health/ready."""
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)
    monkeypatch.delenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", raising=False)
    monkeypatch.delenv("LIMEAUTO_CATALOG_RELEASE_PATH", raising=False)

    payload = client.get("/health/ready").json()
    assert payload["status"] == "ok"
    assert payload["catalog"] == "enabled"
    assert payload["translation"] == "disabled"

    # A glossary built for a different snapshot: English is silently off, so say so.
    overlay = tmp_path / "translation.sqlite"
    connection = sqlite3.connect(overlay)
    connection.executescript(
        """
        CREATE TABLE translation_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE translation_terms (
            term_id TEXT NOT NULL, term_kind TEXT NOT NULL, source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL, lang TEXT NOT NULL, status TEXT NOT NULL,
            source_snapshot_fingerprint TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO translation_meta VALUES ('source_snapshot_fingerprint', ?)", ("9" * 64,)
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(store.database_path))

    payload = client.get("/health/ready").json()
    assert payload["translation"] == "mismatched"


def _entity_overlay(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    overlay = tmp_path / "entity-translation.sqlite"
    connection = sqlite3.connect(overlay)
    connection.executescript(
        """
        CREATE TABLE translation_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE translation_terms (
            term_id TEXT NOT NULL, term_kind TEXT NOT NULL, source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL, lang TEXT NOT NULL, status TEXT NOT NULL,
            source_snapshot_fingerprint TEXT NOT NULL,
            PRIMARY KEY (term_id, lang)
        );
        """
    )
    connection.execute(
        "INSERT INTO translation_meta VALUES ('source_snapshot_fingerprint', 'fingerprint')"
    )
    connection.executemany(
        "INSERT INTO translation_terms VALUES (?, ?, ?, ?, 'en', 'published', 'fingerprint')",
        [(stable_term_id(kind, source), kind, source, english) for kind, source, english in rows],
    )
    connection.commit()
    connection.close()
    return overlay


def test_search_reaches_series_by_name(monkeypatch, tmp_path: Path) -> None:
    """Typing a series name must lead to the series, in the default language."""
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)

    found = client.get("/catalog/search", params={"material_name": "HYE series"})
    assert found.status_code == 200
    assert "匹配的车系" in found.text
    assert f'href="/catalog/{main.release_url_token("HYE")}"' in found.text


def test_search_reaches_a_group_system_that_owns_no_parts(monkeypatch, tmp_path: Path) -> None:
    """A group node has no parts of its own, so a name match must still navigate.

    'Engine' is the group node here (child_count=1, direct_part_count=0). Before this
    slice the only way to reach it was to walk series -> model -> tree by hand.
    """
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)

    found = client.get("/catalog/search", params={"material_name": "Engine"})
    assert found.status_code == 200
    assert "匹配的系统" in found.text
    assert "Engine" in found.text
    expected = (
        f"/catalog/{main.release_url_token('HYE')}"
        f"/{main.release_url_token('HYEE-PZ02')}"
        f"/node/{main.release_url_token(HYE_ROOT)}"
    )
    assert f'href="{expected}"' in found.text


def test_search_reaches_model_and_system_by_their_english_names(monkeypatch, tmp_path: Path) -> None:
    """The parity requirement: an English operator names a model or a system, as in Chinese."""
    store = make_release(tmp_path)
    overlay = _entity_overlay(
        tmp_path,
        [
            ("model", "BYD Seal 08 EV", "Seal 08 EV"),
            ("node", "高压电池", "High Voltage Battery"),
            ("series", "HYE series", "Seal Series"),
        ],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(store.database_path))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    client = enabled_client(monkeypatch, store)

    model_hit = client.get("/catalog/search", params={"lang": "en", "material_name": "Seal 08"})
    assert model_hit.status_code == 200
    assert "Matching models" in model_hit.text
    assert f'href="/catalog/{main.release_url_token("HYE")}/{main.release_url_token("HYEE-PZ02")}"' in model_hit.text

    node_hit = client.get(
        "/catalog/search", params={"lang": "en", "material_name": "High Voltage Battery"}
    )
    assert node_hit.status_code == 200
    assert "Matching systems" in node_hit.text
    assert "高压电池" in node_hit.text or "High Voltage Battery" in node_hit.text

    series_hit = client.get("/catalog/search", params={"lang": "en", "material_name": "Seal Series"})
    assert series_hit.status_code == 200
    assert "Matching series" in series_hit.text
    assert f'href="/catalog/{main.release_url_token("HYE")}"' in series_hit.text


def test_english_and_chinese_reach_the_same_series(monkeypatch, tmp_path: Path) -> None:
    """Same target from both languages -- capability parity, asserted rather than assumed."""
    store = make_release(tmp_path)
    overlay = _entity_overlay(tmp_path, [("series", "HYE series", "Seal Series")])
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(store.database_path))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    client = enabled_client(monkeypatch, store)

    expected = f'href="/catalog/{main.release_url_token("HYE")}"'
    zh = client.get("/catalog/search", params={"material_name": "HYE series"})
    en = client.get("/catalog/search", params={"lang": "en", "material_name": "Seal Series"})
    assert expected in zh.text
    assert expected in en.text


def test_part_name_search_is_unchanged_by_the_named_groups(monkeypatch, tmp_path: Path) -> None:
    """A plain part search must still be a plain part search."""
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)

    found = client.get("/catalog/search", params={"material_name": "Part one"})
    assert found.status_code == 200
    assert "P1" in found.text
    assert "匹配的车系" not in found.text
    assert "匹配的车型" not in found.text
    assert "匹配的系统" not in found.text


def test_english_navigation_groups_show_english_labels(monkeypatch, tmp_path: Path) -> None:
    """The groups must be rendered in the operator's language, and name nothing in Chinese."""
    store = make_release(tmp_path)
    overlay = _entity_overlay(
        tmp_path,
        [
            ("series", "HYE series", "Seal Series"),
            ("model", "BYD Seal 08 EV", "Seal 08 EV"),
        ],
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(store.database_path))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    client = enabled_client(monkeypatch, store)

    page = client.get("/catalog/search", params={"lang": "en", "material_name": "Seal"})
    assert page.status_code == 200
    assert "Matching series" in page.text or "Matching models" in page.text
    # the source series name must not leak into the English page
    assert "HYE series" not in page.text


def test_english_no_match_notice_is_skipped_when_an_entity_matched(monkeypatch, tmp_path: Path) -> None:
    """A resolved series hit means the English name did reach the catalog."""
    store = make_release(tmp_path)
    overlay = _entity_overlay(tmp_path, [("series", "HYE series", "Seal Series")])
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(store.database_path))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    client = enabled_client(monkeypatch, store)

    page = client.get("/catalog/search", params={"lang": "en", "material_name": "Seal Series"})
    assert page.status_code == 200
    assert "No local material renders from that English name" not in page.text

    nothing = client.get("/catalog/search", params={"lang": "en", "material_name": "Zzz Nothing"})
    assert nothing.status_code == 200
    assert "No local material renders from that English name" in nothing.text


def test_single_cjk_character_series_name_still_navigates(monkeypatch, tmp_path: Path) -> None:
    """唐 is one character; "Tang" is four. The part search needs two, the name groups do not.

    Folding the two rules together would hide a whole car series from Chinese operators
    while the same search works in English.
    """
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)

    page = client.get("/catalog/search", params={"material_name": "高"})
    assert page.status_code == 200
    assert "匹配的系统" in page.text
    assert "高压电池" in page.text
    # the parts table is still refused for one character, but the hint would be misleading
    assert "有编码时请优先填编码" not in page.text


def test_single_latin_character_keeps_the_hint(monkeypatch, tmp_path: Path) -> None:
    """A lone Latin letter is not a word, so the two-character rule still applies."""
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)

    page = client.get("/catalog/search", params={"material_name": "P"})
    assert page.status_code == 200
    assert "匹配的系统" not in page.text
    assert "有编码时请优先填编码" in page.text


def test_single_character_name_with_no_match_keeps_the_hint(monkeypatch, tmp_path: Path) -> None:
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)

    page = client.get("/catalog/search", params={"material_name": "Z"})
    assert page.status_code == 200
    assert "匹配的车系" not in page.text
    assert "有编码时请优先填编码" in page.text


def test_empty_parts_list_is_hidden_when_a_name_matched_an_entity(monkeypatch, tmp_path: Path) -> None:
    """'0 items / no matching parts' must not headline a search that did find something."""
    store = make_release(tmp_path)
    client = enabled_client(monkeypatch, store)

    page = client.get("/catalog/search", params={"material_name": "Engine"})
    assert page.status_code == 200
    assert "匹配的系统" in page.text
    assert "没有匹配的物料" not in page.text

    # ...but a genuine no-hit search still says so, as before.
    none = client.get("/catalog/search", params={"material_code": "NOPE"})
    assert none.status_code == 200
    assert "没有匹配的物料" in none.text


# ------------------------------------- S1: a named system must not jump to another vehicle


def test_selected_model_scopes_the_system_link(monkeypatch, tmp_path: Path) -> None:
    """Picking a vehicle and then clicking a matched system must stay in that vehicle.

    The same system name exists in two models here. Resolving the name to a single
    representative node sent the operator to the other one, which is worse than no result:
    the page looked right and the destination was somebody else's vehicle.
    """
    store = make_release(tmp_path, with_shared_node_name=True)
    client = enabled_client(monkeypatch, store)

    scoped = client.get(
        "/catalog/search",
        params={"material_name": "Engine", "series_code": SA_SERIES, "model_code": SA_MODEL},
    )
    assert scoped.status_code == 200
    assert "匹配的系统" in scoped.text
    expected = (
        f"/catalog/{main.release_url_token(SA_SERIES)}"
        f"/{main.release_url_token(SA_MODEL)}"
        f"/node/{main.release_url_token(SA_SYSTEM_NODE)}"
    )
    other = (
        f"/catalog/{main.release_url_token(HYE_SERIES)}"
        f"/{main.release_url_token(HYE_MODEL)}"
        f"/node/{main.release_url_token(HYE_ROOT)}"
    )
    assert f'href="{expected}"' in scoped.text
    assert f'href="{other}"' not in scoped.text


def test_series_scope_alone_also_restricts_the_system_link(monkeypatch, tmp_path: Path) -> None:
    store = make_release(tmp_path, with_shared_node_name=True)
    client = enabled_client(monkeypatch, store)

    scoped = client.get("/catalog/search", params={"material_name": "Engine", "series_code": SA_SERIES})
    assert scoped.status_code == 200
    assert f"/{main.release_url_token(HYE_MODEL)}/node/" not in scoped.text


def test_unscoped_system_search_offers_every_occurrence(monkeypatch, tmp_path: Path) -> None:
    """Without a vehicle selected, all real occurrences are reachable, not just one."""
    store = make_release(tmp_path, with_shared_node_name=True)
    client = enabled_client(monkeypatch, store)

    page = client.get("/catalog/search", params={"material_name": "Engine"})
    assert page.status_code == 200
    assert page.text.count(">Engine</a>") == 2
    assert f"/node/{main.release_url_token(SA_SYSTEM_NODE)}" in page.text
    assert f"/node/{main.release_url_token(HYE_ROOT)}" in page.text


def test_scoped_system_search_with_no_occurrence_shows_no_link(monkeypatch, tmp_path: Path) -> None:
    """A name that does not exist in the selected vehicle must not borrow another's node."""
    store = make_release(tmp_path, with_shared_node_name=True)
    client = enabled_client(monkeypatch, store)

    page = client.get(
        "/catalog/search",
        params={"material_name": "Engine", "series_code": HYE_SERIES, "model_code": "NO-SUCH-MODEL"},
    )
    assert page.status_code == 200
    assert "匹配的系统" not in page.text


# ------------------------------------- S2: an English name whose stored source has padding


def test_english_search_finds_a_part_whose_stored_name_has_edge_whitespace(
    monkeypatch, tmp_path: Path
) -> None:
    """Display stripped the padding but search compared the raw column, so the row was
    findable in Chinese and invisible in English."""
    store = make_release(tmp_path, with_padded_part_name=True)
    overlay = _entity_overlay(
        tmp_path, [("part", PADDED_PART_SOURCE, "Padded Part English Name")]
    )
    monkeypatch.setenv("LIMEAUTO_CATALOG_RELEASE_PATH", str(store.database_path))
    monkeypatch.setenv("LIMEAUTO_CATALOG_TRANSLATION_PATH", str(overlay))
    client = enabled_client(monkeypatch, store)

    english = client.get("/catalog/search", params={"lang": "en", "material_name": "Padded Part English Name"})
    assert english.status_code == 200
    assert PADDED_PART in english.text

    # the Chinese path was never broken and must stay that way
    chinese = client.get("/catalog/search", params={"lang": "zh", "material_name": PADDED_PART_SOURCE})
    assert chinese.status_code == 200
    assert PADDED_PART in chinese.text

    # both reach the same single fitment
    assert english.text.count(f">{PADDED_PART}<") == chinese.text.count(f">{PADDED_PART}<")
