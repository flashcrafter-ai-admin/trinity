#!/usr/bin/env python3
"""Canonicalize agent mounts and reject uncovered writable host state."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
import sys


VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
INVENTORY_KEYS = {"container", "destination", "name", "rw", "source", "type"}


def fail(message: str) -> None:
    raise SystemExit(message)


def safe_text(value: object, description: str, *, absolute: bool = False) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{description} is missing")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        fail(f"{description} contains control characters")
    if absolute and (not value.startswith("/") or str(PurePosixPath(value)) != value):
        fail(f"{description} must be an absolute normalized path")
    return value


def inspect(expected_container: str) -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        fail("Docker returned invalid agent inspection JSON")
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        fail("Docker must return exactly one inspected agent container")
    container = safe_text(payload[0].get("Name"), "agent container name").removeprefix("/")
    if container != expected_container or not container.startswith("agent-"):
        fail("Docker inspection does not match the governed agent container")
    mounts = payload[0].get("Mounts")
    if not isinstance(mounts, list):
        fail("Agent mount inventory is missing")

    records: list[dict[str, object]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            fail("Agent mount inventory contains an invalid record")
        kind = mount.get("Type")
        if kind not in {"bind", "volume"}:
            fail("Agent mount inventory contains an unsupported mount type")
        destination = safe_text(
            mount.get("Destination"), "agent mount destination", absolute=True
        )
        source = safe_text(mount.get("Source"), "agent mount source", absolute=True)
        rw = mount.get("RW")
        if not isinstance(rw, bool):
            fail("Agent mount mode is not explicit")
        raw_name = mount.get("Name")
        name = raw_name if isinstance(raw_name, str) else ""
        if kind == "volume":
            if not VOLUME_NAME.fullmatch(name):
                fail("Agent named volume has an invalid or anonymous identity")
        elif name:
            fail("Agent bind mount unexpectedly names a Docker volume")
        if kind == "bind" and rw:
            fail("Writable agent bind mount is outside governed backup coverage")
        records.append(
            {
                "container": container,
                "destination": destination,
                "name": name,
                "rw": rw,
                "source": source,
                "type": kind,
            }
        )

    workspace = [record for record in records if record["destination"] == "/home/developer"]
    expected_workspace = f"{container}-workspace"
    if len(workspace) != 1 or workspace[0] != {
        "container": container,
        "destination": "/home/developer",
        "name": expected_workspace,
        "rw": True,
        "source": workspace[0]["source"] if workspace else "",
        "type": "volume",
    }:
        fail(f"Agent container does not have governed workspace volume {expected_workspace}")

    for record in sorted(
        records,
        key=lambda value: (
            str(value["destination"]).encode(),
            str(value["type"]).encode(),
            str(value["name"]).encode(),
            str(value["source"]).encode(),
        ),
    ):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")))


def volume_names() -> None:
    names: set[str] = set()
    for raw_line in sys.stdin:
        line = raw_line.rstrip("\n")
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            fail("Canonical agent mount inventory is invalid")
        if not isinstance(record, dict) or set(record) != INVENTORY_KEYS:
            fail("Canonical agent mount inventory has an invalid schema")
        if record.get("type") == "volume":
            name = record.get("name")
            if not isinstance(name, str) or not VOLUME_NAME.fullmatch(name):
                fail("Canonical agent mount inventory has an invalid volume")
            names.add(name)
    for name in sorted(names, key=lambda value: value.encode()):
        print(name)


if len(sys.argv) == 3 and sys.argv[1] == "inspect":
    inspect(sys.argv[2])
elif len(sys.argv) == 2 and sys.argv[1] == "volume-names":
    volume_names()
else:
    fail("usage: verify-agent-mount-inventory.py inspect CONTAINER | volume-names")
