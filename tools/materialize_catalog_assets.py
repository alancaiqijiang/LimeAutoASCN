#!/usr/bin/env python3
"""Materialize the immutable asset tree for one SQLite catalog release.

The release artifact is read-only.  qpren is queried only for image rows bound
to material codes present in that artifact, and the PostgreSQL session is
explicitly read-only.  This tool never updates a release status or a current
release pointer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_catalog_release import (  # noqa: E402
    QPREN_ASSET_PREFIX,
    asset_identity,
)

try:  # Optional so pure filesystem helpers remain importable without psycopg.
    import psycopg  # type: ignore[import-not-found]
    from psycopg.rows import dict_row  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the execution environment
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
ASSET_TYPES = frozenset({"material_image", "epc_drawing", "thumbnail", "other"})
ASSET_STATUSES = frozenset(
    {"pending", "ready", "done", "available", "failed", "missing"}
)
ASSET_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._~+%\-]*$")
OBJECT_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._~+%\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~+%\-]*)*$"
)
RELEASE_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~\-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MaterializerError(Exception):
    """An expected, reportable materialization failure."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class AssetSpec:
    """The release fields needed to materialize one asset."""

    asset_key: str
    material_code: str | None
    system_node_key: str | None
    asset_type: str
    object_key: str
    source_sha256: str | None
    size_bytes: Any
    mime_type: str | None = None
    status: str = "pending"


@dataclass(frozen=True)
class ReleaseManifest:
    """A release identity, its assets, and node-to-material bindings."""

    release_no: str
    release_id: str
    assets: tuple[AssetSpec, ...]
    node_material_codes: Mapping[str, frozenset[str]]

    @property
    def material_codes(self) -> frozenset[str]:
        codes = {
            asset.material_code
            for asset in self.assets
            if asset.material_code is not None
        }
        for asset in self.assets:
            if asset.system_node_key:
                codes.update(self.node_material_codes.get(asset.system_node_key, ()))
        return frozenset(codes)


