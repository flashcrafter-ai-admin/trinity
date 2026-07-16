"""Canonical packaged runtime tree used by sealed-operation admission."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import site
import stat
import sys
import sysconfig
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
    Path("/etc/trinity/codex-operation.rules"),
    Path("/etc/trinity/validate-codex-auth.py"),
    Path("/opt/flashcrafter/AGENTS.md"),
    Path("/opt/flashcrafter/agent.json"),
    Path("/opt/flashcrafter/base.json"),
    Path("/opt/flashcrafter/bin/claude-unrestricted"),
    Path("/opt/flashcrafter/migration-report.json"),
    Path("/opt/flashcrafter/runtime-exclusions.json"),
    Path("/opt/flashcrafter/skills.json"),
    Path("/usr/local/bin/codex"),
    Path("/usr/local/bin/trinity-task-context"),
    Path("/usr/bin/node"),
)
DEFAULT_DYNAMIC_SYMLINKS = {
    Path("/opt/flashcrafter/app/.env"): "/home/developer/.env",
}
DEFAULT_OPERATION_CONTRACT = Path("/opt/flashcrafter/operation-contract.json")


def _existing_directories(paths: set[Path]) -> tuple[Path, ...]:
    return tuple(sorted((path for path in paths if path.is_dir()), key=str))


def python_runtime_roots() -> tuple[Path, ...]:
    """Resolve every Python library tree the packaged server can import."""
    roots: set[Path] = set()
    configured = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = configured.get(key)
        if value:
            roots.add(Path(value))
    for value in site.getsitepackages():
        roots.add(Path(value))
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        roots.add(Path(user_site))
    for pattern in (
        "/usr/lib/python3*",
        "/usr/lib/python3/dist-packages",
        "/usr/local/lib/python3*",
        "/home/developer/.local/lib/python*/site-packages",
    ):
        roots.update(Path("/").glob(pattern.lstrip("/")))
    return _existing_directories(roots)


def operation_executable_paths(
    contract_path: Path = DEFAULT_OPERATION_CONTRACT,
) -> tuple[Path, ...]:
    """Return every exact executable named by the signed operation contract."""
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("operation contract is unavailable for runtime closure") from exc
    operations = contract.get("operations") if isinstance(contract, dict) else None
    if not isinstance(operations, dict) or not operations:
        raise ValueError("operation contract has no executable operations")
    paths: set[Path] = set()
    for operation in operations.values():
        commands = operation.get("commands") if isinstance(operation, dict) else None
        if not isinstance(commands, list) or not commands:
            raise ValueError("operation contract command closure is incomplete")
        for command in commands:
            executable = command.get("executable") if isinstance(command, dict) else None
            if not isinstance(executable, str) or not executable.startswith("/"):
                raise ValueError("operation contract executable is invalid")
            paths.add(Path(executable))
    return tuple(sorted(paths, key=str))


def runtime_roots() -> tuple[Path, ...]:
    return tuple(dict.fromkeys((*DEFAULT_ROOTS, *python_runtime_roots())))


def runtime_external_paths(
    contract_path: Path = DEFAULT_OPERATION_CONTRACT,
) -> tuple[Path, ...]:
    candidates = {
        *DEFAULT_EXTERNAL_PATHS,
        Path(sys.executable),
        *operation_executable_paths(contract_path),
    }
    for path in (Path("/usr/bin/python3"), Path("/usr/local/bin/python3")):
        if path.exists() or path.is_symlink():
            candidates.add(path)
    return tuple(sorted(candidates, key=str))


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
    roots: tuple[Path, ...] | None = None,
    external_paths: tuple[Path, ...] | None = None,
    *,
    require_root_owned: bool = True,
    dynamic_symlinks: Mapping[Path, str] = DEFAULT_DYNAMIC_SYMLINKS,
) -> dict[str, object]:
    if roots is None:
        roots = runtime_roots()
    if external_paths is None:
        external_paths = runtime_external_paths()
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
    roots: tuple[Path, ...] | None = None,
    external_paths: tuple[Path, ...] | None = None,
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
