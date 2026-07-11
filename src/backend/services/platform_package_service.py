"""Immutable, admin-owned platform packages mounted read-only into agents."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import docker


PACKAGE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLATFORM_PACKAGES_LABEL = "trinity.platform-packages"
REGISTRY_ROOT = Path(
    os.getenv("TRINITY_PLATFORM_PACKAGES_PATH", "/data/platform-packages")
)
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
MAX_MEMBERS = 500
MATERIALIZER_IMAGE = "alpine:3.20"


class PlatformPackageError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _public_record(record: dict[str, str]) -> dict[str, str]:
    return {
        "package_id": record["package_id"],
        "sha256": record["sha256"],
        "destination": record["destination"],
    }


def _destination(package_id: str) -> str:
    return f"/opt/trinity/platform-packages/{package_id}"


def _volume_name(package_id: str, digest: str) -> str:
    return f"trinity-platform-package-{package_id}-{digest[:16]}"


def _validate_package_id(value: Any) -> str:
    if not isinstance(value, str) or not PACKAGE_ID_RE.fullmatch(value):
        raise PlatformPackageError(
            "INVALID_PLATFORM_PACKAGE_ID",
            "package_id must match ^[a-z][a-z0-9-]{0,62}$",
        )
    return value


def _validate_digest(value: Any) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PlatformPackageError(
            "INVALID_PLATFORM_PACKAGE_DIGEST",
            "sha256 must be exactly 64 lowercase hexadecimal characters",
        )
    return value


@contextmanager
def _locked_registry(root: Path = REGISTRY_ROOT) -> Iterator[Path]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".registry.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield root / "registry.json"
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_registry(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformPackageError(
            "PLATFORM_PACKAGE_REGISTRY_INVALID",
            "Platform package registry is unreadable",
            503,
        ) from exc
    if not isinstance(value, dict):
        raise PlatformPackageError(
            "PLATFORM_PACKAGE_REGISTRY_INVALID",
            "Platform package registry is invalid",
            503,
        )
    return value


def _write_registry(path: Path, value: dict[str, dict[str, str]]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def validate_platform_package_selections(value: Any) -> list[dict[str, str]]:
    """Validate the closed template surface (ID + exact digest only)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlatformPackageError(
            "INVALID_PLATFORM_PACKAGES",
            "platform_packages must be a list",
        )
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"package_id", "sha256"}:
            raise PlatformPackageError(
                "INVALID_PLATFORM_PACKAGES",
                "Each platform package must contain exactly package_id and sha256",
            )
        package_id = _validate_package_id(item["package_id"])
        digest = _validate_digest(item["sha256"])
        if package_id in seen:
            raise PlatformPackageError(
                "DUPLICATE_PLATFORM_PACKAGE",
                f"Duplicate platform package: {package_id}",
            )
        seen.add(package_id)
        result.append({"package_id": package_id, "sha256": digest})
    return result


def resolve_platform_packages(
    selections: Any,
    root: Path = REGISTRY_ROOT,
) -> list[dict[str, str]]:
    checked = validate_platform_package_selections(selections)
    if not checked:
        return []
    with _locked_registry(root) as path:
        registry = _read_registry(path)
    resolved = []
    for selection in checked:
        record = registry.get(selection["package_id"])
        if not isinstance(record, dict):
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_NOT_FOUND",
                f"Platform package is not registered: {selection['package_id']}",
                404,
            )
        if record.get("sha256") != selection["sha256"]:
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_DIGEST_MISMATCH",
                f"Registered digest does not match for package: {selection['package_id']}",
                409,
            )
        expected_destination = _destination(selection["package_id"])
        expected_volume = _volume_name(selection["package_id"], selection["sha256"])
        if (
            record.get("destination") != expected_destination
            or record.get("volume_name") != expected_volume
        ):
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_REGISTRY_INVALID",
                "Platform package registry metadata is not canonical",
                503,
            )
        resolved.append(record)
    return resolved


def platform_package_volumes(
    records: list[dict[str, str]]
) -> dict[str, dict[str, str]]:
    return {
        record["volume_name"]: {"bind": record["destination"], "mode": "ro"}
        for record in records
    }


def platform_package_label(records: list[dict[str, str]]) -> str:
    return json.dumps(
        [_public_record(record) for record in records],
        sort_keys=True,
        separators=(",", ":"),
    )


