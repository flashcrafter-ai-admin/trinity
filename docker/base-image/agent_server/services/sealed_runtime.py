"""Fail-closed eligibility checks for signed operation execution."""

from dataclasses import dataclass
import grp
import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import re
import stat

from fastapi import HTTPException

from .sealed_artifact_tree import verify_manifest


logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_PATH = Path("/etc/trinity/sealed-runtime.json")
_DEFAULT_ARTIFACT_CONTRACT_PATH = Path(
    "/opt/flashcrafter/sealed-artifact-contract.json"
)
_SOURCE_IDENTITY_PATH = Path("/opt/flashcrafter/source.json")
_PACKAGED_RUNTIME_TREE_PATH = Path("/opt/flashcrafter/packaged-runtime-tree.json")
_EXPECTED_PROFILE = {
    "schemaVersion": "trinity-sealed-runtime/1",
    "developerUid": 1000,
    "operationUid": 1001,
    "sudoAuthorized": False,
    "managedBashHook": True,
    "strictMcp": True,
}

_ARTIFACT_CONTRACT_SCHEMA = "trinity-sealed-artifact-contract/1"
_MAX_ARTIFACTS = 512
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUDO_PATHS = (Path("/usr/bin/sudo"), Path("/bin/sudo"))

# The leaf image owns these artifacts, but Trinity owns the admission rule. The
# leaf must enumerate and hash every one in a root-owned contract so exact image
# attestation can bind the external monitor to reviewed source.
_REQUIRED_ARTIFACT_PATHS = frozenset(
    {
        "/etc/claude-code/managed-settings.json",
        "/etc/trinity/codex-operation.rules",
        "/etc/trinity/validate-codex-auth.py",
        "/etc/trinity/sealed-runtime.json",
        "/opt/flashcrafter/bin/claude-operation-runtime",
        "/opt/flashcrafter/bin/codex-operation-shell",
        "/opt/flashcrafter/bin/entrypoint.sh",
        "/opt/flashcrafter/bin/fc-operation-boundary-canary",
        "/opt/flashcrafter/bin/fc-operation-broker",
        "/opt/flashcrafter/bin/fc-operation-drop",
        "/opt/flashcrafter/bin/fc-operation-exec",
        "/opt/flashcrafter/bin/fc-operation-skill",
        "/opt/flashcrafter/bin/operation-exec.ts",
        "/opt/flashcrafter/bin/operation-hook.ts",
        "/opt/flashcrafter/bin/operation-lib.ts",
        "/opt/flashcrafter/bin/operation-skill.ts",
        "/opt/flashcrafter/bin/operation-verify.ts",
        "/opt/flashcrafter/bin/runtime-audit.sh",
        "/opt/flashcrafter/bin/runtime-portability-audit.sh",
        "/opt/flashcrafter/bin/verify-discovery.sh",
        "/opt/flashcrafter/operation-contract.json",
        "/opt/flashcrafter/operation-grant-public-key.der",
        "/opt/flashcrafter/packaged-runtime-tree.json",
        "/opt/flashcrafter/source.json",
        "/usr/local/bin/bun",
        "/usr/local/bin/claude",
        "/usr/local/bin/trinity-task-context",
    }
)

# These files are split across the governed Trinity base and the hardened
# fc-agency leaf image. A generic Trinity image therefore cannot satisfy this
# boundary merely because it exposes the sealed endpoint.
_REQUIRED_BOUNDARIES = (
    (_DEFAULT_PROFILE_PATH, 0o444, True),
    (_DEFAULT_ARTIFACT_CONTRACT_PATH, 0o444, True),
    (Path("/etc/trinity/codex-operation.rules"), 0o444, True),
    (Path("/etc/trinity/validate-codex-auth.py"), 0o444, True),
    (Path("/usr/local/bin/trinity-task-context"), 0o4555, True),
    (Path("/usr/local/bin/claude"), 0o555, True),
    (Path("/opt/flashcrafter/bin/claude-operation-runtime"), 0o555, True),
    (Path("/opt/flashcrafter/bin/fc-operation-broker"), 0o4555, True),
    (Path("/opt/flashcrafter/bin/fc-operation-drop"), 0o4555, True),
    (Path("/opt/flashcrafter/bin/fc-operation-exec"), 0o555, True),
    (Path("/opt/flashcrafter/bin/fc-operation-skill"), 0o555, True),
    (Path("/opt/flashcrafter/operation-contract.json"), 0o444, True),
    (Path("/opt/flashcrafter/operation-grant-public-key.der"), 0o444, True),
    (Path("/etc/claude-code/managed-settings.json"), 0o444, True),
)


@dataclass(frozen=True)
class SealedRuntimeEligibility:
    eligible: bool
    reason: str | None = None


def _effective_capabilities() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("CapEff:"):
                return int(line.split(":", 1)[1].strip(), 16)
    except (OSError, ValueError):
        return -1
    return -1


