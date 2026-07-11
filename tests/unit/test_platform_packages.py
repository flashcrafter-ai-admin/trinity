"""Security and lifecycle tests for immutable platform packages."""

import base64
import hashlib
import io
import json
import sys
import tarfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import docker


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "src" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import platform_package_service as packages


def archive_with(name: str = "policy.json", content: bytes = b"{}") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def write_registry(root: Path, package_id: str, digest: str) -> dict[str, str]:
    record = {
        "package_id": package_id,
        "sha256": digest,
        "destination": f"/opt/trinity/platform-packages/{package_id}",
        "volume_name": f"trinity-platform-package-{package_id}-{digest[:16]}",
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(json.dumps({package_id: record}))
    return record


class TestTemplateSelection:
    def test_only_id_and_exact_digest_are_accepted(self):
        digest = "a" * 64
        assert packages.validate_platform_package_selections(
            [{"package_id": "policy-bundle", "sha256": digest}]
        ) == [{"package_id": "policy-bundle", "sha256": digest}]

    @pytest.mark.parametrize(
        "field", ["source", "host_path", "volume", "destination", "mode", "revision"]
    )
    def test_caller_controlled_mount_fields_are_rejected(self, field):
        selection = {"package_id": "policy-bundle", "sha256": "a" * 64, field: "latest"}
        with pytest.raises(packages.PlatformPackageError, match="exactly"):
            packages.validate_platform_package_selections([selection])

    @pytest.mark.parametrize("digest", ["", "latest", "A" * 64, "a" * 63])
    def test_missing_or_moving_digest_is_rejected(self, digest):
        with pytest.raises(packages.PlatformPackageError):
            packages.validate_platform_package_selections(
                [{"package_id": "policy-bundle", "sha256": digest}]
            )

    def test_duplicate_ids_are_rejected(self):
        selection = {"package_id": "policy-bundle", "sha256": "a" * 64}
        with pytest.raises(packages.PlatformPackageError, match="Duplicate"):
            packages.validate_platform_package_selections([selection, selection])


class TestRegistryAndMounts:
    def test_registered_digest_resolves_to_fixed_read_only_mount(self, tmp_path):
        record = write_registry(tmp_path, "policy-bundle", "a" * 64)
        resolved = packages.resolve_platform_packages(
            [{"package_id": "policy-bundle", "sha256": "a" * 64}], tmp_path
        )
        assert resolved == [record]
        assert packages.platform_package_volumes(resolved) == {
            record["volume_name"]: {
                "bind": "/opt/trinity/platform-packages/policy-bundle",
                "mode": "ro",
            }
        }

    def test_unknown_and_wrong_digest_fail_closed(self, tmp_path):
        write_registry(tmp_path, "policy-bundle", "a" * 64)
        with pytest.raises(packages.PlatformPackageError) as missing:
            packages.resolve_platform_packages(
                [{"package_id": "undeclared", "sha256": "a" * 64}], tmp_path
            )
        assert missing.value.code == "PLATFORM_PACKAGE_NOT_FOUND"
        with pytest.raises(packages.PlatformPackageError) as mismatch:
            packages.resolve_platform_packages(
                [{"package_id": "policy-bundle", "sha256": "b" * 64}], tmp_path
            )
        assert mismatch.value.code == "PLATFORM_PACKAGE_DIGEST_MISMATCH"

    def test_readiness_requires_named_volume_rw_false_and_no_undeclared_mount(
        self, tmp_path
    ):
        record = write_registry(tmp_path, "policy-bundle", "a" * 64)
        label = packages.platform_package_label([record])
        container = Mock()
        container.attrs = {
            "Config": {"Labels": {packages.PLATFORM_PACKAGES_LABEL: label}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": record["volume_name"],
                    "Destination": record["destination"],
                    "RW": False,
                }
            ],
        }
        assert packages.platform_package_mounts_match(container, tmp_path)
        container.attrs["Mounts"][0]["RW"] = True
        assert not packages.platform_package_mounts_match(container, tmp_path)
        container.attrs["Mounts"][0]["RW"] = False
        corrupted = json.loads(label)
        corrupted[0]["destination"] = "/tmp/caller-selected"
        container.attrs["Config"]["Labels"][
            packages.PLATFORM_PACKAGES_LABEL
        ] = json.dumps(corrupted)
        assert not packages.platform_package_mounts_match(container, tmp_path)
        container.attrs["Config"]["Labels"][packages.PLATFORM_PACKAGES_LABEL] = label
        container.attrs["Mounts"].append(
            {
                "Type": "volume",
                "Name": "other",
                "Destination": "/opt/trinity/platform-packages/other",
                "RW": False,
            }
        )
        assert not packages.platform_package_mounts_match(container, tmp_path)

    def test_legacy_container_with_undeclared_package_mount_fails_readiness(self):
        container = Mock()
        container.attrs = {
            "Config": {"Labels": {}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "unexpected",
                    "Destination": "/opt/trinity/platform-packages/unexpected",
                    "RW": False,
                }
            ],
        }
        assert not packages.platform_package_mounts_match(container)

    def test_materialized_volume_identity_is_verified(self):
        record = {
            "package_id": "policy-bundle",
            "sha256": "a" * 64,
            "destination": "/opt/trinity/platform-packages/policy-bundle",
            "volume_name": "trinity-platform-package-policy-bundle-aaaaaaaaaaaaaaaa",
        }
        client = Mock()
        client.volumes.get.return_value.attrs = {
            "Labels": {
                "trinity.platform": "platform-package",
                "trinity.package-id": "policy-bundle",
                "trinity.package-sha256": "a" * 64,
            }
        }
        packages.verify_platform_package_volumes([record], client)
        client.volumes.get.return_value.attrs["Labels"]["trinity.package-sha256"] = (
            "b" * 64
        )
        with pytest.raises(packages.PlatformPackageError):
            packages.verify_platform_package_volumes([record], client)