def public_platform_packages(records: list[dict[str, str]]) -> list[dict[str, str]]:
    return [_public_record(record) for record in records]


def parse_platform_package_label(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []
    try:
        return _decode_platform_package_label(value)
    except PlatformPackageError:
        return []


def _decode_platform_package_label(value: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PlatformPackageError(
            "PLATFORM_PACKAGE_LABEL_INVALID", "Platform package label is invalid"
        ) from exc
    if not isinstance(parsed, list):
        raise PlatformPackageError(
            "PLATFORM_PACKAGE_LABEL_INVALID", "Platform package label must be a list"
        )
    result = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict) or set(item) != {
            "package_id",
            "sha256",
            "destination",
        }:
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_LABEL_INVALID",
                "Platform package label fields are invalid",
            )
        package_id = _validate_package_id(item["package_id"])
        digest = _validate_digest(item["sha256"])
        if package_id in seen or item["destination"] != _destination(package_id):
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_LABEL_INVALID",
                "Platform package label binding is invalid",
            )
        seen.add(package_id)
        result.append(
            {
                "package_id": package_id,
                "sha256": digest,
                "destination": item["destination"],
            }
        )
    return result


def platform_package_selections_from_label(value: str) -> list[dict[str, str]]:
    return [
        {"package_id": item["package_id"], "sha256": item["sha256"]}
        for item in _decode_platform_package_label(value)
    ]


def platform_package_mounts_match(
    container: Any,
    root: Path = REGISTRY_ROOT,
    client: Any | None = None,
) -> bool:
    """Verify declared packages resolve and are mounted as named volumes RW=false."""
    label = (
        container.attrs.get("Config", {}).get("Labels", {}).get(PLATFORM_PACKAGES_LABEL)
    )
    if label is None:
        return not any(
            mount.get("Destination", "").startswith("/opt/trinity/platform-packages/")
            for mount in container.attrs.get("Mounts", [])
        )
    try:
        normalized = platform_package_selections_from_label(label)
        resolved = resolve_platform_packages(normalized, root)
        if client is not None:
            verify_platform_package_volumes(resolved, client)
    except PlatformPackageError:
        return False
    mounts = container.attrs.get("Mounts", [])
    for record in resolved:
        if not any(
            mount.get("Type") == "volume"
            and mount.get("Name") == record["volume_name"]
            and mount.get("Destination") == record["destination"]
            and mount.get("RW") is False
            for mount in mounts
        ):
            return False
    package_prefix = "/opt/trinity/platform-packages/"
    expected_destinations = {record["destination"] for record in resolved}
    return not any(
        mount.get("Destination", "").startswith(package_prefix)
        and mount.get("Destination") not in expected_destinations
        for mount in mounts
    )


def verify_platform_package_volumes(records: list[dict[str, str]], client: Any) -> None:
    """Fail closed when registered content is not present in its immutable volume."""
    if not records:
        return
    if client is None:
        raise PlatformPackageError(
            "PLATFORM_PACKAGE_STORAGE_UNAVAILABLE",
            "Docker is unavailable for platform package verification",
            503,
        )
    for record in records:
        try:
            volume = client.volumes.get(record["volume_name"])
        except Exception as exc:
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_VOLUME_MISSING",
                f"Materialized volume is missing for package: {record['package_id']}",
                503,
            ) from exc
        labels = getattr(volume, "attrs", {}).get("Labels", {}) or {}
        if (
            labels.get("trinity.platform") != "platform-package"
            or labels.get("trinity.package-id") != record["package_id"]
            or labels.get("trinity.package-sha256") != record["sha256"]
        ):
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_VOLUME_INVALID",
                f"Materialized volume metadata is invalid for package: {record['package_id']}",
                503,
            )


def _safe_extract(archive: bytes, destination: Path) -> None:
    extracted_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS:
                raise PlatformPackageError(
                    "PLATFORM_PACKAGE_TOO_MANY_FILES",
                    "Package archive has too many members",
                )
            for member in members:
                if not (member.isfile() or member.isdir()):
                    raise PlatformPackageError(
                        "INVALID_PLATFORM_PACKAGE_ARCHIVE",
                        "Links and special files are not allowed",
                    )
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise PlatformPackageError(
                        "INVALID_PLATFORM_PACKAGE_ARCHIVE",
                        "Archive path traversal is not allowed",
                    )
                target = (destination / member.name).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise PlatformPackageError(
                        "INVALID_PLATFORM_PACKAGE_ARCHIVE",
                        "Archive path escapes extraction root",
                    )
                extracted_bytes += member.size
                if extracted_bytes > MAX_EXTRACTED_BYTES:
                    raise PlatformPackageError(
                        "PLATFORM_PACKAGE_EXPANDED_TOO_LARGE",
                        "Expanded package is too large",
                    )
            tar.extractall(destination, members=members)
    except tarfile.TarError as exc:
        raise PlatformPackageError(
            "INVALID_PLATFORM_PACKAGE_ARCHIVE", "archive must be a valid tar.gz"
        ) from exc