def _sudo_authorized() -> bool:
    """Return True when any sudo path or sudo-group authority remains.

    A non-interactive ``sudo -n -l`` probe is insufficient: password-based sudo
    returns nonzero even when the image has a known password. Sealed execution
    therefore requires the leaf to remove sudo entirely and detach the runtime
    user from the sudo group.
    """
    for candidate in _SUDO_PATHS:
        try:
            candidate.lstat()
            return True
        except FileNotFoundError:
            pass
        except OSError:
            return True
    try:
        sudo_group = grp.getgrnam("sudo")
        current_user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return False
    return sudo_group.gr_gid in os.getgroups() or current_user in sudo_group.gr_mem


def _boundary_valid(path: Path, expected_mode: int, require_root: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return False
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        return False
    return not require_root or (metadata.st_uid == 0 and metadata.st_gid == 0)


def _artifact_contract_valid(path: Path) -> bool:
    if not _boundary_valid(path, 0o444, True):
        return False
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(contract, dict) or set(contract) != {
        "schemaVersion",
        "sourceRevision",
        "artifacts",
    }:
        return False
    source_revision = contract.get("sourceRevision")
    artifacts = contract.get("artifacts")
    if (
        contract.get("schemaVersion") != _ARTIFACT_CONTRACT_SCHEMA
        or not isinstance(source_revision, str)
        or _REVISION.fullmatch(source_revision) is None
        or not isinstance(artifacts, list)
        or not 1 <= len(artifacts) <= _MAX_ARTIFACTS
    ):
        return False

    seen: set[str] = set()
    ordered_paths: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "path",
            "sha256",
            "mode",
            "uid",
            "gid",
        }:
            return False
        artifact_path = artifact.get("path")
        digest = artifact.get("sha256")
        mode = artifact.get("mode")
        if (
            not isinstance(artifact_path, str)
            or not artifact_path.startswith("/")
            or artifact_path in seen
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(mode, str)
            or re.fullmatch(r"0[0-7]{3,4}", mode) is None
            or artifact.get("uid") != 0
            or artifact.get("gid") != 0
        ):
            return False
        seen.add(artifact_path)
        ordered_paths.append(artifact_path)
        target = Path(artifact_path)
        expected_mode = int(mode, 8)
        if not _boundary_valid(target, expected_mode, True):
            return False
        try:
            actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            return False
        if actual_digest != digest:
            return False

    if seen != _REQUIRED_ARTIFACT_PATHS or ordered_paths != sorted(_REQUIRED_ARTIFACT_PATHS):
        return False
    if not verify_manifest(_PACKAGED_RUNTIME_TREE_PATH):
        return False
    try:
        source_identity = json.loads(_SOURCE_IDENTITY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(source_identity, dict)
        and set(source_identity) == {"revision"}
        and source_identity.get("revision") == source_revision
    )


def _operation_identity_valid() -> bool:
    try:
        operation = pwd.getpwuid(_EXPECTED_PROFILE["operationUid"])
    except KeyError:
        return False
    return (
        operation.pw_name == "fc-operation"
        and operation.pw_uid == _EXPECTED_PROFILE["operationUid"]
        and operation.pw_gid == _EXPECTED_PROFILE["operationUid"]
        and operation.pw_shell in {"/usr/sbin/nologin", "/bin/false"}
    )


def sealed_runtime_eligibility(
    *,
    profile_path: Path = _DEFAULT_PROFILE_PATH,
    artifact_contract_path: Path = _DEFAULT_ARTIFACT_CONTRACT_PATH,
) -> SealedRuntimeEligibility:
    try:
        profile_metadata = profile_path.lstat()
        if not stat.S_ISREG(profile_metadata.st_mode):
            return SealedRuntimeEligibility(False, "boundary")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return SealedRuntimeEligibility(False, "profile")
    if profile != _EXPECTED_PROFILE:
        return SealedRuntimeEligibility(False, "profile")

    for declared_path, expected_mode, require_root in _REQUIRED_BOUNDARIES:
        if declared_path == _DEFAULT_PROFILE_PATH:
            boundary_path = profile_path
        elif declared_path == _DEFAULT_ARTIFACT_CONTRACT_PATH:
            boundary_path = artifact_contract_path
        else:
            boundary_path = declared_path
        if not _boundary_valid(boundary_path, expected_mode, require_root):
            return SealedRuntimeEligibility(False, "boundary")
    if not _artifact_contract_valid(artifact_contract_path):
        return SealedRuntimeEligibility(False, "artifact-contract")
    if not _operation_identity_valid():
        return SealedRuntimeEligibility(False, "operation-identity")

    if (
        os.getuid() != _EXPECTED_PROFILE["developerUid"]
        or os.geteuid() != _EXPECTED_PROFILE["developerUid"]
        or os.getgid() == 0
        or os.getegid() == 0
        or 0 in os.getgroups()
    ):
        return SealedRuntimeEligibility(False, "identity")
    if _effective_capabilities() != 0:
        return SealedRuntimeEligibility(False, "capability")
    if _sudo_authorized():
        return SealedRuntimeEligibility(False, "sudo")
    return SealedRuntimeEligibility(True)


def assert_sealed_runtime_eligible() -> None:
    eligibility = sealed_runtime_eligibility()
    if eligibility.eligible:
        return
    logger.error("event=sealed_runtime_ineligible reason=%s", eligibility.reason)
    raise HTTPException(status_code=503, detail="sealed task runtime is unavailable")
