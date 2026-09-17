#!/usr/bin/env python3
"""Unify a batch of translation units to their corpus-majority wording.

WP2/WP5 driver for `docs/catalog-english-translation-linguistic-pass-plan-20260910.md`,
run per the user's decision: *the majority rendering in the corpus is the standard, the
minority rows are rewritten; every batch is audited, tested and revertible*.

What it does:

  1. re-read the corpus through `effective_text()` and the ledger's aligner, so a row's
     unit renderings are the ones measured -- not a substring guess;
  2. take each unit's `chosen_en` from the ledger.  Units the user has not ruled on
     (`pending_naming_policy`) are skipped unless explicitly included;
  3. rewrite only the rows whose rendering differs, by replacing that exact rendering in
     the row text.  A rendering that occurs more than once in the row is refused rather
     than replaced blindly;
  4. write a **new review run** (`translation/reviews/<run-id>/output/batch-*.jsonl`) in the
     same shape as the Luna and main-process runs, so the overlay builder and every audit
     pick it up by adding the directory to the override order.  The source run, the draft
     and the existing review runs are never modified, which is what makes the batch
     revertible: drop the directory and the previous effective text is back.

No model is called and nothing is marked `published`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tools.audit_translation_integrity import effective_text, load_review_overrides  # noqa: E402
from tools.build_terminology_ledger import align_row, ledger_unit, reads_as_finish  # noqa: E402
from tools.translation_token_rules import translation_token_errors  # noqa: E402

RUN = "translation/runs/full-mimo-20260902"
OVERRIDE_RUNS = [
    "codex-luna-20260910",
    "main-process-navigation-20260910",
    "main-process-materials-20260910",
    "main-process-repairs-20260910",
]
# Units whose wording needs a business decision, not a majority vote.
DECIDED_BY_POLICY = {"pending_naming_policy"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def policy_sweep(ledger: dict[str, Any], source: str, text: str) -> tuple[str, list[dict[str, str]]]:
    """Apply a policy wording to a row whose units the aligner could not separate.

    Only policy-decided units qualify: there the wording is an explicit decision, so finding a
    known variant in the row is enough.  A match is refused when it is glued to another finish
    word (`Zinc Plated` inside `Yellow Zinc Plated` -- the colour in front belongs to the row,
    not to the unit).
    """
    from tools.build_terminology_ledger import FINISH_WORD_SET, SRC_SPLIT

    changes: list[dict[str, str]] = []
    units = [piece.strip() for piece in SRC_SPLIT.split(source) if piece.strip()]
    for unit in units:
        entry = ledger_unit(ledger, unit)
        if not entry or "policy_override" not in entry["flags"]:
            continue
        chosen = (entry.get("chosen_en") or "").strip()
        if not chosen or chosen.lower() in text.lower():
            continue
        for variant in sorted(entry["renderings"], key=len, reverse=True):
            if variant == chosen:
                continue
            matches = list(re.finditer(rf"(?<![A-Za-z]){re.escape(variant)}(?![A-Za-z])", text))
            if len(matches) != 1:
                continue
            before = text[: matches[0].start()].rstrip()
            last_word = re.findall(r"[A-Za-z]+$", before)
            if last_word and last_word[0].lower() in FINISH_WORD_SET:
                continue
            candidate = text[: matches[0].start()] + chosen + text[matches[0].end():]
            if translation_token_errors(source, candidate, []):
                continue
            changes.append({"kind": "chunk", "unit": unit, "rendered": variant, "chosen_en": chosen})
            text = candidate
            break
    return text, changes


def _finish_words(text: str) -> set[str]:
    """Colour/finish words in a phrase, ignoring romanised colour *names*.

    `Qianshan` counts as a name, not a colour, so renaming it to `Thousand Mountains` is a
    name change; the guard only cares about words that describe an actual finish.
    """
    from tools.build_terminology_ledger import FINISH_WORDS, PINYIN_CLUSTERS
    low = text.lower()
    words = {
        word for word in FINISH_WORDS
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low)
    }
    return {word for word in words if not word.startswith(PINYIN_CLUSTERS)}


def _finish_words_subset(rendered: str, chosen: str) -> bool:
    """True when the chosen wording carries every finish word the row's wording carries.

    `Qianshan Emerald` -> `Thousand Mountains Emerald` keeps its colour word, so the unit is
    the same one and the rewrite is safe.  `... Assembly - Matte Titanium Silver` -> `...
    Assembly` drops a colour, which means the row's finish was glued into the name piece and
    the rewrite would delete it -- that case stays refused.
    """
    return _finish_words(rendered) <= _finish_words(chosen)


def apply_text_rules(chain_dirs: list[Path], policy_path: Path, args) -> int:
    """Apply the policy's term corrections to the current text of the corpus.

    The rule set is what WP3 turned up by reading rows: parallel parts that ended up with different
    English (`Headlamp`/`Headlight`, `Damper`/`Shock Absorber`), an abbreviation applied only to the
    right-hand rows (`R Front` vs `Left Front`) and one word-order slip.  Every rewrite is bounded
    (the phrase must occur exactly once), idempotent (skipped when the target wording is present)
    and re-checked against the identifier gate.
    """
    rules = (json.loads(Path(policy_path).read_text(encoding="utf-8")).get("text_rules") or [])
    drafts = {str(row["term_id"]): row for row in load_jsonl(args.draft)}
    manifest = load_jsonl(args.manifest)
    overrides = load_review_overrides([Path(p) for p in chain_dirs])
    run_dir = REPO_ROOT / "translation/reviews" / args.run_id
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    stats: Counter[str] = Counter()
    per_rule: Counter[str] = Counter()
    rewrites: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for row in manifest:
        if str(row.get("term_kind")) != "part":
            continue
        term_id = str(row.get("term_id") or "")
        source = str(row.get("source_text") or "")
        if (overrides.get(term_id) or {}).get("decision") == "needs_human":
            stats["skipped_needs_human"] += 1
            continue
        text = effective_text(overrides, term_id, str((drafts.get(term_id) or {}).get("translated_text") or ""))
        if not text:
            continue
        new_text = text
        changes: list[dict[str, str]] = []
        for rule in rules:
            if not re.search(rule["zh_pattern"], source):
                continue
            target = str(rule["to"])
            pattern = rule.get("from_pattern")
            if pattern:
                matches = list(re.finditer(pattern, new_text))
                if len(matches) != 1:
                    continue
                candidate = new_text[: matches[0].start()] + target + new_text[matches[0].end():]
                rendered = matches[0].group(0)
            else:
                rendered = str(rule["from"])
                # The occurrence count is the guard: after a rule runs the phrase is gone, so a
                # second pass is a no-op.  A "target already present" test would wrongly block
                # `PAD Display Screen` -> `PAD Display` and `Shock Absorber Damper`.
                if new_text.count(rendered) != 1:
                    continue
                candidate = new_text.replace(rendered, target, 1)
            violations = translation_token_errors(source, candidate, [])
            if violations:
                refused.append({"term_id": term_id, "rule": rule["id"], "reason": "identifier_gate",
                                "violations": violations, "source_text": source, "translated_text": text})
                stats["refused_gate"] += 1
                continue
            new_text = candidate
            per_rule[rule["id"]] += 1
            changes.append({"kind": "term_rule", "unit": rule["id"], "rendered": rendered, "chosen_en": target})
        if changes:
            rewrites.append({"term_id": term_id, "term_kind": "part", "source_text": source,
                             "previous_text": text, "reviewed_translation": new_text,
                             "decision": "revise", "reviewer": "main-process-term-rule-v1",
                             "units": changes})
            stats["rows_rewritten"] += 1
            stats["replacements"] += len(changes)

    for path in sorted(output_dir.glob("batch-*.jsonl")):
        path.unlink()
    for index in range(0, len(rewrites), args.batch_size):
        chunk = rewrites[index:index + args.batch_size]
        (output_dir / f"batch-{index // args.batch_size + 1:04d}.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunk), encoding="utf-8")

    summary = {
        "schema": "limeauto.translation-unit-unification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": args.run_id, "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "mode": "apply_text_rules", "policy": str(Path(policy_path).relative_to(REPO_ROOT)),
        "chain_dirs": [str(Path(p).relative_to(REPO_ROOT)) for p in chain_dirs],
        "rules": [rule["id"] for rule in rules],
        "counts": dict(stats), "replacements_per_rule": dict(per_rule),
        "rows_rewritten": len(rewrites),
        "batch_files": len(list(output_dir.glob("batch-*.jsonl"))),
        "refused": len(refused), "refused_examples": refused[:5],
        "rollback": f"delete {run_dir.relative_to(REPO_ROOT)} and remove it from the override order",
        "source_immutable": True,
    }
    summary_path = args.summary or REPO_ROOT / "local-reports/translation-linguistic-pass-20260910" / f"wp3-{args.run_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": args.run_id, "reviewer": "main-process-term-rule-v1",
        "rows_rewritten": len(rewrites), "batch_files": summary["batch_files"],
        "completed_at": summary["generated_at"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("run_id", "counts", "replacements_per_rule",
                                              "rows_rewritten", "batch_files", "refused")},
                     ensure_ascii=False, indent=2))
    print("summary:", summary_path)
    return 0


FINISH_SEPARATORS = (" - ", " – ", "_", "-")


def _locate_finish(ledger: dict[str, Any], unit: str, aligned_tail: str, text: str) -> str:
    """The unit's rendering at the end of the row, preferring the longest known wording.

    The aligner cuts at the last separator, so a hyphenated finish comes back short:
    `B-Pillar Safety Handle-Off-White 1` yields `White 1`, and inserting ` - ` there produced
    `Handle-Off - White 1`.  Taking the longest wording the ledger already recorded for the
    unit keeps `Off-White 1` intact.
    """
    entry = ledger_unit(ledger, unit) or {}
    known = [str(name) for name in (entry.get("renderings") or {})]
    if entry.get("chosen_en"):
        known.append(str(entry["chosen_en"]))
    if aligned_tail:
        known.append(aligned_tail)
    stripped = text.rstrip()
    candidates = [name for name in known if name and stripped.endswith(name)]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _separator_repair_candidate(text: str, rendered: str) -> str | None:
    """Return the ` - `-normalised rewrite of ONE finish join, or None.

    Three shapes exist in the corpus and only the middle one needs repair:

      * `... Combination - Black`  -> already conformant, None
      * `... VDEAU-2915812 20# Black` -> whitespace-only join, REPAIR
      * `... FragranceWoven Brown` -> glued to a part number / no boundary, None

    The caller is responsible for locating `rendered` and the identifier gate.
    """
    index = text.rstrip().rfind(rendered)
    if index <= 0:
        return None
    prefix = text[:index]
    if next((sep for sep in FINISH_SEPARATORS if prefix.endswith(sep)), ""):
        return None  # a separator is already present (either form)
    if not prefix.endswith((" ", "\t")):
        return None  # no whitespace boundary -> out of scope
    head = prefix.rstrip()
    if not head:
        return None
    return f"{head} - {text[index:]}"


def normalize_finish_separator(chain_dirs: list[Path], ledger: dict[str, Any], args) -> int:
    """WP5 separator pass.

    A row's finish phrase is attached three ways in this corpus (` - ` on 8,423 rows, a glued
    `-` on 967, `_` on 185).  The body reads the text from the *full* current chain -- reading
    an older revision would drop every earlier batch's change when this run wins the override --
    and rewrites only the separator in front of the finish.
    """
    drafts = {str(row["term_id"]): row for row in load_jsonl(args.draft)}
    manifest = load_jsonl(args.manifest)
    overrides = load_review_overrides([Path(p) for p in chain_dirs])
    run_dir = REPO_ROOT / "translation/reviews" / args.run_id
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    stats: Counter[str] = Counter()
    rewrites: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for row in manifest:
        if str(row.get("term_kind")) != "part":
            continue
        term_id = str(row.get("term_id") or "")
        source = str(row.get("source_text") or "")
        if (overrides.get(term_id) or {}).get("decision") == "needs_human":
            stats["skipped_needs_human"] += 1
            continue
        text = effective_text(overrides, term_id, str((drafts.get(term_id) or {}).get("translated_text") or ""))
        if not text:
            continue
        aligned = align_row(source, text)
        finish = aligned.get("finish") if aligned["route"] is not None else None
        if not finish or not finish[0]:
            stats["no_finish_rendering"] += 1
            continue
        rendered = _locate_finish(ledger, finish[0], finish[1], text)
        if not rendered:
            stats["no_finish_rendering"] += 1
            continue
        index = text.rstrip().rfind(rendered)
        if index <= 0:
            stats["rendering_not_located"] += 1
            continue
        prefix = text[:index]
        separator = next((sep for sep in FINISH_SEPARATORS if prefix.endswith(sep)), "")
        if not separator:
            # A finish rendering can be glued to the body with NO separator at all
            # (`... Hanger Combination VDEAU-2915812 20# Black`, `Vehicle Fragrance
            # Woven Brown`).  The corpus separates finishes with ' - ' on 636 rows
            # versus 16 space-only rows, so a whitespace-only join is a defect too --
            # skipping these left 17 rows permanently non-conformant.
            candidate = _separator_repair_candidate(text, rendered)
            if candidate is None:
                stats["no_separator_before_finish"] += 1
                continue
            if translation_token_errors(source, candidate, []):
                refused.append({"term_id": term_id, "reason": "identifier_gate", "source_text": source,
                                "previous_text": text, "candidate": candidate})
                stats["refused_gate"] += 1
                continue
            rewrites.append({"term_id": term_id, "term_kind": "part", "source_text": source,
                             "previous_text": text, "reviewed_translation": candidate,
                             "decision": "revise", "reviewer": "main-process-separator-normalisation-v1",
                             "units": [{"kind": "separator", "unit": finish[0],
                                        "rendered": "<none>", "chosen_en": " - "}]})
            stats["rows_rewritten"] += 1
            stats["separator_was_missing"] += 1
            continue
        if separator == " - ":
            stats["already_spaced"] += 1
            continue
        head = prefix[: len(prefix) - len(separator)].rstrip()
        if not head:
            stats["nothing_before_separator"] += 1
            continue
        candidate = f"{head} - {text[index:]}"
        if translation_token_errors(source, candidate, []):
            refused.append({"term_id": term_id, "reason": "identifier_gate", "source_text": source,
                            "previous_text": text, "candidate": candidate})
            stats["refused_gate"] += 1
            continue
        rewrites.append({"term_id": term_id, "term_kind": "part", "source_text": source,
                         "previous_text": text, "reviewed_translation": candidate,
                         "decision": "revise", "reviewer": "main-process-separator-normalisation-v1",
                         "units": [{"kind": "separator", "unit": finish[0],
                                    "rendered": separator, "chosen_en": " - "}]})
        stats["rows_rewritten"] += 1

    for path in sorted(output_dir.glob("batch-*.jsonl")):
        path.unlink()
    for index in range(0, len(rewrites), args.batch_size):
        chunk = rewrites[index:index + args.batch_size]
        (output_dir / f"batch-{index // args.batch_size + 1:04d}.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunk), encoding="utf-8")

    summary = {
        "schema": "limeauto.translation-unit-unification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": args.run_id, "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "mode": "normalize_finish_separator",
        "chain_dirs": [str(Path(p).relative_to(REPO_ROOT)) for p in chain_dirs],
        "decision_rule": "the corpus-standard finish separator is ' - '; glued '-' and '_' are rewritten",
        "counts": dict(stats), "rows_rewritten": len(rewrites),
        "batch_files": len(list(output_dir.glob("batch-*.jsonl"))),
        "refused": len(refused), "refused_examples": refused[:5],
        "rollback": f"delete {run_dir.relative_to(REPO_ROOT)} and remove it from the override order",
        "source_immutable": True,
    }
    summary_path = args.summary or REPO_ROOT / "local-reports/translation-linguistic-pass-20260910" / f"wp5-{args.run_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": args.run_id, "reviewer": "main-process-separator-normalisation-v1",
        "rows_rewritten": len(rewrites), "batch_files": summary["batch_files"],
        "completed_at": summary["generated_at"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("run_id", "run_dir", "counts", "rows_rewritten",
                                              "batch_files", "refused")}, ensure_ascii=False, indent=2))
    print("summary:", summary_path)
    return 0


def title_case(phrase: str) -> str:
    """`e-coat black paint` -> `E-Coat Black Paint` (the audit stores canonical lowercase)."""
    return " ".join(part[:1].upper() + part[1:] for part in phrase.split())


def run_from_findings(args, ledger: dict[str, Any], manifest: list[dict[str, Any]]) -> int:
    """Rewrite the rows an audit flagged so they carry the audit's own canonical wording.

    The finish gate is the audit, so the batch that closes it is driven by the audit's
    findings rather than by a second majority vote: for `finish_variant` and
    `finish_shifted` the row's own trailing phrase is replaced, and for `finish_missing`
    the canonical phrase is appended (checked, counted and reported separately).
    """
    drafts = {str(row["term_id"]): row for row in load_jsonl(args.draft)}
    overrides = load_review_overrides([REPO_ROOT / "translation/reviews" / run / "output" for run in OVERRIDE_RUNS])
    findings = load_jsonl(args.findings)
    run_dir = REPO_ROOT / "translation/reviews" / args.run_id
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    stats: Counter[str] = Counter()
    rewrites: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for finding in findings:
        term_id = str(finding.get("term_id") or "")
        unit = str(finding.get("finish_zh") or "")
        entry = ledger_unit(ledger, unit) or {}
        if entry.get("decision") == "pending_naming_policy":
            stats["skipped_policy"] += 1
            continue
        canonical = title_case(str(finding.get("canonical_en") or ""))
        if not canonical:
            stats["skipped_no_canonical"] += 1
            continue
        if (overrides.get(term_id) or {}).get("decision") == "needs_human":
            stats["skipped_needs_human"] += 1
            continue
        source = str(finding.get("source_text") or "")
        # Use the text the audit actually measured.  Re-reading the corpus through a shorter
        # override chain returns an older revision of the row, and then the phrase the audit
        # flagged cannot be found in it ("occurrence_count 0" refusals).
        text = str(finding.get("translated_text") or "") or effective_text(
            overrides, term_id, str((drafts.get(term_id) or {}).get("translated_text") or ""))
        if not text:
            continue
        rendered = str(finding.get("finish_en") or "")
        if canonical.lower() in text.lower():
            # The row already carries the canonical wording; the audit's tail reader just cut it
            # short (`Off-white 1` -> `white 1`). Replacing would duplicate it on every pass.
            stats["already_canonical"] += 1
            continue
        if rendered and rendered != canonical:
            if text.count(rendered) != 1:
                refused.append({"term_id": term_id, "unit": unit, "reason": "occurrence_count",
                                "rendered": rendered, "occurrences": text.count(rendered),
                                "source_text": source, "translated_text": text})
                stats["refused_ambiguous"] += 1
                continue
            candidate = text.replace(rendered, canonical, 1)
        elif rendered:
            stats["already_canonical"] += 1
            continue
        else:
            if canonical.lower() in text.lower():
                stats["already_canonical"] += 1
                continue
            candidate = f"{text.rstrip()} - {canonical}"
            stats["finish_appended"] += 1
        violations = translation_token_errors(source, candidate, [])
        if violations:
            refused.append({"term_id": term_id, "unit": unit, "reason": "identifier_gate",
                            "violations": violations, "source_text": source, "translated_text": text})
            stats["refused_gate"] += 1
            continue
        rewrites.append({"term_id": term_id, "term_kind": "part", "source_text": source,
                         "previous_text": text, "reviewed_translation": candidate,
                         "decision": "revise", "reviewer": "main-process-finish-canonical-v1",
                         "units": [{"kind": "finish", "unit": unit, "rendered": rendered,
                                    "chosen_en": canonical}]})
        stats["rows_rewritten"] += 1

    for path in sorted(output_dir.glob("batch-*.jsonl")):
        path.unlink()
    for index in range(0, len(rewrites), args.batch_size):
        chunk = rewrites[index:index + args.batch_size]
        (output_dir / f"batch-{index // args.batch_size + 1:04d}.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunk), encoding="utf-8")

    summary = {
        "schema": "limeauto.translation-unit-unification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": args.run_id,
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "driven_by": str(args.findings),
        "decision_rule": "the audit's canonical wording per finish unit; the row's own tail is replaced",
        "counts": dict(stats),
        "rows_rewritten": len(rewrites),
        "batch_files": len(list(output_dir.glob("batch-*.jsonl"))),
        "refused": len(refused),
        "refused_examples": refused[:10],
        "rollback": f"delete {run_dir.relative_to(REPO_ROOT)} and remove it from the override order",
        "source_immutable": True,
    }
    summary_path = args.summary or REPO_ROOT / "local-reports/translation-linguistic-pass-20260910" / f"wp2-{args.run_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": args.run_id, "reviewer": "main-process-finish-canonical-v1",
        "rows_rewritten": len(rewrites), "batch_files": summary["batch_files"],
        "completed_at": summary["generated_at"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("run_id", "run_dir", "counts", "rows_rewritten",
                                              "batch_files", "refused")}, ensure_ascii=False, indent=2))
    print("summary:", summary_path)
    return 0


def _default_chain(exclude: str = "") -> list[Path]:
    """Review-run output dirs to read the current text from, in dependency order.

    `exclude` is the run_id being computed: a stage must never read its own
    output (that would make the result depend on the previous revision of
    itself), and it must not read any stage that comes *after* it in the chain
    unless that stage is declared upstream.  wp3 (term rules) and wp5 (separator
    normalisation) are mutually visible by default and therefore never converge,
    so wp3 skips wp5's dir and wp5 keeps it.
    """
    later = {
        "wp3-term-rules-20260910": {"wp5-separator-normalisation-20260910"},
    }.get(exclude, set())
    skip = set(later) | {exclude}
    return sorted(
        d / "output"
        for d in (REPO_ROOT / "translation/reviews").glob("*20260910")
        if (d / "output").is_dir() and d.name not in skip
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path,
                        default=REPO_ROOT / "translation/glossary/terminology-ledger.json")
    parser.add_argument("--naming-policy", type=Path,
                        default=REPO_ROOT / "translation/glossary/naming-policy.json")
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / RUN / "manifest.jsonl")
    parser.add_argument("--draft", type=Path, default=REPO_ROOT / RUN / "ai_draft.jsonl")
    parser.add_argument("--unit", choices=["finish", "chunk", "both"], default="finish")
    parser.add_argument("--apply-text-rules", action="store_true",
                        help="apply the naming policy's text_rules to the current chain (term corrections "
                             "read out row by row in WP3)")
    parser.add_argument("--normalize-finish-separator", action="store_true",
                        help="WP5: give the finish phrase the corpus-standard separator ' - ' "
                             "when a row glues it with '-' or '_'")
    parser.add_argument("--chain-dir", type=Path, action="append", default=[],
                        help="override chain to read the current text from (default: every review run)")
    parser.add_argument("--policy-sweep", action="store_true",
                        help="for policy-decided units only: replace a known variant anywhere in the row "
                             "when the aligner could not separate it, still bounded and gate-checked")
    parser.add_argument("--include-pending-review", action="store_true",
                        help="also apply the majority wording to units the ledger left pending_review")
    parser.add_argument("--findings", type=Path, default=None,
                        help="drive the batch from an audit findings file instead of the ledger "
                             "(finish rows: rewrite the row's own tail to the audit's canonical)")
    parser.add_argument("--run-id", default="main-process-unification-20260910")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args()

    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    manifest = load_jsonl(args.manifest)
    if args.apply_text_rules:
        # Both stages below default their input chain to every `*20260910` review dir.  Once BOTH
        # exist that makes the pair mutually recursive -- apply_text_rules reads
        # wp5-separator-normalisation's output while wp5 reads this run's output -- so a rebuild
        # flips rows back and forth instead of converging (measured 2026-09-12: a full rebuild
        # cycle changed 7 rows, and re-introduced both the orphan `E-Electrophoretic` prefix and
        # glued finish separators).
        #
        # The dependency is genuinely one-way: wp3's term rules decide the wording, wp5 only
        # normalises the separator in front of a finish it already located.  So the chain must
        # EXCLUDE the stage currently being computed -- and any *later* stage.
        chain = [Path(p) for p in args.chain_dir] or _default_chain(exclude=args.run_id)
        return apply_text_rules(chain, args.naming_policy, args)
    if args.normalize_finish_separator:
        chain = [Path(p) for p in args.chain_dir] or _default_chain(exclude=args.run_id)
        return normalize_finish_separator(chain, ledger, args)
    if args.findings:
        return run_from_findings(args, ledger, manifest)
    drafts = {str(row["term_id"]): row for row in load_jsonl(args.draft)}
    overrides = load_review_overrides([REPO_ROOT / "translation/reviews" / run / "output" for run in OVERRIDE_RUNS])

    run_dir = REPO_ROOT / "translation/reviews" / args.run_id
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    stats: Counter[str] = Counter()
    rewrites: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    policy_pending: Counter[str] = Counter()

    for row in manifest:
        if str(row.get("term_kind")) != "part":
            continue
        term_id = str(row.get("term_id") or "")
        source = str(row.get("source_text") or "")
        if (overrides.get(term_id) or {}).get("decision") == "needs_human":
            stats["skipped_needs_human"] += 1      # a human flag outranks a deterministic rewrite
            continue
        text = effective_text(overrides, term_id, str((drafts.get(term_id) or {}).get("translated_text") or ""))
        if not text:
            continue
        aligned = align_row(source, text)
        # An unaligned row still gets the policy sweep: the aligner refusing to split a row is
        # no reason to leave a decided wording unapplied (`... Sub-Assembly-Yuanshan Dai`).
        units: list[tuple[str, str, str]] = []
        if aligned["route"] is not None and args.unit in ("chunk", "both"):
            units += [("chunk", chunk, rendered) for chunk, rendered in aligned["chunks"]]
        finish = aligned.get("finish") if aligned["route"] is not None else None
        if finish and args.unit in ("finish", "both") and finish[1]:
            units += [("finish", finish[0], finish[1])]

        new_text = text
        row_changes: list[dict[str, str]] = []
        for kind, unit, rendered in units:
            entry = ledger_unit(ledger, unit)
            if entry is None:
                stats["skipped_unknown_unit"] += 1
                continue
            decision = entry["decision"]
            if decision in DECIDED_BY_POLICY:
                policy_pending[unit] += 1
                stats["skipped_policy"] += 1
                continue
            if decision == "pending_review" and not args.include_pending_review:
                stats["skipped_pending_review"] += 1
                continue
            # A unit the ledger left `pending_review` still has a majority rendering; the
            # user's rule is that the majority is the standard, so fall back to it.
            chosen = entry.get("chosen_en") or entry.get("majority_en")
            if not chosen:
                stats["skipped_no_decision"] += 1
                continue
            if rendered == chosen:
                continue
            # A name piece whose rendering itself reads as a finish phrase is mixed evidence:
            # the row's finish was not separated, so unifying the name would delete the finish.
            # Doing exactly that once cost 136 rows their colour (finish audit 14 -> 150).
            if kind == "chunk" and reads_as_finish(rendered) and not _finish_words_subset(rendered, chosen):
                refused.append({"term_id": term_id, "kind": kind, "unit": unit,
                                "rendered": rendered, "chosen_en": chosen,
                                "reason": "chunk_rendering_carries_finish",
                                "source_text": source, "translated_text": text})
                stats["refused_carries_finish"] += 1
                continue
            occurrences = new_text.count(rendered)
            if occurrences != 1:
                refused.append({"term_id": term_id, "kind": kind, "unit": unit,
                                "rendered": rendered, "chosen_en": chosen,
                                "occurrences": occurrences, "source_text": source, "translated_text": text})
                stats["refused_ambiguous"] += 1
                continue
            candidate = new_text.replace(rendered, chosen, 1)
            # Defence in depth: never emit a rewrite that breaks the identifier gate.
            violations = translation_token_errors(source, candidate, [])
            if violations:
                refused.append({"term_id": term_id, "kind": kind, "unit": unit,
                                "rendered": rendered, "chosen_en": chosen,
                                "reason": "identifier_gate", "violations": violations,
                                "source_text": source, "translated_text": text})
                stats["refused_gate"] += 1
                continue
            new_text = candidate
            row_changes.append({"kind": kind, "unit": unit, "rendered": rendered, "chosen_en": chosen})

        if args.policy_sweep and args.unit == "chunk":
            swept_text, swept = policy_sweep(ledger, source, new_text)
            if swept:
                new_text = swept_text
                row_changes += swept

        if row_changes:
            rewrites.append({"term_id": term_id, "term_kind": "part", "source_text": source,
                             "previous_text": text, "reviewed_translation": new_text,
                             "decision": "revise", "reviewer": "main-process-majority-unification-v1",
                             "units": row_changes})
            stats["rows_rewritten"] += 1
            stats["unit_replacements"] += len(row_changes)

    # Batch files in the same shape as the other review runs.
    for path in sorted(output_dir.glob("batch-*.jsonl")):
        path.unlink()
    for index in range(0, len(rewrites), args.batch_size):
        chunk = rewrites[index:index + args.batch_size]
        name = f"batch-{index // args.batch_size + 1:04d}.jsonl"
        (output_dir / name).write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunk), encoding="utf-8")

    summary_path = args.summary or REPO_ROOT / "local-reports/translation-linguistic-pass-20260910" / f"wp2-{args.run_id}.json"
    summary = {
        "schema": "limeauto.translation-unit-unification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": args.run_id,
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "unit_scope": args.unit,
        "include_pending_review": args.include_pending_review,
        "decision_rule": "corpus majority per unit; minority rows rewritten to the majority wording",
        "counts": dict(stats),
        "rows_rewritten": len(rewrites),
        "batch_files": len(list(output_dir.glob("batch-*.jsonl"))),
        "refused": len(refused),
        "refused_gate": stats["refused_gate"],
        "refused_examples": refused[:10],
        "units_left_to_policy": dict(policy_pending.most_common(10)),
        "rollback": f"delete {run_dir.relative_to(REPO_ROOT)} and remove it from the override order",
        "source_immutable": True,
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": args.run_id, "reviewer": "main-process-majority-unification-v1",
        "unit_scope": args.unit, "rows_rewritten": len(rewrites),
        "batch_files": summary["batch_files"], "completed_at": summary["generated_at"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: summary[k] for k in ("run_id", "run_dir", "unit_scope", "counts",
                                              "rows_rewritten", "batch_files", "refused")},
                     ensure_ascii=False, indent=2))
    print("summary:", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