class TestPublishing:
    def test_preexisting_unregistered_volume_is_rejected_without_registry_commit(
        self, tmp_path
    ):
        archive = archive_with()
        digest = hashlib.sha256(archive).hexdigest()
        client = Mock()
        client.volumes.get.return_value = Mock()

        with pytest.raises(packages.PlatformPackageError) as collision:
            packages.publish_platform_package(
                "policy-bundle",
                digest,
                base64.b64encode(archive).decode(),
                root=tmp_path,
                docker_client=client,
            )

        assert collision.value.code == "PLATFORM_PACKAGE_VOLUME_COLLISION"
        assert not (tmp_path / "registry.json").exists()
        client.volumes.create.assert_not_called()
        client.containers.create.assert_not_called()

    def test_publish_verifies_digest_materializes_once_and_is_immutable(
        self, tmp_path, monkeypatch
    ):
        archive = archive_with()
        digest = hashlib.sha256(archive).hexdigest()
        materialized = []
        monkeypatch.setattr(
            packages,
            "_materialize_volume",
            lambda client, record, source: materialized.append(record["volume_name"]),
        )
        client = Mock()
        client.volumes.get.return_value.attrs = {
            "Labels": {
                "trinity.platform": "platform-package",
                "trinity.package-id": "policy-bundle",
                "trinity.package-sha256": digest,
            }
        }
        first = packages.publish_platform_package(
            "policy-bundle",
            digest,
            base64.b64encode(archive).decode(),
            root=tmp_path,
            docker_client=client,
        )
        second = packages.publish_platform_package(
            "policy-bundle",
            digest,
            base64.b64encode(archive).decode(),
            root=tmp_path,
            docker_client=client,
        )
        assert first == second
        assert materialized == [f"trinity-platform-package-policy-bundle-{digest[:16]}"]
        other = archive_with(content=b"changed")
        with pytest.raises(packages.PlatformPackageError) as conflict:
            packages.publish_platform_package(
                "policy-bundle",
                hashlib.sha256(other).hexdigest(),
                base64.b64encode(other).decode(),
                root=tmp_path,
                docker_client=client,
            )
        assert conflict.value.code == "PLATFORM_PACKAGE_IMMUTABLE_CONFLICT"

    def test_digest_mismatch_and_archive_links_are_rejected(
        self, tmp_path, monkeypatch
    ):
        archive = archive_with()
        with pytest.raises(packages.PlatformPackageError) as mismatch:
            packages.publish_platform_package(
                "policy-bundle",
                "a" * 64,
                base64.b64encode(archive).decode(),
                root=tmp_path,
                docker_client=Mock(),
            )
        assert mismatch.value.code == "PLATFORM_PACKAGE_DIGEST_MISMATCH"

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        linked = buffer.getvalue()
        monkeypatch.setattr(packages, "_materialize_volume", Mock())
        with pytest.raises(packages.PlatformPackageError) as invalid:
            packages.publish_platform_package(
                "linked",
                hashlib.sha256(linked).hexdigest(),
                base64.b64encode(linked).decode(),
                root=tmp_path,
                docker_client=Mock(),
            )
        assert invalid.value.code == "INVALID_PLATFORM_PACKAGE_ARCHIVE"


