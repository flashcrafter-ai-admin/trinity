#!/usr/bin/env python3
"""Verify an extracted release tree byte-for-byte against one exact Git object."""

from __future__ import annotations

import configparser
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import unicodedata


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


def normalized_repository_path(value: str, description: str) -> str:
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        fail(f"{description} contains a non-UTF-8 path")
    if (
        not value
        or unicodedata.normalize("NFC", value) != value
        or value.startswith("/")
        or str(PurePosixPath(value)) != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        fail(f"{description} contains an unsafe path")
    return value


object_format = git("rev-parse", "--show-object-format").decode().strip()
if object_format not in {"sha1", "sha256"}:
    fail("unsupported Git object format")

tree_entries: list[tuple[str, str, str, str]] = []
for record in git("ls-tree", "-rz", "-r", "-t", "--full-tree", revision).split(b"\0"):
    if not record:
        continue
    metadata, separator, raw_path = record.partition(b"\t")
    fields = metadata.decode("ascii").split()
    path = raw_path.decode("utf-8", "surrogateescape")
    if not separator or len(fields) != 3:
        fail("exact Git tree contains an unsupported entry")
    normalized_repository_path(path, "exact Git tree")
    mode, kind, object_id = fields
    tree_entries.append((path, mode, kind, object_id))

policy_path = "scripts/deploy/release-excluded-gitlinks.txt"
policy_payload = git("show", f"{revision}:{policy_path}").decode("utf-8", "strict")
if "\r" in policy_payload or not policy_payload.endswith("\n"):
    fail("release gitlink policy must use LF and end in LF")
allowed_gitlinks = [
    normalized_repository_path(line, "release gitlink policy")
    for line in policy_payload[:-1].split("\n")
    if line and not line.startswith("#")
]
if len(allowed_gitlinks) != len(set(allowed_gitlinks)) or allowed_gitlinks != sorted(
    allowed_gitlinks, key=lambda value: value.encode("utf-8")
):
    fail("release gitlink policy must be unique and byte sorted")
if any(
    right.startswith(f"{left}/")
    for index, left in enumerate(allowed_gitlinks)
    for right in allowed_gitlinks[index + 1 :]
):
    fail("release gitlink policy paths must not overlap")

gitlinks = {
    path
    for path, mode, kind, _object_id in tree_entries
    if mode == "160000" or kind == "commit"
}
if gitlinks != set(allowed_gitlinks):
    fail("exact release tree gitlinks do not match the release exclusion policy")

modules_payload = git("show", f"{revision}:.gitmodules").decode("utf-8", "strict")
modules = configparser.ConfigParser(interpolation=None, strict=True)
try:
    modules.read_string(modules_payload)
except configparser.Error:
    fail("exact .gitmodules is not canonical")
declared_gitlinks: dict[str, str] = {}
for section in modules.sections():
    if not section.startswith('submodule "') or not section.endswith('"'):
        fail("exact .gitmodules contains an unsupported section")
    path = modules.get(section, "path", fallback=None)
    update = modules.get(section, "update", fallback=None)
    if path is None or update is None:
        fail("release-excluded gitlink lacks path or update policy")
    normalized = normalized_repository_path(path, "exact .gitmodules")
    if normalized in declared_gitlinks:
        fail("exact .gitmodules declares one path more than once")
    declared_gitlinks[normalized] = update
if set(declared_gitlinks) != set(allowed_gitlinks) or any(
    update != "none" for update in declared_gitlinks.values()
):
    fail("release-excluded gitlinks must be declared exactly once with update=none")

expected: dict[str, tuple[str, str, str]] = {}
for path, mode, kind, object_id in tree_entries:
    if path in gitlinks:
        continue
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

for gitlink in allowed_gitlinks:
    descendants = [
        path for path in actual if path == gitlink or path.startswith(f"{gitlink}/")
    ]
    if any(path != gitlink for path in descendants):
        fail(f"release-excluded gitlink contains extracted bytes: {gitlink}")
    observed = actual.pop(gitlink, None)
    if observed is not None and observed[:2] != ("040000", "tree"):
        fail(f"release-excluded gitlink is not an empty directory: {gitlink}")

for path, (mode, kind, object_id) in expected.items():
    observed = actual.get(path)
    if observed is None or observed[0] != mode or observed[1] != kind:
        fail(f"extracted release tree differs from exact Git object: {path}")
    if kind == "blob" and observed[2] != object_id:
        fail(f"extracted release bytes differ from exact Git object: {path}")
for path in actual:
    if path not in expected:
        fail(f"extracted release tree contains an undeclared path: {path}")
