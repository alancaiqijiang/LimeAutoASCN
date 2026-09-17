from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.build_epc_enriched_catalog_revision as builder

GIF = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 32


def make_manifest(root: Path, *, content: bytes = GIF, relative: str = "assets/a.gif", url: str = "https://example.invalid/a.gif", **changes: object) -> Path:
    asset = root / relative
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(content)
    row: dict[str, object] = {
        "accepted": True,
        "url": url,
        "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
        "body_sha256": hashlib.sha256(content).hexdigest(),
        "byte_count": len(content),
        "content_type": "image/gif",
        "local_relative_path": relative,
    }
    row.update(changes)
    path = root / "manifest.json"
    path.write_text(json.dumps({"results": [row]}), encoding="utf-8")
    return path


class SupplementalTests(unittest.TestCase):
    def test_no_manifest_legacy_merge_and_cli(self) -> None:
        qpren = {("epc_thumbnail", "https://example.invalid/existing.gif"): {"id": "qpren"}}
        merged, summary = builder.merge_image_rows(qpren, {})
        self.assertEqual(merged, qpren)
        self.assertEqual(summary["added_count"], 0)
        args = builder.parse_args(["--dsn", "redacted", "--supplemental-manifest", "m.json"])
        self.assertEqual(args.supplemental_manifest, Path("m.json"))

    def test_valid_gif_is_loaded_and_bound_to_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows, info = builder.load_supplemental_manifest(make_manifest(root))
            self.assertEqual(info["accepted_unique_count"], 1)
            merged, merge_info = builder.merge_image_rows({}, rows)
            self.assertEqual(merge_info["added_count"], 1)
            tree = root / "tree"
            tree.mkdir()
            (tree / "S_M.json").write_text(json.dumps({"code": 200, "data": {"vinInfo": {"seriesCode": "S", "modelCode": "M"}, "epcTree": [{"grpName": "Engine", "objCode": "O", "thumbnailUrl": "https://example.invalid/a.gif"}]}}), encoding="utf-8")
            nodes = [{"series_code": "S", "model_code": "M", "source_obj_code": "O", "path_key": "Engine", "node_key": "N"}]
            with mock.patch.object(builder, "RAW_TREE", tree):
                specs, _ = builder.collect_epc_assets(nodes, merged)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0]["asset_type"], "thumbnail")
            self.assertEqual(specs[0]["mime_type"], "image/gif")

    def test_metadata_media_and_path_failures(self) -> None:
        cases = [
            ({"url_sha256": "0" * 64}, "url hash mismatch"),
            ({"body_sha256": "0" * 64}, "body hash mismatch"),
            ({"byte_count": len(GIF) + 1}, "size mismatch"),
            ({"content_type": "image/jpeg"}, "MIME mismatch"),
        ]
        for changes, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(RuntimeError, message):
                    builder.load_supplemental_manifest(make_manifest(Path(temp), **changes))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "GIF magic mismatch"):
                builder.load_supplemental_manifest(make_manifest(Path(temp), content=b"not-gif"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root.parent / "outside.gif"
            outside.write_bytes(GIF)
            with self.assertRaisesRegex(RuntimeError, "escapes manifest root"):
                builder.load_supplemental_manifest(make_manifest(root, relative="../outside.gif"))
            outside.unlink()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "real.gif"
            target.write_bytes(GIF)
            (root / "link.gif").symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                builder.load_supplemental_manifest(make_manifest(root, relative="link.gif"))

    def test_qpren_duplicate_is_preserved_and_conflict_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            url = "https://example.invalid/same.gif"
            supplemental, _ = builder.load_supplemental_manifest(make_manifest(root, url=url))
            qpren_row = {"kind": "epc_thumbnail", "url": url, "sha256": hashlib.sha256(GIF).hexdigest(), "size_bytes": len(GIF), "local_path": "/var/lib/qpren/images/x.gif"}
            merged, info = builder.merge_image_rows({("epc_thumbnail", url): qpren_row}, supplemental)
            self.assertIs(merged[("epc_thumbnail", url)], qpren_row)
            self.assertEqual(info["added_count"], 0)
            conflict = dict(supplemental)
            conflict[("epc_thumbnail", url)] = dict(next(iter(conflict.values())), sha256="0" * 64)
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                builder.merge_image_rows({("epc_thumbnail", url): qpren_row}, conflict)


if __name__ == "__main__":
    unittest.main()
