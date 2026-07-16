#!/usr/bin/env python3
"""Reject Compose build inputs that escape an exact immutable source tree."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


if len(sys.argv) != 2:
    raise SystemExit("usage: validate-compose-build-inputs.py EXACT_SOURCE_ROOT")
root = Path(sys.argv[1]).resolve(strict=True)
try:
    config = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeError) as exc:
    raise SystemExit("resolved Compose build configuration is unavailable") from exc


def local_path(value, *, base=root, kind="build input"):
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{kind} is invalid")
    if value.startswith(("http://", "https://", "git://", "github.com/")):
        raise SystemExit(f"{kind} is a mutable remote source")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"{kind} is unavailable") from exc
    if resolved != root and root not in resolved.parents:
        raise SystemExit(f"{kind} escapes the exact release source")
    return resolved


services = config.get("services") if isinstance(config, dict) else None
if not isinstance(services, dict):
    raise SystemExit("resolved Compose services are invalid")
top_level_secrets = config.get("secrets", {})
if not isinstance(top_level_secrets, dict):
    raise SystemExit("resolved Compose secrets are invalid")

for service_name, service in services.items():
    build = service.get("build") if isinstance(service, dict) else None
    if build is None:
        continue
    if isinstance(build, str):
        context = local_path(build, kind=f"{service_name} build context")
        dockerfile = context / "Dockerfile"
    elif isinstance(build, dict):
        context = local_path(build.get("context", "."), kind=f"{service_name} build context")
        if build.get("dockerfile_inline") not in (None, ""):
            raise SystemExit(f"{service_name} inline Dockerfile is not an exact source artifact")
        dockerfile = local_path(
            build.get("dockerfile", "Dockerfile"),
            base=context,
            kind=f"{service_name} Dockerfile",
        )
        if build.get("ssh") not in (None, [], {}):
            raise SystemExit(f"{service_name} build exposes ungoverned SSH authority")
        additional = build.get("additional_contexts", {})
        if isinstance(additional, list):
            pairs = []
            for item in additional:
                if not isinstance(item, str) or "=" not in item:
                    raise SystemExit(f"{service_name} additional context is invalid")
                pairs.append(item.split("=", 1))
        elif isinstance(additional, dict):
            pairs = additional.items()
        else:
            raise SystemExit(f"{service_name} additional contexts are invalid")
        for name, value in pairs:
            if not isinstance(name, str) or not isinstance(value, str):
                raise SystemExit(f"{service_name} additional context is invalid")
            if value.startswith("service:"):
                continue
            if value.startswith("docker-image://"):
                if re.fullmatch(r"docker-image://.+@sha256:[0-9a-f]{64}", value) is None:
                    raise SystemExit(f"{service_name} image context is not digest-bound")
                continue
            local_path(value, kind=f"{service_name} additional context {name}")
        build_secrets = build.get("secrets", [])
        if not isinstance(build_secrets, list):
            raise SystemExit(f"{service_name} build secrets are invalid")
        for reference in build_secrets:
            source = reference if isinstance(reference, str) else reference.get("source")
            declaration = top_level_secrets.get(source) if isinstance(source, str) else None
            if not isinstance(declaration, dict) or "file" not in declaration:
                raise SystemExit(f"{service_name} build secret is not a frozen file")
            local_path(declaration["file"], kind=f"{service_name} build secret")
    else:
        raise SystemExit(f"{service_name} build configuration is invalid")
    if not context.is_dir() or not dockerfile.is_file():
        raise SystemExit(f"{service_name} build context or Dockerfile is invalid")
