"""
Credential management endpoints.
"""
import os
import base64
import binascii
import json
import logging
import stat
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Query

from ..models import (
    CredentialUpdateRequest,
    CredentialReadRequest,
    CredentialReadResponse,
    CredentialInjectRequest,
    CredentialInjectResponse,
    CredentialListItem,
    CredentialListResponse,
    TokenReloadRequest,
    TokenReloadResponse,
)
from ..state import agent_state
from ..services.trinity_mcp import inject_trinity_mcp_if_configured
from ..utils.credential_sanitizer import refresh_credential_values
from ..utils.credential_status import credential_file_status
# Second-layer credential-path policy (Invariant #5) — byte-identical vendored
# copy of src/backend/services/credential_paths.py (#11).
from ..credential_paths import CODEX_SUBSCRIPTION_AUTH_PATH, is_allowed_credential_path

_HOME = Path("/home/developer")


def _safe_credential_target(rel_path: str) -> Path:
    """Validate ``rel_path`` against the curated policy AND confirm it resolves
    inside the agent home (defense-in-depth traversal guard the original write
    loop lacked). Returns the absolute target path or raises HTTP 400."""
    if not is_allowed_credential_path(rel_path):
        logger.warning(f"Credential injection blocked: disallowed path '{rel_path}'")
        raise HTTPException(status_code=400, detail=f"Disallowed credential file path: '{rel_path}'")
    home = _HOME.resolve()
    target = _HOME / rel_path
    parent = target.parent.resolve()
    if parent != home and home not in parent.parents:
        logger.warning(f"Credential injection blocked: path escapes home '{rel_path}'")
        raise HTTPException(status_code=400, detail=f"Path escapes workspace: '{rel_path}'")
    if target.is_symlink():
        logger.warning(f"Credential injection blocked: symlink target '{rel_path}'")
        raise HTTPException(status_code=400, detail=f"Unsafe credential file target: '{rel_path}'")
    return target


def _validate_codex_subscription_auth(payload: bytes) -> None:
    if not 1 <= len(payload) <= 262_144:
        raise HTTPException(status_code=400, detail="Codex subscription auth size is invalid")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        auth = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Codex subscription auth is invalid") from exc
    if not isinstance(auth, dict) or set(auth) - {
        "OPENAI_API_KEY", "auth_mode", "last_refresh", "tokens"
    }:
        raise HTTPException(status_code=400, detail="Codex subscription auth schema is invalid")
    if auth.get("auth_mode") != "chatgpt" or auth.get("OPENAI_API_KEY") is not None:
        raise HTTPException(status_code=400, detail="Codex subscription auth must use ChatGPT")
    if auth.get("last_refresh") is not None and not isinstance(auth["last_refresh"], str):
        raise HTTPException(status_code=400, detail="Codex subscription refresh metadata is invalid")
    tokens = auth.get("tokens")
    allowed_token_keys = {"access_token", "account_id", "id_token", "refresh_token"}
    if not isinstance(tokens, dict) or set(tokens) - allowed_token_keys:
        raise HTTPException(status_code=400, detail="Codex subscription token schema is invalid")
    for required in ("access_token", "refresh_token"):
        if not isinstance(tokens.get(required), str) or not tokens[required].strip():
            raise HTTPException(status_code=400, detail="Codex subscription tokens are incomplete")
    for optional in ("account_id", "id_token"):
        if tokens.get(optional) is not None and (
            not isinstance(tokens[optional], str) or not tokens[optional].strip()
        ):
            raise HTTPException(status_code=400, detail="Codex subscription token is invalid")


