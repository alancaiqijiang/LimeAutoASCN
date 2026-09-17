"""The promotion surface must refuse more than it allows.

One transition is authorised: `draft -> validated`. Everything else has to fail loudly,
because this is the tool that can make a release servable in production, and the failure
mode that matters is promoting an artifact no validation evidence describes.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "promote_catalog_release", REPO / "tools/promote_catalog_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_release(path: Path, status: str = "draft",
                 release_no: str = "rel-1", counts: int = 1) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE catalog_releases (
            release_id TEXT, release_no TEXT, status TEXT, schema_version TEXT,
            source_snapshot_fingerprint TEXT, notes TEXT
        );
        CREATE TABLE release_models (release_id TEXT);
        CREATE TABLE system_nodes (release_id TEXT);
        CREATE TABLE catalog_parts (release_id TEXT);
        CREATE TABLE fitments (release_id TEXT);
        """
    )
    connection.execute(
        "INSERT INTO catalog_releases VALUES ('rid-1', ?, ?, 'n2a', 'fp-abc', 'note')",
        (release_no, status))
    for table in ("release_models", "system_nodes", "catalog_parts", "fitments"):
        for _ in range(counts):
            connection.execute(f"INSERT INTO {table} VALUES ('rid-1')")
    connection.commit()
    connection.close()
    return path


def run(tool, argv):
    original = sys.argv
    sys.argv = ["promote_catalog_release.py", *argv]
    try:
        return tool.main()
    finally:
        sys.argv = original


def status_of(path: Path) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
        return str(c.execute("SELECT status FROM catalog_releases").fetchone()[0])


def digest(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def base_args(path: Path, sha: str, release_no: str = "rel-1") -> list[str]:
    return ["--release", str(path), "--release-no", release_no,
            "--approved-by", "reviewer", "--expect-sha256", sha]


def test_check_reports_without_changing_anything(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    before = digest(release)
    rc = run(tool, base_args(release, before) + ["--check"])
    assert rc == 0
    assert status_of(release) == "draft"
    assert digest(release) == before, "check mode must not touch the artifact"
    assert json.loads(capsys.readouterr().out)["would_promote"] is True


def test_promotes_draft_to_validated_and_nothing_else(tmp_path: Path, capsys) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    report_path = tmp_path / "report.json"
    rc = run(tool, base_args(release, digest(release))
             + ["--write", "--snapshot", str(tmp_path / "snap.sqlite"),
                "--report", str(report_path)])
    assert rc == 0
    assert status_of(release) == "validated"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["promoted"] is True
    assert all(report["checks"].values()), report["checks"]
    assert report["column_changed"] == "catalog_releases.status"
    assert report["columns_that_changed_besides_status"] == []
    assert report["release"]["status_before"] == "draft"
    assert report["release_after"]["status"] == "validated"
    assert report["recovery_point"]["restores"] is True
    # the bytes really did change, which is why revalidation is required
    assert report["release"]["sha256_before"] != report["release_after"]["sha256"]


def test_refuses_a_release_that_is_not_draft(tmp_path: Path) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite", status="validated")
    rc = run(tool, base_args(release, digest(release)) + ["--check"])
    assert rc == 2
    assert status_of(release) == "validated"


def test_refuses_published_because_there_is_no_way_to_ask_for_it(tmp_path: Path) -> None:
    """`published` must not be reachable through this tool at all."""
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite", status="published")
    rc = run(tool, base_args(release, digest(release)) + ["--write"])
    assert rc == 2
    assert status_of(release) == "published"


def test_refuses_a_sha256_that_does_not_match_the_artifact(tmp_path: Path) -> None:
    """The pin is what stops the promotion drifting onto bytes no evidence describes."""
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    before = digest(release)
    rc = run(tool, base_args(release, "0" * 64) + ["--write"])
    assert rc == 2
    assert status_of(release) == "draft"
    assert digest(release) == before


def test_refuses_a_release_no_that_does_not_match(tmp_path: Path) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite", release_no="rel-1")
    rc = run(tool, base_args(release, digest(release), release_no="rel-something-else") + ["--write"])
    assert rc == 2
    assert status_of(release) == "draft"


def test_refuses_a_missing_artifact(tmp_path: Path) -> None:
    tool = load_tool()
    rc = run(tool, base_args(tmp_path / "absent.sqlite", "0" * 64) + ["--check"])
    assert rc == 2


def test_refuses_an_artifact_with_more_than_one_release_row(tmp_path: Path) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    with sqlite3.connect(release) as c:
        c.execute("INSERT INTO catalog_releases VALUES ('rid-2','rel-2','draft','n2a','fp','')")
    rc = run(tool, base_args(release, digest(release)) + ["--check"])
    assert rc == 2


def test_counts_and_fingerprint_survive_the_transition(tmp_path: Path) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite", counts=3)
    run(tool, base_args(release, digest(release)) + ["--write", "--snapshot", str(tmp_path / "s.sqlite")])
    with sqlite3.connect(f"file:{release}?mode=ro", uri=True) as c:
        assert c.execute("SELECT COUNT(*) FROM fitments").fetchone()[0] == 3
        assert c.execute("SELECT source_snapshot_fingerprint FROM catalog_releases").fetchone()[0] == "fp-abc"
        assert c.execute("SELECT notes FROM catalog_releases").fetchone()[0] == "note"
        assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_recovery_point_restores_the_draft_state(tmp_path: Path) -> None:
    """The snapshot must be a real way back, not a file that merely exists."""
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    snap = tmp_path / "pre.sqlite"
    run(tool, base_args(release, digest(release)) + ["--write", "--snapshot", str(snap)])
    assert status_of(release) == "validated"
    assert status_of(snap) == "draft"
    with sqlite3.connect(f"file:{snap}?mode=ro", uri=True) as c:
        assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_write_requires_an_approver(tmp_path: Path) -> None:
    """An unattributed promotion must not be possible; argparse refuses it outright."""
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    with pytest.raises(SystemExit) as excinfo:
        run(tool, ["--release", str(release), "--release-no", "rel-1",
                   "--expect-sha256", digest(release), "--write"])
    assert excinfo.value.code != 0
    assert status_of(release) == "draft"


def test_an_approver_is_recorded_in_the_report(tmp_path: Path) -> None:
    tool = load_tool()
    release = make_release(tmp_path / "release.sqlite")
    report_path = tmp_path / "r.json"
    run(tool, ["--release", str(release), "--release-no", "rel-1",
               "--approved-by", "named operator",
               "--expect-sha256", digest(release),
               "--write", "--snapshot", str(tmp_path / "s.sqlite"),
               "--report", str(report_path)])
    assert json.loads(report_path.read_text(encoding="utf-8"))["approved_by"] == "named operator"
