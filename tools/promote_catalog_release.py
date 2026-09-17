#!/usr/bin/env python3
"""Promote an immutable catalog release from `draft` to `validated`.

This is deliberately the narrowest possible promotion surface. It performs exactly one
transition -- `draft` to `validated` -- on exactly the artifact you name, and it refuses
everything else:

  * any other target status, including `published`;
  * a release whose current status is not `draft`;
  * a release number that does not match --release-no (identity check);
  * an artifact whose sha256 does not match --expect-sha256, so the promotion cannot drift
    onto bytes that no validation evidence describes;
  * a database that does not satisfy the runtime store's own contract.

It never touches the production `current` pointer (which lives outside the database), and it
changes exactly one column: `catalog_releases.status`.

Why the sha256 pin matters: promoting rewrites the artifact, so the hash changes and the
validation evidence bound to the old bytes stops describing it. Pinning the hash proves the
tool promoted the artifact the evidence belongs to, and the report carries both hashes so
the revalidation can be bound to the new one.

Always takes a recovery point first, using the SQLite online backup API rather than a file
copy, because the database may have readers.

Usage
    # inspect only; changes nothing
    .venv/bin/python tools/promote_catalog_release.py \
        --release /path/release.sqlite \
        --release-no rayah-n7-...-20260901 \
        --expect-sha256 bd9be9d8... \
        --approved-by "operator name" \
        --check

    # perform the transition, then re-hash and revalidate
    .venv/bin/python tools/promote_catalog_release.py ... --write --snapshot /path/pre.snapshot
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FROM_STATUS = "draft"
TO_STATUS = "validated"
CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_release(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM catalog_releases").fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise ValueError(f"expected exactly one release row, found {len(rows)}")
    return dict(rows[0])


def table_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("release_models", "system_nodes", "catalog_parts", "fitments")
        }
    finally:
        connection.close()


def make_snapshot(source: Path, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()
    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        quick = str(check.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        check.close()
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "sha256": sha256_file(target),
        "quick_check": quick,
        "restores": quick == "ok" and target.stat().st_size > 0,
    }


def refuse(reason: str) -> int:
    print(json.dumps({"refused": reason}, ensure_ascii=False, indent=1))
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-no", required=True,
                        help="the release_no this promotion is authorized for")
    parser.add_argument("--expect-sha256", required=True,
                        help="sha256 of the artifact being promoted; pins it to its evidence")
    parser.add_argument("--approved-by", required=True,
                        help="recorded in the report as the approval, not written into the database")
    parser.add_argument("--snapshot", type=Path, default=None,
                        help="recovery point path; defaults next to the release")
    parser.add_argument("--report", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="inspect only; change nothing")
    mode.add_argument("--write", action="store_true", help="perform the transition")
    args = parser.parse_args()

    release = args.release.resolve()
    if not release.is_file():
        return refuse(f"release artifact not found: {release}")

    # -- identity ------------------------------------------------------------
    try:
        before = read_release(release)
    except (ValueError, sqlite3.Error) as exc:
        return refuse(f"release metadata is unusable: {exc}")
    current_status = str(before.get("status") or "")
    if str(before.get("release_no") or "") != args.release_no:
        return refuse(
            f"release_no mismatch: artifact says {before.get('release_no')!r}, "
            f"authorization names {args.release_no!r}"
        )

    # -- only draft -> validated --------------------------------------------
    if current_status != FROM_STATUS:
        return refuse(f"release is {current_status!r}; only {FROM_STATUS!r} may be promoted")
    # (the target is fixed by design: there is no --to flag to misuse)

    # -- pin the bytes the evidence describes -------------------------------
    original_sha = sha256_file(release)
    if original_sha != args.expect_sha256:
        return refuse(
            f"artifact sha256 {original_sha} does not match the authorized "
            f"{args.expect_sha256}; refusing to promote bytes no evidence describes"
        )

    counts_before = table_counts(release)

    report: dict[str, Any] = {
        "kind": "limeauto_catalog_release_promotion",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": args.approved_by,
        "release": {
            "path": str(release),
            "release_no": before.get("release_no"),
            "release_id": before.get("release_id"),
            "status_before": current_status,
            "status_after_target": TO_STATUS,
            "sha256_before": original_sha,
            "source_snapshot_fingerprint": before.get("source_snapshot_fingerprint"),
        },
        "counts_before": counts_before,
        "column_changed": "catalog_releases.status",
    }

    if args.check:
        report["mode"] = "check"
        report["would_promote"] = True
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    # -- recovery point ------------------------------------------------------
    snapshot = args.snapshot or release.with_suffix(".pre-promotion.snapshot")
    report["recovery_point"] = make_snapshot(release, snapshot)
    if not report["recovery_point"]["restores"]:
        return refuse("recovery point did not pass quick_check; refusing to promote")

    # -- the single transition ----------------------------------------------
    connection = sqlite3.connect(release)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE catalog_releases SET status = ? WHERE release_id = ? AND status = ?",
            (TO_STATUS, before.get("release_id"), FROM_STATUS),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return refuse(f"expected to change exactly one row, changed {changed}")
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        return refuse(f"database error during promotion: {exc}")
    finally:
        connection.close()

    # -- read back and prove nothing else moved ------------------------------
    after = read_release(release)
    counts_after = table_counts(release)
    drifted = sorted(
        key for key, value in before.items()
        if key != "status" and after.get(key) != value
    )
    report["mode"] = "write"
    report["release_after"] = {
        "status": after.get("status"),
        "sha256": sha256_file(release),
        "bytes": release.stat().st_size,
    }
    report["counts_after"] = counts_after
    report["columns_that_changed_besides_status"] = drifted
    report["checks"] = {
        "status_is_validated": after.get("status") == TO_STATUS,
        "only_status_changed": not drifted,
        "counts_unchanged": counts_before == counts_after,
        "fingerprint_unchanged": after.get("source_snapshot_fingerprint")
        == before.get("source_snapshot_fingerprint"),
    }
    report["promoted"] = all(report["checks"].values())

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))

    # The artifact changed, so every earlier hash-bound claim about it is stale.
    print(
        "\nNEXT: the artifact hash changed; recompute it and re-run the validation bindings "
        "before citing any earlier validation evidence for this release.",
        file=sys.stderr,
    )
    return 0 if report["promoted"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
