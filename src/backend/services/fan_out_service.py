"""
Fan-Out Service — Parallel task dispatch and result collection (FANOUT-001).

Dispatches N independent tasks to an agent in parallel, throttled by a
configurable concurrency limit, and collects results with an overall deadline.

Each subtask follows the standard TaskExecutionService path so all executions
appear on the dashboard with full observability (cost, tokens, logs).
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from services.task_execution_service import (
    TaskExecutionResult,
    TaskExecutionErrorCode,
    get_task_execution_service,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FanOutTaskInput:
    """A single task in a fan-out request."""
    id: str
    message: str


@dataclass
class FanOutTaskResult:
    """Result of a single fan-out subtask."""
    id: str
    status: str           # "completed" | "failed"
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    execution_id: Optional[str] = None
    cost: Optional[float] = None
    context_used: Optional[int] = None
    duration_ms: Optional[int] = None


@dataclass
class FanOutResult:
    """Aggregated result of a fan-out operation."""
    fan_out_id: str
    status: str           # "completed" | "deadline_exceeded"
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResult]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class FanOutService:
    """Coordinates parallel fan-out task dispatch and result collection."""

    async def execute(
        self,
        agent_name: str,
        tasks: List[FanOutTaskInput],
        max_concurrency: int = 3,
        timeout_seconds: Optional[int] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        # Origin tracking (passed through to TaskExecutionService)
        source_user_id: Optional[int] = None,
        source_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
    ) -> FanOutResult:
        """
        Dispatch tasks in parallel and collect results.

        Args:
            agent_name: Target agent (typically self).
            tasks: List of tasks to execute.
            max_concurrency: Max concurrent subtasks (semaphore size).
            timeout_seconds: Optional overall deadline for the entire fan-out.
                When None, no outer deadline is applied — each sub-task is
                still bounded by the target agent's configured
                execution_timeout_seconds (TIMEOUT-001).
            model: Model override for subtasks.
            system_prompt: System prompt for subtasks.
            allowed_tools: Tool restrictions for subtasks.
            source_*: Origin tracking fields forwarded to execution records.

        Returns:
            FanOutResult with per-task results and aggregate counts.
        """
        fan_out_id = f"fo_{secrets.token_urlsafe(12)}"
        task_service = get_task_execution_service()
        semaphore = asyncio.Semaphore(max_concurrency)
        # Safe for concurrent writes: asyncio is single-threaded, no preemption between awaits.
        results: dict[str, FanOutTaskResult] = {}

        deadline_desc = f"{timeout_seconds}s" if timeout_seconds is not None else "per-agent"
        logger.info(
            f"[FanOut] Starting {fan_out_id}: {len(tasks)} tasks on '{agent_name}' "
            f"(concurrency={max_concurrency}, deadline={deadline_desc})"
        )

        async def run_subtask(task: FanOutTaskInput) -> None:
            """Execute a single subtask, throttled by semaphore."""
            start = datetime.utcnow()
            async with semaphore:
                try:
                    # Per-subtask timeout: pass None so TaskExecutionService
                    # resolves the target agent's configured
                    # execution_timeout_seconds (TIMEOUT-001). The optional
                    # overall `timeout_seconds` parameter governs the outer
                    # fan-out deadline, not the individual task ceiling.
                    result = await task_service.execute_task_governed(
                        agent_name=agent_name,
                        message=task.message,
                        triggered_by="fan_out",
                        source_user_id=source_user_id,
                        source_user_email=source_user_email,
                        source_agent_name=source_agent_name or agent_name,
                        source_mcp_key_id=source_mcp_key_id,
                        source_mcp_key_name=source_mcp_key_name,
                        model=model,
                        timeout_seconds=None,
                        system_prompt=system_prompt,
                        allowed_tools=allowed_tools,
                        fan_out_id=fan_out_id,
                    )
                    elapsed_ms = int((datetime.utcnow() - start).total_seconds() * 1000)

                    if result.status == "success":
                        results[task.id] = FanOutTaskResult(
                            id=task.id,
                            status="completed",
                            response=result.response,
                            execution_id=result.execution_id,
                            cost=result.cost,
                            context_used=result.context_used,
                            duration_ms=elapsed_ms,
                        )
                    else:
                        results[task.id] = FanOutTaskResult(
                            id=task.id,
                            status="failed",
                            error=result.error,
                            error_code=result.error_code.value if result.error_code else None,
                            execution_id=result.execution_id,
                            cost=result.cost,
                            duration_ms=elapsed_ms,
                        )
                except asyncio.CancelledError:
                    results[task.id] = FanOutTaskResult(
                        id=task.id,
                        status="failed",
                        error="Cancelled (deadline exceeded)",
                        error_code="timeout",
                    )
                except Exception as e:
                    elapsed_ms = int((datetime.utcnow() - start).total_seconds() * 1000)
                    logger.error(f"[FanOut] {fan_out_id} subtask '{task.id}' failed: {e}")
                    results[task.id] = FanOutTaskResult(
                        id=task.id,
                        status="failed",
                        error=str(e),
                        error_code="agent_error",
                        duration_ms=elapsed_ms,
                    )

        # Dispatch all tasks. When an overall deadline is set, wrap in
        # asyncio.timeout so slow subtasks get cancelled once the deadline
        # hits. Without a deadline, each subtask is still individually
        # bounded by the target agent's execution_timeout_seconds.
        # return_exceptions=True ensures all coroutines complete even if one
        # raises — individual failures are handled inside run_subtask.
        deadline_exceeded = False
        coroutines = [run_subtask(t) for t in tasks]

        try:
            if timeout_seconds is not None:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.gather(*coroutines, return_exceptions=True)
            else:
                await asyncio.gather(*coroutines, return_exceptions=True)
        except TimeoutError:
            deadline_exceeded = True
            logger.warning(f"[FanOut] {fan_out_id} deadline exceeded after {timeout_seconds}s")
            # Fill in results for tasks that didn't complete
            for task in tasks:
                if task.id not in results:
                    results[task.id] = FanOutTaskResult(
                        id=task.id,
                        status="failed",
                        error=f"Deadline exceeded ({timeout_seconds}s)",
                        error_code="timeout",
                    )

        # Build ordered results matching input order
        ordered_results = [results.get(t.id, FanOutTaskResult(
            id=t.id, status="failed", error="Unknown error",
        )) for t in tasks]

        completed_count = sum(1 for r in ordered_results if r.status == "completed")
        failed_count = sum(1 for r in ordered_results if r.status == "failed")

        logger.info(
            f"[FanOut] {fan_out_id} finished: {completed_count}/{len(tasks)} completed, "
            f"{failed_count} failed"
        )

        return FanOutResult(
            fan_out_id=fan_out_id,
            status="deadline_exceeded" if deadline_exceeded else "completed",
            total=len(tasks),
            completed=completed_count,
            failed=failed_count,
            results=ordered_results,
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_fan_out_service: Optional[FanOutService] = None


def get_fan_out_service() -> FanOutService:
    """Get the global FanOutService instance."""
    global _fan_out_service
    if _fan_out_service is None:
        _fan_out_service = FanOutService()
    return _fan_out_service
