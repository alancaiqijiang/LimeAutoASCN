"""Contracts for the terminology ledger aligner.

Each test here locks a bug that produced **plausible-looking corpus defects which were
arithmetic** -- the ledger reported them as translation problems before they were caught:

* substring removal of a source piece turned `Bolt Fixed` into `olt Fixed`;
* hyphen splitting turned `Co-Pilot Seat Assembly` into `Co`;
* a finish cut at the wrong hyphen turned `Self-Made Part` into `Self`.

If one of these regresses the ledger silently starts inventing defects again, so they are
pinned by test rather than by comment.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.build_terminology_ledger import (  # noqa: E402
    align_row,
    cut_finish,
    decide,
    en_pieces,
    is_finish,
    reads_as_finish,
    remove_verbatim,
)


def test_verbatim_removal_is_word_boundary_anchored():
    """A one-letter source piece must not eat the first letter of an English word."""
    assert remove_verbatim("Bolt Fixed", ["B"]) == "Bolt Fixed"
    assert remove_verbatim("Distribution Box", ["D"]) == "Distribution Box"
    # ... while a standalone piece is still removed.
    assert remove_verbatim("B Bolt Fixed", ["B"]) == "Bolt Fixed"
    assert remove_verbatim("Fuse_ANS-S_32V", ["ANS-S", "32V"]) == "Fuse"


def test_verbatim_removal_keeps_intra_word_hyphens():
    assert remove_verbatim("HA3HA-5101451Q/70 Left Rear", ["HA3HA-5101451Q/70"]) == "Left Rear"
    assert remove_verbatim("Self-Made Part", ["M00666"]) == "Self-Made Part"


def test_english_pieces_do_not_split_inside_a_word():
    assert en_pieces("Co-Pilot Seat Assembly - Gray White 2") == ["Co-Pilot Seat Assembly", "Gray White 2"]
    assert en_pieces("Self-Made Part") == ["Self-Made Part"]
    assert en_pieces("Fuse_ANS-S") == ["Fuse", "ANS-S"]


def test_finish_cut_never_lands_inside_a_word():
    assert cut_finish("Assembly - E-Coat", "电泳", 1, []) == ("Assembly", "E-Coat")
    assert cut_finish("Co-Pilot Seat Assembly - Gray White 2", "灰白2", 1, []) == (
        "Co-Pilot Seat Assembly", "Gray White 2")
    # A name that merely contains a hyphenated word is not cut: nothing reads as a finish.
    assert cut_finish("Self-Made Part", "深黑5", 1, []) == ("", "")


def test_finish_cut_tolerates_identifiers_in_front_of_the_name():
    """`防护板_VE8_280VK_黑色` is one name piece plus a finish, not three pieces."""
    assert cut_finish("Protective Plate_VE8_280VK_Black", "黑色", 1, ["VE8", "280VK"]) == (
        "Protective Plate_VE8_280VK", "Black")


def test_align_row_route_a_with_finish():
    result = align_row("副驾驶座椅总成-灰白2", "Co-Pilot Seat Assembly - Gray White 2")
    assert result["route"] == "A"
    assert result["chunks"] == [("副驾驶座椅总成", "Co-Pilot Seat Assembly")]
    assert result["finish"] == ("灰白2", "Gray White 2")


def test_align_row_refuses_a_piece_count_mismatch():
    """Unreadable alignment must be reported, never guessed."""
    result = align_row("副仪表板本体总成", "Sub Instrument Panel Body Assembly Extra Words Here")
    assert result["route"] != "piece_count_mismatch"
    mismatch = align_row("左前门_右后门", "Left Front Door")
    assert mismatch["route"] is None


def test_is_finish_rejects_material_nouns():
    assert is_finish("深黑5") is True
    assert is_finish("金属卡扣") is False


def test_decide_requires_a_dominant_majority_and_flags_pinyin():
    split = decide(Counter({"Twilight Gray": 83, "Dusk Cloud Gray": 33}), 157)
    assert split["decision"] == "pending_review"
    assert "no_dominant_majority" in split["flags"]

    pinyin = decide(Counter({"Yuanshan Dai": 150, "Distant Mountain Dark Green": 3}), 158)
    assert pinyin["decision"] == "pending_naming_policy"

    stable = decide(Counter({"Deep Black 5": 1113, "Dark Black 5": 26}), 1144)
    assert stable["decision"] == "unify"
    assert stable["chosen_en"] == "Deep Black 5"


def test_finish_cut_never_swallows_a_source_spec():
    """`... Assembly_M5×16_Black` is name + spec + finish, not a finish called `M5×16_Black`.

    Unifying the finish on that reading deleted the `M5×16` spec from the row and tripped
    the identifier gate -- caught by the audit, fixed here.
    """
    # The corpus shape that broke: the spec sits between the name and the finish with no
    # separator of its own, so the tail looked like a finish phrase.
    assert cut_finish("Cross Recessed Pan Head Screw with Flat Washer Assembly - M5×16 Black",
                      "黑色", 1, ["M5×16"]) == ("", "")
    # When the spec has its own separator the cut keeps it in the name part.
    assert cut_finish("Cross Recessed Pan Head Screw Assembly_M5×16_Black",
                      "黑色", 1, ["M5×16"]) == ("Cross Recessed Pan Head Screw Assembly_M5×16", "Black")


def test_decide_honours_a_naming_policy_over_the_corpus_majority():
    """A business decision outranks the vote, and says so in the record."""
    votes = Counter({"Yuanshan Dai": 150, "Distant Mountain Green": 2})
    without = decide(votes, 158)
    assert without["decision"] == "pending_naming_policy"
    assert without["chosen_en"] is None

    with_policy = decide(votes, 158, {"chosen_en": "Distant Mountain Green", "reason": "readable in English"})
    assert with_policy["decision"] == "unify"
    assert with_policy["chosen_en"] == "Distant Mountain Green"
    assert "policy_override" in with_policy["flags"]
    assert with_policy["renderings"] == {"Yuanshan Dai": 150, "Distant Mountain Green": 2}


def test_finish_detection_is_word_based_not_substring():
    """`tan` lives inside `Distant`, which made a whole colour name look like a finish."""
    assert reads_as_finish("Distant Mountain Dai") is False
    assert reads_as_finish("Sub-Instrument Panel Rear Air Vent Panel Assembly") is False
    assert reads_as_finish("Deep Black 5") is True
    assert reads_as_finish("E-Coat Black Paint") is True
    assert reads_as_finish("Run Mi") is True


def test_finish_word_detection_handles_hyphenated_phrases():
    """`assembly-beige` must still count as carrying a finish word."""
    from tools.audit_finish_consistency import has_finish_word
    assert has_finish_word("B-Pillar Safety Handle Assembly-Beige 10") is True
    assert has_finish_word("...-Off-white 1") is True
    assert has_finish_word("Distant Mountain Dai") is False


def test_chunk_rewrite_is_refused_only_when_it_would_drop_a_finish_word():
    """Keep the guard narrow: a colour-preserving rename is safe, a colour-dropping one is not."""
    from tools.unify_translation_units import _finish_words_subset
    assert _finish_words_subset("Qianshan Emerald", "Thousand Mountains Emerald") is True
    assert _finish_words_subset("Assembly - Matte Titanium Silver", "Assembly") is False


def test_separator_normalisation_keeps_a_hyphenated_finish_intact():
    """`Off-White 1` must not be cut into `Off - White 1`."""
    from tools.unify_translation_units import _locate_finish
    ledger = {"finishes": {"米白1": {"rows": 154, "renderings": {"Off-White 1": 83, "Ivory White 1": 23},
                                    "chosen_en": "Off-White 1", "decision": "unify", "flags": []}}}
    text = "B-Pillar Safety Handle-Off-White 1"
    assert _locate_finish(ledger, "米白1", "White 1", text) == "Off-White 1"


def test_separator_normalisation_also_repairs_a_whitespace_only_join():
    """A finish glued on with a bare space is a defect, not a licence to skip.

    The corpus writes ` - ` before a finish on 636 rows and a bare space on 16, so
    the space-only form must be rewritten.  Skipping it left 17 rows permanently
    non-conformant (`... VDEAU-2915812 20# Black`, `Vehicle Fragrance Woven Brown`).
    """
    from tools.unify_translation_units import _separator_repair_candidate

    # space-only join -> repairable
    assert _separator_repair_candidate("Forging Hanger Combination Black", "Black") == \
        "Forging Hanger Combination - Black"
    # already spaced -> nothing to do
    assert _separator_repair_candidate("Forging Hanger Combination - Black", "Black") is None
    # glued to a part number / no whitespace boundary -> must be left alone
    assert _separator_repair_candidate("Vehicle FragranceWoven Brown", "Woven Brown") is None
    assert _separator_repair_candidate("O-Ring Seal 209×64.5×3.55 EPDM Black", "Black") is not None
