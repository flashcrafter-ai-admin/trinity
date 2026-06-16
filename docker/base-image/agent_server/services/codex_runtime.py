"""
OpenAI Codex CLI execution service.

This runtime uses Codex CLI's ChatGPT/Codex authentication path, not the
standard OpenAI API. Authentication is provided by either a cached
``~/.codex/auth.json`` inside the agent container or ``CODEX_ACCESS_TOKEN``.
"""
import asyncio
import json
import logging
import os
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..state import agent_state
from ..utils.orphan_sweep import kill_cgroup_orphans
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME, capture_pgid
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-subproc")


class CodexRuntime(AgentRuntime):
    """Codex CLI implementation of AgentRuntime."""

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ["codex", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_default_model(self) -> str:
        return "gpt-5.5"

    def get_context_window(self, model: Optional[str] = None) -> int:
        return 400000

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        from .trinity_mcp import _configure_codex_mcp_servers
        return _configure_codex_mcp_servers(mcp_servers)

    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        response, log, metadata, session_id = await self.execute_headless(
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            timeout_seconds=900,
            execution_id=execution_id,
            persist_session=continue_session,
        )
        metadata.session_id = metadata.session_id or session_id
        return response, log, metadata, [{"type": "item.completed", "item": {"type": "agent_message", "text": response}}]

    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
        if not self.is_available():
            raise HTTPException(status_code=503, detail="Codex CLI is not available in this container")

        await self._ensure_access_token_login()

        session_id = execution_id or str(uuid.uuid4())
        model_name = model or os.getenv("AGENT_RUNTIME_MODEL") or self.get_default_model()
        combined_prompt = prompt
        if system_prompt:
            combined_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        output_path = Path(tempfile.gettempdir()) / f"codex-last-{session_id}.txt"
        cmd = [
            "codex",
            "exec",
            "--json",
            "--output-last-message",
            str(output_path),
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            model_name,
            "-",
        ]

        raw_messages: List[Dict] = []
        execution_log: List[ExecutionLogEntry] = []
        metadata = ExecutionMetadata(
            session_id=session_id,
            execution_id=session_id,
            context_window=self.get_context_window(model_name),
            model_name=model_name,
        )
        response_parts: List[str] = []
        started = datetime.now()

        def run_codex():
            registry = get_process_registry()
            process = None
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(Path.home()),
                start_new_session=True,
                env={**os.environ, EXECUTION_TAG_NAME: session_id},
            )
            process_pgid = capture_pgid(process)
            registry.register(session_id, process, metadata={
                "type": "codex_task",
                "message_preview": prompt[:100],
                "pgid": process_pgid,
            })

            stderr_chunks: List[str] = []
            try:
                stdout, stderr = process.communicate(combined_prompt, timeout=timeout_seconds)
                if stderr:
                    stderr_chunks.append(stderr)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                if stderr:
                    stderr_chunks.append(stderr)
                raise TimeoutError(f"Codex task exceeded {timeout_seconds}s timeout")
            finally:
                try:
                    kill_cgroup_orphans()
                except Exception:
                    logger.exception("[Codex] cgroup sweep raised after execution")
                registry.unregister(session_id)

            return process.returncode, stdout or "", "".join(stderr_chunks)

        loop = asyncio.get_event_loop()
        try:
            return_code, stdout, stderr = await loop.run_in_executor(_executor, run_codex)
        except TimeoutError as e:
            raise HTTPException(status_code=504, detail=str(e))

        metadata.duration_ms = int((datetime.now() - started).total_seconds() * 1000)

        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("[Codex] non-json output: %s", line[:200])
                continue
            raw_messages.append(msg)
            self._process_event(msg, response_parts, metadata, execution_log)

        if output_path.exists():
            try:
                final_text = output_path.read_text().strip()
                if final_text:
                    response_parts = [final_text]
            finally:
                try:
                    output_path.unlink()
                except OSError:
                    pass

        if return_code != 0:
            detail = (stderr or stdout or f"Codex exited with code {return_code}")[:800]
            if "not logged in" in detail.lower() or "login" in detail.lower():
                detail = "Codex authentication is not configured. Sign in with ChatGPT or provide CODEX_ACCESS_TOKEN."
            raise HTTPException(
                status_code=500,
                detail=f"Codex execution failed (exit code {return_code}): {detail}",
            )

        response_text = "\n".join(part for part in response_parts if part).strip()
        if not response_text:
            response_text = "(No response from Codex)"

        metadata.tool_count = len([entry for entry in execution_log if entry.type == "tool_use"])
        agent_state.session_total_output_tokens += metadata.output_tokens
        if metadata.input_tokens > agent_state.session_context_tokens:
            agent_state.session_context_tokens = metadata.input_tokens
        agent_state.session_context_window = metadata.context_window

        return response_text, execution_log, metadata, metadata.session_id or session_id

    def _process_event(
        self,
        msg: Dict,
        response_parts: List[str],
        metadata: ExecutionMetadata,
        execution_log: List[ExecutionLogEntry],
    ) -> None:
        msg_type = msg.get("type")

        if msg_type == "thread.started":
            metadata.session_id = msg.get("thread_id") or metadata.session_id
            return

        if msg_type == "turn.completed":
            usage = msg.get("usage") or {}
            metadata.input_tokens = int(usage.get("input_tokens") or 0)
            metadata.output_tokens = int(usage.get("output_tokens") or 0)
            metadata.cache_read_tokens = int(usage.get("cached_input_tokens") or 0)
            return

        if msg_type != "item.completed":
            return

        item = msg.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text") or ""
            if text:
                response_parts.append(text)
        elif item_type == "tool_call":
            execution_log.append(ExecutionLogEntry(
                id=item.get("id") or str(uuid.uuid4()),
                type="tool_use",
                tool=item.get("name") or item.get("tool") or "codex_tool",
                input=item.get("arguments") or item.get("input") or {},
                timestamp=datetime.now().isoformat(),
            ))
        elif item_type == "tool_call_output":
            execution_log.append(ExecutionLogEntry(
                id=item.get("id") or str(uuid.uuid4()),
                type="tool_result",
                tool=item.get("name") or item.get("tool") or "codex_tool",
                output=item.get("output") or "",
                success=True,
                timestamp=datetime.now().isoformat(),
            ))

    async def _ensure_access_token_login(self) -> None:
        token = os.getenv("CODEX_ACCESS_TOKEN")
        if not token:
            return

        auth_path = Path.home() / ".codex" / "auth.json"
        if auth_path.exists():
            return

        def login():
            return subprocess.run(
                ["codex", "login", "--with-access-token"],
                input=token,
                text=True,
                capture_output=True,
                timeout=30,
            )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_executor, login)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail="Codex access-token login failed",
            )


_codex_runtime = None


def get_codex_runtime() -> CodexRuntime:
    global _codex_runtime
    if _codex_runtime is None:
        _codex_runtime = CodexRuntime()
    return _codex_runtime
