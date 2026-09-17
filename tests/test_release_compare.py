from __future__ import annotations

import pytest

from tools.compare_release_to_qpren import (
    classify_count,
    filter_material_code_rows,
    parse_model_selector,
)


def test_parse_model_selector_prefers_double_colon_for_reserved_slashes() -> None:
    assert parse_model_selector("SA2HG/K::EV/中文%") == ("SA2HG/K", "EV/中文%")


def test_parse_model_selector_accepts_last_slash_for_simple_codes() -> None:
    assert parse_model_selector("HYE/HYEE-PZ02") == ("HYE", "HYEE-PZ02")


def test_filter_material_code_rows_is_exact_and_handles_reserved_characters() -> None:
    rows = [
        {"material_code": "SA2HG/K", "row": "slash"},
        {"material_code": "EV/中文%", "row": "unicode-percent"},
        {"material_code": "EV/中文", "row": "near-match"},
        {"material_code": "other", "row": "out-of-scope"},
        {"material_code": None, "row": "unbound"},
    ]

    selected = list(filter_material_code_rows(rows, {"SA2HG/K", "EV/中文%"}))

    assert [row["row"] for row in selected] == ["slash", "unicode-percent"]


@pytest.mark.parametrize("value", ["", "HYE", "::MODEL", "SERIES::"])
def test_parse_model_selector_rejects_ambiguous_or_empty_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_model_selector(value)


def test_classify_count_marks_expected_projection_and_blocking_mismatch() -> None:
    assert classify_count("nodes", 3, 4)["classification"] == "blocking_mismatch"
    assert (
        classify_count("assets", 3, 4, expected=False)["classification"]
        == "expected_projection"
    )
    assert classify_count("parts", 4, 4)["classification"] == "exact_projection"
