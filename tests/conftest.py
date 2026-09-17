from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The catalog is closed by default in tests; route tests enable it explicitly.
os.environ["LIMEAUTO_CATALOG_BROWSE"] = "0"
os.environ.setdefault("LIMEAUTO_AFTERCARE_HMAC_SECRET", "pytest-aftercare-secret")

# Corpora that are deliberately kept out of Git because of size or because they are
# generated: the review overlays (80 MB), the translation runs (95 MB) and the derived
# terminology ledger (3.3 MB). Code and curated policy files are tracked, so a bare
# checkout is reproducible; the contract tests that need a corpus say so and skip instead
# of failing, which would read as broken code and invite someone to weaken the assertion.
CORPUS_PATHS = {
    "review_overlay": "translation/reviews/codex-luna-20260910/catalog_translation_candidate_wp2.sqlite",
    "translation_runs": "translation/runs/full-mimo-20260902",
    "terminology_ledger": "translation/glossary/terminology-ledger.json",
}


def corpus_absent(*names: str) -> list[str]:
    """Return the requested corpora that are not present in this working tree."""
    unknown = [name for name in names if name not in CORPUS_PATHS]
    if unknown:
        raise KeyError(f"unknown corpus name(s): {unknown}")
    return [name for name in names if not (ROOT / CORPUS_PATHS[name]).exists()]


def requires_corpus(*names: str) -> pytest.MarkDecorator:
    """Skip a contract test when its (uncommitted) corpus is not checked out."""
    missing = corpus_absent(*names)
    return pytest.mark.skipif(
        bool(missing),
        reason="corpus not present in this checkout: "
        + ", ".join(f"{name} ({CORPUS_PATHS[name]})" for name in missing),
    )

