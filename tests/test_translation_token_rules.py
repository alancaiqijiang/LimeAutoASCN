"""Behaviour lock for the LimeAuto translation gate, integrity auditor and repair tool.

The gate's contract (user's words): 审核目的是核对中英译意涵一致性 -- the review exists to
keep the *meaning* equal. Real identifiers must survive; descriptive values (displacement,
power, dimensions, position/trim indices) must be free to take normal English typography.
These tests pin both sides so a future rule change cannot silently stop protecting an
identifier or start rejecting correct English.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.audit_translation_integrity import (  # noqa: E402
    audit_row,
    digit_sequences_preserved,
    effective_text,
    foreign_codes,
    load_review_overrides,
    unexpected_finish_term,
)
from tools.repair_translation_defects import (  # noqa: E402
    _replace_fragments,
    code_restore,
    glossary_repair,
    strip_unexpected_finish,
)
from tools.translation_token_rules import (  # noqa: E402
    compositional_powertrains,
    ordered_groups_present,
    protected_tokens,
    skeleton,
    token_requirements,
    translation_token_errors,
    year_context_tokens,
)

GLOSSARY_PATH = REPO / "translation/glossary/color-finish-en.json"
MORPHEMES = "色米棕橙绿蓝灰黑白红黄金银赭紫粉褐青彩陶"


# --------------------------------------------------------------------------- gate
def test_skeleton_keeps_decimals_and_folds_case():
    assert skeleton("BYD6420S5D (Premium)") == "byd6420s5dpremium"
    assert skeleton("D12.42×1.78") == "d12.421.78"


def test_ordered_groups_ignore_spacing_case_and_hyphens():
    assert ordered_groups_present("BYD6420S5D", "BYD6420S5D (1.5L DCT Premium)")
    assert ordered_groups_present("PLUS5G", "PLUS 5G")
    assert ordered_groups_present("Y55KM", "Y 55 km")
    assert ordered_groups_present("dilink3.0", "DiLink3.0")


def test_ordered_groups_fail_on_dropped_or_reordered_group():
    assert not ordered_groups_present("BYD6420S5D", "BYD6420S5 Premium")
    assert not ordered_groups_present("471Q-3707801", "3707801-471Q")


def test_strict_numeric_tail_allows_century_but_not_a_wrong_year():
    assert ordered_groups_present("19", "Y - 2019", strict_numeric_tail=True)
    assert not ordered_groups_present("19", "Y - 1990", strict_numeric_tail=True)


def test_year_context_tokens_uses_the_marker():
    # the marker sits after the whole run `400km-19`, because a hyphen stays inside a run
    assert year_context_tokens("BYD7003BEV3(尊贵型-带备胎罩（400km-19款）)") == {"400km-19"}
    assert year_context_tokens("BYD6420S5D(1.5LDCT)") == set()


def test_compositional_powertrain_is_split_not_treated_as_one_code():
    spans = compositional_powertrains("BYD6420S5D(1.5LDCT 豪华型)")
    assert [(d, t) for d, t, _ in spans] == [("1.5L", "DCT")]
    literals, specs = token_requirements("BYD6420S5D(1.5LDCT 豪华型)")
    assert "BYD6420S5D" in literals
    assert "DCT" in literals
    assert "1.5LDCT" not in literals
    assert any(label.endswith("displacement") for label, _ in specs)


def test_power_spec_is_a_spec_not_an_identifier():
    literals, specs = token_requirements("BYD6470MBEV2(尊享型Y（70kw+右舵版）)")
    assert "BYD6470MBEV2" in literals
    assert "70kw" not in literals
    assert any(label == "70kw" for label, _ in specs)


def test_part_number_fragment_is_not_mistaken_for_horsepower():
    # 61HP/71HP only ever appear inside part numbers (HA2HPA-10型, ...T1F71HP1.25)
    literals, specs = token_requirements("HA2HPA-10型")
    assert "HA2HPA-10" in literals
    assert not any("HP" in label for label, _ in specs)


def test_dimension_pairs_are_specs():
    _, specs = token_requirements("BYDQ727Y2035_20X35腰形橡胶堵盖")
    assert any(label == "20X35" for label, _ in specs)


def test_digit_only_run_keeps_its_value():
    assert "6" in protected_tokens("右前门护板总成-灰黑6_M00666")


def test_alphabetic_abbreviation_is_literal_but_single_letter_is_not():
    assert "PP" in protected_tokens("普通注塑_SC3HBF-8100170_主驾吹脚风道总成_黑色_PP")
    assert "A" not in protected_tokens("BYDQ675110_A型蜗轮蜗杆卡箍")


def test_gate_accepts_correct_english_typography():
    source = "BYD6420S5D(1.5LDCT尊贵型)"
    assert translation_token_errors(source, "BYD6420S5D (1.5L DCT Premium)") == []


def test_gate_still_blocks_a_dropped_transmission():
    source = "BYD6420S5D(1.5LDCT尊贵型)"
    assert translation_token_errors(source, "BYD6420S5D (1.5L Premium)")


def test_gate_accepts_unit_re_spacing_and_blocks_a_changed_value():
    source = "BYD6470MBEV2(尊享型Y（70kw+右舵版）)"
    assert translation_token_errors(source, "BYD6470MBEV2 (Premium Y, 70 kW, RHD)") == []
    assert translation_token_errors(source, "BYD6470MBEV2 (Premium Y, 0.7 kW, RHD)")


def test_gate_accepts_dimension_restyle_and_blocks_lost_dimension():
    source = "BYDQ727Y2035_20X35腰形橡胶堵盖_M00666"
    assert translation_token_errors(source, "BYDQ727Y2035 20 × 35 Oblong Rubber Plug M00666") == []
    assert translation_token_errors(source, "BYDQ727Y2035 Oblong Rubber Plug M00666")


def test_spec_check_survives_an_underscore_separator():
    # a trailing \b would fail here because `_` is a word character
    assert translation_token_errors("铜排_10mm_Carbon", "Busbar 10mm_Carbon") == []


def test_material_designation_alias_is_accepted():
    source = "模压橡胶件_TZ180XYD-2105134A-D1_电机密封圈_FPM氟橡胶70"
    assert translation_token_errors(
        source, "Molded Rubber Part_TZ180XYD-2105134A-D1_Motor Seal_FKM Fluororubber 70"
    ) == []


def test_zero_leading_position_number_must_survive():
    source = "0型圈-D12.42×1.78"
    assert translation_token_errors(source, "O-Ring - D12.42×1.78")
    assert translation_token_errors(source, "O-Ring (0-Type) - D12.42×1.78") == []


def test_year_is_checked_tightly_inside_a_real_source():
    source = "BYD7003BEV3(尊贵型-带备胎罩（400km-19款）)"
    assert translation_token_errors(
        source, "BYD7003BEV3 (Prestige Edition - With Spare Tire Cover (400km-19))"
    ) == []
    assert translation_token_errors(
        source, "BYD7003BEV3 (Prestige Edition - With Spare Tire Cover (400km-19)) "
                "BYD7003BEV3 400km 1990"
    ) == []


# ------------------------------------------------------------------ integrity audit
def test_digit_loss_is_detected_on_a_shifted_row():
    source = "HYEA-3658300_前碰传感器Ⅰ"
    assert digit_sequences_preserved(source, "HYEA-3658500 Side Impact Sensor"), "lost digits must be reported"
    assert digit_sequences_preserved(source, "HYEA-3658300 Front Collision Sensor I") == []


def test_foreign_code_detects_the_shift_signature():
    source = "HYEA-3658300_前碰传感器Ⅰ"
    assert foreign_codes(source, "HYEA-3658500 Side Impact Sensor") == ["HYEA-3658500"]
    assert foreign_codes(source, "HYEA-3658300 Front Collision Sensor I") == []


def test_foreign_code_does_not_fire_on_legitimate_translation():
    assert foreign_codes("XX_2.0排量", "XX 2.0L") == []
    assert foreign_codes("XX_七座", "XX 7-Seat") == []
    assert foreign_codes("XX_座椅", "XX Seat Assembly-2011") == []


def test_unexpected_finish_term_reports_a_colour_the_source_cannot_carry():
    assert unexpected_finish_term("ISOFIX钢丝罩盖", "ISOFIX Wire Cover - Oat Beige", {"燕麦米"}, {"Oat Beige"}) == "Oat Beige"
    # a source that does carry a colour tail is never reported
    assert unexpected_finish_term("ISOFIX罩盖-米黄色", "ISOFIX Cover - Beige White", set(), {"Oat Beige"}) is None


def test_audit_row_flags_empty_residual_and_clean_rows():
    assert audit_row({"source_text": "鼓风机"}, {"translated_text": ""}) == {"empty_translation": True}
    residual = audit_row({"source_text": "主驾座垫护面及发泡总成-米陶色"},
                         {"translated_text": "Driver Seat Cushion Cover & Foam Assembly -米陶色"})
    assert residual and residual.get("residual_cjk")
    assert audit_row({"source_text": "鼓风机"}, {"translated_text": "Blower"}) is None


def test_audit_row_catches_a_mistyped_code_letter():
    findings = audit_row(
        {"source_text": "普通冲压件_EKEA-6104110_左前门升降器支架总成_组合件"},
        {"translated_text": "Stamped Part EQEA-6104110 Left Front Door Regulator Bracket Assembly"},
    )
    assert findings and ("gate_errors" in findings or "damaged_codes" in findings)


def test_effective_text_reads_the_reviewed_text_not_the_wrong_key(tmp_path):
    """The override map nests the reviewed text under `text`.

    Reading `reviewed_translation` off that map yields nothing and silently falls back to the AI
    draft -- three analysis scripts measured the draft while believing they measured the repaired
    corpus (finish flags were inflated by 119 rows).  This test pins the field contract.
    """
    output = tmp_path / "output"
    output.mkdir()
    (output / "batch-0001.jsonl").write_text(
        json.dumps({"term_id": "part:aaa", "decision": "revise", "reviewer": "x",
                    "reviewed_translation": "Corrected Text"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    overrides = load_review_overrides([output])
    assert overrides["part:aaa"]["text"] == "Corrected Text"
    assert "reviewed_translation" not in overrides["part:aaa"]
    assert effective_text(overrides, "part:aaa", "Draft Text") == "Corrected Text"
    assert effective_text(overrides, "part:missing", "Draft Text") == "Draft Text"


# --------------------------------------------------------------------------- repair
def test_code_restore_rewrites_the_source_spelling():
    # production passes the *source* code; the tool finds the mistyped token in the translation
    source = "普通冲压件_EKEA-6104110_左前门升降器支架总成_组合件"
    repaired = code_restore(source, "Stamped Part EQEA-6104110 Left Front Door Bracket", ["EKEA-6104110"])
    assert repaired is not None and "EKEA-6104110" in repaired and "EQEA-6104110" not in repaired


def test_glossary_repair_replaces_a_chinese_finish_tail():
    glossary = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    assert "远山黛" in glossary["terms"]
    repaired = glossary_repair("仪表板上本体总成-远山黛", "Instrument Panel Upper Body Assembly - 远山黛", glossary)
    assert repaired == "Instrument Panel Upper Body Assembly - Yuanshan Dai"


def test_strip_unexpected_finish_removes_a_foreign_colour_tail():
    stripped = strip_unexpected_finish(
        "ISOFIX钢丝罩盖", "ISOFIX Wire Cover - Oat Beige", {"燕麦米"}, {"Oat Beige"}, MORPHEMES
    )
    assert stripped == "ISOFIX Wire Cover"
    # a source that legitimately carries the finish is left alone
    assert strip_unexpected_finish("ISOFIX罩盖-米黄色", "ISOFIX Cover - Oat Beige", set(), {"Oat Beige"}, MORPHEMES) is None


def test_fragment_replacement_inserts_the_space_english_needs():
    assert _replace_fragments("3rd Row Seat Front扣手 Screw Plug", {"扣手": "Pull Handle"}) == (
        "3rd Row Seat Front Pull Handle Screw Plug"
    )
    assert _replace_fragments("Blower", {"扣手": "Pull Handle"}) is None


@pytest.mark.parametrize(
    "source,translated",
    [
        ("BYDQ832B0515_叶子片卡扣_黑色", "BYDQ832B0515 Trim Clip Black"),
        ("熔断器_旋入式_3120-0001_MIDI_150A", "Fuse_Screw-in Type_3120-0001_MIDI_150A"),
        ("垫圈-72mm", "Washer - 72mm"),
        ("DS3-3707110_组合仪表_M00000", "DS3-3707110_Instrument Cluster_M00000"),
    ],
)
def test_gate_leaves_already_correct_rows_alone(source, translated):
    assert translation_token_errors(source, translated) == []
