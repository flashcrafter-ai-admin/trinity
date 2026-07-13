"""Credential-file metadata safe to expose through the status endpoint."""

import hashlib
import os
from datetime import datetime
from pathlib import Path


def credential_file_status(filepath: Path) -> dict:
    digest = hashlib.sha256()
    with filepath.open("rb") as source:
        stat = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return {
        "exists": True,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "sha256": digest.hexdigest(),
    }
