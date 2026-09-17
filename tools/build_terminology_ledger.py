#!/usr/bin/env python3
"""Build the LimeAuto catalog terminology ledger from the corpus itself.

WP1 of `docs/catalog-english-translation-linguistic-pass-plan-20260910.md`.

Why a ledger: the corpus repeats the same Chinese fragments thousands of times
(`深黑5` on 1,144 rows, `六角法兰面螺栓` on 537), so the linguistic review is not 41,207
rows of reading -- it is a few thousand unit decisions applied to rows. The ledger is
where those decisions live, so later rounds read them instead of re-deciding.

Three self-inflicted measurement bugs are fixed here and locked by tests, because each
one produced plausible-looking "corpus defects" that were arithmetic:

* removing every non-Chinese source piece from the translation by substring: a bare `B`
  then ate the first letter of `Bolt Fixed` / `Distribution Box` / `Fuse` (57 invented
  defects). Removal is now **word-boundary anchored**;
* splitting English at every hyphen: `Co-Pilot` -> `Co`, `Self-Made Part` -> `Self`.
  English pieces are split on `_`, on spaced hyphens, but not inside a word;
* cutting the trailing finish at the last hyphen regardless: same damage. The finish cut
  now scans separators right-to-left, rejects a tail that crosses a spaced hyphen, and
  caps the tail length against the source finish it belongs to.

Method (no model, no invented English):

  1. take the **effective** text (`effective_text()` -- never a direct key read);
  2. cut the trailing finish from both sides (source last piece + its English rendering);
  3. remove the source's non-Chinese pieces from the translation by word-boundary match;
  4. align what is left to the Chinese pieces in source order: 1 piece -> the whole
     remaining English body; N pieces -> exactly N English pieces, otherwise the row is
     **unaligned and never counted as read**;
  5. tally renderings per unit, elect the majority, and flag the families that need a rule
     or a human (pinyin vs meaning, no dominant majority, no evidence).

Writes `translation/glossary/terminology-ledger.json` plus a deviation list. Read-only
over the corpus: it rewrites no translation and never marks anything `published`.
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
from tools.audit_translation_integrity import CJK_RE, effective_text, load_review_overrides  # noqa: E402

RUN = "translation/runs/full-mimo-20260902"
REVIEW_RUNS = [
    "translation/reviews/codex-luna-20260910",
    "translation/reviews/main-process-navigation-20260910",
    "translation/reviews/main-process-materials-20260910",
    "translation/reviews/main-process-repairs-20260910",
]

# `，`/`,` separate spec clauses in the source (`镀彩锌，10.9级`) and the translation mirrors
# them, so both sides must split there or the row cannot be aligned at all.
SRC_SPLIT = re.compile(r"[-_－，,]")
# Split English on `_`, on a hyphen that has whitespace on either side, but never inside
# a word: `Co-Pilot`, `Self-Made`, `E-Coat` must survive as one piece.
EN_SPLIT = re.compile(r"\s+-\s*|\s*-\s+|[_－，,]")
SPACE_RUN = re.compile(r"\s{2,}")
LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9]")
PUNCT_ONLY = re.compile(r"^[\W_]+$")
COLOUR_MORPHEMES = set("色米棕橙绿蓝灰黑白红黄金银赭紫粉褐青彩陶")
FINISH_WORDS = (
    "black", "white", "gray", "grey", "blue", "red", "green", "brown", "beige", "orange",
    "gold", "silver", "ochre", "ivory", "ceramic", "pottery", "tan", "cream", "wine", "sand",
    "yellow", "purple", "pink", "stone", "coffee", "jade", "ink", "dusk", "twilight", "crimson",
    "zinc", "plated", "primer", "brushed", "chrome", "matte", "gloss", "glossy", "anodiz",
    "e-coat", "coat", "electrophoretic", "electrophoresis", "tea", "smoke", "mist", "cloud",
    "sky", "bronze", "copper", "emerald", "teal", "navy", "khaki", "paint", "scarlet", "sunset",
    "gravel", "oat", "oatmeal", "cardamom", "tahiti", "tahitian", "coral", "sardine", "reef",
    "terracotta", "halberd", "hutong", "french", "mysterious", "dawn", "lime", "porcelain",
    "pearl", "platinum", "titanium", "titan", "amber", "cocoa", "caramel", "charcoal",
    "graphite", "gunmetal", "camel", "mustard", "olive", "mint", "azure", "cobalt", "indigo",
    "violet", "lavender", "lilac", "mauve", "plum", "berry", "rust", "brass", "nickel",
    "champagne", "steel", "shimmer", "turkish", "piano", "moist", "arber", "arbor", "bodhi",
    "eclipse", "mousse", "knight", "hyacinth", "delan", "satin", "leather",
    # Romanised colour names actually present in the corpus.  Without them a row such as
    # `第三排左座椅分装总成-润米` -> `... Sub-Assembly - Runmi` keeps the finish glued to the
    # name, and the pinyin then shows up as a name-body rendering.
    "runmi", "run mi", "ruyao", "ru kiln", "chixi", "chi xi", "xuantian", "mushan",
    "xuankong", "xuan kong", "qianshan", "pergamino", "nuan se", "yao mi",
)

MIN_ROWS_FOR_DECISION = 2
DOMINANT_SHARE = 0.9
MAX_FINISH_LEN = 10
# The finish phrase can be long (`哑光钛银` -> `Matte Titanium Silver`, 21 chars), so the
# budget has to clear a four-character colour name.  The real guard against swallowing the
# name is the head piece-count check, not this length cap.
MIN_TAIL_BUDGET = 30
TAIL_BUDGET_FACTOR = 4


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def src_pieces(text: str) -> list[str]:
    return [piece.strip() for piece in SRC_SPLIT.split(text)]


def en_pieces(text: str) -> list[str]:
    return [piece.strip() for piece in EN_SPLIT.split(text) if piece.strip()]


def is_chinese(piece: str) -> bool:
    return bool(CJK_RE.search(piece))


MATERIAL_NOUN = re.compile(r"(金属|合金|毫米|金刚|金具)")


def is_finish(piece: str) -> bool:
    """A trailing colour/finish segment, not a part name that merely contains 金/银."""
    piece = piece.strip()
    if not piece or len(piece) > MAX_FINISH_LEN or not is_chinese(piece):
        return False
    if MATERIAL_NOUN.search(piece):
        return False
    return any(char in COLOUR_MORPHEMES for char in piece)


def _finish_vocabulary(words):
    """Split into single tokens and multi-word phrases.

    Tokens are matched individually, so a hyphenated entry must contribute its parts and the
    tokeniser must not treat `assembly-beige` as one word -- that is how `Beige 10` stopped
    counting as a finish word altogether.
    """
    tokens, phrases = set(), []
    for word in words:
        if " " in word:
            phrases.append(word)
        else:
            tokens.update(part for part in word.split("-") if part)
    return frozenset(tokens), tuple(phrases)


FINISH_WORD_SET, FINISH_PHRASES = _finish_vocabulary(FINISH_WORDS)


def reads_as_finish(piece: str) -> bool:
    """Does this fragment read as a colour/finish phrase rather than part wording?

    Word match, not substring: `tan` inside `Distant` made `Distant Mountain Dai` look like a
    finish, so the writer refused 113 rows it should have unified.
    """
    low = re.sub(r"\s+", " ", piece.strip().lower())
    if not low or not re.search(r"[a-z]", low):
        return False
    if set(re.findall(r"[a-z0-9]+", low)) & FINISH_WORD_SET:
        return True
    return any(phrase in low for phrase in FINISH_PHRASES)


def remove_verbatim(text: str, pieces: list[str]) -> str:
    """Drop the source's non-Chinese pieces, anchored on word boundaries.

    A plain ``str.replace`` is what invented 57 defects: deleting the source piece `B`
    from `Distribution Box` leaves `istribution ox`. The lookarounds make the piece match
    only when it stands alone, so `B Bolt Fixed` loses its `B` while `Bolt Fixed` is safe.
    """
    out = text
    for piece in sorted({p for p in pieces if p and LATIN_OR_DIGIT.search(p)}, key=len, reverse=True):
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(piece) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        out = pattern.sub(" ", out)
    return SPACE_RUN.sub(" ", out).strip(" -_－").strip()


def cut_finish(text: str, finish_zh: str, name_piece_count: int, verbatim: list[str]) -> tuple[str, str]:
    """Split the trailing English finish off, or return ('', '') when it cannot be found.

    The head is measured *after* the identifiers/specs are removed, otherwise a row such as
    `防护板_VE8_280VK_黑色` counts three English pieces in front of its finish and the cut is
    refused even though the name is a single piece.
    """
    budget = max(MIN_TAIL_BUDGET, TAIL_BUDGET_FACTOR * len(finish_zh))
    for match in reversed(list(re.finditer(r"[-_－]", text))):
        head = text[: match.start()].strip()
        tail = text[match.end():].strip()
        if not head or not tail or len(tail) > budget:
            continue
        if re.search(r"\s[-_－]\s", tail):      # the tail crossed a spaced separator: not a finish
            continue
        if not reads_as_finish(tail):
            continue
        # A finish phrase never carries the part's own identifier or spec.  Without this,
        # `... Assembly_M5×16_Black` was read as name + finish `M5×16_Black`, and unifying
        # the finish then deleted the `M5×16` spec from the row.
        if any(piece and piece.lower() in tail.lower() for piece in verbatim):
            continue
        if len(en_pieces(remove_verbatim(head, verbatim))) != name_piece_count:
            continue
        return head, tail
    return "", ""


def align_row(source: str, text: str) -> dict[str, Any]:
    """Line up each Chinese source piece with the English text that renders it."""
    pieces = src_pieces(source)
    finish_zh = pieces[-1] if pieces and is_finish(pieces[-1]) else ""
    name_src = pieces[:-1] if finish_zh else pieces
    zh = [piece for piece in name_src if is_chinese(piece) and not PUNCT_ONLY.match(piece)]
    verbatim = [piece for piece in name_src if not is_chinese(piece)]

    finish = (finish_zh, "") if finish_zh else None
    work, finish_en = text, ""
    if finish_zh:
        cut_head, cut_tail = cut_finish(text, finish_zh, len(zh), verbatim)
        if cut_head:
            work, finish_en = cut_head, cut_tail
            finish = (finish_zh, finish_en)

    work = remove_verbatim(work, verbatim)

    if not zh:
        return {"route": None, "reason": "no_chinese_piece", "finish": finish}
    if not work:
        return {"route": None, "reason": "nothing_left_after_verbatim_removal", "finish": finish}

    parts = en_pieces(work)
    if len(zh) == 1:
        pairs, route = [(zh[0], work)], "A"
    elif len(parts) == len(zh):
        pairs, route = list(zip(zh, parts)), "B"
    else:
        return {"route": None, "reason": "piece_count_mismatch", "zh_pieces": len(zh),
                "en_pieces": len(parts), "finish": finish}

    return {"route": route, "chunks": [(src, en) for src, en in pairs if not is_finish(src)],
            "finish": finish}


# Letter clusters that occur in romanised Chinese but not in ordinary English part/colour
# wording.  An earlier version also flagged any single English word it did not recognise,
# which put `装配 -> Assembled`, `钢 -> Steel` and `高音喇叭 -> Tweeter` into
# "pending_naming_policy" -- a category that must mean "romanisation vs meaning", nothing else.
PINYIN_CLUSTERS = ("zh", "yu", "qi", "qu", "xi", "xu", "uan", "uai", "iong", "iao",
                   "runmi", "cui", "qian", "shan", "xuan", "zong")


def looks_pinyin(text: str) -> bool:
    """Heuristic: a Latin rendering that reads as romanised Chinese rather than English.

    The cluster must open a word: `Xuan Kong`, `Yuanshan Dai`, `Runmi` are romanisations,
    while `Fixing Bracket` and `Oxide` merely contain the same letters.
    """
    words = [word.lower() for word in re.findall(r"[A-Za-z]+", text)]
    return any(word.startswith(cluster) for word in words for cluster in PINYIN_CLUSTERS)


def ledger_unit(ledger: dict[str, Any], unit: str) -> dict[str, Any] | None:
    """Look a unit up in either ledger section (full entry, whatever its shape)."""
    entry = (ledger.get("chunks") or {}).get(unit) or (ledger.get("finishes") or {}).get(unit)
    if entry:
        return entry
    single = (ledger.get("chunks_single_occurrence") or {}).get(unit)
    if single:
        return {"rows": single[1], "voted_rows": single[1],
                "renderings": {single[0]: single[1]}, "majority_en": single[0],
                "majority_rows": single[1], "majority_share": 1.0,
                "flags": ["single_occurrence"], "decision": "single_occurrence",
                "chosen_en": single[0], "evidence": "one occurrence", "decided_at": None}
    return None


def load_naming_policy(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(unit): dict(entry) for unit, entry in (data.get("decisions") or {}).items()}


def decide(votes: Counter[str], rows: int, policy: dict[str, Any] | None = None,
           policy_source: str = "") -> dict[str, Any]:
    ranked = votes.most_common()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    if not ranked:
        # No aligned rendering, but a naming policy decision still stands: the rows carry it
        # even when this build cannot re-derive it (identifier glued to the finish, etc.).
        if policy:
            return {"rows": rows, "voted_rows": 0, "renderings": {}, "majority_en": "",
                    "majority_rows": 0, "majority_share": 0.0,
                    "flags": ["no_render_evidence", "policy_override"],
                    "decision": "unify", "chosen_en": str(policy.get("chosen_en") or "").strip(),
                    "evidence": f"{policy.get('reason', '')} (policy: {policy_source})".strip(),
                    "decided_at": stamp}
        return {"rows": rows, "voted_rows": 0, "renderings": {}, "majority_en": "",
                "majority_rows": 0, "majority_share": 0.0, "flags": ["no_render_evidence"],
                "decision": "pending_review", "chosen_en": None,
                "evidence": "no aligned rendering found", "decided_at": None}
    top, top_rows = ranked[0]
    voted = sum(count for _, count in ranked)
    flags: list[str] = []
    if rows < MIN_ROWS_FOR_DECISION:
        flags.append("single_occurrence")
    if len(ranked) > 1:
        flags.append("competitive")
        if top_rows / max(voted, 1) < DOMINANT_SHARE:
            flags.append("no_dominant_majority")
    if len(ranked) > 1 and looks_pinyin(top) and any(not looks_pinyin(en) for en, _ in ranked[1:]):
        flags.append("pinyin_majority_with_meaning_minority")
    if all(looks_pinyin(en) for en, _ in ranked) and len(ranked) > 1:
        flags.append("pinyin_only")
    decision = "unify"
    if "single_occurrence" in flags:
        decision = "single_occurrence"
    elif "pinyin_majority_with_meaning_minority" in flags:
        decision = "pending_naming_policy"
    elif "no_dominant_majority" in flags:
        decision = "pending_review"
    chosen = top if decision == "unify" else None
    evidence = f"corpus majority {top_rows}/{voted} aligned rows of {rows}"
    decided_at = stamp if decision == "unify" else None
    if policy:
        # A business decision outranks the corpus vote; the reason and the corpus evidence
        # stay next to it so a later round can re-open it without guessing.
        chosen = str(policy.get("chosen_en") or "").strip() or chosen
        decision = "unify"
        flags = [f for f in flags if f not in {"no_dominant_majority"}] + ["policy_override"]
        evidence = f"{policy.get('reason', '')} (policy: {policy_source}; corpus evidence {policy.get('corpus_evidence')})".strip()
        decided_at = stamp
    return {
        "rows": rows,
        "voted_rows": voted,
        "renderings": dict(ranked),
        "majority_en": top,
        "majority_rows": top_rows,
        "majority_share": round(top_rows / voted, 4) if voted else 0.0,
        "flags": flags,
        "decision": decision,
        "chosen_en": chosen,
        "evidence": evidence,
        "decided_at": decided_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / RUN / "manifest.jsonl")
    parser.add_argument("--draft", type=Path, default=REPO_ROOT / RUN / "ai_draft.jsonl")
    parser.add_argument("--review-dir", type=Path, action="append",
                        default=[REPO_ROOT / run / "output" for run in REVIEW_RUNS])
    parser.add_argument("--naming-policy", type=Path,
                        default=REPO_ROOT / "translation/glossary/naming-policy.json",
                        help="units whose wording is decided by policy rather than by the corpus majority")
    parser.add_argument("--extra-review-dir", type=Path, action="append", default=[],
                        help="additional override directories, applied after the base review runs")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "translation/glossary/terminology-ledger.json")
    parser.add_argument("--deviations", type=Path,
                        default=REPO_ROOT / "local-reports/translation-linguistic-pass-20260910/wp1-deviations.jsonl")
    parser.add_argument("--coverage", type=Path,
                        default=REPO_ROOT / "local-reports/translation-linguistic-pass-20260910/coverage-ledger.jsonl",
                        help="one line per manifest row: which review level it has reached")
    args = parser.parse_args()

    manifest = load_jsonl(args.manifest)
    drafts = {str(row["term_id"]): row for row in load_jsonl(args.draft)}
    override_dirs = [Path(p) for p in args.review_dir] + [Path(p) for p in args.extra_review_dir]
    overrides = load_review_overrides(override_dirs)

    chunk_votes: dict[str, Counter[str]] = defaultdict(Counter)
    finish_votes: dict[str, Counter[str]] = defaultdict(Counter)
    chunk_rows: Counter[str] = Counter()
    finish_rows: Counter[str] = Counter()
    stats: Counter[str] = Counter()
    records: list[dict[str, Any]] = []

    for row in manifest:
        if str(row.get("term_kind")) != "part":
            continue
        term_id = str(row.get("term_id") or "")
        source = str(row.get("source_text") or "")
        text = effective_text(overrides, term_id, str((drafts.get(term_id) or {}).get("translated_text") or ""))
        result = align_row(source, text)
        stats[f"route_{result['route'] or 'none'}_rows"] += 1
        if result["route"] is None:
            stats[f"unaligned::{result.get('reason')}"] += 1
        else:
            for chunk, rendered in result["chunks"]:
                chunk_rows[chunk] += 1
                if rendered and not PUNCT_ONLY.match(rendered):
                    chunk_votes[chunk][rendered] += 1
                else:
                    stats["chunk_without_usable_rendering"] += 1
        finish = result.get("finish")
        if finish:
            finish_rows[finish[0]] += 1
            if finish[1] and not PUNCT_ONLY.match(finish[1]):
                finish_votes[finish[0]][finish[1]] += 1
            else:
                stats["finish_without_rendering"] += 1
        records.append({"term_id": term_id, "route": result["route"], "source_text": source,
                        "translated_text": text,
                        "chunks": [c for c, _ in result.get("chunks") or []],
                        "rendered": [r for _, r in result.get("chunks") or []],
                        "finish_zh": (finish or ("", ""))[0], "finish_en": (finish or ("", ""))[1]})

    policy = load_naming_policy(args.naming_policy)
    policy_source = str(args.naming_policy.relative_to(REPO_ROOT)) if args.naming_policy else ""
    chunks_out = {chunk: decide(chunk_votes[chunk], rows, policy.get(chunk), policy_source)
                  for chunk, rows in chunk_rows.items()}
    finishes_out = {finish: decide(finish_votes[finish], rows, policy.get(finish), policy_source)
                    for finish, rows in finish_rows.items()}

    ledger = {
        "schema": "limeauto.terminology-ledger.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_run": RUN,
        "effective_text_entrypoint": "tools.audit_translation_integrity.effective_text",
        "override_order": [str(p) for p in override_dirs],
        "method": {
            "route_a": "one Chinese source piece -> the whole remaining English body renders it",
            "route_b": "N Chinese pieces and exactly N English pieces -> aligned in source order",
            "finish_cut": "source last piece plus its English tail, scanned right-to-left with a length budget",
            "verbatim_removal": "source pieces without Chinese are removed from the translation by word-boundary match",
            "naming_policy": "docs/catalog-english-translation-linguistic-pass-plan-20260910.md WP0.3",
        },
        "alignment_stats": dict(stats),
        "chunk_count": len(chunks_out),
        "finish_count": len(finishes_out),
        # Units seen once carry no unification decision, so they are stored compactly:
        # `{zh: [chosen_en, rows]}`.  Read them through `ledger_unit()` like the rest.
        "chunks": dict(sorted(((k, v) for k, v in chunks_out.items() if v["rows"] >= MIN_ROWS_FOR_DECISION),
                              key=lambda kv: -kv[1]["rows"])),
        "chunks_single_occurrence": {k: [v["chosen_en"], v["rows"]]
                                     for k, v in chunks_out.items() if v["rows"] < MIN_ROWS_FOR_DECISION},
        "finishes": dict(sorted(finishes_out.items(), key=lambda kv: -kv[1]["rows"])),
        "summary": {
            "chunks_unify": sum(1 for e in chunks_out.values() if e["decision"] == "unify"),
            "chunks_single_occurrence": sum(1 for e in chunks_out.values() if e["decision"] == "single_occurrence"),
            "chunks_pending_naming_policy": sum(1 for e in chunks_out.values() if e["decision"] == "pending_naming_policy"),
            "chunks_pending_review": sum(1 for e in chunks_out.values() if e["decision"] == "pending_review"),
            "chunk_row_hits": sum(e["rows"] for e in chunks_out.values()),
            "chunk_rows_voted": sum(e["voted_rows"] for e in chunks_out.values()),
            "finishes_total": len(finishes_out),
            "finishes_pending_review": sum(1 for e in finishes_out.values() if e["decision"] == "pending_review"),
            "finishes_without_rendering": stats["finish_without_rendering"],
            "policy_override_units": sum(1 for e in list(chunks_out.values()) + list(finishes_out.values())
                                         if "policy_override" in e["flags"]),
        },
        "naming_policy": {"path": str(args.naming_policy) if args.naming_policy else "", "units": sorted(policy)},
        "origin": {"manifest": str(args.manifest), "draft": str(args.draft), "manifest_rows": len(manifest)},
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_name(f".{args.out.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, args.out)

    args.deviations.parent.mkdir(parents=True, exist_ok=True)
    flagged = []
    for record in records:
        for chunk, rendered in zip(record["chunks"], record["rendered"]):
            chosen = (chunks_out.get(chunk) or {}).get("chosen_en")
            if chosen and rendered != chosen:
                flagged.append({"term_id": record["term_id"], "unit": "chunk", "chunk": chunk,
                                "rendered": rendered, "chosen_en": chosen,
                                "source_text": record["source_text"], "translated_text": record["translated_text"]})
        chosen_finish = (finishes_out.get(record["finish_zh"]) or {}).get("chosen_en")
        if chosen_finish and record["finish_en"] and record["finish_en"] != chosen_finish:
            flagged.append({"term_id": record["term_id"], "unit": "finish", "chunk": record["finish_zh"],
                            "rendered": record["finish_en"], "chosen_en": chosen_finish,
                            "source_text": record["source_text"], "translated_text": record["translated_text"]})
    args.deviations.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in flagged), encoding="utf-8")

    # Coverage ledger (plan WP0.4): every row gets exactly one level, and a row that could
    # not be aligned is recorded as L1 -- it must never be counted as linguistically read.
    args.coverage.parent.mkdir(parents=True, exist_ok=True)
    by_id = {record["term_id"]: record for record in records}
    levels: Counter[str] = Counter()
    tmp_coverage = args.coverage.with_name(f".{args.coverage.name}.{os.getpid()}.tmp")
    with tmp_coverage.open("w", encoding="utf-8") as handle:
        for row in manifest:
            term_id = str(row.get("term_id") or "")
            kind = str(row.get("term_kind"))
            if kind != "part":
                entry = {"term_id": term_id, "term_kind": kind, "level": "L1_navigation",
                         "route": None, "units": []}
            else:
                record = by_id.get(term_id) or {}
                units = record.get("chunks") or []
                if record.get("finish_zh"):
                    units = units + [record["finish_zh"]]
                aligned = bool(record.get("route"))
                entry = {"term_id": term_id, "term_kind": kind,
                         "level": "L2" if aligned else "L1",
                         "route": record.get("route"), "units": units}
            levels[entry["level"]] += 1
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp_coverage, args.coverage)

    print(json.dumps({
        "ledger": str(args.out),
        "chunks": ledger["chunk_count"],
        "finishes": ledger["finish_count"],
        "alignment_stats": ledger["alignment_stats"],
        "summary": ledger["summary"],
        "deviations": len(flagged),
        "deviations_file": str(args.deviations),
        "coverage": dict(levels),
        "coverage_file": str(args.coverage),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
