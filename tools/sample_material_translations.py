#!/usr/bin/env python3
"""Draw a reproducible stratified sample of material (part) rows for linguistic review.

Reads the immutable manifest, the AI draft and every review run, resolves the
*effective* translation that the runtime would show (repairs last, later runs
override earlier ones), classifies each material row into one material class,
then draws a seeded sample of N rows per class.

Read-only: touches nothing but its own output directory.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO))

from tools.audit_translation_integrity import effective_text, load_review_overrides  # noqa: E402

# --- material classes -------------------------------------------------------
# First match wins, so the order below is the priority order.
CODE_LED = re.compile(r"^[A-Za-z]{1,4}[A-Za-z0-9]*[-_][A-Za-z0-9-]{2,}")
HYPHEN_CODE_ANY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,5}[0-9][A-Za-z0-9]*-[A-Za-z0-9-]{3,})")
COLOUR_MORPHEMES = set("色米棕橙绿蓝灰黑白红黄金银赭紫粉褐青彩黛陶")
DIMENSION = re.compile(r"(\d+(?:\.\d+)?\s*[×x*]\s*\d+|\d+(?:\.\d+)?\s*(?:mm|MM|cm|寸|mAh|mA|kg|KM|km|kW|KW|kw|W|V|A|L|G|GB|T)\b|[°Ω])")
MATERIAL_CODE = re.compile(
    r"(?<![A-Za-z0-9])(PP|PE|ABS|PC|PA|PA6|PA66|POM|PVC|PU|PUR|EPDM|NBR|FKM|FPM|SIL|SMT|PCBA|PCB|"
    r"LED|LCD|NFC|EPS|DMS|ISOFIX|USB|GPS|TPMS|OBD|CAN|EVA|TPU|TPE|BMC|GFRP|CFRP|"
    r"T1|T2|T5|SUS|Q235|40Cr|8\.8|10\.9)(?![A-Za-z0-9])"
)
ASSEMBLY_TERM = re.compile(
    r"(总成|组件|支架|罩盖|护板|护面|线束|传感器|开关|卡扣|螺栓|螺母|垫圈|密封|轴承|弹簧|"
    r"齿轮|电机|灯|饰板|堵盖|盖板|托架|加强板|隔音|隔热|铰链|锁|拉手|踏板|把手|钳|泵|阀|管|束)"
)
LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
UNDERSCORE = re.compile(r"_")


def material_class(source: str) -> str:
    s = source.strip()
    ascii_only = re.fullmatch(r"[\x00-\x7f]+", s) is not None
    if CODE_LED.match(s):
        return "01_编号开头件"
    if HYPHEN_CODE_ANY.search(s):
        return "02_编号嵌中件"
    if any(ch in COLOUR_MORPHEMES for ch in s) and re.search(r"(色|米|棕|橙|绿|蓝|灰|黑|白|红|金|银|赭)", s):
        return "03_颜色饰面件"
    if DIMENSION.search(s):
        return "04_尺寸规格件"
    if s.count("_") >= 2:
        return "05_多段下划线件"
    if MATERIAL_CODE.search(s):
        return "06_材质缩写件"
    if ASSEMBLY_TERM.search(s):
        return "07_结构总成件"
    if ascii_only:
        return "08_纯拉丁件"
    if not re.search(r"[A-Za-z0-9]", s):
        return "09_纯中文件"
    if LATIN_WORD.search(s):
        return "10_中英混排件"
    return "11_其他"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--draft", required=True)
    ap.add_argument("--review-dir", action="append", default=[], required=True)
    ap.add_argument("--output", required=True, help="output directory")
    ap.add_argument("--per-class", type=int, default=15)
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--min-class-size", type=int, default=200)
    args = ap.parse_args()

    manifest = [json.loads(x) for x in Path(args.manifest).read_text(encoding="utf-8").splitlines() if x.strip()]
    drafts = {}
    for x in Path(args.draft).read_text(encoding="utf-8").splitlines():
        if x.strip():
            row = json.loads(x)
            drafts[row["term_id"]] = row
    overrides = load_review_overrides([Path(p) for p in args.review_dir])

    rows = []
    for m in manifest:
        if m.get("term_kind") != "part":
            continue
        tid = m["term_id"]
        d = drafts.get(tid, {})
        ov = overrides.get(tid)
        draft_text = d.get("translated_text") or ""
        reviewed = effective_text(ov, tid)
        text, origin = (reviewed, "reviewed") if reviewed else (draft_text, "draft")
        rows.append(
            {
                "term_id": tid,
                "source_text": m["source_text"],
                "translated_text": text,
                "text_origin": origin,
                "usage_count": m.get("usage_count"),
                "material_class": material_class(m["source_text"]),
            }
        )

    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_class[r["material_class"]].append(r)

    sizes = Counter({k: len(v) for k, v in by_class.items()})
    eligible = sorted(k for k, v in sizes.items() if v >= args.min_class_size)

    rng = random.Random(args.seed)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    sample: list[dict[str, Any]] = []
    rounds = []
    for i, cls in enumerate(eligible[: args.rounds], start=1):
        pool = sorted(by_class[cls], key=lambda r: r["term_id"])
        picked = rng.sample(pool, args.per_class)
        for r in picked:
            r = dict(r)
            r["round"] = i
            r["pool_size"] = len(pool)
            sample.append(r)
        rnd = out / f"round-{i:02d}.jsonl"
        rnd.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in picked), encoding="utf-8")
        rounds.append({"round": i, "material_class": cls, "pool_size": len(pool), "drawn": len(picked)})
        print(f"round {i:02d}  {cls:16s} pool={len(pool):6d} drawn={len(picked)}")

    (out / "sample-all.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sample), encoding="utf-8"
    )
    meta = {
        "schema": "limeauto.material-linguistic-sample.v1",
        "seed": args.seed,
        "per_class": args.per_class,
        "material_rows_total": len(rows),
        "class_sizes": dict(sizes.most_common()),
        "stata_eligible": eligible,
        "rounds": rounds,
        "sample_size": len(sample),
        "text_basis": "effective translation (AI draft overridden by review runs, repairs last)",
        "unique_term_ids": len({r["term_id"] for r in sample}),
    }
    (out / "sample-meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sample_size": len(sample), "unique": meta["unique_term_ids"], "classes": len(rounds)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
