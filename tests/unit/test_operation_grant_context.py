"""Sealed operation grants stay outside model-visible task surfaces."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_server.models import ParallelTaskRequest
from agent_server.routers import chat
from agent_server.services.headless_executor import (
    HeadlessRunContext,
    _headless_stdin_payload,
    _setup_headless_command,
)


def _grant() -> str:
    return f"{'a' * 96}.{'b' * 96}"


def _sealed_request(**overrides) -> ParallelTaskRequest:
    values = {
        "message": "run",
        "model": "gpt-5.6-sol",
        "allowed_tools": ["Bash"],
        "system_prompt": "platform boundary",
        "timeout_seconds": 900,
        "max_turns": 2,
        "execution_id": "exec-sealed",
        "resume_session_id": None,
        "persist_session": False,
        "images": None,
        "operation_grant": _grant(),
        "async_result": False,
    }
    values.update(overrides)
    return ParallelTaskRequest(**values)


def test_request_masks_operation_grant_in_repr_and_json():
    grant = _grant()
    request = ParallelTaskRequest(message="run", operation_grant=grant)

    assert request.operation_grant is not None
    assert request.operation_grant.get_secret_value() == grant
    assert grant not in repr(request)
    assert grant not in request.model_dump_json()


def test_setup_wraps_claude_without_putting_grant_in_argv():
    grant = _grant()
    context = _setup_headless_command(
        prompt="perform task",
        model="claude-sonnet-4-6",
        allowed_tools=["Bash"],
        system_prompt=None,
        timeout_seconds=30,
        max_turns=2,
        execution_id="operation-test",
        resume_session_id=None,
        persist_session=False,
        images=None,
        operation_grant=grant,
    )

    assert context.cmd[:2] == [
        "/usr/local/bin/trinity-task-context",
        "/usr/local/bin/claude",
    ]
    assert all(grant not in argument for argument in context.cmd)
    assert context.operation_grant == grant


def test_sealed_setup_forces_empty_mcp_and_no_session_persistence(monkeypatch, tmp_path):
    """WHY: a long sealed turn must not inherit MCP tools or write resumable JSONL state."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    (tmp_path / ".mcp.json").write_text('{"mcpServers":{"danger":{}}}')

    context = _setup_headless_command(
        prompt="perform task",
        model="claude-sonnet-4-6",
        allowed_tools=["Bash"],
        system_prompt=None,
        timeout_seconds=3600,
        max_turns=12,
        execution_id="operation-test",
        resume_session_id=None,
        persist_session=False,
        images=None,
        operation_grant=_grant(),
    )

    assert "--no-session-persistence" in context.cmd
    assert "--strict-mcp-config" in context.cmd
    mcp_index = context.cmd.index("--mcp-config")
    assert context.cmd[mcp_index + 1] == '{"mcpServers":{}}'
    assert str(tmp_path / ".mcp.json") not in context.cmd


def test_stdin_prefixes_grant_once_before_the_model_prompt():
    grant = _grant()
    context = HeadlessRunContext(
        cmd=["/usr/local/bin/claude"],
        task_session_id="operation-test",
        task_start_iso="2026-07-13T00:00:00Z",
        effective_timeout=30,
        images=None,
        prompt="perform task",
        operation_grant=grant,
    )

    assert _headless_stdin_payload(context) == f"{grant}\nperform task"


