"""Consolidated-translation export contract.

Locks the properties that make the deliverable trustworthy: it is keyed by term,
it uses the SAME effective-text entrypoint the audits use (never the raw draft),
it never publishes, and it reports the manifest's duplicate term_ids explicitly
instead of duplicating or dropping them.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.conftest import requires_corpus  # noqa: E402

EXPORT = REPO / "tools/export_catalog_translation_consolidated.py"
OVERLAY = REPO / "translation/reviews/codex-luna-20260910/catalog_translation_candidate_wp2.sqlite"

# The export reads the review overlays, the translation runs and the derived terminology
# ledger, none of which are tracked; the contract is asserted wherever that corpus exists.
NEEDS_CORPUS = requires_corpus("review_overlay", "translation_runs", "terminology_ledger")


@NEEDS_CORPUS
def test_consolidated_export_is_keyed_by_term_and_matches_the_overlay():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        proc = subprocess.run(
            [sys.executable, str(EXPORT), "--out", str(out)],
            cwd=REPO, capture_output=True, text=True, timeout=600,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        summary = json.loads((out / "catalog-translation-consolidated.json").read_text(encoding="utf-8"))

        # one record per unique term, and the count must equal the overlay's
        con = sqlite3.connect(f"file:{OVERLAY}?mode=ro", uri=True)
        overlay_terms = con.execute("SELECT COUNT(*) FROM translation_terms").fetchone()[0]
        con.close()
        assert summary["terms"] == overlay_terms, (summary["terms"], overlay_terms)

        lines = (out / "catalog-translation-consolidated.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == summary["terms"]
        ids = [json.loads(l)["term_id"] for l in lines]
        assert len(set(ids)) == len(ids), "deliverable must be keyed by term"

        # the manifest repeats a few ids; they must be accounted for, not hidden
        assert summary["counts"]["manifest_rows"] == 41207
        assert summary["counts"]["unique_terms"] == 41189
        assert summary["counts"]["duplicate_term_id_conflicting"] == 0
        assert summary["counts"]["duplicate_term_id_identical"] == 18


@NEEDS_CORPUS
def test_consolidated_export_never_publishes_and_uses_effective_text():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        proc = subprocess.run(
            [sys.executable, str(EXPORT), "--out", str(out)],
            cwd=REPO, capture_output=True, text=True, timeout=600,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        summary = json.loads((out / "catalog-translation-consolidated.json").read_text(encoding="utf-8"))
        assert summary["published"] == 0, "the export must not publish anything"

        con = sqlite3.connect(f"file:{OVERLAY}?mode=ro", uri=True)
        published = con.execute(
            "SELECT COUNT(*) FROM translation_terms WHERE status='published'"
        ).fetchone()[0]
        con.close()
        assert published == 0, "candidate overlay must keep published at 0"

        # the reviewed text must differ from the raw AI draft wherever a review won
        recs = [json.loads(l) for l in
                (out / "catalog-translation-consolidated.jsonl").read_text(encoding="utf-8").splitlines()]
        changed = sum(1 for r in recs if r["changed"])
        assert changed == summary["counts"]["changed_terms"] > 10000
        assert summary["counts"]["differs_from_draft"] >= changed

        # no empty translations, and the only residual Chinese is the registered exception
        assert summary["counts"]["empty_effective_text"] == 0
        cjk = [r for r in recs if any("\u4e00" <= ch <= "\u9fff" for ch in r["english"])]
        assert len(cjk) == 1 and "SMT_PCBA-SJJC" in cjk[0]["source_text"], cjk


@NEEDS_CORPUS
def test_gaps_report_names_the_outstanding_work():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        subprocess.run([sys.executable, str(EXPORT), "--out", str(out)],
                       cwd=REPO, capture_output=True, text=True, timeout=600, check=True)
        gaps = json.loads((out / "catalog-translation-review-gaps.json").read_text(encoding="utf-8"))
        assert gaps["needs_human_count"] == 12
        assert "WP4_stratified_sampling" in gaps["not_done"]
        assert "runtime_wiring" in gaps["not_done"]
        assert len(gaps["registered_residues"]) >= 70
