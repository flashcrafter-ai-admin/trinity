#!/usr/bin/env python3
"""Verify an extracted release tree byte-for-byte against one exact Git object."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


def fail(message: str) -> None:
    raise SystemExit(message)


if len(sys.argv) != 4:
    fail("usage: verify-exact-git-tree.py REPOSITORY REVISION EXTRACTED_ROOT")

repository = Path(sys.argv[1]).resolve(strict=True)
revision = sys.argv[2]
root = Path(sys.argv[3]).resolve(strict=True)
if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
    fail("exact Git tree verification requires a full lowercase revision")

tool_state = tempfile.TemporaryDirectory(prefix="trinity-exact-tree-verify-")
environment = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": tool_state.name,
    "TMPDIR": tool_state.name,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}


def git(*arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    if result.returncode != 0:
        fail("unable to read exact Git object for release verification")
    return result.stdout


object_format = git("rev-parse", "--show-object-format").decode().strip()
if object_format not in {"sha1", "sha256"}:
    fail("unsupported Git object format")

expected: dict[str, tuple[str, str, str]] = {}
for record in git("ls-tree", "-rz", "-r", "-t", "--full-tree", revision).split(b"\0"):
    if not record:
        continue
    metadata, separator, raw_path = record.partition(b"\t")
    fields = metadata.decode("ascii").split()
    path = raw_path.decode("utf-8", "surrogateescape")
    if not separator or len(fields) != 3 or path.startswith("/") or ".." in Path(path).parts:
        fail("exact Git tree contains an unsupported entry")
    mode, kind, object_id = fields
    if mode == "160000" or kind == "commit":
        fail("exact release tree contains an unsupported submodule")
    expected[path] = (mode, kind, object_id)


def blob_id(payload: bytes) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(payload)}\0".encode())
    digest.update(payload)
    return digest.hexdigest()


actual: dict[str, tuple[str, str, str]] = {}
for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
    relative = path.relative_to(root).as_posix()
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        actual[relative] = ("040000", "tree", "")
    elif stat.S_ISREG(metadata.st_mode):
        mode = "100755" if metadata.st_mode & 0o111 else "100644"
        actual[relative] = (mode, "blob", blob_id(path.read_bytes()))
    elif stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path).encode("utf-8", "surrogateescape")
        actual[relative] = ("120000", "blob", blob_id(target))
    else:
        fail(f"extracted release tree has an unsupported node: {relative}")

for path, (mode, kind, object_id) in expected.items():
    observed = actual.get(path)
    if observed is None or observed[0] != mode or observed[1] != kind:
        fail(f"extracted release tree differs from exact Git object: {path}")
    if kind == "blob" and observed[2] != object_id:
        fail(f"extracted release bytes differ from exact Git object: {path}")
for path in actual:
    if path not in expected:
        fail(f"extracted release tree contains an undeclared path: {path}")
