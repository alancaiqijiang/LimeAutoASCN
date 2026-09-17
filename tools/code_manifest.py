#!/usr/bin/env python3
"""Emit and compare a deterministic LimeAuto source/deploy manifest.

The default manifest covers canonical application, schema, tool, test, and
root configuration files. Documentation/history, VCS data, caches, runtime
databases, generated assets, and reports are intentionally outside the
deployment manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "limeauto-code-manifest.v1"
ROOT_DIRS = ("app", "release_schema", "tests", "tools")
ROOT_FILES = ("README.md", "catalog.env.example", "pytest.ini", "requirements.txt")
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        ".venv",
        "venv",
        "data",
        "runtime",
        "assets",
        "catalog-assets",
        "reports",
        "build",
        "dist",
        "docs",
    }
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_files(root: Path, *, exclude: Iterable[str] = ()) -> list[Path]:
    excluded = set(exclude)
    paths: list[Path] = []
    for relative in ROOT_FILES:
        path = root / relative
        if path.is_file() and not path.is_symlink() and relative not in excluded:
            paths.append(path)
    for directory in ROOT_DIRS:
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            continue
        for current, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in EXCLUDED_DIRS and not (Path(current) / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(current) / filename
                relative_path = path.relative_to(root).as_posix()
                if relative_path in excluded or path.is_symlink() or not path.is_file():
                    continue
                paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def build_manifest(root: Path, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    root = root.expanduser().resolve()
    files = []
    for path in canonical_files(root, exclude=exclude):
        relative = path.relative_to(root).as_posix()
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "file_count": len(files),
        "files": files,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError(f"invalid manifest: {path}")
    return payload


def compare_manifests(left_path: Path, right_path: Path) -> dict[str, Any]:
    left = load_manifest(left_path)
    right = load_manifest(right_path)

    def indexed(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(item["path"]): item for item in payload["files"] if isinstance(item, dict)}

    left_files = indexed(left)
    right_files = indexed(right)
    changed = [
        path
        for path in sorted(left_files.keys() & right_files.keys())
        if (
            left_files[path].get("bytes") != right_files[path].get("bytes")
            or left_files[path].get("sha256") != right_files[path].get("sha256")
        )
    ]
    only_left = sorted(left_files.keys() - right_files.keys())
    only_right = sorted(right_files.keys() - left_files.keys())
    return {
        "schema_version": SCHEMA_VERSION,
        "same": not changed and not only_left and not only_right,
        "changed": changed,
        "only_left": only_left,
        "only_right": only_right,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--compare",
        nargs=2,
        type=Path,
        metavar=("LEFT", "RIGHT"),
        help="compare two JSON manifests instead of generating one",
    )
    args = parser.parse_args()
    if args.compare:
        result = compare_manifests(args.compare[0], args.compare[1])
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", end="")
        return 0 if result["same"] else 1

    output_relative = set()
    if args.output:
        try:
            output_relative.add(args.output.expanduser().resolve().relative_to(args.root.expanduser().resolve()).as_posix())
        except ValueError:
            pass
    result = build_manifest(args.root, exclude=output_relative)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
