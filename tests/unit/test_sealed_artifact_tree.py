from __future__ import annotations

from pathlib import Path

import pytest

from agent_server.services.sealed_artifact_tree import (
    DEFAULT_EXTERNAL_PATHS,
    DEFAULT_ROOTS,
    build_manifest,
)


def _paths(manifest: dict[str, object]) -> list[str]:
    entries = manifest["entries"]
    assert isinstance(entries, list)
    return [str(entry["path"]) for entry in entries]


def test_default_manifest_covers_server_instructions_hooks_and_packaged_metadata():
    assert DEFAULT_ROOTS == (
        Path("/app"),
        Path("/opt/flashcrafter/app"),
        Path("/opt/flashcrafter/bin"),
        Path("/opt/flashcrafter/fixtures"),
        Path("/opt/flashcrafter/skills"),
        Path("/opt/trinity"),
    )
    assert {
        Path("/etc/claude-code/managed-settings.json"),
        Path("/opt/flashcrafter/AGENTS.md"),
        Path("/opt/flashcrafter/agent.json"),
        Path("/opt/flashcrafter/base.json"),
        Path("/opt/flashcrafter/migration-report.json"),
        Path("/opt/flashcrafter/runtime-exclusions.json"),
        Path("/opt/flashcrafter/skills.json"),
    }.issubset(DEFAULT_EXTERNAL_PATHS)


def test_manifest_closes_runtime_trees_and_resolved_launcher_targets(tmp_path: Path):
    app = tmp_path / "app"
    instructions = tmp_path / "AGENTS.md"
    package = tmp_path / "node_modules" / "@vendor" / "runtime"
    bin_dir = tmp_path / "bin"
    app.mkdir()
    package.mkdir(parents=True)
    bin_dir.mkdir()
    (app / "instructions.md").write_text("sealed instructions\n")
    instructions.write_text("sealed agent contract\n")
    target = package / "cli.js"
    target.write_text("sealed launcher\n")
    launcher = bin_dir / "runtime"
    launcher.symlink_to(target)

    manifest = build_manifest(
        (app,), (instructions, launcher), require_root_owned=False
    )
    paths = _paths(manifest)
    assert paths == sorted(paths)
    assert str(app / "instructions.md") in paths
    assert str(instructions) in paths
    assert str(launcher) in paths
    assert str(target) in paths

    target.write_text("mutated launcher\n")
    assert build_manifest((app,), (launcher,), require_root_owned=False) != manifest


def test_manifest_rejects_writable_runtime_nodes(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    mutable = app / "mutable"
    mutable.write_text("unsafe\n")
    mutable.chmod(0o666)

    with pytest.raises(ValueError, match="group/other writable"):
        build_manifest((app,), (), require_root_owned=False)


def test_manifest_rejects_unreviewed_symlink_targets_outside_closed_roots(
    tmp_path: Path,
):
    app = tmp_path / "app"
    outside = tmp_path / "outside"
    app.mkdir()
    outside.write_text("unreviewed executable\n")
    (app / "escape").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes the closed roots"):
        build_manifest((app,), (), require_root_owned=False, dynamic_symlinks={})


def test_manifest_closes_every_packaged_launcher_not_only_named_artifacts(
    tmp_path: Path,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "reviewed-launcher").write_text("reviewed\n")
    first = build_manifest((bin_dir,), (), require_root_owned=False)

    (bin_dir / "unexpected-launcher").write_text("unexpected\n")
    second = build_manifest((bin_dir,), (), require_root_owned=False)

    assert first != second
    assert str(bin_dir / "unexpected-launcher") in _paths(second)


def test_manifest_allows_only_the_exact_declared_dynamic_projection(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    projection = app / ".env"
    projection.symlink_to("/ephemeral/agent.env")

    manifest = build_manifest(
        (app,),
        (),
        require_root_owned=False,
        dynamic_symlinks={projection: "/ephemeral/agent.env"},
    )
    assert str(projection) in _paths(manifest)

    projection.unlink()
    projection.symlink_to("/ephemeral/other.env")
    with pytest.raises(ValueError, match="unavailable"):
        build_manifest(
            (app,),
            (),
            require_root_owned=False,
            dynamic_symlinks={projection: "/ephemeral/agent.env"},
        )