@dataclass(frozen=True)
class _Layout:
    source_root: Path
    output_root: Path
    final_dir: Path
    release_segment: str
    output_paths: Mapping[tuple[str, str], Path]


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a report without exposing a partially written JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def file_sha256(path: Path) -> str:
    """Return a file digest using bounded memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_source_path(source_root: Path, local_path: str | None) -> Path:
    """Map only ``/var/lib/qpren`` into ``source_root`` safely.

    The lexical check is deliberate: ``Path`` normalizes ``..`` before a
    normal ``relative_to`` call, so traversal is rejected before normalization.
    """
    if local_path is None or "\x00" in str(local_path):
        raise MaterializerError("source_path_missing")
    text = str(local_path)
    if "\\" in text:
        raise MaterializerError("source_path_escape")

    source = PurePosixPath(text)
    prefix = PurePosixPath(str(QPREN_ASSET_PREFIX))
    if not source.is_absolute() or source.parts[: len(prefix.parts)] != prefix.parts:
        raise MaterializerError("source_path_escape")
    relative_parts = source.parts[len(prefix.parts) :]
    if any(part in {"..", "."} for part in relative_parts):
        raise MaterializerError("source_path_escape")

    root = Path(source_root).expanduser().resolve(strict=False)
    try:
        candidate = (root.joinpath(*relative_parts)).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise MaterializerError("source_path_escape") from exc
    if not _under(root, candidate):
        raise MaterializerError("source_path_escape")
    return candidate


def safe_output_path(output_root: Path, object_key: str) -> Path:
    """Resolve one release object key without allowing output-root escapes."""
    text = str(object_key)
    if (
        not text
        or "\x00" in text
        or "\\" in text
        or not OBJECT_KEY_RE.fullmatch(text)
    ):
        raise MaterializerError("object_key_invalid")
    parts = PurePosixPath(text).parts
    root = Path(output_root).expanduser().resolve(strict=False)
    try:
        candidate = (root.joinpath(*parts)).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise MaterializerError("object_key_invalid") from exc
    if not _under(root, candidate):
        raise MaterializerError("object_key_escape")
    return candidate


def verify_file(path: Path, expected_size: Any, expected_sha256: Any) -> str:
    """Return a generic verification state for one non-empty file."""
    try:
        expected_size_int = _positive_size(expected_size)
    except MaterializerError:
        return "expected_size_invalid"
    try:
        expected_hash = _required_hash(expected_sha256)
    except MaterializerError:
        return "expected_hash_invalid"

    candidate = Path(path)
    try:
        if not candidate.is_file():
            return "file_missing"
        actual_size = candidate.stat().st_size
    except (OSError, RuntimeError):
        return "file_unreadable"
    if actual_size <= 0:
        return "file_empty"
    if actual_size != expected_size_int:
        return "size_mismatch"
    try:
        actual_hash = file_sha256(candidate)
    except OSError:
        return "file_unreadable"
    if actual_hash != expected_hash:
        return "hash_mismatch"
    return "ok"


def _required_hash(value: Any) -> str:
    if value is None:
        raise MaterializerError("expected_hash_missing")
    normalized = str(value).strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise MaterializerError("expected_hash_invalid")
    return normalized


def _positive_size(value: Any) -> int:
    if value is None or isinstance(value, bool):
        raise MaterializerError("expected_size_invalid")
    if isinstance(value, float) and not value.is_integer():
        raise MaterializerError("expected_size_invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MaterializerError("expected_size_invalid") from exc
    if normalized <= 0:
        raise MaterializerError("expected_size_invalid")
    return normalized


def _row_value(row: Any, name: str, position: int = 0) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return row[position]


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sqlite_read_only_uri(path: Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return f"file:{quote(str(resolved), safe='/')}?mode=ro"


def read_release_manifest(database: Path) -> ReleaseManifest:
    """Read one release and its asset rows through SQLite's read-only URI."""
    connection = sqlite3.connect(_sqlite_read_only_uri(database), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        releases = connection.execute(
            "SELECT release_id, release_no FROM catalog_releases ORDER BY release_id"
        ).fetchall()
        if len(releases) != 1:
            raise MaterializerError("release_metadata_invalid")
        release_id = _text_or_none(releases[0]["release_id"])
        release_no = _text_or_none(releases[0]["release_no"])
        if release_id is None or release_no is None:
            raise MaterializerError("release_metadata_invalid")

        rows = connection.execute(
            """
            SELECT asset_key, material_code, system_node_key, asset_type,
                   object_key, source_sha256, size_bytes, mime_type, status
            FROM catalog_assets
            WHERE release_id = ?
            ORDER BY asset_key, object_key
            """,
            (release_id,),
        ).fetchall()
        assets = tuple(
            AssetSpec(
                asset_key=str(row["asset_key"] or ""),
                material_code=_text_or_none(row["material_code"]),
                system_node_key=_text_or_none(row["system_node_key"]),
                asset_type=str(row["asset_type"] or ""),
                object_key=str(row["object_key"] or ""),
                source_sha256=_text_or_none(row["source_sha256"]),
                size_bytes=row["size_bytes"],
                mime_type=_text_or_none(row["mime_type"]),
                status=str(row["status"] or ""),
            )
            for row in rows
        )

        node_keys = sorted(
            {asset.system_node_key for asset in assets if asset.system_node_key is not None}
        )
        node_material_codes: dict[str, set[str]] = {key: set() for key in node_keys}
        if node_keys:
            placeholders = ",".join("?" for _ in node_keys)
            fitments = connection.execute(
                f"""
                SELECT node_key, material_code
                FROM fitments
                WHERE release_id = ? AND node_key IN ({placeholders})
                """,
                (release_id, *node_keys),
            ).fetchall()
            for row in fitments:
                node_key = _text_or_none(row["node_key"])
                material_code = _text_or_none(row["material_code"])
                if node_key in node_material_codes and material_code is not None:
                    node_material_codes[node_key].add(material_code)
        return ReleaseManifest(
            release_no=release_no,
            release_id=release_id,
            assets=assets,
            node_material_codes={
                key: frozenset(value) for key, value in node_material_codes.items()
            },
        )
    finally:
        connection.close()


def verify_read_only_session(connection: Any) -> None:
    """Refuse source queries unless both PostgreSQL read-only settings are on."""
    transaction_row = connection.execute("SHOW transaction_read_only").fetchone()
    default_row = connection.execute(
        "SELECT current_setting('default_transaction_read_only') AS setting"
    ).fetchone()
    transaction_value = str(
        _row_value(transaction_row, "transaction_read_only")
    ).strip().lower()
    default_value = str(_row_value(default_row, "setting")).strip().lower()
    if transaction_value != "on" or default_value != "on":
        raise MaterializerError("read_only_session")


def query_source_images(
    connection: Any, material_codes: Iterable[str]
) -> list[dict[str, Any]]:
    """Read only image rows for the release's needed material codes."""
    codes = sorted({str(code) for code in material_codes if str(code).strip()})
    if not codes:
        return []
    rows = connection.execute(
        """
        SELECT url, kind, material_code, name, local_path, sha256,
               size_bytes, status
        FROM images
        WHERE material_code = ANY(%s)
        ORDER BY material_code, url, kind
        """,
        (codes,),
    ).fetchall()
    return [dict(row) if isinstance(row, Mapping) else dict(row) for row in rows]


def _release_segment(release_no: str) -> str:
    """Return the builder-compatible, single path segment for a release."""
    if not RELEASE_NO_RE.fullmatch(release_no):
        raise MaterializerError("release_no_invalid")
    segment = quote(release_no, safe="-._~")
    if segment != release_no:
        raise MaterializerError("release_no_invalid")
    return segment


def _issue(kind: str, asset: AssetSpec | None = None) -> dict[str, str]:
    issue: dict[str, str] = {"kind": kind}
    if asset is not None:
        issue["asset_key"] = asset.asset_key
        issue["object_key"] = asset.object_key
    return issue


def _asset_entries(
    assets: Sequence[AssetSpec], states: Mapping[tuple[str, str], str]
) -> list[dict[str, str]]:
    return [
        {
            "asset_key": asset.asset_key,
            "object_key": asset.object_key,
            "state": states.get((asset.asset_key, asset.object_key), "not_materialized"),
        }
        for asset in assets
    ]


def _report(
    manifest: ReleaseManifest | None,
    *,
    status: str,
    assets: Sequence[AssetSpec] = (),
    states: Mapping[tuple[str, str], str] | None = None,
    errors: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    asset_list = tuple(assets if manifest is None else manifest.assets)
    state_map = states or {}
    entries = _asset_entries(asset_list, state_map)
    materialized = sum(entry["state"] == "materialized" for entry in entries)
    reused = sum(entry["state"] == "reused" for entry in entries)
    failed = sum(entry["state"] == "failed" for entry in entries)
    return {
        "tool": "materialize_catalog_assets",
        "release_no": manifest.release_no if manifest is not None else None,
        "status": status,
        "counts": {
            "expected": len(asset_list),
            "materialized": materialized,
            "reused": reused,
            "failed": failed,
        },
        "assets": entries,
        "errors": [dict(error) for error in errors],
    }


def _layout(manifest: ReleaseManifest, source_root: Path, output_root: Path) -> _Layout:
    source = Path(source_root).expanduser().resolve(strict=False)
    output = Path(output_root).expanduser().resolve(strict=False)
    if output == source or _under(source, output):
        raise MaterializerError("output_root_boundary")

    segment = _release_segment(manifest.release_no)
    final_dir = output / "release" / segment
    if not _under(output, final_dir.resolve(strict=False)):
        raise MaterializerError("output_root_boundary")

    prefix = f"release/{segment}/"
    output_paths: dict[tuple[str, str], Path] = {}
    issues: list[dict[str, str]] = []
    seen_object_keys: set[str] = set()
    for asset in manifest.assets:
        asset_issues: list[dict[str, str]] = []
        material_bound = bool(asset.material_code)
        node_bound = bool(asset.system_node_key)
        if not asset.asset_key or not ASSET_KEY_RE.fullmatch(asset.asset_key):
            asset_issues.append(_issue("asset_key_invalid", asset))
        if material_bound == node_bound:
            asset_issues.append(_issue("asset_binding_invalid", asset))
        if asset.asset_type not in ASSET_TYPES:
            asset_issues.append(_issue("asset_type_invalid", asset))
        if asset.status not in ASSET_STATUSES:
            asset_issues.append(_issue("asset_status_invalid", asset))
        try:
            _required_hash(asset.source_sha256)
        except MaterializerError as exc:
            asset_issues.append(_issue(exc.kind, asset))
        try:
            _positive_size(asset.size_bytes)
        except MaterializerError as exc:
            asset_issues.append(_issue(exc.kind, asset))
        if not asset.object_key.startswith(prefix):
            asset_issues.append(_issue("object_key_release_mismatch", asset))
        try:
            output_path = safe_output_path(output, asset.object_key)
        except MaterializerError as exc:
            asset_issues.append(_issue(exc.kind, asset))
            output_path = None
        if asset.object_key in seen_object_keys:
            asset_issues.append(_issue("object_key_collision", asset))
        seen_object_keys.add(asset.object_key)
        if asset_issues:
            issues.extend(asset_issues)
        elif output_path is not None:
            output_paths[(asset.asset_key, asset.object_key)] = output_path

    if issues:
        # Keep the layout exception generic while preserving per-asset keys in
        # the caller's concise report.
        error = MaterializerError("asset_manifest_invalid")
        error.issues = issues  # type: ignore[attr-defined]
        raise error
    return _Layout(source, output, final_dir, segment, output_paths)


def _existing_release_state(
    manifest: ReleaseManifest, layout: _Layout
) -> tuple[str, list[dict[str, str]]]:
    """Return reuse, absent, or invalid without changing an existing release."""
    final_dir = layout.final_dir
    if not os.path.lexists(final_dir):
        return "absent", []
    if final_dir.is_symlink() or not final_dir.is_dir():
        return "invalid", [_issue("existing_release_invalid")]
    if not _under(layout.output_root, final_dir.resolve(strict=False)):
        return "invalid", [_issue("existing_release_invalid")]

    issues: list[dict[str, str]] = []
    for asset in manifest.assets:
        path = layout.output_paths[(asset.asset_key, asset.object_key)]
        state = verify_file(path, asset.size_bytes, asset.source_sha256)
        if state != "ok":
            issues.append(_issue(f"existing_{state}", asset))
    return ("reuse", []) if not issues else ("invalid", issues)


def _source_row_identity(release_no: str, row: Mapping[str, Any]) -> tuple[str, str] | None:
    try:
        identity = asset_identity(
            release_no,
            row.get("kind"),
            str(row.get("url") or ""),
            row.get("sha256"),
        )
    except (TypeError, ValueError):
        return None
    return str(identity["asset_key"]), str(identity["object_key"])


def _source_matches_binding(
    asset: AssetSpec,
    row: Mapping[str, Any],
    node_material_codes: Mapping[str, frozenset[str]],
) -> bool:
    row_material = _text_or_none(row.get("material_code"))
    if asset.material_code is not None:
        return row_material == asset.material_code
    if asset.system_node_key is None:
        return False
    row_node = _text_or_none(row.get("system_node_key") or row.get("node_key"))
    if row_node is not None:
        return row_node == asset.system_node_key
    return row_material in node_material_codes.get(asset.system_node_key, frozenset())


def _source_candidates(
    manifest: ReleaseManifest, source_rows: Iterable[Mapping[str, Any]]
) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    candidates: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for raw_row in source_rows:
        row = dict(raw_row)
        identity_key = _source_row_identity(manifest.release_no, row)
        if identity_key is None:
            continue
        candidates.setdefault(identity_key, []).append(row)
    return candidates


def _verify_source_row(
    asset: AssetSpec, row: Mapping[str, Any], source_root: Path
) -> tuple[Path | None, str]:
    try:
        source_path = resolve_source_path(source_root, _text_or_none(row.get("local_path")))
    except MaterializerError as exc:
        return None, exc.kind
    state = verify_file(source_path, asset.size_bytes, asset.source_sha256)
    return (source_path, state) if state == "ok" else (None, state)


def _copy_atomically(
    source_path: Path, destination: Path, expected_size: int, expected_sha256: str
) -> None:
    """Copy and verify one file, replacing only a staging temporary path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        copied = 0
        with source_path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        if copied != expected_size or digest.hexdigest() != expected_sha256:
            raise MaterializerError("source_changed_during_copy")
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _cleanup_staging(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        # The directory is private and contains no final release.  A cleanup
        # failure is not allowed to turn into a report containing local paths.
        pass


def materialize_assets(
    manifest: ReleaseManifest,
    source_rows: Iterable[Mapping[str, Any]],
    source_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Pure-core materialization entry point used by tests without PostgreSQL."""
    try:
        layout = _layout(manifest, source_root, output_root)
    except MaterializerError as exc:
        issues = getattr(exc, "issues", [_issue(exc.kind)])
        return _report(manifest, status="failed", errors=issues)

    try:
        existing_state, existing_issues = _existing_release_state(manifest, layout)
    except (OSError, RuntimeError):
        existing_state, existing_issues = "invalid", [_issue("existing_release_invalid")]
    if existing_state == "reuse":
        states = {(asset.asset_key, asset.object_key): "reused" for asset in manifest.assets}
        return _report(manifest, status="reused", states=states)
    if existing_state == "invalid":
        states = {
            (asset.asset_key, asset.object_key): "failed"
            for asset in manifest.assets
            if any(
                issue.get("asset_key") == asset.asset_key
                and issue.get("object_key") == asset.object_key
                for issue in existing_issues
            )
        }
        return _report(
            manifest,
            status="failed",
            states=states,
            errors=existing_issues,
        )

    candidates = _source_candidates(manifest, source_rows)
    selected: list[tuple[AssetSpec, Path, int, str]] = []
    issues: list[dict[str, str]] = []
    failed_keys: set[tuple[str, str]] = set()
    for asset in manifest.assets:
        key = (asset.asset_key, asset.object_key)
        source_candidates = sorted(
            (
                row
                for row in candidates.get(key, [])
                if _source_matches_binding(asset, row, manifest.node_material_codes)
            ),
            key=lambda row: (
                str(row.get("local_path") or ""),
                str(row.get("url") or ""),
                str(row.get("kind") or ""),
                str(row.get("name") or ""),
            ),
        )
        if not source_candidates:
            issues.append(_issue("source_asset_missing", asset))
            failed_keys.add(key)
            continue

        # qpren can contain duplicate rows for one deterministic identity. Pick
        # the first candidate whose mapped file verifies, in a stable order;
        # duplicate identities are not an error when their content evidence is
        # equivalent. A bad duplicate must not hide a valid one.
        verified_candidate: tuple[Path, str] | None = None
        candidate_states: list[str] = []
        for candidate in source_candidates:
            source_path, state = _verify_source_row(asset, candidate, layout.source_root)
            candidate_states.append(state)
            if source_path is not None:
                verified_candidate = (source_path, state)
                break
        if verified_candidate is None:
            state = candidate_states[0] if candidate_states else "file_missing"
            issue_kind = state if state.startswith("source_") else f"source_{state}"
            issues.append(_issue(issue_kind, asset))
            failed_keys.add(key)
            continue
        source_path, _ = verified_candidate
        try:
            expected_size = _positive_size(asset.size_bytes)
            expected_hash = _required_hash(asset.source_sha256)
        except MaterializerError as exc:  # guarded by _layout, kept defensive
            issues.append(_issue(exc.kind, asset))
            failed_keys.add(key)
            continue
        selected.append((asset, source_path, expected_size, expected_hash))

    if issues:
        states = {key: "failed" for key in failed_keys}
        return _report(manifest, status="failed", states=states, errors=issues)

    output_root_path = layout.output_root
    output_root_path.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_root_path.name}.n6-", dir=str(output_root_path.parent)
        )
    )
    staging_release = staging_root / "release" / layout.release_segment
    try:
        staging_release.mkdir(parents=True, exist_ok=True)
        for asset, source_path, expected_size, expected_hash in selected:
            destination = staging_root / PurePosixPath(asset.object_key)
            if not _under(staging_root, destination.resolve(strict=False)):
                raise MaterializerError("object_key_escape")
            _copy_atomically(source_path, destination, expected_size, expected_hash)

        final_parent = output_root_path / "release"
        final_parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(layout.final_dir):
            # A final directory that appeared during staging is never replaced.
            raise MaterializerError("existing_release_changed")
        os.replace(staging_release, layout.final_dir)
        states = {(asset.asset_key, asset.object_key): "materialized" for asset in manifest.assets}
        return _report(manifest, status="materialized", states=states)
    except Exception as exc:
        kind = exc.kind if isinstance(exc, MaterializerError) else "atomic_materialization_failed"
        states = {
            (asset.asset_key, asset.object_key): "failed" for asset in manifest.assets
        }
        return _report(manifest, status="failed", states=states, errors=[_issue(kind)])
    finally:
        _cleanup_staging(staging_root)


