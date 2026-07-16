#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_restored_tree", ROOT / "scripts/deploy/verify-restored-tree.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SemanticTreeVerificationTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> None:
        nested = root / "nested"
        nested.mkdir()
        payload = nested / "payload.txt"
        payload.write_bytes(b"reviewed payload\n")
        payload.chmod(0o640)
        os.utime(payload, (1_700_000_000, 1_700_000_000))
        os.link(payload, nested / "payload-hardlink.txt")
        (root / "payload-link").symlink_to("nested/payload.txt")

    def archive(self, root: Path, path: Path) -> None:
        with tarfile.open(path, "w:gz", dereference=False) as archive:
            archive.add(root, arcname=".", recursive=True)

    def test_directory_mtime_only_drift_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            self.make_fixture(root)
            archive = Path(temporary) / "tree.tgz"
            self.archive(root, archive)
            os.utime(root, (1_800_000_000, 1_800_000_000))
            os.utime(root / "nested", (1_800_000_001, 1_800_000_001))
            self.assertFalse(MODULE.compare_source_to_archive(str(root), str(archive)))

    def test_live_property_drifts_fail_closed(self) -> None:
        mutations = {
            "mtime": lambda root: os.utime(
                root / "nested/payload.txt", (1_700_000_001, 1_700_000_001)
            ),
            "content": lambda root: (root / "nested/payload.txt").write_bytes(
                b"drifted payload!\n"
            ),
            "size": lambda root: (root / "nested/payload.txt").write_bytes(b"short\n"),
            "mode": lambda root: (root / "nested/payload.txt").chmod(0o600),
            "type": lambda root: (
                (root / "payload-link").unlink(),
                (root / "payload-link").mkdir(),
            ),
            "hardlinks": lambda root: (
                (root / "nested/payload-hardlink.txt").unlink(),
                shutil.copyfile(
                    root / "nested/payload.txt", root / "nested/payload-hardlink.txt"
                ),
            ),
            "target": lambda root: (
                (root / "payload-link").unlink(),
                (root / "payload-link").symlink_to("nested/payload-hardlink.txt"),
            ),
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "source"
                root.mkdir()
                self.make_fixture(root)
                archive = Path(temporary) / "tree.tgz"
                self.archive(root, archive)
                mutate(root)
                differences = MODULE.compare_source_to_archive(str(root), str(archive))
                self.assertTrue(differences)
                self.assertIn(expected, differences)

    def test_synthetic_owner_link_and_device_drifts_fail_closed(self) -> None:
        base = {
            b".": {"kind": "directory", "mode": 0o755, "uid": 1, "gid": 2},
            b"file": {
                "kind": "file",
                "mode": 0o640,
                "uid": 1,
                "gid": 2,
                "mtime": 3,
                "size": 4,
                "content": "digest",
                "nlink": 1,
                "hardlinks": (b"file",),
            },
            b"device": {
                "kind": "character-device",
                "mode": 0o600,
                "uid": 1,
                "gid": 2,
                "mtime": 3,
                "device": (1, 5),
            },
        }
        mutations = {
            "uid": (b"file", "uid", 9),
            "gid": (b"file", "gid", 9),
            "nlink": (b"file", "nlink", 2),
            "hardlinks": (b"file", "hardlinks", (b"file", b"other")),
            "device": (b"device", "device", (1, 6)),
        }
        for expected, (path, field, value) in mutations.items():
            with self.subTest(expected=expected):
                changed = {key: dict(record) for key, record in base.items()}
                changed[path][field] = value
                differences = MODULE.compare_record_maps(base, changed)
                self.assertEqual(differences[expected], 1)


if __name__ == "__main__":
    unittest.main()