def test_real_docker_named_volume_mount_is_read_only():
    """Integration proof using Docker directly without requiring the backend."""
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        pytest.skip("Docker daemon is unavailable")

    name = f"trinity-platform-package-test-{uuid.uuid4().hex[:12]}"
    volume = client.volumes.create(
        name=name, labels={"trinity.platform": "platform-package-test"}
    )
    container = None
    try:
        client.containers.run(
            "alpine:3.20",
            ["sh", "-c", "printf immutable > /package/value"],
            volumes={name: {"bind": "/package", "mode": "rw"}},
            remove=True,
        )
        container = client.containers.run(
            "alpine:3.20",
            [
                "sh",
                "-c",
                'test "$(cat /package/value)" = immutable && ! touch /package/changed',
            ],
            volumes={name: {"bind": "/package", "mode": "ro"}},
            detach=True,
        )
        result = container.wait(timeout=20)
        container.reload()
        mount = next(
            item
            for item in container.attrs["Mounts"]
            if item["Destination"] == "/package"
        )
        assert result["StatusCode"] == 0
        assert mount["RW"] is False
    finally:
        if container is not None:
            container.remove(force=True)
        volume.remove(force=True)


def test_recreate_rebuilds_registered_package_as_ro_from_registry(monkeypatch):
    """Mocked Docker create-call evidence for the recreation path."""
    from services.agent_service import lifecycle

    digest = "a" * 64
    record = {
        "package_id": "policy-bundle",
        "sha256": digest,
        "destination": "/opt/trinity/platform-packages/policy-bundle",
        "volume_name": "trinity-platform-package-policy-bundle-aaaaaaaaaaaaaaaa",
    }
    old = Mock()
    old.attrs = {
        "Config": {
            "Image": "trinity-agent-base:latest",
            "Env": [],
            "Labels": {
                "trinity.ssh-port": "2222",
                "trinity.cpu": "2",
                "trinity.memory": "4g",
                packages.PLATFORM_PACKAGES_LABEL: packages.platform_package_label(
                    [record]
                ),
            },
        },
        "HostConfig": {},
        "Mounts": [
            {
                "Type": "volume",
                "Name": "agent-demo-workspace",
                "Destination": "/home/developer",
                "RW": True,
            },
            {
                "Type": "volume",
                "Name": "caller-volume",
                "Destination": record["destination"],
                "RW": True,
            },
        ],
    }
    created = Mock()
    run = AsyncMock(return_value=created)
    monkeypatch.setattr(lifecycle, "validate_base_image", Mock())
    monkeypatch.setattr(
        lifecycle, "resolve_platform_packages", Mock(return_value=[record])
    )
    monkeypatch.setattr(lifecycle, "verify_platform_package_volumes", Mock())
    monkeypatch.setattr(lifecycle, "container_stop", AsyncMock())
    monkeypatch.setattr(lifecycle, "container_remove", AsyncMock())
    monkeypatch.setattr(lifecycle, "containers_run", run)
    monkeypatch.setattr(
        lifecycle,
        "get_agent_default_resources",
        Mock(return_value={"cpu": "2", "memory": "4g"}),
    )
    monkeypatch.setattr(
        lifecycle, "get_agent_full_capabilities", Mock(return_value=False)
    )
    monkeypatch.setattr(
        lifecycle.db, "get_agent_subscription_id", Mock(return_value=None)
    )
    monkeypatch.setattr(
        lifecycle.db, "get_use_platform_api_key", Mock(return_value=False)
    )
    monkeypatch.setattr(lifecycle.db, "get_guardrails_config", Mock(return_value=None))
    monkeypatch.setattr(lifecycle.db, "get_resource_limits", Mock(return_value=None))
    monkeypatch.setattr(
        lifecycle.db, "get_shared_folder_config", Mock(return_value=None)
    )
    monkeypatch.setattr(
        lifecycle.db, "get_file_sharing_enabled", Mock(return_value=False)
    )

    result = __import__("asyncio").run(
        lifecycle.recreate_container_with_updated_config("demo", old, "system")
    )

    assert result is created
    volumes = run.await_args.kwargs["volumes"]
    assert "caller-volume" not in volumes
    assert volumes[record["volume_name"]] == {
        "bind": record["destination"],
        "mode": "ro",
    }


def test_deploy_local_rejects_mount_controls_before_creation():
    """Deploy-local cannot turn template data into Docker mount authority."""
    import asyncio

    from fastapi import HTTPException
    from models import DeployLocalRequest, User
    from services.agent_service.deploy import deploy_local_agent_logic

    template = b"""name: demo
resources:
  cpu: '1'
  memory: 1g
platform_packages:
  - package_id: policy-bundle
    sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    destination: /tmp/caller-selected
"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in {
            "template.yaml": template,
            "CLAUDE.md": b"# Demo\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    create = AsyncMock()
    request = DeployLocalRequest(archive=base64.b64encode(buffer.getvalue()).decode())

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            deploy_local_agent_logic(
                request,
                User(id=1, username="operator", role="admin"),
                Mock(),
                create,
            )
        )

    assert rejected.value.status_code == 400
    assert rejected.value.detail["code"] == "INVALID_PLATFORM_PACKAGES"
    create.assert_not_awaited()
