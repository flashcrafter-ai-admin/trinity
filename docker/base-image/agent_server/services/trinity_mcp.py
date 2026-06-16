"""
Trinity MCP injection service for agent-to-agent collaboration.

Supports Claude Code (.mcp.json), Gemini CLI (settings.json), and Codex CLI
(config.toml).
"""
import os
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def inject_trinity_mcp_if_configured() -> bool:
    """
    Inject Trinity MCP server - runtime aware.

    This enables agent-to-agent communication via the Trinity platform.
    Called on agent startup.

    For Claude Code: Writes to ~/.mcp.json
    For Gemini CLI: Writes to ~/.gemini/settings.json
    For Codex CLI: Writes to ~/.codex/config.toml
    """
    trinity_mcp_url = os.getenv("TRINITY_MCP_URL")
    trinity_mcp_api_key = os.getenv("TRINITY_MCP_API_KEY")

    if not trinity_mcp_url or not trinity_mcp_api_key:
        logger.info("Trinity MCP not configured - skipping injection")
        return False

    runtime = os.getenv("AGENT_RUNTIME", "claude-code").lower()

    if runtime == "gemini-cli":
        return _inject_gemini_mcp(trinity_mcp_url, trinity_mcp_api_key)
    elif runtime in ("codex-cli", "codex", "openai-codex"):
        return _inject_codex_mcp(trinity_mcp_url)
    else:
        return _inject_claude_mcp(trinity_mcp_url, trinity_mcp_api_key)


def _inject_claude_mcp(trinity_mcp_url: str, trinity_mcp_api_key: str) -> bool:
    """Inject Trinity MCP into Claude Code's .mcp.json file."""
    home_dir = Path("/home/developer")
    mcp_file = home_dir / ".mcp.json"

    # Trinity MCP server configuration using HTTP transport
    trinity_mcp_entry = {
        "trinity": {
            "type": "http",
            "url": trinity_mcp_url,
            "headers": {
                "Authorization": f"Bearer {trinity_mcp_api_key}"
            }
        }
    }

    try:
        # Read existing .mcp.json if it exists
        if mcp_file.exists():
            content = mcp_file.read_text()
            if content.strip():
                mcp_config = json.loads(content)
            else:
                mcp_config = {"mcpServers": {}}
        else:
            mcp_config = {"mcpServers": {}}

        # Ensure mcpServers key exists
        if "mcpServers" not in mcp_config:
            mcp_config["mcpServers"] = {}

        # Add Trinity MCP (overwrite if exists)
        mcp_config["mcpServers"]["trinity"] = trinity_mcp_entry["trinity"]

        # Write back to file
        mcp_file.write_text(json.dumps(mcp_config, indent=2))
        logger.info(f"Injected Trinity MCP server into {mcp_file} (Claude Code)")
        return True

    except Exception as e:
        logger.warning(f"Failed to inject Trinity MCP for Claude Code: {e}")
        return False


def _inject_gemini_mcp(trinity_mcp_url: str, trinity_mcp_api_key: str) -> bool:
    """
    Inject Trinity MCP into Gemini CLI by writing to settings.json.

    Note: `gemini mcp add --transport http` has a bug where it creates a 'type' field
    that the config parser rejects as unrecognized. We work around this by writing
    directly to ~/.gemini/settings.json with the correct format.

    The correct format for HTTP/SSE MCP servers uses 'url' and 'headers' fields.
    """
    try:
        home_dir = Path("/home/developer")
        gemini_dir = home_dir / ".gemini"
        settings_file = gemini_dir / "settings.json"

        # Ensure .gemini directory exists
        gemini_dir.mkdir(parents=True, exist_ok=True)

        # Read existing settings or create new
        if settings_file.exists():
            content = settings_file.read_text()
            settings = json.loads(content) if content.strip() else {}
        else:
            settings = {}

        # Ensure mcpServers key exists
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}

        # Add/update Trinity MCP server with HTTP transport and auth header
        # Using 'url' and 'headers' format (NOT 'type' which causes parser errors)
        settings["mcpServers"]["trinity"] = {
            "url": trinity_mcp_url,
            "headers": {
                "Authorization": f"Bearer {trinity_mcp_api_key}"
            }
        }

        # Write settings back
        settings_file.write_text(json.dumps(settings, indent=2))

        logger.info(f"Injected Trinity MCP server into {settings_file} (Gemini CLI)")
        return True

    except Exception as e:
        logger.warning(f"Failed to inject Trinity MCP for Gemini CLI: {e}")
        return False


def _toml_string(value: str) -> str:
    """Return a TOML-safe quoted string for simple generated config values."""
    return json.dumps(value)


def _inject_codex_mcp(trinity_mcp_url: str) -> bool:
    """
    Inject Trinity MCP into Codex CLI's config.toml.

    Codex supports streamable HTTP MCP servers with bearer tokens sourced from
    environment variables, so the MCP API key stays in TRINITY_MCP_API_KEY and
    is not written into config.toml.
    """
    try:
        home_dir = Path("/home/developer")
        codex_dir = home_dir / ".codex"
        config_file = codex_dir / "config.toml"

        codex_dir.mkdir(parents=True, exist_ok=True)

        managed_start = "# BEGIN TRINITY MANAGED MCP"
        managed_end = "# END TRINITY MANAGED MCP"
        managed_block = "\n".join([
            managed_start,
            "[mcp_servers.trinity]",
            f"url = {_toml_string(trinity_mcp_url)}",
            'bearer_token_env_var = "TRINITY_MCP_API_KEY"',
            "enabled = true",
            "startup_timeout_sec = 20",
            "tool_timeout_sec = 120",
            'default_tools_approval_mode = "auto"',
            managed_end,
            "",
        ])

        existing = config_file.read_text() if config_file.exists() else ""
        if managed_start in existing and managed_end in existing:
            before, rest = existing.split(managed_start, 1)
            _, after = rest.split(managed_end, 1)
            content = before.rstrip() + "\n\n" + managed_block + after.lstrip()
        else:
            content = existing.rstrip() + ("\n\n" if existing.strip() else "") + managed_block

        config_file.write_text(content)
        logger.info(f"Injected Trinity MCP server into {config_file} (Codex CLI)")
        return True

    except Exception as e:
        logger.warning(f"Failed to inject Trinity MCP for Codex CLI: {e}")
        return False