def _materialize_volume(client: Any, record: dict[str, str], source: Path) -> None:
    volume_name = record["volume_name"]
    try:
        client.volumes.get(volume_name)
        return
    except docker.errors.NotFound:
        pass
    client.volumes.create(
        name=volume_name,
        labels={
            "trinity.platform": "platform-package",
            "trinity.package-id": record["package_id"],
            "trinity.package-sha256": record["sha256"],
        },
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(source, arcname=".")
    transient = None
    try:
        try:
            client.images.get(MATERIALIZER_IMAGE)
        except docker.errors.ImageNotFound:
            client.images.pull(MATERIALIZER_IMAGE)
        transient = client.containers.create(
            MATERIALIZER_IMAGE,
            command=[
                "sh",
                "-c",
                "find /package -type d -exec chmod 0555 {} + && find /package -type f -exec chmod 0444 {} +",
            ],
            volumes={volume_name: {"bind": "/package", "mode": "rw"}},
        )
        if not transient.put_archive("/package", buffer.getvalue()):
            raise RuntimeError("put_archive returned false")
        transient.start()
        result = transient.wait(timeout=30)
        if result.get("StatusCode") != 0:
            raise RuntimeError("package permission hardening failed")
    except Exception:
        try:
            client.volumes.get(volume_name).remove(force=True)
        except Exception:
            pass
        raise
    finally:
        if transient is not None:
            try:
                transient.remove(force=True)
            except Exception:
                pass


def publish_platform_package(
    package_id: Any,
    sha256: Any,
    archive_b64: Any,
    *,
    root: Path = REGISTRY_ROOT,
    docker_client: Any | None = None,
) -> dict[str, str]:
    package_id = _validate_package_id(package_id)
    digest = _validate_digest(sha256)
    if not isinstance(archive_b64, str):
        raise PlatformPackageError(
            "INVALID_PLATFORM_PACKAGE_ARCHIVE", "archive must be base64 text"
        )
    try:
        archive = base64.b64decode(archive_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PlatformPackageError(
            "INVALID_PLATFORM_PACKAGE_ARCHIVE", "archive is not valid base64"
        ) from exc
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise PlatformPackageError(
            "PLATFORM_PACKAGE_ARCHIVE_TOO_LARGE", "Package archive exceeds 10 MiB"
        )
    if hashlib.sha256(archive).hexdigest() != digest:
        raise PlatformPackageError(
            "PLATFORM_PACKAGE_DIGEST_MISMATCH",
            "Archive digest does not match sha256",
            409,
        )

    record = {
        "package_id": package_id,
        "sha256": digest,
        "destination": _destination(package_id),
        "volume_name": _volume_name(package_id, digest),
    }
    with _locked_registry(root) as registry_path:
        registry = _read_registry(registry_path)
        existing = registry.get(package_id)
        if existing:
            if existing == record:
                verify_platform_package_volumes(
                    [record], docker_client or docker.from_env()
                )
                return _public_record(record)
            raise PlatformPackageError(
                "PLATFORM_PACKAGE_IMMUTABLE_CONFLICT",
                "package_id is already registered with different immutable content",
                409,
            )
        with tempfile.TemporaryDirectory(
            prefix="trinity-platform-package-"
        ) as temporary:
            extracted = Path(temporary)
            _safe_extract(archive, extracted)
            client = docker_client or docker.from_env()
            try:
                _materialize_volume(client, record, extracted)
            except Exception as exc:
                raise PlatformPackageError(
                    "PLATFORM_PACKAGE_MATERIALIZATION_FAILED",
                    "Failed to materialize platform package",
                    503,
                ) from exc
        registry[package_id] = record
        _write_registry(registry_path, registry)
    return _public_record(record)