def _write_failure(
    report_path: Path, manifest: ReleaseManifest | None, kind: str
) -> dict[str, Any]:
    report = _report(manifest, status="failed", errors=[_issue(kind)])
    atomic_write_json(report_path, report)
    return report


def materialize_release(
    database: Path,
    dsn: str,
    source_root: Path,
    output_root: Path,
    report_path: Path,
) -> dict[str, Any]:
    """Run N6: read the artifact, query read-only qpren, then materialize."""
    manifest: ReleaseManifest | None = None
    try:
        manifest = read_release_manifest(database)
    except MaterializerError as exc:
        return _write_failure(report_path, None, exc.kind)
    except (OSError, sqlite3.Error, ValueError):
        return _write_failure(report_path, None, "release_database_invalid")

    try:
        layout = _layout(manifest, source_root, output_root)
    except MaterializerError as exc:
        issues = getattr(exc, "issues", [_issue(exc.kind)])
        report = _report(manifest, status="failed", errors=issues)
        atomic_write_json(report_path, report)
        return report

    try:
        existing_state, existing_issues = _existing_release_state(manifest, layout)
    except (OSError, RuntimeError):
        existing_state, existing_issues = "invalid", [_issue("existing_release_invalid")]
    if existing_state == "reuse":
        states = {(asset.asset_key, asset.object_key): "reused" for asset in manifest.assets}
        report = _report(manifest, status="reused", states=states)
        atomic_write_json(report_path, report)
        return report
    if existing_state == "invalid":
        states = {
            (asset.asset_key, asset.object_key): "failed"
            for asset in manifest.assets
            if any(
                issue.get("asset_key") == asset.asset_key
                and issue.get("object_key") == asset.object_key
                for issue in existing_issues
            )
        }
        report = _report(
            manifest,
            status="failed",
            states=states,
            errors=existing_issues,
        )
        atomic_write_json(report_path, report)
        return report

    if psycopg is None or dict_row is None:
        return _write_failure(report_path, manifest, "psycopg_unavailable")
    if not str(dsn).strip():
        return _write_failure(report_path, manifest, "source_connection_missing")

    try:
        with psycopg.connect(
            dsn,
            connect_timeout=20,
            row_factory=dict_row,
            options=READ_ONLY_OPTIONS,
        ) as connection:
            connection.autocommit = True
            verify_read_only_session(connection)
            source_rows = query_source_images(connection, manifest.material_codes)
    except MaterializerError as exc:
        return _write_failure(report_path, manifest, exc.kind)
    except Exception:
        return _write_failure(report_path, manifest, "source_query_failed")

    report = materialize_assets(manifest, source_rows, source_root, output_root)
    atomic_write_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        report = materialize_release(
            database=args.database,
            dsn=args.dsn,
            source_root=args.source_root,
            output_root=args.output_root,
            report_path=args.report,
        )
    except Exception:
        # Keep CLI diagnostics generic; detailed failures belong to the
        # concise report and never include connection or source metadata.
        print("materialize_catalog_assets failed: materializer_exception", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": report["status"],
                "release_no": report["release_no"],
                "counts": report["counts"],
                "error_count": len(report["errors"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] in {"materialized", "reused"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
