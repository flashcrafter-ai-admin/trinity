"""Administrator API for immutable platform packages."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from dependencies import require_role
from models import User
from services.platform_package_service import (
    PlatformPackageError,
    publish_platform_package,
)


router = APIRouter(prefix="/api/admin/platform-packages", tags=["platform-packages"])


class PublishPlatformPackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: str
    sha256: str
    archive: str


@router.post("")
async def publish_package(
    body: PublishPlatformPackageRequest,
    current_user: User = Depends(require_role("admin")),
):
    try:
        return publish_platform_package(body.package_id, body.sha256, body.archive)
    except PlatformPackageError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "error": str(exc)},
        ) from exc
