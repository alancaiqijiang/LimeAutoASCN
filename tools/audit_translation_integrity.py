#!/usr/bin/env python3
"""Full-corpus integrity audit of the LimeAuto catalog translation drafts.

Read-only. No network, no model calls. It re-instates, over every draft row, the
checks that must hold before any English name can be reviewed or published, and
it adds two checks the earlier pipeline lacked:

* **part-number integrity** -- every identifier-shaped token in the source must
  survive in the translation with its digits intact;
* **cross-row contamination** -- an identifier present in the translation but
  absent from the source is the signature of a shifted/damaged output row
  (a real defect found in this corpus: consecutive rows carry their
  neighbour's translation).

Outputs a summary JSON plus a findings JSONL, one line per offending row, so the
result stays auditable instead of being a count with no evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tools.translation_token_rules import (
    DIMENSION_RE,
    RUN_RE,
    SPEC_RE,
    ordered_groups_present,
    protected_tokens,
    translation_token_errors,
)

CJK_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|FIXME|XXX|\?\?\?)\b", re.IGNORECASE)
DIGIT_RUN_RE = re.compile(r"\d+")

# A token only counts as a source identifier when it carries a digit and is long
# enough to be a code rather than a trim index.
MIN_CODE_LEN = 4
# A translated token only counts as a foreign code when it carries a number of
# at least this length; shorter numbers are trim/seat/spec wording.
MIN_DIGIT_GROUP = 3
# Longest letter group a real catalog code carries; anything longer is English.
MAX_CODE_LETTERS = 4

# Chinese colour morphemes. A source tail containing one of these is treated as
# a colour term even when the glossary has no entry for that exact variant.
COLOUR_MORPHEMES = set("色米棕橙绿蓝灰黑白红黄金银赭紫粉褐青彩")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_review_overrides(review_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    """Effective reviewed text per term_id, later directories winning."""
    overrides: dict[str, dict[str, Any]] = {}
    for root in review_dirs:
        for path in sorted(root.glob("batch-*.jsonl")):
            for row in load_jsonl(path):
                term_id = str(row.get("term_id") or "")
                if not term_id:
                    continue
                decision = str(row.get("decision") or "")
                text = str(row.get("reviewed_translation") or "")
                if decision in {"approve", "revise", "needs_human"}:
                    overrides[term_id] = {"text": text, "reviewer": row.get("reviewer"),
                                          "decision": decision}
                elif decision == "reject_with_source_fallback":
                    overrides[term_id] = {"text": "", "reviewer": row.get("reviewer"),
                                          "decision": decision}
    return overrides


def effective_text(overrides: dict[str, dict[str, Any]], term_id: str, draft_text: str = "") -> str:
    """The text the runtime would actually show for a term.

    Two different layers carry a translation and they are easy to confuse:

    * a *review batch row* (``output/batch-*.jsonl``) carries ``reviewed_translation``;
    * the *override map* returned by :func:`load_review_overrides` nests it under ``text``.

    Reading ``reviewed_translation`` off the override map silently yields nothing and falls
    back to the AI draft -- which made three analysis scripts measure the draft while believing
    they measured the repaired corpus. Every caller must go through this helper.
    """
    return str((overrides.get(term_id) or {}).get("text") or "") or draft_text


def candidate_codes(text: str) -> list[str]:
    """Identifier-shaped tokens: contain a digit and are long enough to be a code."""
    return [
        match.group(0)
        for match in RUN_RE.finditer(text)
        if len(match.group(0)) >= MIN_CODE_LEN and any(c.isdigit() for c in match.group(0))
    ]


def digits_of(text: str) -> list[str]:
    return DIGIT_RUN_RE.findall(text)


def load_glossary_values(path: Path | None) -> tuple[set[str], set[str]]:
    """Return (Chinese terms, English renderings) from the colour/finish glossary."""
    if path is None or not path.exists():
        return set(), set()
    data = json.loads(path.read_text(encoding="utf-8"))
    terms = data.get("terms") or {}
    chinese = {key for key in terms}
    english = {str(value.get("en") or "") for value in terms.values()}
    return chinese, {value for value in english if value}


def unexpected_finish_term(
    source: str, translated: str, glossary_zh: set[str], glossary_en: set[str]
) -> str | None:
    """Report a colour/finish tail the source never carries.

    The presence-only gate cannot see *extra* content, which is exactly how a
    shifted output row hides: the source is `ISOFIX钢丝罩盖` (no colour) while the
    stored text ends in `- Oat Beige`, a colour belonging to the next row.

    A source tail that carries a Chinese colour morpheme is never reported: the
    glossary cannot enumerate every variant (`米黄色`, `塔西提蓝`), and those rows
    are correctly translated.
    """
    if not glossary_en:
        return None
    source_tail = re.split(r"[-_]", source)[-1].strip()
    if source_tail in glossary_zh:
        return None
    if any(char in COLOUR_MORPHEMES for char in source_tail):
        return None
    candidate = re.split(r"[-_]\s*", translated.strip())[-1].strip()
    for value in glossary_en:
        if candidate.casefold() == value.casefold():
            return candidate
    return None


def digit_sequences_preserved(source: str, translated: str) -> list[str]:
    """Digit groups of the source code tokens that are missing from the translation.

    Digit loss is the damage that matters most for a material number, so it is
    checked separately from the ordered-group test.
    """
    missing: list[str] = []
    haystack = re.sub(r"[^0-9]", "|", translated)
    for code in candidate_codes(source):
        for group in digits_of(code):
            if len(group) < 3:
                continue
            if group not in haystack:
                missing.append(f"{code}:{group}")
    return missing


def foreign_codes(source: str, translated: str) -> list[str]:
    """Code-shaped tokens in the translation that the source cannot account for.

    A token is reported only when it carries a letter *and* the source cannot
    explain it. Two things are deliberately excluded:

    * unit and dimension specs -- `2.0排量` legitimately becomes `2.0L`, which
      must not look like a foreign code;
    * tokens whose digits all appear somewhere in the source -- those are
      re-spacings of source numbers, not contamination.

    What remains is the real signature of a shifted output row: a
    letter-bearing code such as `HYEA-3658500` sitting on the row whose source
    is `HYEA-3658300`.
    """
    source_folded = re.sub(r"[^A-Za-z0-9]", "", source).casefold()
    source_digits = set(re.findall(r"\d+", source))
    foreign: list[str] = []
    for token in dict.fromkeys(candidate_codes(translated)):
        if not any(c.isalpha() for c in token) or not any(c.isdigit() for c in token):
            continue
        if SPEC_RE.fullmatch(token) or DIMENSION_RE.fullmatch(token):
            continue
        # Require a real code-length number: `七座` -> `7-Seat` and `尊贵型`
        # -> `Premium 4G` are translations, not contamination.
        if not any(len(group) >= MIN_DIGIT_GROUP for group in re.findall(r"\d+", token)):
            continue
        # Codes carry short letter groups; a long run such as `Assembly-2011`
        # is a translated phrase that merely lost its space.
        letter_groups = re.findall(r"[A-Za-z]+", token)
        if not letter_groups or max(len(group) for group in letter_groups) > MAX_CODE_LETTERS:
            continue
        folded = re.sub(r"[^A-Za-z0-9]", "", token).casefold()
        if not folded or folded in source_folded:
            continue
        token_digits = re.findall(r"\d+", token)
        if token_digits and all(d in source_digits for d in token_digits):
            continue
        foreign.append(token)
    return foreign


def audit_row(
    row: dict[str, Any],
    draft: dict[str, Any],
    glossary_zh: set[str] | None = None,
    glossary_en: set[str] | None = None,
) -> dict[str, Any] | None:
    source = str(row.get("source_text") or "")
    translated = str(draft.get("translated_text") or "").strip()
    findings: dict[str, Any] = {}

    if not translated:
        findings["empty_translation"] = True
        return findings

    if CJK_RE.search(translated):
        findings["residual_cjk"] = sorted(set(CJK_RE.findall(translated)))

    if CJK_RE.search(source) and translated == source:
        findings["identical_to_source"] = True

    if PLACEHOLDER_RE.search(translated):
        findings["placeholder_marker"] = True

    gate = translation_token_errors(source, translated, draft.get("qa_errors") or [])
    if gate:
        findings["gate_errors"] = gate

    missing_digits = digit_sequences_preserved(source, translated)
    if missing_digits:
        findings["missing_digit_sequences"] = missing_digits

    foreign = foreign_codes(source, translated)
    if foreign:
        findings["foreign_codes"] = foreign

    unexpected = unexpected_finish_term(
        source, translated, glossary_zh or set(), glossary_en or set()
    )
    if unexpected:
        findings["unexpected_finish_term"] = [unexpected]

    # A source identifier that is present but with altered letters is caught by
    # the ordered-group test; report it explicitly for the part-number report.
    damaged = [
        code
        for code in protected_tokens(source, draft.get("qa_errors") or [])
        if len(code) >= MIN_CODE_LEN
        and any(c.isdigit() for c in code)
        and not ordered_groups_present(code, translated)
    ]
    if damaged:
        findings["damaged_codes"] = damaged

    return findings or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--findings", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-findings", type=int, default=0,
                        help="cap the findings file (0 = no cap)")
    parser.add_argument("--reviews-dir", type=Path, action="append",
                        help="repeatable: audit the effective reviewed text instead of the raw draft")
    parser.add_argument("--glossary", type=Path,
                        help="colour/finish glossary; enables the unexpected-finish-term check")
    args = parser.parse_args()

    manifest = load_jsonl(args.manifest)
    ordered_ids = [str(row.get("term_id") or "") for row in manifest]
    drafts = {str(row["term_id"]): row for row in load_jsonl(args.draft)}
    overrides = load_review_overrides(args.reviews_dir or [])
    glossary_zh, glossary_en = load_glossary_values(args.glossary)

    counts: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    findings_written = 0
    args.findings.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.findings.with_name(f".{args.findings.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(manifest):
            term_id = str(row.get("term_id") or "")
            draft = drafts.get(term_id)
            if draft is None:
                counts["missing_draft"] += 1
                record = {"index": index, "term_id": term_id,
                          "term_kind": row.get("term_kind"),
                          "source_text": row.get("source_text"),
                          "findings": {"missing_draft": True}}
            else:
                effective = draft
                override = overrides.get(term_id)
                if override is not None:
                    effective = dict(draft)
                    effective["translated_text"] = override["text"]
                result = audit_row(row, effective, glossary_zh, glossary_en)
                if not result:
                    continue
                for key in result:
                    counts[key] += 1
                by_kind[str(row.get("term_kind"))] += 1
                record = {
                    "index": index,
                    "term_id": term_id,
                    "term_kind": row.get("term_kind"),
                    "source_text": row.get("source_text"),
                    "translated_text": effective.get("translated_text"),
                    "status": draft.get("status"),
                    "reviewed_by": (override or {}).get("reviewer"),
                    "findings": result,
                }
            if args.max_findings and findings_written >= args.max_findings:
                continue
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            findings_written += 1
    os.replace(tmp, args.findings)

    offending = set()
    for line in args.findings.read_text(encoding="utf-8").splitlines():
        if line.strip():
            offending.add(json.loads(line)["term_id"])

    summary = {
        "schema": "limeauto.translation-integrity-audit.v1",
        "generated_at": now_iso(),
        "manifest": str(args.manifest),
        "draft": str(args.draft),
        "rows_total": len(manifest),
        "rows_offending": len(offending),
        "check_counts": dict(counts),
        "findings_by_term_kind": dict(by_kind),
        "check_meaning": {
            "residual_cjk": "translated text still contains Chinese characters",
            "identical_to_source": "translated text equals the Chinese source",
            "empty_translation": "no translated text",
            "placeholder_marker": "TODO/TBD/FIXME-style marker in the output",
            "gate_errors": "identifier or composed-value gate failure",
            "missing_digit_sequences": "a digit group of a source code is absent from the translation",
            "damaged_codes": "a source code's letter/number groups are absent or reordered",
            "foreign_codes": "the translation carries a code the source does not contain (shifted row)",
            "missing_draft": "no draft row for this manifest term_id",
        },
        "findings_file": str(args.findings),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
