#!/usr/bin/env python3
"""Safely merge JSON into an owned artifact and replace it atomically."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, value in patch.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    return patch


def load_existing(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        invalid = path.with_name(f"{path.name}.{stamp}.invalid")
        shutil.move(str(path), str(invalid))
        return {
            "events": [{
                "event": "invalid_json_moved_aside",
                "artifact": str(path),
                "invalid_artifact": str(invalid),
                "timestamp": dt.datetime.utcnow().isoformat() + "Z",
            }]
        }


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="JSON artifact path to update")
    parser.add_argument("--patch-file", help="JSON patch file; defaults to stdin")
    parser.add_argument("--replace", action="store_true", help="Replace instead of deep merge")
    args = parser.parse_args()

    patch_text = Path(args.patch_file).read_text() if args.patch_file else sys.stdin.read()
    patch = json.loads(patch_text)
    path = Path(args.path)
    data = patch if args.replace else deep_merge(load_existing(path), patch)
    atomic_write_json(path, data)
    print(json.dumps({"ok": True, "path": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