def configure_mcp_servers(mcp_servers: dict) -> bool:
    """
    Configure additional MCP servers for the agent - runtime aware.

    Args:
        mcp_servers: Dict of server configs from template
                     {"server_name": {"command": "...", "args": [...]}}
    """
    if not mcp_servers:
        return True

    runtime = os.getenv("AGENT_RUNTIME", "claude-code").lower()

    if runtime == "gemini-cli":
        return _configure_gemini_mcp_servers(mcp_servers)
    elif runtime in ("codex-cli", "codex", "openai-codex"):
        return _configure_codex_mcp_servers(mcp_servers)
    else:
        return _configure_claude_mcp_servers(mcp_servers)


def _configure_claude_mcp_servers(mcp_servers: dict) -> bool:
    """Configure MCP servers for Claude Code via .mcp.json."""
    home_dir = Path("/home/developer")
    mcp_file = home_dir / ".mcp.json"

    try:
        if mcp_file.exists():
            content = mcp_file.read_text()
            mcp_config = json.loads(content) if content.strip() else {"mcpServers": {}}
        else:
            mcp_config = {"mcpServers": {}}

        if "mcpServers" not in mcp_config:
            mcp_config["mcpServers"] = {}

        # Add each MCP server
        for server_name, config in mcp_servers.items():
            mcp_config["mcpServers"][server_name] = config

        mcp_file.write_text(json.dumps(mcp_config, indent=2))
        logger.info(f"Configured {len(mcp_servers)} MCP servers for Claude Code")
        return True

    except Exception as e:
        logger.warning(f"Failed to configure MCP servers for Claude Code: {e}")
        return False


def _configure_gemini_mcp_servers(mcp_servers: dict) -> bool:
    """Configure MCP servers for Gemini CLI via `gemini mcp add` commands."""
    success_count = 0

    for server_name, config in mcp_servers.items():
        try:
            command = config.get("command", "")
            args = config.get("args", [])

            if not command:
                logger.warning(f"Skipping MCP server '{server_name}': no command specified")
                continue

            # Build the gemini mcp add command with --scope user for home directory
            cmd = ["gemini", "mcp", "add", "--scope", "user", server_name, command] + args

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Added MCP server '{server_name}' for Gemini CLI")
                success_count += 1
            else:
                logger.warning(f"Failed to add MCP server '{server_name}': {result.stderr}")

        except Exception as e:
            logger.warning(f"Error adding MCP server '{server_name}': {e}")

    logger.info(f"Configured {success_count}/{len(mcp_servers)} MCP servers for Gemini CLI")
    return success_count > 0 or len(mcp_servers) == 0


def _configure_codex_mcp_servers(mcp_servers: dict) -> bool:
    """Configure additional MCP servers for Codex CLI via config.toml."""
    home_dir = Path("/home/developer")
    codex_dir = home_dir / ".codex"
    config_file = codex_dir / "config.toml"

    try:
        codex_dir.mkdir(parents=True, exist_ok=True)
        existing = config_file.read_text() if config_file.exists() else ""

        blocks = []
        for server_name, config in mcp_servers.items():
            safe_name = str(server_name).replace('"', '\\"')
            lines = [f'[mcp_servers."{safe_name}"]']
            if config.get("url"):
                lines.append(f"url = {_toml_string(config['url'])}")
                headers = config.get("headers") or {}
                auth = headers.get("Authorization")
                if auth and auth.startswith("Bearer "):
                    env_name = f"MCP_{str(server_name).upper().replace('-', '_')}_TOKEN"
                    os.environ[env_name] = auth[len("Bearer "):]
                    lines.append(f"bearer_token_env_var = {_toml_string(env_name)}")
            elif config.get("command"):
                lines.append(f"command = {_toml_string(config['command'])}")
                args = config.get("args") or []
                if args:
                    lines.append("args = [" + ", ".join(_toml_string(str(arg)) for arg in args) + "]")
                env = config.get("env") or {}
                if env:
                    lines.append(f'[mcp_servers."{safe_name}".env]')
                    for key, value in env.items():
                        lines.append(f"{key} = {_toml_string(str(value))}")
            else:
                logger.warning(f"Skipping MCP server '{server_name}': no url or command specified")
                continue
            blocks.append("\n".join(lines))

        if not blocks:
            return True

        managed_start = "# BEGIN TRINITY MANAGED EXTRA MCP"
        managed_end = "# END TRINITY MANAGED EXTRA MCP"
        managed_block = managed_start + "\n" + "\n\n".join(blocks) + "\n" + managed_end + "\n"

        if managed_start in existing and managed_end in existing:
            before, rest = existing.split(managed_start, 1)
            _, after = rest.split(managed_end, 1)
            content = before.rstrip() + "\n\n" + managed_block + after.lstrip()
        else:
            content = existing.rstrip() + ("\n\n" if existing.strip() else "") + managed_block

        config_file.write_text(content)
        logger.info(f"Configured {len(blocks)} MCP servers for Codex CLI")
        return True

    except Exception as e:
        logger.warning(f"Failed to configure MCP servers for Codex CLI: {e}")
        return False
