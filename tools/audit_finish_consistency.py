#!/usr/bin/env python3
"""Read-only audit: is a row's finish/colour rendering the one its own source asks for?

Built after the 150-row material sample exposed an `ISOFIX罩盖` family where every row
carried the *next* row's colour (源 贝壳白 -> Shell White sat on 砂金米, and so on).
No existing check can see it: the translation still contains a colour word, no code is
involved and no Chinese is left behind.

Method -- no model, no invented vocabulary:
  1. take the source's finish segment (after the last `-`/`_`, Chinese, carries a colour
     morpheme, at most 8 characters);
  2. collect the English finish phrase each row actually renders after its last separator;
  3. per source segment, elect the **majority** rendering as canonical (a contaminated
     minority cannot outvote the clean rows of the other families);
  4. flag every row whose own rendering differs from canonical, and additionally flag the
     suspicious case where the row's rendering equals the canonical rendering of a
     neighbouring finish segment (the shift signature).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.audit_translation_integrity import effective_text, load_review_overrides  # noqa: E402

ZH_COLOUR = set("色米棕橙绿蓝灰黑白红黄金银赭紫粉褐青彩陶")
MATERIAL_NOUN = re.compile(r"(金属|合金|毫米|金刚|金具)")
SPLIT = re.compile(r"[-_－]")
# The trailing phrase may carry its own hyphen or digit (`E-Coat Black`, `Gray-Black 6`).
# Truncating at the hyphen read `E-Coat Black Paint` as `Coat Black Paint`, which made a
# rewrite impossible to locate and hid the real wording from the vote.
FINISH_TAIL = re.compile(r"[-–]\s*([A-Za-z][A-Za-z0-9 .'\-]{2,40})$")
NON_FINISH = re.compile(r"^(assembly|kit|cover|panel|bracket|wire|board|module|sensor|switch|type|set|part|component|group)$", re.I)
# a finish the translation may express with a space, an underscore or a hyphen separator --
# judging "missing" by the trailing phrase alone produced 210 false positives on rows that
# render `... EPDM Black`.  Missing means: no finish word anywhere in the translation.
EN_FINISH_WORDS = (
    "black", "white", "gray", "grey", "blue", "red", "green", "brown", "beige", "orange",
    "gold", "silver", "ochre", "ivory", "ceramic", "pottery", "tan", "cream", "wine", "sand",
    "yellow", "purple", "pink", "stone", "coffee", "jade", "ink", "dusk", "twilight", "crimson",
    "zinc", "plated", "primer", "brushed", "chrome", "matte", "gloss", "glossy", "anodiz",
    "e-coat", "electrophoretic", "electrophoresis", "tea", "smoke", "mist", "cloud", "sky",
    "bronze", "copper", "emerald", "teal", "navy", "khaki", "paint",
    "scarlet", "sunset", "dusk", "gravel", "oat", "oatmeal", "cardamom", "tahiti", "tahitian",
    "reef", "terracotta", "halberd", "hutong", "french", "mysterious", "dawn", "lime",
    "porcelain", "pearl", "platinum", "titanium", "titan", "amber", "cocoa", "caramel",
    "charcoal", "graphite", "gunmetal", "camel", "mustard", "olive", "mint", "azure",
    "cobalt", "indigo", "violet", "lavender", "lilac", "mauve", "plum", "berry", "rust",
    "brass", "nickel", "champagne", "steel", "shimmer", "turkish", "tahitian",
    "pergamino", "color", "body",
)


def _finish_vocabulary(words):
    tokens, phrases = set(), []
    for word in words:
        if " " in word:
            phrases.append(word)
        else:
            tokens.update(part for part in word.split("-") if part)
    return frozenset(tokens), tuple(phrases)


FINISH_WORD_SET, FINISH_PHRASES = _finish_vocabulary(EN_FINISH_WORDS)


def has_finish_word(text: str) -> bool:
    """Word match, not substring: `tan` appears inside `Distant`, no finish there."""
    low = text.lower()
    if set(re.findall(r"[a-z0-9]+", low)) & FINISH_WORD_SET:
        return True
    return any(phrase in low for phrase in FINISH_PHRASES)


def canonical_present(text: str, canonical: str) -> bool:
    """True when every word of the canonical rendering appears in the text.

    Rows that separate the finish with a space or an underscore (`... EPDM Black`) have no
    trailing `- phrase`, so the phrase extractor returns nothing; without this test they were
    reported as a variant even though the canonical finish is right there.
    """
    low = text.lower()
    return all(w in low for w in canonical.split())


def finish_segment(source: str) -> str:
    tail = SPLIT.split(source)[-1].strip()
    if not tail or len(tail) > 8 or MATERIAL_NOUN.search(tail):
        return ""
    if not any(c in ZH_COLOUR for c in tail):
        return ""
    if re.fullmatch(r"[\dA-Za-z./+ ()]+", tail):
        return ""
    return tail


def rendered_finish(text: str) -> str:
    """The trailing finish phrase of a translation.

    Two passes over the separators, right to left.  A separator with whitespace beside it is
    the real delimiter (`Bracket - E-Coat Black Paint`); only when none exists does the scan
    fall back to every separator, and it rejects candidates that open with a code-like token
    (`BYDQ832A0716-A7 Trim Clip ...`).  Truncating at a hyphen inside the phrase read
    `E-Coat Black Paint` as `Coat Black Paint`, which could not be located for a rewrite.
    """
    stripped = text.strip()
    positions = [(m.start(), m.end()) for m in re.finditer(r"[-–_]", stripped)]
    spaced = [(start, end) for start, end in positions
              if (start > 0 and stripped[start - 1].isspace())
              or (end < len(stripped) and stripped[end].isspace())]
    for group in (spaced, positions):
        for _start, end in reversed(group):
            phrase = re.sub(r"\s+", " ", stripped[end:]).strip()
            if not phrase or len(phrase) > 40 or not re.search(r"[A-Za-z]", phrase):
                continue
            # A candidate that opens with a short uppercase token is a name fragment sitting
            # behind an identifier (`BYDQ832B0515-DJ` + `DJ Trim Clip Pigeon Gray`), not the finish.
            if re.match(r"^[A-Za-z]{1,3}\d", phrase) or re.match(r"^[A-Z]{1,3}\s", phrase):
                continue
            # ... or with an identifier fragment that ends in a digit (`2F3 Leaf Spring Nut ...`).
            if re.match(r"^\S*\d\S*\s", phrase):
                continue
            if NON_FINISH.match(phrase.split()[-1].lower()):
                continue
            return phrase
    # Last resort for rows whose only separator sits inside the identifier
    # (`BYDQ832B0515-DJ Trim Clip Pigeon Gray`): the finish word itself still marks the tail.
    words = stripped.split()
    for width in (2, 1, 3):
        if len(words) >= width + 1:
            phrase = " ".join(words[-width:])
            if has_finish_word(phrase):
                return phrase
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--draft", required=True)
    ap.add_argument("--review-dir", action="append", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--findings", required=True)
    ap.add_argument("--min-support", type=int, default=3)
    args = ap.parse_args()

    mani = [json.loads(l) for l in Path(args.manifest).read_text(encoding="utf-8").splitlines() if l.strip()]
    draft = {json.loads(l)["term_id"]: json.loads(l)
             for l in Path(args.draft).read_text(encoding="utf-8").splitlines() if l.strip()}
    ov = load_review_overrides([Path(p) for p in args.review_dir])

    rows: list[dict[str, Any]] = []
    for m in mani:
        if m["term_kind"] != "part":
            continue
        seg = finish_segment(m["source_text"])
        if not seg:
            continue
        text = effective_text(ov, m["term_id"], draft.get(m["term_id"], {}).get("translated_text") or "")
        rows.append({"term_id": m["term_id"], "source_text": m["source_text"], "translated_text": text,
                     "finish_zh": seg, "finish_en": rendered_finish(text)})

    votes: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["finish_en"]:
            votes[r["finish_zh"]][r["finish_en"].lower()] += 1

    canonical = {}
    for seg, counter in votes.items():
        top, n = counter.most_common(1)[0]
        if n >= args.min_support:
            canonical[seg] = top

    canon_index: dict[str, str] = {}
    for seg, en in canonical.items():
        canon_index.setdefault(en, seg)

    findings = []
    for r in rows:
        r["canonical_en"] = canonical.get(r["finish_zh"], "")
        r["canonical_of_rendered"] = canon_index.get(r["finish_en"].lower(), "")
        if not r["canonical_en"]:
            continue
        if not has_finish_word(r["translated_text"]):
            r["issue"] = "finish_missing"
        elif r["finish_en"].lower() == r["canonical_en"] or canonical_present(r["translated_text"], r["canonical_en"]):
            continue
        elif r["canonical_of_rendered"] and r["canonical_of_rendered"] != r["finish_zh"]:
            r["issue"] = "finish_shifted"          # carries another finish's rendering
        else:
            r["issue"] = "finish_variant"          # same idea, non-canonical wording
        findings.append(r)

    Path(args.findings).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in findings), encoding="utf-8")
    summary = {
        "schema": "limeauto.translation-finish-consistency.v1",
        "rows_with_finish_segment": len(rows),
        "distinct_finish_segments": len(votes),
        "canonical_segments": len(canonical),
        "rows_flagged": len(findings),
        "issue_counts": dict(Counter(x["issue"] for x in findings)),
        "top_shifted_segments": Counter(x["finish_zh"] for x in findings if x["issue"] == "finish_shifted").most_common(20),
        "top_missing_segments": Counter(x["finish_zh"] for x in findings if x["issue"] == "finish_missing").most_common(20),
        "canonical_table": dict(sorted(canonical.items(), key=lambda kv: -votes[kv[0]][kv[1]])[:60]),
    }
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("rows_with_finish_segment", "distinct_finish_segments",
                                              "canonical_segments", "rows_flagged", "issue_counts")}, ensure_ascii=False))
    print("shifted top:", summary["top_shifted_segments"])
    print("missing top:", summary["top_missing_segments"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
