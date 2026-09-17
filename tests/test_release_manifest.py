"""The release manifest must report the truth about a release combination.

It is the record that decides whether prior validation evidence may be reused and whether a
glossary actually serves the release it is deployed with, so the interesting cases are the
disagreeing ones: a mismatched fingerprint, a missing artifact, an unnamed code commit.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FINGERPRINT = "a6ef6746536d28c6cc8ed178eab610e57ae9d3d79177722d50a61a356f8e5af7"


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "release_manifest", REPO / "tools/release_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_release(path: Path, fingerprint: str = FINGERPRINT) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE catalog_releases (
            release_id TEXT, release_no TEXT, status TEXT, schema_version TEXT,
            source_snapshot_fingerprint TEXT
        );
        CREATE TABLE release_models (release_id TEXT);
        CREATE TABLE system_nodes (release_id TEXT);
        CREATE TABLE catalog_parts (release_id TEXT);
        CREATE TABLE fitments (release_id TEXT);
        CREATE TABLE catalog_assets (release_id TEXT);
        """
    )
    connection.execute(
        "INSERT INTO catalog_releases VALUES ('rid-1','rel-1','draft','n2a',?)", (fingerprint,)
    )
    for table in ("release_models", "system_nodes", "catalog_parts", "fitments", "catalog_assets"):
        connection.execute(f"INSERT INTO {table} VALUES ('rid-1')")
    connection.commit()
    connection.close()
    return path


def make_glossary(path: Path, fingerprint: str = FINGERPRINT, rows: int = 2) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE translation_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE translation_terms (
            term_id TEXT NOT NULL, term_kind TEXT NOT NULL, source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL, lang TEXT NOT NULL, status TEXT NOT NULL,
            source_snapshot_fingerprint TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO translation_meta VALUES ('source_snapshot_fingerprint', ?)", (fingerprint,)
    )
    for index in range(rows):
        connection.execute(
            "INSERT INTO translation_terms VALUES (?,?,?,?,'en','published',?)",
            (f"part:t{index}", "part", f"源{index}", f"English {index}", fingerprint),
        )
    connection.commit()
    connection.close()
    return path


def run(tool, argv: list[str], monkeypatch=None):
    original = sys.argv
    sys.argv = ["release_manifest.py", *argv]
    try:
        return tool.main()
    finally:
        sys.argv = original


def test_consistent_when_the_glossary_serves_that_release(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    glossary = make_glossary(tmp_path / "glossary.sqlite")
    out = tmp_path / "manifest.json"
    rc = run(tool, ["--release", str(release), "--glossary", str(glossary), "--out", str(out)])
    assert rc == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["consistent"] is True
    assert manifest["glossary"]["matches_release_fingerprint"] is True
    assert manifest["glossary"]["published"] == 2
    assert manifest["release"]["release"]["status"] == "draft"
    # the release hash is expensive, so it must not be claimed unless asked for
    assert "sha256" not in manifest["release"]
    assert manifest["release"]["bytes"] > 0


def test_inconsistent_when_the_glossary_is_from_another_release(tmp_path: Path, capsys) -> None:
    """The state that silently turns the whole English catalogue back into Chinese."""
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    glossary = make_glossary(tmp_path / "glossary.sqlite", fingerprint="b" * 64)
    out = tmp_path / "manifest.json"
    rc = run(tool, ["--release", str(release), "--glossary", str(glossary), "--out", str(out)])
    assert rc == 1, "a mismatched glossary must not report success"
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["consistent"] is False
    assert manifest["glossary"]["matches_release_fingerprint"] is False


def test_missing_release_is_refused_rather_than_described(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    glossary = make_glossary(tmp_path / "glossary.sqlite")
    rc = run(tool, ["--release", str(tmp_path / "absent.sqlite"), "--glossary", str(glossary)])
    assert rc == 2
    assert "missing release" in capsys.readouterr().out


def test_missing_glossary_is_refused(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    rc = run(tool, ["--release", str(release), "--glossary", str(tmp_path / "absent.sqlite")])
    assert rc == 2
    assert "missing glossary" in capsys.readouterr().out


def test_records_a_code_commit_and_hashes_named_artifacts(tmp_path: Path) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    glossary = make_glossary(tmp_path / "glossary.sqlite")
    extra = tmp_path / "corpus.json"
    extra.write_text('{"a":1}', encoding="utf-8")
    out = tmp_path / "manifest.json"
    rc = run(tool, ["--release", str(release), "--glossary", str(glossary),
                    "--artifacts", str(extra), "--code-commit", "deadbee",
                    "--code-clean", "no", "--out", str(out)])
    assert rc == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["code"] == {"commit": "deadbee", "worktree_clean": "no"}
    assert manifest["artifacts"][0]["path"] == str(extra)
    assert len(manifest["artifacts"][0]["sha256"]) == 64


def test_asset_root_not_on_this_host_is_reported_not_guessed(tmp_path: Path) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    glossary = make_glossary(tmp_path / "glossary.sqlite")
    out = tmp_path / "manifest.json"
    rc = run(tool, ["--release", str(release), "--glossary", str(glossary),
                    "--asset-root", str(tmp_path / "no-assets"), "--out", str(out)])
    assert rc == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["assets"]["exists"] is False
