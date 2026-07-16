#!/usr/bin/env python3
"""Compare a live filesystem tree with the exact semantics of a tar archive."""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
import sys
import tarfile
from collections import Counter
from typing import Any

Record = dict[str, Any]
RecordMap = dict[bytes, Record]


def _digest_file(path: bytes) -> str:
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal_path(raw: bytes) -> bytes:
    while raw.startswith(b"./"):
        raw = raw[2:]
    raw = raw.rstrip(b"/")
    if raw in (b"", b"."):
        return b"."
    if raw.startswith(b"/") or b"\0" in raw:
        raise ValueError("archive contains a non-relative path")
    normalized = posixpath.normpath(raw)
    if normalized in (b"", b".", b"..") or normalized.startswith(b"../"):
        raise ValueError("archive path escapes the restored tree")
    if normalized != raw:
        raise ValueError("archive contains a non-canonical path")
    return normalized


def _node_record(info: os.stat_result) -> Record:
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        kind = "directory"
    elif stat.S_ISREG(mode):
        kind = "file"
    elif stat.S_ISLNK(mode):
        kind = "symlink"
    elif stat.S_ISCHR(mode):
        kind = "character-device"
    elif stat.S_ISBLK(mode):
        kind = "block-device"
    elif stat.S_ISFIFO(mode):
        kind = "fifo"
    elif stat.S_ISSOCK(mode):
        kind = "socket"
    else:
        kind = "unknown"
    record: Record = {
        "kind": kind,
        "mode": stat.S_IMODE(mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }
    if kind != "directory":
        record["mtime"] = info.st_mtime_ns // 1_000_000_000
    if kind == "file":
        record["size"] = info.st_size
        record["nlink"] = info.st_nlink
    elif kind in ("character-device", "block-device"):
        record["device"] = (os.major(info.st_rdev), os.minor(info.st_rdev))
    return record


def snapshot_tree(root: str | os.PathLike[str]) -> RecordMap:
    absolute = os.fsencode(os.path.abspath(root))
    rows: RecordMap = {}
    regular_inodes: dict[tuple[int, int], list[bytes]] = {}
    stack: list[tuple[bytes, bytes]] = [(absolute, b".")]
    while stack:
        path, relative = stack.pop()
        info = os.lstat(path)
        record = _node_record(info)
        if record["kind"] == "file":
            record["content"] = _digest_file(path)
            regular_inodes.setdefault((info.st_dev, info.st_ino), []).append(relative)
        elif record["kind"] == "symlink":
            record["target"] = os.fsencode(os.readlink(path))
        elif record["kind"] == "directory":
            children = sorted(
                os.scandir(path), key=lambda entry: os.fsencode(entry.name), reverse=True
            )
            for child in children:
                name = os.fsencode(child.name)
                child_relative = name if relative == b"." else relative + b"/" + name
                stack.append((os.path.join(path, name), child_relative))
        rows[relative] = record
    for paths in regular_inodes.values():
        topology = tuple(sorted(paths))
        for path in paths:
            rows[path]["hardlinks"] = topology
    return rows


def _tar_kind(member: tarfile.TarInfo) -> str:
    if member.isreg() or member.islnk():
        return "file"
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    if member.ischr():
        return "character-device"
    if member.isblk():
        return "block-device"
    if member.isfifo():
        return "fifo"
    return "unknown"


def snapshot_archive(path: str | os.PathLike[str]) -> RecordMap:
    rows: RecordMap = {}
    hardlink_targets: dict[bytes, bytes] = {}
    with tarfile.open(path, "r:*", encoding="utf-8", errors="surrogateescape") as archive:
        for member in archive.getmembers():
            relative = _normal_path(os.fsencode(member.name))
            if relative in rows:
                raise ValueError("archive contains duplicate paths")
            kind = _tar_kind(member)
            if kind == "unknown":
                raise ValueError("archive contains an unsupported node type")
            record: Record = {
                "kind": kind,
                "mode": member.mode,
                "uid": member.uid,
                "gid": member.gid,
            }
            if kind != "directory":
                record["mtime"] = int(member.mtime)
            if member.isreg():
                handle = archive.extractfile(member)
                if handle is None:
                    raise ValueError("archive regular file has no content")
                digest = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                record.update(size=member.size, content=digest.hexdigest())
            elif member.islnk():
                hardlink_targets[relative] = _normal_path(os.fsencode(member.linkname))
            elif kind == "symlink":
                record["target"] = os.fsencode(member.linkname)
            elif kind in ("character-device", "block-device"):
                record["device"] = (member.devmajor, member.devminor)
            rows[relative] = record

    parents = {entry: entry for entry, row in rows.items() if row["kind"] == "file"}

    def find(entry: bytes) -> bytes:
        while parents[entry] != entry:
            parents[entry] = parents[parents[entry]]
            entry = parents[entry]
        return entry

    def union(left: bytes, right: bytes) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for source, target in hardlink_targets.items():
        if target not in parents:
            raise ValueError("archive hardlink target is missing or not a file")
        union(source, target)

    groups: dict[bytes, list[bytes]] = {}
    for entry in parents:
        groups.setdefault(find(entry), []).append(entry)
    for paths in groups.values():
        topology = tuple(sorted(paths))
        content_rows = [rows[item] for item in paths if "content" in rows[item]]
        if len(content_rows) != 1:
            raise ValueError("archive hardlink group has no unique content source")
        content = content_rows[0]
        for item in paths:
            rows[item]["content"] = content["content"]
            rows[item]["size"] = content["size"]
            rows[item]["nlink"] = len(paths)
            rows[item]["hardlinks"] = topology
    return rows


def compare_record_maps(source: RecordMap, archive: RecordMap) -> Counter[str]:
    differences: Counter[str] = Counter()
    differences["missing"] += len(source.keys() - archive.keys())
    differences["extra"] += len(archive.keys() - source.keys())
    for path in source.keys() & archive.keys():
        left, right = source[path], archive[path]
        for field in ("kind", "mode", "uid", "gid"):
            label = "type" if field == "kind" else field
            differences[label] += left.get(field) != right.get(field)
        if left.get("kind") == right.get("kind"):
            fields = {
                "file": ("mtime", "size", "content", "nlink", "hardlinks"),
                "symlink": ("mtime", "target"),
                "character-device": ("mtime", "device"),
                "block-device": ("mtime", "device"),
                "fifo": ("mtime",),
                "socket": ("mtime",),
            }.get(str(left.get("kind")), ())
            for field in fields:
                differences[field] += left.get(field) != right.get(field)
    return Counter({key: value for key, value in differences.items() if value})


def compare_source_to_archive(source: str, archive: str) -> Counter[str]:
    return compare_record_maps(snapshot_tree(source), snapshot_archive(archive))


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify-restored-tree.py SOURCE_ROOT ARCHIVE")
    differences = compare_source_to_archive(sys.argv[1], sys.argv[2])
    if differences:
        summary = ",".join(f"{key}={differences[key]}" for key in sorted(differences))
        print(f"semantic tree mismatch: {summary}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