@pytest.mark.asyncio
async def test_legacy_task_endpoint_rejects_sealed_grant():
    with pytest.raises(HTTPException, match="sealed task endpoint") as failure:
        await chat.execute_task(ParallelTaskRequest(message="run", operation_grant=_grant()))

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_sealed_task_endpoint_requires_grant():
    with pytest.raises(HTTPException, match="requires an operation grant") as failure:
        await chat.execute_sealed_task(ParallelTaskRequest(message="run"))

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_async_sealed_task_rejected_before_dispatch():
    with pytest.raises(HTTPException, match="invalid sealed task contract") as failure:
        await chat.execute_sealed_task(_sealed_request(async_result=True))

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_unsupported_runtime_rejects_sealed_grant_before_execution(monkeypatch):
    runtime = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(sealed_operation_grant=False)
    )
    monkeypatch.setattr(chat.result_callback, "try_spawn_async", lambda _request: False)
    monkeypatch.setattr(chat, "get_runtime", lambda: runtime)
    monkeypatch.setattr(chat, "assert_sealed_runtime_eligible", lambda: None)

    with pytest.raises(HTTPException, match="does not support sealed operation grants") as failure:
        await chat.execute_sealed_task(_sealed_request())

    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_exact_inner_sealed_wire_reaches_runtime_gate(monkeypatch):
    request = _sealed_request()
    assert request.operation_wire_exact is True

    async def accepted(_request, *, operation_grant):
        assert operation_grant == _grant()
        return {"accepted": True}

    monkeypatch.setattr(chat, "_execute_task", accepted)
    monkeypatch.setattr(chat, "assert_sealed_runtime_eligible", lambda: None)
    assert await chat.execute_sealed_task(request) == {"accepted": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"shadow_override": "discarded"},
        {"max_turns": "2"},
        {"max_turns": True},
        {"async_result": 0},
        {"persist_session": 0},
        {"timeout_seconds": "900"},
        {"operation_wire_exact": True},
    ],
)
async def test_inner_sealed_wire_rejects_coercion_and_extra_fields(
    monkeypatch, overrides
):
    request = _sealed_request(**overrides)
    assert request.operation_wire_exact is False

    async def unexpected_dispatch(*_args, **_kwargs):
        pytest.fail("non-exact sealed wire reached runtime dispatch")

    monkeypatch.setattr(chat, "_execute_task", unexpected_dispatch)
    with pytest.raises(HTTPException, match="invalid sealed task contract") as failure:
        await chat.execute_sealed_task(request)

    assert failure.value.status_code == 422


def test_generic_runtime_without_hardened_profile_is_ineligible(tmp_path):
    from agent_server.services.sealed_runtime import sealed_runtime_eligibility

    result = sealed_runtime_eligibility(profile_path=tmp_path / "missing.json")
    assert result.eligible is False
    assert result.reason == "profile"


