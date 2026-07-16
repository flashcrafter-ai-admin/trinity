"""Credential-file metadata safe to expose through the status endpoint."""

import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path


def credential_file_status(filepath: Path, *, include_parent: bool = False) -> dict:
    digest = hashlib.sha256()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(filepath.parent, directory_flags)
    try:
        parent_metadata = os.fstat(parent_fd)
        fd = os.open(filepath.name, file_flags, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
            after = os.fstat(source.fileno())
        named = os.stat(filepath.name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(parent_fd)
    identity = (
        "st_dev",
        "st_ino",
        "st_uid",
        "st_gid",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in identity) or any(
        getattr(after, field) != getattr(named, field) for field in identity
    ):
        raise OSError("credential path changed while its descriptor was read")
    result = {
        "exists": True,
        "size": after.st_size,
        "modified": datetime.fromtimestamp(after.st_mtime).isoformat(),
        "sha256": digest.hexdigest(),
        "mode": f"{stat.S_IMODE(after.st_mode):04o}",
        "uid": after.st_uid,
        "gid": after.st_gid,
        "nlink": after.st_nlink,
    }
    if include_parent:
        result.update({
            "parent_mode": f"{stat.S_IMODE(parent_metadata.st_mode):04o}",
            "parent_uid": parent_metadata.st_uid,
            "parent_gid": parent_metadata.st_gid,
        })
    return result
