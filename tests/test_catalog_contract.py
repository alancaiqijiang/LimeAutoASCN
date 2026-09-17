from __future__ import annotations

import json

from app.catalog_contract import build_catalog_report, make_path_key, parse_quantity


def _row(
    node_path: str,
    obj_code: str,
    direct_part_count: int | str = 0,
    *,
    series_code: str = "S",
    model_code: str = "M",
) -> dict[str, object]:
    return {
        "series_code": series_code,
        "model_code": model_code,
        "obj_code": obj_code,
        "node_path": node_path,
        "direct_part_count": direct_part_count,
    }


def test_one_level_path_is_retained_as_a_source_node() -> None:
    report = build_catalog_report([_row("Engine", "N-1", 2)])

    assert report["blocking_errors"] == []
    assert report["nodes"] == [
        {
            "series_code": "S",
            "model_code": "M",
            "obj_code": "N-1",
            "node_key": "source:S:M:Engine:N-1",
            "is_derived": False,
            "tree_key": None,
            "node_path": "Engine",
            "path_key": "Engine",
            "parent_key": None,
            "depth": 1,
            "display_name": "Engine",
            "child_count": 0,
            "direct_part_count": 2,
            "descendant_part_count": 0,
            "path_variant_count": 1,
        }
    ]
    assert report["summary"]["source_node_count"] == 1
    assert report["summary"]["node_count"] == 1


def test_multi_level_path_adds_each_missing_derived_parent() -> None:
    report = build_catalog_report(
        [
            _row("Engine > Block > Bolt", "N-1", 2),
            _row("Engine > Block > Nut", "N-2", 3),
        ]
    )
    nodes = {node["node_path"]: node for node in report["nodes"]}

    assert report["summary"]["derived_parent_count"] == 2
    assert nodes["Engine"]["is_derived"] is True
    assert nodes["Engine"]["obj_code"] is None
    assert nodes["Engine"]["node_key"] == "derived:S:M:Engine"
    assert nodes["Engine>Block"]["is_derived"] is True
    assert nodes["Engine>Block"]["node_key"] == "derived:S:M:Engine%3EBlock"
    assert nodes["Engine>Block"]["child_count"] == 2
    assert nodes["Engine"]["descendant_part_count"] == 5


def test_parent_with_zero_direct_parts_is_retained_when_it_has_descendants() -> None:
    report = build_catalog_report(
        [
            _row("Engine > Block", "PARENT", 0),
            _row("Engine > Block > Bolt", "CHILD", 4),
        ]
    )
    parent = next(node for node in report["nodes"] if node["node_path"] == "Engine>Block")

    assert parent["obj_code"] == "PARENT"
    assert parent["direct_part_count"] == 0
    assert parent["descendant_part_count"] == 4
    assert report["summary"]["zero_direct_part_retained_count"] >= 1


def test_report_output_is_deterministic_for_different_input_order() -> None:
    rows = [
        _row("Zeta > Leaf", "Z-1", 1),
        _row("Alpha", "A-1", 2),
        _row("Zeta > Branch > Leaf", "Z-2", 3),
    ]

    assert build_catalog_report(rows) == build_catalog_report(reversed(rows))


def test_path_keys_are_collision_safe_per_segment() -> None:
    assert make_path_key(("A>B",)) != make_path_key(("A", "B"))
    assert make_path_key(("A%B",)) != make_path_key(("A", "B"))
    assert "%3E" in make_path_key(("A>B",))
    assert "%25" in make_path_key(("A%B",))


def test_same_model_and_path_with_conflicting_obj_codes_is_non_blocking() -> None:
    report = build_catalog_report(
        [
            _row("Engine > Block", "OBJ-1", 2),
            _row("Engine > Block", "OBJ-2", 2),
        ]
    )

    conflict = {
        "kind": "path_conflict",
        "message": "node path maps to more than one source obj_code",
        "series_code": "S",
        "model_code": "M",
        "path_key": "Engine>Block",
        "node_path": "Engine>Block",
        "obj_codes": ["OBJ-1", "OBJ-2"],
    }

    assert report["summary"]["blocking_error_count"] == 0
    assert report["summary"]["path_conflict_count"] == 1
    assert report["blocking_errors"] == []
    assert report["structural_conflicts"] == [conflict]
    assert report["warnings"] == [conflict]

    variants = [
        node for node in report["nodes"] if node["path_key"] == "Engine>Block"
    ]
    assert {node["obj_code"] for node in variants} == {"OBJ-1", "OBJ-2"}
    assert len({node["node_key"] for node in variants}) == 2
    assert all(node["node_key"].startswith("source:") for node in variants)
    assert all(node["path_variant_count"] == 2 for node in variants)
    assert report["summary"]["source_node_count"] == 2
    assert report["summary"]["derived_parent_count"] == 1
    assert report["summary"]["node_count"] == 3


def test_node_key_encoding_keeps_source_and_derived_namespaces_distinct() -> None:
    report = build_catalog_report(
        [
            _row("A>B > Leaf", "OBJ:1"),
            _row("A > B > Branch", "OBJ%1"),
        ]
    )

    node_keys = {node["node_key"] for node in report["nodes"]}
    assert len(node_keys) == report["summary"]["node_count"]
    assert any(key.startswith("source:") for key in node_keys)
    assert any(key.startswith("derived:") for key in node_keys)
    assert len(report["structural_conflicts"]) == 0


def test_path_conflict_record_keeps_all_conflicting_obj_codes() -> None:
    report = build_catalog_report(
        [
            _row("Engine > Block", "OBJ-2"),
            _row("Engine > Block", "OBJ-1"),
        ]
    )

    assert report["structural_conflicts"] == [
        {
            "kind": "path_conflict",
            "message": "node path maps to more than one source obj_code",
            "series_code": "S",
            "model_code": "M",
            "path_key": "Engine>Block",
            "node_path": "Engine>Block",
            "obj_codes": ["OBJ-1", "OBJ-2"],
        }
    ]


def test_empty_and_malformed_rows_return_structured_errors_and_warnings() -> None:
    report = build_catalog_report(
        [
            _row(" > ", "EMPTY"),
            _row("A", "MISSING-SCOPE", series_code="", model_code="M"),
            _row("A", "", direct_part_count=1),
            _row("A", "BAD-COUNT", direct_part_count="not-an-integer"),
        ]
    )

    error_kinds = {error["kind"] for error in report["blocking_errors"]}
    assert {"empty_node_path", "missing_model_scope", "missing_obj_code"} <= error_kinds
    assert all({"kind", "message"} <= set(error) for error in report["blocking_errors"])
    assert report["warnings"][0]["kind"] == "invalid_direct_part_count"
    json.dumps(report["blocking_errors"])


def test_parse_quantity_preserves_non_integer_raw_values() -> None:
    assert parse_quantity("4") == {"quantity": 4, "quantity_raw": "4"}
    assert parse_quantity("1.5") == {"quantity": None, "quantity_raw": "1.5"}
    assert parse_quantity("2-4 pcs") == {"quantity": None, "quantity_raw": "2-4 pcs"}
    assert parse_quantity(None) == {"quantity": None, "quantity_raw": None}
