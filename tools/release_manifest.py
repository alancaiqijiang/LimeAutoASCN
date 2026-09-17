#!/usr/bin/env python3
"""Emit the release manifest: every artifact a LimeAuto release is made of, by hash.

Why this exists: the catalogue is assembled from a code commit plus several artifacts that
are not all in Git (a 5.8 GB release, an 86 GB asset root, a runtime glossary, a thumbnail
map, an aftercare database, and corpora kept out of the repository on purpose). Without one
record naming all of them by hash and location, "which release is live" and "can the old
validator evidence be reused" are unanswerable.

The tool is read-only. It hashes the small artifacts always, and hashes the release only
when asked, because the release is ~5.8 GB and the target host is resource-limited:

    --hash-release     stream the release through sha256 (minutes, not seconds)
    --asset-sample N   stat N files spread across the asset root instead of walking 86 GB

Asset contents are never claimed to be fully re-verified here; the manifest says what it
actually checked, and reuses a prior verified count with an explicit provenance line when
one is supplied.

Usage
    .venv/bin/python tools/release_manifest.py \
        --release /path/release.sqlite \
        --glossary /path/catalog_translation_en.sqlite \
        --asset-root /path/assets \
        --thumbnail-map /path/series-map.json \
        --aftercare /path/aftercare.sqlite3 \
        --artifacts translation/glossary/terminology-ledger.json \
        --out docs/release-manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def describe_file(path: Path, *, digest: bool = True) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        return {"path": str(path), "exists": False, "error": type(exc).__name__}
    info: dict[str, Any] = {
        "path": str(path),
        "exists": True,
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
    if digest:
        info["sha256"] = sha256_file(path)
    return info


def schema_fingerprint(connection: sqlite3.Connection) -> dict[str, Any]:
    """A stable description of the schema, so a release can name the shape it needs."""
    tables: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        name = str(row[0])
        tables[name] = [str(r[1]) for r in connection.execute(f'PRAGMA table_info("{name}")')]
    encoded = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "tables": len(tables),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "columns": {name: len(cols) for name, cols in tables.items()},
    }


def describe_release(path: Path, *, digest: bool) -> dict[str, Any]:
    info = describe_file(path, digest=digest)
    if not info.get("exists"):
        return info
    with read_only(path) as connection:
        row = connection.execute("SELECT * FROM catalog_releases").fetchone()
        info["release"] = {
            key: row[key]
            for key in (
                "release_id",
                "release_no",
                "status",
                "schema_version",
                "source_snapshot_fingerprint",
            )
            if key in row.keys()
        }
        if digest:
            info["counts"] = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "release_models",
                    "system_nodes",
                    "catalog_parts",
                    "fitments",
                    "catalog_assets",
                )
            }
    return info


def describe_glossary(path: Path, release_fingerprint: str) -> dict[str, Any]:
    info = describe_file(path)
    if not info.get("exists"):
        return info
    with read_only(path) as connection:
        fingerprint_row = connection.execute(
            "SELECT value FROM translation_meta WHERE key = 'source_snapshot_fingerprint'"
        ).fetchone()
        fingerprint = str(fingerprint_row[0]) if fingerprint_row else ""
        info["source_snapshot_fingerprint"] = fingerprint
        info["rows"] = int(connection.execute("SELECT COUNT(*) FROM translation_terms").fetchone()[0])
        info["published"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM translation_terms WHERE status = 'published'"
            ).fetchone()[0]
        )
        info["kinds"] = {
            str(r[0]): int(r[1])
            for r in connection.execute(
                "SELECT term_kind, COUNT(*) FROM translation_terms GROUP BY term_kind"
            )
        }
    # The runtime only speaks English while these agree; record the answer, not the hope.
    info["matches_release_fingerprint"] = bool(
        fingerprint and release_fingerprint and fingerprint == release_fingerprint
    )
    return info


def describe_assets(root: Path, sample: int) -> dict[str, Any]:
    if not root.is_dir():
        return {"path": str(root), "exists": False}
    info: dict[str, Any] = {"path": str(root), "exists": True, "contents_reverified": False}
    if sample <= 0:
        return info
    # Bounded: stat `sample` files spread across the tree rather than walking 86 GB.
    popped: list[tuple[int, str, int]] = []
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            total += 1
            popped.append((total, os.path.join(dirpath, name), 0))
            if total >= sample * 40:  # cap the walk itself
                break
        if total >= sample * 40:
            break
    step = max(1, len(popped) // sample) if popped else 1
    picked = popped[::step][:sample]
    ok = 0
    for _index, file_path, _unused in picked:
        try:
            if os.path.getsize(file_path) > 0:
                ok += 1
        except OSError:
            pass
    info.update(
        {
            "walked_files_capped_at": sample * 40,
            "walked_files": total,
            "sampled": len(picked),
            "sampled_nonempty": ok,
            "note": "bounded sample only; the full asset tree is verified by the "
            "release validator, not by this manifest",
        }
    )
    return info


def describe_thumbnail_map(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"configured": False}
    info = describe_file(path)
    if info.get("exists"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            series = payload.get("series") if isinstance(payload, dict) else None
            info["series_entries"] = len(series) if isinstance(series, dict) else 0
        except (OSError, ValueError):
            info["series_entries"] = None
    return info


def describe_aftercare(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"configured": False}
    info = describe_file(path)
    if not info.get("exists"):
        return info
    with read_only(path) as connection:
        info["quick_check"] = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        info["schema"] = schema_fingerprint(connection)
        info["rows"] = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("staff_users", "vehicles", "maintenance_records")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        }
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--glossary", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, default=None)
    parser.add_argument("--thumbnail-map", type=Path, default=None)
    parser.add_argument("--aftercare", type=Path, default=None)
    parser.add_argument("--artifacts", type=Path, nargs="*", default=[],
                        help="extra files (often untracked corpora) to record by hash")
    parser.add_argument("--code-commit", default="", help="git commit of the running code")
    parser.add_argument("--code-clean", choices=["yes", "no", "unknown"], default="unknown")
    parser.add_argument("--release-environment", default="", help="e.g. internal-staging")
    parser.add_argument("--hash-release", action="store_true",
                        help="stream the ~5.8 GB release through sha256 (slow)")
    parser.add_argument("--asset-sample", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if not args.release.is_file():
        print(json.dumps({"error": f"missing release {args.release}"}, ensure_ascii=False))
        return 2
    if not args.glossary.is_file():
        print(json.dumps({"error": f"missing glossary {args.glossary}"}, ensure_ascii=False))
        return 2

    release = describe_release(args.release, digest=args.hash_release)
    fingerprint = str((release.get("release") or {}).get("source_snapshot_fingerprint") or "")
    manifest = {
        "kind": "limeauto_release_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": args.release_environment,
        "code": {"commit": args.code_commit, "worktree_clean": args.code_clean},
        "release": release,
        "glossary": describe_glossary(args.glossary, fingerprint),
        "assets": describe_assets(args.asset_root, args.asset_sample) if args.asset_root else {"configured": False},
        "thumbnail_map": describe_thumbnail_map(args.thumbnail_map),
        "aftercare": describe_aftercare(args.aftercare),
        "artifacts": [describe_file(path) for path in args.artifacts],
        "consistent": None,
    }
    manifest["consistent"] = bool(
        release.get("exists") and manifest["glossary"].get("matches_release_fingerprint")
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    return 0 if manifest["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
