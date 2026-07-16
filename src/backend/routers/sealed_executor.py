"""Owner surface for rotating per-agent sealed execution credentials."""

from fastapi import APIRouter, Depends

from database import db
from dependencies import get_current_user, OwnedAgentByName
from models import SealedExecutorKeySecret, User


router = APIRouter(prefix="/api/agents", tags=["sealed_executor"])


@router.post("/{agent_name}/sealed-executor/key", response_model=SealedExecutorKeySecret)
async def regenerate_sealed_executor_key(
    agent_name: OwnedAgentByName,
    current_user: User = Depends(get_current_user),
):
    secret = db.regenerate_sealed_executor_key(agent_name, current_user.id)
    return SealedExecutorKeySecret(
        agent_name=agent_name,
        key_id=secret["key_id"],
        api_key=secret["api_key"],
        key_prefix=secret["key_prefix"],
    )


@router.delete("/{agent_name}/sealed-executor/key")
async def revoke_sealed_executor_key(agent_name: OwnedAgentByName):
    db.revoke_sealed_executor_key(agent_name)
    return {"revoked": True, "agent_name": agent_name}
