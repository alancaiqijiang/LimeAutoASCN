#!/usr/bin/env python3
"""Extract a compact unique-name manifest from an immutable catalog release."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

QUERIES = {
    "series": """
        SELECT series_name_source, COUNT(*) AS usage_count
        FROM release_models
        WHERE release_id = ? AND trim(coalesce(series_name_source, '')) <> ''
        GROUP BY series_name_source
        ORDER BY series_name_source
    """,
    "model": """
        SELECT model_name_source, COUNT(*) AS usage_count
        FROM release_models
        WHERE release_id = ? AND trim(coalesce(model_name_source, '')) <> ''
        GROUP BY model_name_source
        ORDER BY model_name_source
    """,
    "node": """
        SELECT name_source, COUNT(*) AS usage_count
        FROM system_nodes
        WHERE release_id = ? AND trim(coalesce(name_source, '')) <> ''
        GROUP BY name_source
        ORDER BY name_source
    """,
    "part": """
        SELECT description, COUNT(*) AS usage_count
        FROM catalog_parts
        WHERE release_id = ? AND trim(coalesce(description, '')) <> ''
        GROUP BY description
        ORDER BY description
    """,
}


def stable_id(kind: str, source: str) -> str:
    return f"{kind}:{hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.release}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    release = connection.execute(
        "SELECT release_id, release_no, source_snapshot, source_snapshot_fingerprint FROM catalog_releases"
    ).fetchone()
    if release is None:
        raise SystemExit("catalog release metadata is missing")
    rows: list[dict[str, Any]] = []
    for kind, query in QUERIES.items():
        for row in connection.execute(query, (release["release_id"],)):
            source = str(row[0]).strip()
            rows.append(
                {
                    "term_id": stable_id(kind, source),
                    "term_kind": kind,
                    "source_text": source,
                    "usage_count": int(row[1]),
                    "context_samples": [],
                    "source_snapshot": release["release_no"],
                    "source_snapshot_fingerprint": release["source_snapshot_fingerprint"],
                }
            )
    rows.sort(key=lambda row: (row["term_kind"], row["source_text"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "release_no": release["release_no"],
                "source_snapshot_fingerprint": release["source_snapshot_fingerprint"],
                "rows": len(rows),
                "counts": {kind: sum(row["term_kind"] == kind for row in rows) for kind in QUERIES},
                "output": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
