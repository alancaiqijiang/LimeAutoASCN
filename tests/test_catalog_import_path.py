"""Import-path contract: a material-library update must not be able to lose English.

Covers the two offline pieces added for the import workflow:
  * build_catalog_translation_runtime.py --allow-growth  (accepts growth, refuses loss)
  * report_catalog_translation_delta.py                  (the carry-forward gate)
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.catalog_translation import stable_term_id  # noqa: E402

FROZEN_ROWS = [
    ("series", "唐", "Tang"),
    ("model", "唐80尊贵型", "Tang 80 Premium"),
    ("node", "前保险杠", "Front Bumper"),
    ("part", "前保险杠上本体", "Front Bumper Upper Body"),
    ("part", "后视镜总成", "Rearview Mirror Assembly"),
]
FROZEN_KINDS = {"series": 1, "model": 1, "node": 1, "part": 2}


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "runtime_builder", REPO / "tools/build_catalog_translation_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_delta_tool():
    spec = importlib.util.spec_from_file_location(
        "delta_tool", REPO / "tools/report_catalog_translation_delta.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows_for(extra: list[tuple[str, str, str]] = (), drop: list[str] = ()):
    rows = [
        {"term_id": stable_term_id(k, s), "term_kind": k, "source_text": s, "english": e}
        for k, s, e in FROZEN_ROWS
    ]
    for k, s, e in extra:
        rows.append({"term_id": stable_term_id(k, s), "term_kind": k, "source_text": s, "english": e})
    if drop:
        rows = [r for r in rows if r["term_id"] not in drop]
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- builder


def test_frozen_mode_is_still_exact():
    module = load_builder()
    problems = module.verify_rows(rows_for(), expected_rows=5, expected_kinds=FROZEN_KINDS)
    assert problems == []


def test_growth_mode_accepts_a_new_material():
    module = load_builder()
    grown = rows_for([("part", "新增转向助力泵总成", "New Power Steering Pump Assembly")])
    problems = module.verify_rows(
        grown, expected_rows=5, expected_kinds=FROZEN_KINDS, allow_growth=True
    )
    assert problems == [], problems


def test_growth_mode_refuses_a_lost_term():
    """The whole point: an import may add, never remove."""
    module = load_builder()
    shrunk = rows_for(drop=[stable_term_id("part", "后视镜总成")])
    problems = module.verify_rows(
        shrunk, expected_rows=5, expected_kinds=FROZEN_KINDS, allow_growth=True
    )
    assert any("shrank" in p for p in problems), problems


def test_growth_mode_refuses_an_unknown_kind():
    module = load_builder()
    rows = rows_for([("bogus", "x", "X")])
    problems = module.verify_rows(
        rows, expected_rows=5, expected_kinds=FROZEN_KINDS, allow_growth=True
    )
    assert any("unknown term kinds" in p for p in problems), problems


def test_growth_mode_refuses_below_baseline_row_count():
    module = load_builder()
    problems = module.verify_rows(
        rows_for(), expected_rows=99, expected_kinds=FROZEN_KINDS, allow_growth=True
    )
    assert any("growth must not lose rows" in p for p in problems), problems


def test_frozen_mode_still_rejects_growth():
    module = load_builder()
    grown = rows_for([("part", "新增转向助力泵总成", "New Power Steering Pump Assembly")])
    problems = module.verify_rows(grown, expected_rows=5, expected_kinds=FROZEN_KINDS)
    assert any("row count" in p for p in problems), problems


def test_empty_english_is_still_refused_under_growth():
    module = load_builder()
    rows = rows_for([("part", "新增转向助力泵总成", "   ")])
    problems = module.verify_rows(
        rows, expected_rows=5, expected_kinds=FROZEN_KINDS, allow_growth=True
    )
    assert any("empty english" in p for p in problems), problems


def test_duplicate_term_ids_are_still_refused_under_growth():
    module = load_builder()
    rows = rows_for()
    rows.append(dict(rows[0]))
    problems = module.verify_rows(
        rows, expected_rows=5, expected_kinds=FROZEN_KINDS, allow_growth=True
    )
    assert any("duplicate term_ids" in p for p in problems), problems


# ------------------------------------------------- baseline term-set invariant


def test_same_kind_swap_is_invisible_to_counts_but_caught_by_the_baseline_check():
    """Lose one part, gain another: row count and kind counts both stay equal."""
    module = load_builder()
    dropped = stable_term_id("part", "后视镜总成")
    swapped = rows_for(
        extra=[("part", "新增转向助力泵总成", "New Power Steering Pump Assembly")],
        drop=[dropped],
    )
    # The count-based checks cannot see this...
    assert module.verify_rows(
        swapped, expected_rows=5, expected_kinds=FROZEN_KINDS, allow_growth=True
    ) == []
    # ...so the ID-set comparison is what actually protects the import.
    problems = module.verify_baseline_survives(swapped, rows_for())
    assert len(problems) == 1
    assert "lost 1 baseline terms" in problems[0]
    assert "后视镜总成" in problems[0]


def test_baseline_check_passes_for_a_pure_addition():
    module = load_builder()
    grown = rows_for([("part", "新增转向助力泵总成", "New Power Steering Pump Assembly")])
    assert module.verify_baseline_survives(grown, rows_for()) == []


def test_baseline_check_ignores_the_order_of_rows():
    module = load_builder()
    shuffled = list(reversed(rows_for()))
    assert module.verify_baseline_survives(shuffled, rows_for()) == []


def test_builder_cli_refuses_when_baseline_equals_input(tmp_path: Path):
    """A vacuous baseline must fail loudly rather than silently skip the check."""
    module = load_builder()
    snapshot = write_jsonl(tmp_path / "snap.jsonl", rows_for())
    argv = sys.argv
    sys.argv = [
        "build_catalog_translation_runtime.py",
        "--jsonl", str(snapshot), "--check",
        "--allow-growth", "--baseline-rows", "5",
        "--baseline-jsonl", str(snapshot),
    ]
    try:
        rc = module.main()
    finally:
        sys.argv = argv
    assert rc == 4


# --------------------------------------------------------------------------- delta tool


def make_glossary(path: Path, rows: list[tuple[str, str, str]], fingerprint: str) -> Path:
    connection = sqlite3.connect(path)
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
    connection.execute("INSERT INTO translation_meta VALUES ('source_snapshot_fingerprint', ?)",
                       (fingerprint,))
    for kind, source, english in rows:
        connection.execute(
            "INSERT INTO translation_terms (term_id, term_kind, source_text, translated_text,"
            " lang, status, source_snapshot_fingerprint) VALUES (?,?,?,?,'en','published',?)",
            (stable_term_id(kind, source), kind, source, english, fingerprint),
        )
    connection.commit()
    connection.close()
    return path


def manifest_rows(rows: list[tuple[str, str, int]], fingerprint: str = "a" * 64) -> list[dict]:
    return [
        {"term_id": stable_term_id(k, s), "term_kind": k, "source_text": s,
         "usage_count": u, "source_snapshot_fingerprint": fingerprint}
        for k, s, u in rows
    ]


def test_delta_reports_zero_when_nothing_changed(tmp_path: Path):
    tool = load_delta_tool()
    glossary = make_glossary(tmp_path / "gl.sqlite", FROZEN_ROWS, "a" * 64)
    manifest = manifest_rows([(k, s, 1) for k, s, _ in FROZEN_ROWS])
    report = tool.classify(manifest, tool.read_glossary(glossary)[0])
    assert report["counts"]["missing"] == 0
    assert report["counts"]["dead_glossary_rows"] == 0
    assert report["safe_to_carry_forward"] is True
    assert all(v["coverage"] == 1.0 for v in report["by_kind"].values())


def test_delta_lists_a_new_material_but_stays_safe(tmp_path: Path):
    tool = load_delta_tool()
    glossary = make_glossary(tmp_path / "gl.sqlite", FROZEN_ROWS, "a" * 64)
    rows = [(k, s, 1) for k, s, _ in FROZEN_ROWS] + [("part", "新增转向助力泵总成", 7)]
    report = tool.classify(manifest_rows(rows), tool.read_glossary(glossary)[0])
    assert report["counts"]["missing"] == 1
    assert [r["source_text"] for r in report["added"]] == ["新增转向助力泵总成"]
    assert report["added"][0]["usage_count"] == 7
    # Adding a term is safe: existing English still covers every old material.
    assert report["safe_to_carry_forward"] is True


def test_delta_marks_carry_forward_unsafe_when_a_term_disappears(tmp_path: Path):
    """A removal/rename is exactly the case that must stop an automatic rebuild."""
    tool = load_delta_tool()
    glossary = make_glossary(tmp_path / "gl.sqlite", FROZEN_ROWS, "a" * 64)
    rows = [(k, s, 1) for k, s, _ in FROZEN_ROWS if s != "后视镜总成"]
    report = tool.classify(manifest_rows(rows), tool.read_glossary(glossary)[0])
    assert report["counts"]["dead_glossary_rows"] == 1
    assert [r["source_text"] for r in report["removed"]] == ["后视镜总成"]
    assert report["safe_to_carry_forward"] is False


def test_delta_deduplicates_manifest_term_ids(tmp_path: Path):
    """The real manifest carries duplicate term_ids; that must not read as a gap."""
    tool = load_delta_tool()
    glossary = make_glossary(tmp_path / "gl.sqlite", FROZEN_ROWS, "a" * 64)
    rows = [(k, s, 1) for k, s, _ in FROZEN_ROWS]
    rows += [("part", "后视镜总成", 3)]  # same term, second occurrence
    report = tool.classify(manifest_rows(rows), tool.read_glossary(glossary)[0])
    assert report["counts"]["release_terms"] == 5
    assert report["by_kind"]["part"]["release_terms"] == 2
    assert report["by_kind"]["part"]["missing"] == 0


def test_delta_detects_source_text_drift(tmp_path: Path):
    """Same term_id but a mismatched source/kind must not be treated as translated."""
    tool = load_delta_tool()
    glossary = make_glossary(tmp_path / "gl.sqlite", FROZEN_ROWS, "a" * 64)
    manifest = manifest_rows([(k, s, 1) for k, s, _ in FROZEN_ROWS])
    for row in manifest:
        if row["source_text"] == "后视镜总成":
            row["term_kind"] = "node"  # pretend the release moved the term between levels
    report = tool.classify(manifest, tool.read_glossary(glossary)[0])
    assert report["counts"]["drifted"] == 1
    assert report["safe_to_carry_forward"] is False


def test_delta_reads_the_stored_fingerprint(tmp_path: Path):
    tool = load_delta_tool()
    glossary = make_glossary(tmp_path / "gl.sqlite", FROZEN_ROWS, "b" * 64)
    _, fingerprint = tool.read_glossary(glossary)
    assert fingerprint == "b" * 64


def test_delta_cli_writes_a_report(tmp_path: Path, capsys):
    tool = load_delta_tool()
    glossary = make_glossary(tmp_path / "gl.sqlite", FROZEN_ROWS, "a" * 64)
    manifest = write_jsonl(tmp_path / "m.jsonl", manifest_rows([(k, s, 1) for k, s, _ in FROZEN_ROWS]))
    out = tmp_path / "delta.json"
    argv = sys.argv
    sys.argv = ["report_catalog_translation_delta.py", "--manifest", str(manifest),
                "--glossary", str(glossary), "--output", str(out)]
    try:
        rc = tool.main()
    finally:
        sys.argv = argv
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["counts"]["release_terms"] == 5
    assert payload["safe_to_carry_forward"] is True


def test_delta_cli_rejects_a_missing_input(tmp_path: Path):
    tool = load_delta_tool()
    argv = sys.argv
    sys.argv = ["report_catalog_translation_delta.py", "--manifest", str(tmp_path / "nope.jsonl"),
                "--glossary", str(tmp_path / "nope.sqlite")]
    try:
        rc = tool.main()
    finally:
        sys.argv = argv
    assert rc == 2
