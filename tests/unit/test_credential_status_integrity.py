import hashlib
import os
import stat
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "docker" / "base-image"))

from agent_server.utils.credential_status import credential_file_status


def test_status_exposes_exact_sha256_without_file_contents(tmp_path):
    secret = b"TOKEN=placeholder-secret\n"
    path = tmp_path / ".env"
    path.write_bytes(secret)

    status = credential_file_status(path)

    assert status["exists"] is True
    assert status["size"] == len(secret)
    assert status["sha256"] == hashlib.sha256(secret).hexdigest()
    assert status["mode"] == f"{stat.S_IMODE(path.stat().st_mode):04o}"
    assert status["uid"] == os.geteuid()
    assert status["gid"] == os.getegid()
    assert status["nlink"] == 1
    assert secret.decode() not in repr(status)
