"""Persistence for one per-agent sealed execution principal."""

from sqlalchemy import delete, insert, select

from .engine import get_engine
from .mcp_keys import McpKeyOperations
from .tables import agent_ownership, mcp_api_keys
from utils.helpers import utc_now_iso


_SCOPE = "sealed_executor"


class SealedExecutorKeyOperations:
    def regenerate_key(self, agent_name: str, user_id: int) -> dict:
        """Atomically replace the only sealed-executor key for an agent."""
        api_key = McpKeyOperations._generate_mcp_api_key()
        key_hash = McpKeyOperations._hash_api_key(api_key)
        key_id = McpKeyOperations._generate_id()
        now = utc_now_iso()
        with get_engine().begin() as conn:
            owner = conn.execute(
                select(agent_ownership.c.id)
                .where(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
                .with_for_update()
            ).first()
            if owner is None:
                raise ValueError("sealed-executor key requires an active agent")
            conn.execute(
                delete(mcp_api_keys).where(
                    mcp_api_keys.c.agent_name == agent_name,
                    mcp_api_keys.c.scope == _SCOPE,
                )
            )
            conn.execute(
                insert(mcp_api_keys).values(
                    id=key_id,
                    name=f"sealed-executor-{agent_name}-key",
                    description=f"Sealed execution credential for agent {agent_name}",
                    key_prefix=api_key[:20],
                    key_hash=key_hash,
                    created_at=now,
                    user_id=user_id,
                    agent_name=agent_name,
                    scope=_SCOPE,
                    is_active=1,
                )
            )
        return {"api_key": api_key, "key_prefix": api_key[:20], "key_id": key_id}

    def revoke_key(self, agent_name: str) -> bool:
        with get_engine().begin() as conn:
            owner = conn.execute(
                select(agent_ownership.c.id)
                .where(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
                .with_for_update()
            ).first()
            if owner is None:
                return False
            result = conn.execute(
                delete(mcp_api_keys).where(
                    mcp_api_keys.c.agent_name == agent_name,
                    mcp_api_keys.c.scope == _SCOPE,
                )
            )
            return result.rowcount > 0