def test_hardened_profile_requires_exact_root_owned_runtime_boundaries(monkeypatch, tmp_path):
    from agent_server.services import sealed_runtime

    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"schemaVersion":"trinity-sealed-runtime/1","developerUid":1000,'
        '"operationUid":1001,"sudoAuthorized":false,'
        '"managedBashHook":true,"strictMcp":true}'
    )
    profile.chmod(0o444)
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"revision": "a" * 40}))
    source.chmod(0o444)
    artifact = tmp_path / "operation-monitor"
    artifact.write_text("reviewed monitor\n")
    artifact.chmod(0o555)
    contract = tmp_path / "artifact-contract.json"
    contract.write_text(
        json.dumps(
            {
                "schemaVersion": "trinity-sealed-artifact-contract/1",
                "sourceRevision": "a" * 40,
                "artifacts": [
                    {
                        "path": str(artifact),
                        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        "mode": "0555",
                        "uid": 0,
                        "gid": 0,
                    }
                ],
            }
        )
    )
    contract.chmod(0o444)
    monkeypatch.setattr(
        sealed_runtime,
        "_REQUIRED_BOUNDARIES",
        ((profile, 0o444, False), (contract, 0o444, False)),
    )
    monkeypatch.setattr(sealed_runtime, "_REQUIRED_ARTIFACT_PATHS", frozenset({str(artifact)}))
    monkeypatch.setattr(sealed_runtime, "_SOURCE_IDENTITY_PATH", source)
    monkeypatch.setattr(sealed_runtime, "verify_manifest", lambda _path: True)
    monkeypatch.setattr(
        sealed_runtime,
        "_boundary_valid",
        lambda path, expected_mode, _require_root: path.is_file()
        and (path.stat().st_mode & 0o777) == expected_mode,
    )
    monkeypatch.setattr(sealed_runtime.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "getgid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "getegid", lambda: 1000)
    monkeypatch.setattr(sealed_runtime.os, "getgroups", lambda: [])
    monkeypatch.setattr(sealed_runtime, "_effective_capabilities", lambda: 0)
    monkeypatch.setattr(sealed_runtime, "_sudo_authorized", lambda: False)
    monkeypatch.setattr(sealed_runtime, "_operation_identity_valid", lambda: True)

    assert sealed_runtime.sealed_runtime_eligibility(
        profile_path=profile, artifact_contract_path=contract
    ).eligible is True

    original_contract = contract.read_text()
    extra = tmp_path / "unreviewed-helper"
    extra.write_text("unreviewed helper\n")
    extra.chmod(0o555)
    expanded = json.loads(original_contract)
    expanded["artifacts"].append(
        {
            "path": str(extra),
            "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
            "mode": "0555",
            "uid": 0,
            "gid": 0,
        }
    )
    expanded["artifacts"].sort(key=lambda row: row["path"])
    contract.chmod(0o644)
    contract.write_text(json.dumps(expanded))
    contract.chmod(0o444)
    result = sealed_runtime.sealed_runtime_eligibility(
        profile_path=profile, artifact_contract_path=contract
    )
    assert result.eligible is False
    assert result.reason == "artifact-contract"
    contract.chmod(0o644)
    contract.write_text(original_contract)
    contract.chmod(0o444)

    artifact.chmod(0o755)
    artifact.write_text("tampered monitor\n")
    artifact.chmod(0o555)
    result = sealed_runtime.sealed_runtime_eligibility(
        profile_path=profile, artifact_contract_path=contract
    )
    assert result.eligible is False
    assert result.reason == "artifact-contract"
    artifact.chmod(0o755)
    artifact.write_text("reviewed monitor\n")
    artifact.chmod(0o555)

    profile.chmod(0o644)
    result = sealed_runtime.sealed_runtime_eligibility(
        profile_path=profile, artifact_contract_path=contract
    )
    assert result.eligible is False
    assert result.reason == "boundary"


def test_hardened_runtime_rejects_any_sudo_binary(monkeypatch, tmp_path):
    from agent_server.services import sealed_runtime

    sudo = tmp_path / "sudo"
    sudo.write_text("not executable but still authority drift")
    monkeypatch.setattr(sealed_runtime, "_SUDO_PATHS", (sudo,))
    assert sealed_runtime._sudo_authorized() is True


def test_operation_identity_requires_exact_nologin_uid_and_gid(monkeypatch):
    from agent_server.services import sealed_runtime

    valid = SimpleNamespace(
        pw_name="fc-operation",
        pw_uid=1001,
        pw_gid=1001,
        pw_shell="/usr/sbin/nologin",
    )
    monkeypatch.setattr(sealed_runtime.pwd, "getpwuid", lambda _uid: valid)
    assert sealed_runtime._operation_identity_valid() is True

    monkeypatch.setattr(
        sealed_runtime.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(**{**valid.__dict__, "pw_gid": 1000}),
    )
    assert sealed_runtime._operation_identity_valid() is False


def test_sealed_route_is_distinct_from_legacy_task_route():
    paths = {route.path for route in chat.router.routes}
    assert "/api/task" in paths
    assert "/api/task/sealed" in paths


def test_native_runner_is_fixed_to_reviewed_runtimes_and_root_context_paths():
    source = (
        Path(__file__).parents[2] / "docker/base-image/task-context-runner.c"
    ).read_text()

    assert '"/usr/local/bin/claude"' in source
    assert '"/usr/local/bin/codex"' in source
    assert 'CODEX_SOURCE_HOME "/home/developer/.codex-subscription"' in source
    assert 'CODEX_SOURCE_PARENT "/home/developer"' in source
    assert 'CODEX_SOURCE_DIRECTORY ".codex-subscription"' in source
    assert 'CODEX_TASK_ROOT "/run/trinity-codex"' in source
    assert 'CODEX_RULES_SOURCE "/etc/trinity/codex-operation.rules"' in source
    assert 'CONTEXT_ROOT "/run/trinity-task-context"' in source
    assert "setsid()" in source
    assert '"session-start"' in source
    assert '"/proc/%ld/stat"' in source
    assert "PR_SET_PDEATHSIG" in source
    assert "O_NOFOLLOW" in source
    assert "clearenv()" in source
    assert "setresgid(OPERATION_GID, OPERATION_GID, OPERATION_GID)" in source
    assert "setresuid(OPERATION_UID, OPERATION_UID, OPERATION_UID)" in source
    assert '#define OPERATION_UID 1001' in source
    assert 'set_optional_environment("CLAUDE_CODE_OAUTH_TOKEN"' in source
    assert 'set_optional_environment("CODEX_HOME", is_codex ? codex_task_home : NULL)' in source
    assert "open_codex_auth_source(&codex_auth_source_parent_fd" in source
    assert "fstatat(parent_fd, CODEX_SOURCE_DIRECTORY" in source
    assert "openat(directory_fd, \"auth.json\"" in source
    assert "flock(fd, LOCK_EX)" in source
    assert "getrandom(random_bytes" in source
    assert "install_codex_task_home(codex_task_home, codex_auth_source_fd)" in source
    assert "refresh_codex_auth(codex_task_home, codex_auth_source_parent_fd" in source
    assert "remove_codex_task_home(codex_task_home)" in source
    assert 'copy_optional_environment("OPENAI_API_KEY")' not in source
    assert 'copy_optional_environment("CODEX_API_KEY")' not in source
    assert 'is_claude ? copy_optional_environment("HTTP_PROXY") : NULL' in source
    assert 'is_claude ? copy_optional_environment("NODE_EXTRA_CA_CERTS") : NULL' in source

    verifier_start = source.index("if (verifier_pid == 0)")
    verifier_child = source[
        verifier_start : source.index('fail("exec verifier")', verifier_start)
    ]
    assert verifier_child.index("drop_to_operation_identity();") < verifier_child.index(
        "execl(CONTEXT_VERIFIER"
    )


def test_codex_refresh_is_atomic_and_every_fatal_path_attempts_cleanup():
    """WHY: rejected or interrupted refreshes must not truncate auth or strand task state."""
    source = (
        Path(__file__).parents[2] / "docker/base-image/task-context-runner.c"
    ).read_text()

    fail_body = source[source.index("static void fail(") : source.index("static void forward_signal")]
    assert "cleanup_active_runtime();" in fail_body
    assert fail_body.index("cleanup_active_runtime();") < fail_body.index("_exit(126);")
    assert source.count("_exit(126);") == 2

    atomic_replace = source[
        source.index("static int atomic_replace_codex_auth(") : source.index(
            "static int refresh_codex_auth(",
            source.index("static int atomic_replace_codex_auth("),
        )
    ]
    assert "ftruncate(" not in atomic_replace
    assert "rename_auth_exchange(" in atomic_replace
    assert "rollback_auth_exchange(" in atomic_replace
    assert "fsync(auth_source_directory_fd)" in atomic_replace
    assert "unlinkat(auth_source_directory_fd, temporary_name, 0)" in atomic_replace
    assert "source_projection_target_unchanged(" in atomic_replace
    assert atomic_replace.index("rename_auth_exchange(") < atomic_replace.index(
        "fchown(temporary_fd"
    )

    refresh = source[
        source.index("static int refresh_codex_auth(") : source.index(
            "static int remove_tree_node", source.index("static int refresh_codex_auth(")
        )
    ]
    assert "validate_codex_auth(buffer, length) != 0" in refresh
    assert "atomic_replace_codex_auth(" in refresh

    assert "active_codex_task_home" in source
    assert "active_context_pid" in source


@pytest.mark.skipif(sys.platform != "linux", reason="native runner targets Linux")
def test_codex_auth_atomic_replace_preserves_original_on_failure(tmp_path):
    """WHY: a failed refresh must preserve auth bytes and remove its temporary file."""
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("gcc is required for the native runner harness")

    runner = Path(__file__).parents[2] / "docker/base-image/task-context-runner.c"
    include_path = str(runner).replace("\\", "\\\\").replace('"', '\\"')
    harness = tmp_path / "task-context-atomic-refresh.c"
    harness.write_text(
        f'''#define _GNU_SOURCE
#include <dirent.h>
#define main trinity_task_context_main
#include "{include_path}"
#undef main

static void harness_fail(const char *message) {{
  perror(message);
  exit(1);
}}

static void write_exact(int fd, const char *value, size_t length) {{
  if (write_all_status(fd, value, length) != 0 || fsync(fd) != 0)
    harness_fail("write fixture");
}}

int main(void) {{
  char root[] = "/tmp/trinity-auth-refresh-XXXXXX";
  if (mkdtemp(root) == NULL) harness_fail("mkdtemp");
  char source_directory[512];
  if (snprintf(source_directory, sizeof(source_directory), "%s/%s", root,
               CODEX_SOURCE_DIRECTORY) >= (int)sizeof(source_directory))
    harness_fail("source path");
  if (mkdir(source_directory, 0700) != 0) harness_fail("mkdir source");

  int parent_fd = open(root, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (parent_fd < 0) harness_fail("open parent");
  int directory_fd = openat(parent_fd, CODEX_SOURCE_DIRECTORY,
                            O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (directory_fd < 0) harness_fail("open source directory");
  int fixture_fd = openat(directory_fd, "auth.json",
                          O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fixture_fd < 0) harness_fail("create auth");
  write_exact(fixture_fd, "original", 8);
  if (close(fixture_fd) != 0) harness_fail("close fixture");

  int source_fd = openat(directory_fd, "auth.json", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (source_fd < 0 || flock(source_fd, LOCK_EX) != 0) harness_fail("lock source");
  struct stat directory_identity;
  struct stat auth_identity;
  if (fstat(directory_fd, &directory_identity) != 0 || fstat(source_fd, &auth_identity) != 0)
    harness_fail("source identity");

  if (atomic_replace_codex_auth(parent_fd, directory_fd, source_fd,
                                &directory_identity, &auth_identity,
                                "fresh", 5, getuid(), getgid()) != 0)
    harness_fail("atomic replace");

  int named_fd = openat(directory_fd, "auth.json", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (named_fd < 0) harness_fail("open replaced auth");
  struct stat named_identity;
  if (fstat(named_fd, &named_identity) != 0 ||
      (named_identity.st_dev == auth_identity.st_dev &&
       named_identity.st_ino == auth_identity.st_ino))
    harness_fail("replacement identity");
  char bytes[16] = {{0}};
  if (read(named_fd, bytes, sizeof(bytes)) != 5 || memcmp(bytes, "fresh", 5) != 0)
    harness_fail("replacement bytes");
  memset(bytes, 0, sizeof(bytes));
  if (lseek(source_fd, 0, SEEK_SET) != 0 || read(source_fd, bytes, sizeof(bytes)) != 8 ||
      memcmp(bytes, "original", 8) != 0)
    harness_fail("original bytes");
  if (close(named_fd) != 0 || close(source_fd) != 0) harness_fail("close first source");

  source_fd = openat(directory_fd, "auth.json", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (source_fd < 0 || flock(source_fd, LOCK_EX) != 0 ||
      fstat(source_fd, &auth_identity) != 0)
    harness_fail("reopen source");
  if (atomic_replace_codex_auth(parent_fd, directory_fd, source_fd,
                                &directory_identity, &auth_identity,
                                "bad", 3, (uid_t)-1, getgid()) == 0) {{
    fputs("invalid replacement ownership was accepted\\n", stderr);
    return 1;
  }}
  if (close(source_fd) != 0) harness_fail("close stale source");

  named_fd = openat(directory_fd, "auth.json", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  memset(bytes, 0, sizeof(bytes));
  if (named_fd < 0 || read(named_fd, bytes, sizeof(bytes)) != 5 ||
      memcmp(bytes, "fresh", 5) != 0)
    harness_fail("preserved bytes");
  if (close(named_fd) != 0) harness_fail("close preserved auth");

  DIR *directory = fdopendir(dup(directory_fd));
  if (directory == NULL) harness_fail("scan source directory");
  struct dirent *entry;
  while ((entry = readdir(directory)) != NULL) {{
    if (strncmp(entry->d_name, ".auth.json.refresh-", 19) == 0) {{
      fputs("temporary auth file was stranded\\n", stderr);
      return 1;
    }}
  }}
  if (closedir(directory) != 0) harness_fail("close source scan");

  if (unlinkat(directory_fd, "auth.json", 0) != 0 || close(directory_fd) != 0 ||
      close(parent_fd) != 0 || rmdir(source_directory) != 0 || rmdir(root) != 0)
    harness_fail("cleanup fixture");
  return 0;
}}
'''
    )
    binary = tmp_path / "task-context-atomic-refresh"
    compile_result = subprocess.run(
        [
            gcc,
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fPIE",
            "-pie",
            str(harness),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run([str(binary)], capture_output=True, text=True, check=False)
    assert run_result.returncode == 0, run_result.stderr


def test_public_request_masks_and_excludes_operation_grant():
    from models import ParallelTaskRequest as PublicParallelTaskRequest

    grant = _grant()
    request = PublicParallelTaskRequest(message="run", operation_grant=grant)

    assert request.operation_grant is not None
    assert request.operation_grant.get_secret_value() == grant
    assert grant not in repr(request)
    assert "operation_grant" not in request.model_dump()
    assert "operation_wire_exact" not in request.model_dump()
