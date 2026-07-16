"""Canonical packaged runtime tree used by sealed-operation admission."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Mapping

SCHEMA_VERSION = "trinity-packaged-runtime-tree/1"
DEFAULT_ROOTS = (
    Path("/app"),
    Path("/opt/flashcrafter/app"),
    Path("/opt/flashcrafter/bin"),
    Path("/opt/flashcrafter/fixtures"),
    Path("/opt/flashcrafter/skills"),
    Path("/opt/trinity"),
)
DEFAULT_EXTERNAL_PATHS = (
    Path("/etc/claude-code/managed-settings.json"),
    Path("/opt/flashcrafter/AGENTS.md"),
    Path("/opt/flashcrafter/agent.json"),
    Path("/opt/flashcrafter/base.json"),
    Path("/opt/flashcrafter/bin/claude-unrestricted"),
    Path("/opt/flashcrafter/migration-report.json"),
    Path("/opt/flashcrafter/runtime-exclusions.json"),
    Path("/opt/flashcrafter/skills.json"),
    Path("/usr/local/bin/codex"),
    Path("/usr/bin/node"),
)
DEFAULT_DYNAMIC_SYMLINKS = {
    Path("/opt/flashcrafter/app/.env"): "/home/developer/.env",
}


def _node_package_root(path: Path) -> Path | None:
    parts = path.parts
    try:
        marker = parts.index("node_modules")
    except ValueError:
        return None
    if marker + 1 >= len(parts):
        return None
    length = marker + 3 if parts[marker + 1].startswith("@") else marker + 2
    if length > len(parts):
        return None
    return Path(*parts[:length])


def _entry(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    common: dict[str, object] = {
        "gid": metadata.st_gid,
        "mode": f"0{stat.S_IMODE(metadata.st_mode):o}",
        "path": str(path),
        "uid": metadata.st_uid,
    }
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_nlink != 1:
            raise ValueError(f"packaged runtime file has multiple links: {path}")
        return {
            **common,
            "kind": "file",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    if stat.S_ISDIR(metadata.st_mode):
        return {**common, "kind": "directory"}
    if stat.S_ISLNK(metadata.st_mode):
        return {**common, "kind": "symlink", "target": os.readlink(path)}
    raise ValueError(f"unsupported packaged runtime node: {path}")


def build_manifest(
    roots: tuple[Path, ...] = DEFAULT_ROOTS,
    external_paths: tuple[Path, ...] = DEFAULT_EXTERNAL_PATHS,
    *,
    require_root_owned: bool = True,
    dynamic_symlinks: Mapping[Path, str] = DEFAULT_DYNAMIC_SYMLINKS,
) -> dict[str, object]:
    expanded_roots = set(roots)
    for path in external_paths:
        resolved = path.resolve(strict=True)
        package_root = _node_package_root(resolved)
        if package_root is not None:
            expanded_roots.add(package_root)

    entries: dict[str, dict[str, object]] = {}

    def covered(path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in expanded_roots)

    def add(path: Path) -> dict[str, object]:
        entry = _entry(path)
        if require_root_owned and (entry["uid"] != 0 or entry["gid"] != 0):
            raise ValueError(f"packaged runtime node is not root-owned: {path}")
        mode = int(str(entry["mode"]), 8)
        if entry["kind"] != "symlink" and mode & 0o022:
            raise ValueError(f"packaged runtime node is group/other writable: {path}")
        entries[str(path)] = entry
        return entry

    for root in sorted(expanded_roots, key=str):
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"packaged runtime root is invalid: {root}")
        pending = [root]
        while pending:
            path = pending.pop()
            entry = add(path)
            if entry["kind"] == "directory":
                pending.extend(
                    sorted(path.iterdir(), key=lambda item: str(item), reverse=True)
                )
            elif entry["kind"] == "symlink":
                target = str(entry["target"])
                if dynamic_symlinks.get(path) == target:
                    continue
                try:
                    resolved = path.resolve(strict=True)
                except OSError as exc:
                    raise ValueError(
                        f"packaged runtime symlink target is unavailable: {path}"
                    ) from exc
                if not covered(resolved):
                    raise ValueError(
                        f"packaged runtime symlink escapes the closed roots: {path}"
                    )
    for path in external_paths:
        add(path)
        resolved = path.resolve(strict=True)
        if not covered(resolved):
            add(resolved)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "entries": [entries[path] for path in sorted(entries)],
    }


def write_manifest(output: Path) -> None:
    payload = build_manifest()
    output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chown(output, 0, 0)
    output.chmod(0o444)


def verify_manifest(
    path: Path,
    roots: tuple[Path, ...] = DEFAULT_ROOTS,
    external_paths: tuple[Path, ...] = DEFAULT_EXTERNAL_PATHS,
) -> bool:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or metadata.st_nlink != 1
        ):
            return False
        declared = json.loads(path.read_text(encoding="utf-8"))
        return declared == build_manifest(roots, external_paths)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "write":
        raise SystemExit("usage: sealed_artifact_tree.py write OUTPUT")
    write_manifest(Path(sys.argv[2]))
