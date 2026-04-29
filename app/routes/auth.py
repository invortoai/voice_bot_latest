"""
Auth endpoints:

  POST /auth/login   — credential exchange; returns access + refresh token pair.
  POST /auth/refresh — rotate refresh token; returns new access + refresh token pair.
  POST /auth/logout  — revoke refresh token (best-effort).

Rate-limited per IP (RATE_LIMIT_LOGIN, default 10/minute).

Security notes:
  - password_hash: client must send SHA-256(password) — raw password never reaches this service.
  - All failure paths on /login return 401 with the same message to prevent account enumeration.
  - Refresh tokens are single-use (rotated on every /auth/refresh call).
  - org_id/user_id/role are NOT returned in response bodies — decode the JWT on the client if needed.
"""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.core.auth import (
    create_access_token,
    create_refresh_token,
    revoke_refresh_token,
    verify_and_rotate_refresh_token,
)
from app.core.database import get_cursor
from app.core.rate_limiter import limiter
from app.config import JWT_EXPIRE_MINUTES, JWT_REFRESH_EXPIRE_DAYS, RATE_LIMIT_LOGIN

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: str
    password_hash: str  # SHA-256(password) — hashed by the client before sending


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    refresh_expires_in_days: int


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    refresh_expires_in_days: int


class LogoutRequest(BaseModel):
    refresh_token: str


@router.post("/login", response_model=LoginResponse)
@limiter.limit(RATE_LIMIT_LOGIN)
async def login(request: Request, req: LoginRequest) -> LoginResponse:
    """
    Exchange org-user credentials for an access + refresh token pair.

    - Client must send SHA-256(password) in the password_hash field.
    - Returns a short-lived Bearer JWT (JWT_EXPIRE_HOURS) and a long-lived
      refresh token (JWT_REFRESH_EXPIRE_DAYS) for silent renewal.
    - Rate-limited to RATE_LIMIT_LOGIN requests per IP (default: 10/minute).
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.org_id, u.email, u.role, u.is_active,
                   o.is_active AS org_is_active
            FROM   org_users u
            JOIN   organizations o ON o.id = u.org_id
            WHERE  LOWER(u.email) = LOWER(%s)
              AND  u.password_hash = %s
            """,
            (req.email, req.password_hash),
        )
        user = cur.fetchone()

    # All failure paths return 401 with the same message — no enumeration possible
    if not user or not user["is_active"] or not user["org_is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    user_id = str(user["id"])
    org_id = str(user["org_id"])

    access_token = create_access_token(
        {
            "org_id": org_id,
            "user_id": user_id,
            "email": user["email"],
            "role": user["role"],
        }
    )
    refresh_token = create_refresh_token(user_id, org_id)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in_minutes=JWT_EXPIRE_MINUTES,
        refresh_expires_in_days=JWT_REFRESH_EXPIRE_DAYS,
    )


@router.post("/refresh", response_model=RefreshResponse)
@limiter.limit(RATE_LIMIT_LOGIN)
async def refresh(request: Request, req: RefreshRequest) -> RefreshResponse:
    """
    Rotate a refresh token and return a fresh access + refresh token pair.

    The old refresh token is invalidated immediately (single-use rotation).
    Returns 401 if the token is missing, expired, or already revoked.
    """
    tokens = await verify_and_rotate_refresh_token(req.refresh_token)
    return RefreshResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_in_minutes=JWT_EXPIRE_MINUTES,
        refresh_expires_in_days=JWT_REFRESH_EXPIRE_DAYS,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(req: LogoutRequest) -> None:
    """
    Revoke a refresh token on explicit logout. Best-effort — always returns 204
    so the client can safely call this even if the token is already expired.
    """
    revoke_refresh_token(req.refresh_token)
