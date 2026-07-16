"""Fail-closed eligibility checks for signed operation execution."""

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import stat
import subprocess

from fastapi import HTTPException


logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_PATH = Path("/etc/trinity/sealed-runtime.json")
_EXPECTED_PROFILE = {
    "schemaVersion": "trinity-sealed-runtime/1",
    "developerUid": 1000,
    "operationUid": 1001,
    "sudoAuthorized": False,
    "managedBashHook": True,
    "strictMcp": True,
}

# These files are split across the governed Trinity base and the hardened
# fc-agency leaf image. A generic Trinity image therefore cannot satisfy this
# boundary merely because it exposes the sealed endpoint.
_REQUIRED_BOUNDARIES = (
    (_DEFAULT_PROFILE_PATH, 0o444, True),
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
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "-l"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"HOME": "/var/empty", "PATH": "/usr/bin:/bin"},
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _boundary_valid(path: Path, expected_mode: int, require_root: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        return False
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        return False
    return not require_root or (metadata.st_uid == 0 and metadata.st_gid == 0)


def sealed_runtime_eligibility(
    *, profile_path: Path = _DEFAULT_PROFILE_PATH
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
        boundary_path = profile_path if declared_path == _DEFAULT_PROFILE_PATH else declared_path
        if not _boundary_valid(boundary_path, expected_mode, require_root):
            return SealedRuntimeEligibility(False, "boundary")

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
