from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from models import AdminLoginRequest, AdminLoginResponse, AdminStatsResponse, RecentRequest
from services import (
    AdminBannedError,
    AdminNotConfiguredError,
    AdminService,
    InvalidAdminPasswordError,
    InvalidAdminTokenError,
    LOGIN_FAILED_MESSAGE,
    check_login_allowed,
    register_login_failure,
    register_login_success,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_bearer_scheme = HTTPBearer(auto_error=False)
admin_service = AdminService()


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else None


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    try:
        admin_service.require_token(credentials.credentials if credentials else None)
    except AdminNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Admin panel is not configured")
    except InvalidAdminTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired admin token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


@router.post(
    "/login",
    response_model=AdminLoginResponse,
    summary="Exchange the admin password for a bearer token",
)
async def admin_login(payload: AdminLoginRequest, request: Request):
    ip = _client_ip(request)
    try:
        await check_login_allowed(ip)
    except AdminBannedError as exc:
        raise HTTPException(status_code=429, detail=LOGIN_FAILED_MESSAGE) from exc
    try:
        result = admin_service.authenticate(payload.password)
    except AdminNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="Admin panel is not configured")
    except InvalidAdminPasswordError as exc:
        await register_login_failure(ip)
        raise HTTPException(status_code=401, detail=LOGIN_FAILED_MESSAGE) from exc
    await register_login_success(ip)
    return AdminLoginResponse(**result)


@router.get(
    "/stats",
    response_model=AdminStatsResponse,
    summary="Aggregated request statistics for the selected period",
)
async def admin_stats(
    period: str | None = Query(None, description="One of today, 24h, 7d, 30d, 90d, 1y"),
    days: int | None = Query(None, ge=1, le=365, description="Deprecated: use period"),
    top_endpoints: str | None = Query(
        None,
        description="Comma-separated endpoint route paths to include in top site/username stats",
    ),
    visitor: str | None = Query(None, description="Filter recent requests by visitor ip_hash"),
    _: None = Depends(require_admin),
):
    endpoints = (
        [e.strip() for e in top_endpoints.split(",") if e.strip()] if top_endpoints else None
    )
    days_value = days if days is not None else 30
    return AdminStatsResponse(
        **await admin_service.get_stats(days_value, period, endpoints, visitor)
    )


@router.get(
    "/recent",
    response_model=list[RecentRequest],
    summary="Paged recent requests for the selected period, newest first",
)
async def admin_recent(
    period: str | None = Query(None, description="One of today, 24h, 7d, 30d, 90d, 1y"),
    days: int | None = Query(None, ge=1, le=365, description="Deprecated: use period"),
    visitor: str | None = Query(None, description="Filter recent requests by visitor ip_hash"),
    limit: int = Query(100, ge=1, le=200, description="Page size"),
    offset: int = Query(0, ge=0, description="Rows to skip from the newest"),
    _: None = Depends(require_admin),
):
    days_value = days if days is not None else 30
    rows = await admin_service.get_recent(days_value, period, visitor, limit, offset)
    return [RecentRequest(**row) for row in rows]