def _open_credential_parent(rel_path: str) -> tuple[int, str]:
    parts = Path(rel_path).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise HTTPException(status_code=400, detail=f"Unsafe credential path: '{rel_path}'")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        current_fd = os.open(_HOME, directory_flags)
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            metadata = os.fstat(current_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise OSError("credential parent boundary is unsafe")
        if rel_path == CODEX_SUBSCRIPTION_AUTH_PATH:
            os.fchmod(current_fd, 0o700)
        return current_fd, parts[-1]
    except OSError as exc:
        if "current_fd" in locals():
            os.close(current_fd)
        raise HTTPException(status_code=400, detail=f"Unsafe credential parent: '{rel_path}'") from exc


def _write_credential_file(rel_path: str, *, text: str = None, b64: str = None) -> str:
    """Policy-checked, traversal-guarded write with parent-dir creation and
    0o600 perms. Exactly one of ``text``/``b64`` is provided."""
    # #11 review (defense in depth): `.mcp.json` is content-validated on the
    # backend text path only — never accept it as binary, which would bypass
    # that guard (#590 RCE-by-config).
    if b64 is not None and rel_path.rsplit("/", 1)[-1] == ".mcp.json":
        raise HTTPException(status_code=400, detail=".mcp.json may not be injected as binary")
    if b64 is not None:
        try:
            payload = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid base64 for '{rel_path}'")
    else:
        payload = (text or "").encode()
    if rel_path == CODEX_SUBSCRIPTION_AUTH_PATH:
        _validate_codex_subscription_auth(payload)
    _safe_credential_target(rel_path)
    parent_fd, filename = _open_credential_parent(rel_path)
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            fd = os.open(filename, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open(filename, flags, dir_fd=parent_fd)
        with os.fdopen(fd, "wb", closefd=True) as output:
            metadata = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or metadata.st_nlink != 1
            ):
                raise OSError("credential file boundary is unsafe")
            os.fchmod(output.fileno(), 0o600)
            os.ftruncate(output.fileno(), 0)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Unsafe credential file target: '{rel_path}'") from exc
    finally:
        os.close(parent_fd)
    logger.info(f"Wrote credential file: {rel_path}")
    return rel_path

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/credentials/update")
async def update_credentials(request: CredentialUpdateRequest):
    """
    Update agent credentials by writing .env and regenerating .mcp.json.

    This endpoint is called by the Trinity backend when credentials are updated.
    It writes the new credentials to files that MCP servers read at startup/runtime.

    Flow:
    1. Write credentials to /home/developer/.env
    2. If mcp_config provided, write to /home/developer/.mcp.json
    3. If .mcp.json.template exists, generate .mcp.json from it using envsubst
    """
    home_dir = _HOME
    env_file = home_dir / ".env"
    mcp_file = home_dir / ".mcp.json"
    mcp_template = home_dir / ".mcp.json.template"

    updated_files = []

    try:
        # 1. Write .env file
        env_lines = ["# Generated by Trinity - Agent credentials", ""]
        for var_name, value in request.credentials.items():
            # Escape special characters in values
            escaped_value = str(value).replace('"', '\\"')
            env_lines.append(f'{var_name}="{escaped_value}"')

        env_content = "\n".join(env_lines) + "\n"
        env_file.write_text(env_content)
        updated_files.append(str(env_file))
        logger.info(f"Updated .env with {len(request.credentials)} credentials")

        # 2. Handle .mcp.json generation
        if request.mcp_config:
            # If backend provides pre-generated .mcp.json, use it
            mcp_file.write_text(request.mcp_config)
            updated_files.append(str(mcp_file))
            logger.info("Updated .mcp.json from provided config")

            # Re-inject Trinity MCP after updating .mcp.json
            if inject_trinity_mcp_if_configured():
                logger.info("Re-injected Trinity MCP after credential reload")

        elif mcp_template.exists():
            # Generate .mcp.json from template using envsubst-style substitution
            template_content = mcp_template.read_text()

            # Perform variable substitution (${VAR_NAME} -> value)
            generated_content = template_content
            for var_name, value in request.credentials.items():
                placeholder = f"${{{var_name}}}"
                generated_content = generated_content.replace(placeholder, str(value))

            mcp_file.write_text(generated_content)
            updated_files.append(str(mcp_file))
            logger.info("Generated .mcp.json from template")

            # Re-inject Trinity MCP after regenerating .mcp.json
            # This uses the same injection logic as agent startup
            if inject_trinity_mcp_if_configured():
                logger.info("Re-injected Trinity MCP after credential reload")

        # 3. Also export credentials to environment (for current process)
        # Note: This won't affect already-running subprocesses, but helps for new ones
        for var_name, value in request.credentials.items():
            os.environ[var_name] = str(value)

        # SECURITY: Refresh credential sanitizer cache after updating credentials
        refresh_credential_values()

        # 4. Write file-type credentials (e.g., service account JSON files).
        # Policy-checked + traversal-guarded via the shared helper (#11) — the
        # original loop wrote arbitrary paths with no allowlist or `..` guard.
        files_written = []
        if request.files:
            for file_path, content in request.files.items():
                files_written.append(_write_credential_file(file_path, text=content))
        if request.files_b64:
            for file_path, b64 in request.files_b64.items():
                files_written.append(_write_credential_file(file_path, b64=b64))

        return {
            "status": "success",
            "updated_files": updated_files + files_written,
            "credential_count": len(request.credentials),
            "files_written": files_written,
            "note": "MCP servers may need to be restarted to pick up new credentials"
        }

    except Exception as e:
        logger.error(f"Failed to update credentials: {e}")
        raise HTTPException(status_code=500, detail=f"Credential update failed: {str(e)}")


# Writable-layer override path (#1089). Deliberately NOT under /home/developer —
# that path is the persistent agent-{name}-workspace volume which
# `recreate_container_with_updated_config` preserves, so a token written there
# would survive a recreate and shadow the freshly-baked Config.Env (DB token).
# The writable layer instead survives a plain stop+start (same container) but is
# wiped on recreate (new container, fresh layer) — self-reconciling by Docker
# semantics, no marker logic needed. The directory is created + chowned to UID
# 1000 in the base-image Dockerfile (before the USER switch).
_TOKEN_OVERRIDE = Path("/var/lib/trinity/oauth-token")


@router.post("/api/credentials/reload-token", response_model=TokenReloadResponse)
async def reload_subscription_token(request: TokenReloadRequest):
    """Hot-reload CLAUDE_CODE_OAUTH_TOKEN for the NEXT claude subprocess (#1089).

    Mutates the agent-server process env so the next `subprocess.Popen` for
    `claude` inherits the rotated token; in-flight subprocesses keep their
    already-inherited old token and finish. Also persists the token to the
    writable-layer override so it survives a plain stop+start (fleet restart
    bypasses `start_agent_internal`, which would otherwise revert to the old
    Config.Env token — F2).

    Deliberately does NOT rewrite .env / .mcp.json or re-inject Trinity MCP: the
    subscription token is not a .env credential, and the `/update` / `/inject`
    endpoints destructively rewrite whole files.
    """
    if not request.token:
        raise HTTPException(status_code=400, detail="token is required")

    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = request.token
    if request.remove_api_key:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    # Persist to the writable-layer override. Parent dir is created + chowned in
    # the Dockerfile, so the agent (UID 1000) can write here. Create the file
    # atomically with 0600 via os.open() rather than write_text()+chmod(): the
    # latter creates the file under the process umask (typically 0644) and leaves
    # it world-readable until the follow-up chmod, a brief but avoidable window.
    # The mode arg only applies on *creation*, so also fchmod the fd — a
    # pre-existing override (older write path / tampering) keeps its own perms
    # through O_CREAT|O_TRUNC, and we must still force it back to 0600.
    fd = os.open(_TOKEN_OVERRIDE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(request.token)

    # Add the new token to the log-redaction set (drops the old exact-match
    # value; OAuth tokens stay caught by the sk-ant value regex regardless).
    refresh_credential_values()

    logger.info("Hot-reloaded CLAUDE_CODE_OAUTH_TOKEN (next subprocess; in-flight turns unaffected)")
    return TokenReloadResponse(status="success", reloaded=True)


@router.get("/api/credentials/status")
async def get_credentials_status():
    """
    Get current credential status - which files exist and when they were last modified.
    """
    home_dir = _HOME
    files_status = {}

    credential_files = [
        ".env",
        ".mcp.json",
        ".mcp.json.template",
        ".credentials.enc",  # Encrypted credentials file
        CODEX_SUBSCRIPTION_AUTH_PATH,
    ]

    for filename in credential_files:
        filepath = home_dir / filename
        if filepath.exists():
            status = credential_file_status(
                filepath,
                include_parent=filename == CODEX_SUBSCRIPTION_AUTH_PATH,
            )
            files_status[filename] = status
        else:
            files_status[filename] = {"exists": False}

    # Count credentials in .env if it exists
    env_file = home_dir / ".env"
    credential_count = 0
    if env_file.exists():
        content = env_file.read_text()
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                credential_count += 1

    return {
        "agent_name": agent_state.agent_name,
        "files": files_status,
        "credential_count": credential_count
    }


# ============================================================================
# New Credential Endpoints (CRED-002: Simplified Credential System)
# ============================================================================

@router.get("/api/credentials/read")
async def read_credential_files(paths: str = Query(..., description="Comma-separated list of file paths")):
    """
    Read credential files from workspace.

    Used by the backend to read existing credential files before encrypting.

    Args:
        paths: Comma-separated list of file paths relative to /home/developer
               e.g., ".env,.mcp.json"
    """
    home_dir = Path("/home/developer")
    files = {}
    files_b64 = {}

    path_list = [p.strip() for p in paths.split(",") if p.strip()]

    for rel_path in path_list:
        # Security: prevent path traversal
        clean_path = rel_path.lstrip("/").lstrip(".")
        if ".." in clean_path:
            logger.warning(f"Path traversal attempt blocked: {rel_path}")
            continue

        # Handle paths starting with . (like .env, .mcp.json)
        if rel_path.startswith("."):
            filepath = home_dir / rel_path
        else:
            filepath = home_dir / clean_path

        try:
            if filepath.exists() and filepath.is_file():
                # Verify the resolved path is still under home_dir
                resolved = filepath.resolve()
                if str(resolved).startswith(str(home_dir.resolve())):
                    raw = filepath.read_bytes()
                    try:
                        # Text files round-trip as-is; non-UTF-8 (binary) creds
                        # come back base64 so cert/key bytes survive (#11).
                        files[rel_path] = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        files_b64[rel_path] = base64.b64encode(raw).decode("ascii")
                    logger.debug(f"Read credential file: {rel_path} ({len(raw)} bytes)")
                else:
                    logger.warning(f"Path resolved outside home directory: {rel_path}")
        except Exception as e:
            logger.warning(f"Failed to read {rel_path}: {e}")

    return CredentialReadResponse(files=files, files_b64=files_b64)


@router.get("/api/credentials/list", response_model=CredentialListResponse)
async def list_credential_files():
    """Walk the workspace and return every present file that satisfies the
    curated credential-path policy (#11). Drives "export captures the full
    injected credential set" — the backend reads these and encrypts them all.
    Returns paths only (+ size + binary flag), never contents."""
    items: List[CredentialListItem] = []
    home = _HOME.resolve()
    for abs_path in home.rglob("*"):
        if not abs_path.is_file():
            continue
        try:
            rel = str(abs_path.relative_to(home))
        except ValueError:
            continue
        if not is_allowed_credential_path(rel):
            continue
        try:
            raw = abs_path.read_bytes()
            try:
                raw.decode("utf-8")
                binary = False
            except UnicodeDecodeError:
                binary = True
            items.append(CredentialListItem(path=rel, size=len(raw), binary=binary))
        except Exception as e:
            logger.warning(f"Failed to stat credential file {rel}: {e}")
    return CredentialListResponse(files=items)


@router.post("/api/credentials/inject")
async def inject_credential_files(request: CredentialInjectRequest):
    """
    Inject credential files directly into workspace.

    Second-layer enforcement of the curated credential-path policy (Invariant
    #5; the backend validates first). Text files arrive in ``files``, binary
    files (e.g. .p12/.pfx/DER) base64-encoded in ``files_b64`` (#11). Every path
    is policy-checked + traversal-guarded; parents are created; perms are 0o600.
    """
    home_dir = _HOME
    files_written = []

    for rel_path, content in request.files.items():
        files_written.append(_write_credential_file(rel_path, text=content))
    for rel_path, b64 in (request.files_b64 or {}).items():
        files_written.append(_write_credential_file(rel_path, b64=b64))

    # Re-inject Trinity MCP if .mcp.json was updated
    if ".mcp.json" in files_written:
        if inject_trinity_mcp_if_configured():
            logger.info("Re-injected Trinity MCP after credential injection")

    # Export updated credentials to environment for this process
    # (helps new subprocesses, though existing ones won't see changes)
    if ".env" in files_written:
        env_file = home_dir / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key:
                        os.environ[key] = value

        # SECURITY: Refresh credential sanitizer cache after updating credentials
        refresh_credential_values()

    return CredentialInjectResponse(
        status="success",
        files_written=files_written
    )
