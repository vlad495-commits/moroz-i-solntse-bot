from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from auth import get_current_user
from rbac import require_role
from system_metrics import collect_system_metrics


router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics_page(request: Request):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    registry = await collect_system_metrics()
    return PlainTextResponse(registry.to_prometheus(), media_type="text/plain")
