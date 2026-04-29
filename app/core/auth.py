"""
Authentication module — three co-existing mechanisms:

  1. verify_api_key      — global API_KEY, for /workers endpoint
  2. verify_org_api_key  — two paths:
       A) API_KEY + X-Org-ID (internal: edge functions, queue processors)
       B) per-org key from org_api_keys table (external API consumers)
  3. verify_jwt_token    — short-lived JWT for /api-keys/* management endpoints

Helper functions:
  generate_api_key              — cryptographically-strong key with prefix
  hash_api_key                  — SHA-256; raw key is NEVER stored
  create_access_token           — sign JWT payload
  create_refresh_token          — generate + store a refresh token, return raw value
  verify_and_rotate_refresh_token — validate, revoke old, issue new pair
  revoke_refresh_token          — explicit logout / token invalidation
  require_org_admin             — dependency: JWT + role == admin
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.config import (
    API_KEY,
    API_KEY_PREFIX,
    IS_LOCAL,
    JWT_ALGORITHM,
    JWT_EXPIRE_MINUTES,
    JWT_REFRESH_EXPIRE_DAYS,
    JWT_SECRET_KEY,
)
from app.core.context import set_log_context
from app.core.database import get_cursor

# ── 1. Global infra key (for /workers only — unchanged) ───────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(_api_key_header)) -> str:
    """Global API key check. Used exclusively for /workers endpoint."""
    if not API_KEY:
        if IS_LOCAL:
            return ""  # disabled in dev
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY not configured on server",
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not hmac.compare_digest(api_key, API_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key"
        )
    return api_key


# ── 1b. Global key + X-Org-ID (for internal outbound call endpoints) ──────────


async def verify_global_key_with_org(
    x_api_key: Optional[str] = Security(_api_key_header),
    x_org_id: Optional[str] = Header(default=None),
) -> dict:
    """
    Internal-only auth for outbound call endpoints.
    Requires X-API-Key == API_KEY (global service key) AND X-Org-ID header.
    Per-org keys are NOT accepted here.
    Returns: {"org_id": str}
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not API_KEY:
        if IS_LOCAL:
            return {"org_id": x_org_id or ""}  # disabled in dev
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY not configured on server",
        )
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key"
        )
    if not x_org_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Org-ID header required",
        )
    try:
        import uuid as _uuid

        _uuid.UUID(x_org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="X-Org-ID must be a valid UUID",
        )
    with get_cursor() as cur:
        cur.execute(
            "SELECT id FROM organizations WHERE id = %s AND is_active = TRUE",
            (x_org_id,),
        )
        if not cur.fetchone():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organisation not found or inactive",
            )
    set_log_context(org_id=str(x_org_id))
    return {"org_id": str(x_org_id)}


# ── 2. Per-org API key (for /assistants, /phone-numbers, /calls) ──────────────


async def verify_customer_api_key(
    x_api_key: Optional[str] = Security(_api_key_header),
) -> dict:
    """
    Customer-facing auth: accepts only a per-org key from the org_api_keys table.
    No X-Org-ID header — the org identity is derived from the key itself.
    Used by /calls and /insights endpoints.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    key_hash = hash_api_key(x_api_key)

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT k.id, k.org_id, k.key_prefix, k.is_active, k.expires_at, k.scopes
            FROM   org_api_keys k
            JOIN   organizations o ON o.id = k.org_id
            WHERE  k.key_hash = %s
              AND  o.is_active = TRUE
            """,
            (key_hash,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key"
        )

    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key is inactive"
        )

    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key has expired"
        )

    try:
        with get_cursor() as cur:
            cur.execute(
                "UPDATE org_api_keys SET last_used_at = NOW() WHERE id = %s",
                (row["id"],),
            )
    except Exception:
        pass

    set_log_context(org_id=str(row["org_id"]))
    return {
        "org_id": str(row["org_id"]),
        "key_id": str(row["id"]),
        "key_prefix": row["key_prefix"],
        "scopes": row["scopes"] or [],
    }


async def verify_org_api_key(
    x_api_key: Optional[str] = Security(_api_key_header),
    x_org_id: Optional[str] = Header(default=None),
) -> dict:
    """
    Verify an org API key from the X-API-Key header.

    Two authentication paths:

    A) Service key path (internal callers — edge functions, queue processors):
       X-API-Key == API_KEY  AND  X-Org-ID: <org_uuid>
       Grants full service-level access for that org without a DB lookup on
       org_api_keys. API_KEY must be a non-empty env var to enable this path.

    B) Per-org key path (external API consumers):
       X-API-Key == org-specific key from org_api_keys table.

    Returns: {"org_id": str, "key_id": str|None, "key_prefix": str|None, "scopes": list}
    Raises 401 if missing, 403 if invalid/inactive/expired.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # ── Path A: service key (API_KEY + X-Org-ID) ─────────────────────────────
    # Trusted internal callers (Supabase edge functions, queue processors) present
    # X-API-Key = API_KEY and X-Org-ID to act on behalf of an org.
    if API_KEY and hmac.compare_digest(x_api_key, API_KEY):
        if not x_org_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-Org-ID header required when using service key",
            )
        # Verify the org exists and is active
        with get_cursor() as cur:
            cur.execute(
                "SELECT id FROM organizations WHERE id = %s AND is_active = TRUE",
                (x_org_id,),
            )
            if not cur.fetchone():
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Organisation not found or inactive",
                )
        set_log_context(org_id=str(x_org_id))
        return {
            "org_id": str(x_org_id),
            "key_id": None,
            "key_prefix": "service",
            "scopes": ["*"],
        }

    # ── Path B: per-org API key ───────────────────────────────────────────────
    key_hash = hash_api_key(x_api_key)

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT k.id, k.org_id, k.key_prefix, k.is_active, k.expires_at, k.scopes
            FROM   org_api_keys k
            JOIN   organizations o ON o.id = k.org_id
            WHERE  k.key_hash = %s
              AND  o.is_active = TRUE
            """,
            (key_hash,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key"
        )

    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key is inactive"
        )

    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key has expired"
        )

    # Bump last_used_at — best-effort, never fails the request
    try:
        with get_cursor() as cur:
            cur.execute(
                "UPDATE org_api_keys SET last_used_at = NOW() WHERE id = %s",
                (row["id"],),
            )
    except Exception:
        pass

    set_log_context(org_id=str(row["org_id"]))
    return {
        "org_id": str(row["org_id"]),
        "key_id": str(row["id"]),
        "key_prefix": row["key_prefix"],
        "scopes": row["scopes"] or [],
    }


# ── 3. JWT Bearer (for /auth/login → /api-keys/* management) ──────────────────

_bearer = HTTPBearer(auto_error=False)


def create_access_token(payload: dict, expires_in_minutes: Optional[int] = None) -> str:
    """Sign a JWT. payload must contain org_id, user_id, role."""
    minutes = (
        expires_in_minutes if expires_in_minutes is not None else JWT_EXPIRE_MINUTES
    )
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return jwt.encode(
        {**payload, "exp": expire}, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM
    )


async def verify_jwt_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """Verify Bearer JWT. Returns decoded payload: org_id, user_id, role, email."""
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not JWT_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT_SECRET_KEY not configured on server",
        )
    try:
        payload = jwt.decode(
            credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired"
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}"
        )

    # Recheck user and org are still active — catches deactivations after token issue
    user_id = payload.get("user_id")
    if user_id:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT u.is_active, o.is_active AS org_is_active
                FROM   org_users u
                JOIN   organizations o ON o.id = u.org_id
                WHERE  u.id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
        if not row or not row["is_active"] or not row["org_is_active"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is no longer active",
            )

    set_log_context(org_id=payload.get("org_id", ""))
    return payload


async def require_org_admin(token: dict = Depends(verify_jwt_token)) -> dict:
    """Dependency: valid JWT AND role must be 'admin'."""
    if token.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return token


# ── 4. Refresh token helpers ───────────────────────────────────────────────────


def create_refresh_token(user_id: str, org_id: str) -> str:
    """
    Generate a refresh token, persist its hash, and return the raw value.
    The raw token is shown exactly once — the caller must return it to the client.
    """
    raw_token = secrets.token_urlsafe(64)  # 512-bit entropy
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=JWT_REFRESH_EXPIRE_DAYS)
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO refresh_tokens (user_id, org_id, token_hash, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, org_id, token_hash, expires_at),
        )
    return raw_token


async def verify_and_rotate_refresh_token(raw_token: str) -> dict:
    """
    Validate a refresh token, revoke it (rotation), and issue a new access + refresh pair.
    Returns {"access_token": str, "refresh_token": str}.
    Raises HTTP 401 on any invalid/expired/revoked token.
    """
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token"
        )

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT rt.id, rt.user_id, rt.org_id, rt.expires_at, rt.revoked_at,
                   u.email, u.role, u.is_active, o.is_active AS org_is_active
            FROM   refresh_tokens rt
            JOIN   org_users      u ON u.id = rt.user_id
            JOIN   organizations  o ON o.id = rt.org_id
            WHERE  rt.token_hash = %s
            """,
            (token_hash,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    if row["revoked_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )
    if row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token has expired"
        )
    if not row["is_active"] or not row["org_is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is no longer active",
        )

    # Revoke the consumed token (rotation — old token is single-use)
    with get_cursor() as cur:
        cur.execute(
            "UPDATE refresh_tokens SET revoked_at = NOW() WHERE id = %s",
            (row["id"],),
        )

    user_id = str(row["user_id"])
    org_id = str(row["org_id"])

    access_token = create_access_token(
        {
            "org_id": org_id,
            "user_id": user_id,
            "email": row["email"],
            "role": row["role"],
        }
    )
    new_refresh_token = create_refresh_token(user_id, org_id)

    return {"access_token": access_token, "refresh_token": new_refresh_token}


def revoke_refresh_token(raw_token: str) -> None:
    """Mark a refresh token as revoked (explicit logout). Best-effort — never raises."""
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    try:
        with get_cursor() as cur:
            cur.execute(
                "UPDATE refresh_tokens SET revoked_at = NOW() WHERE token_hash = %s AND revoked_at IS NULL",
                (token_hash,),
            )
    except Exception:
        pass


# ── Key generation helpers ─────────────────────────────────────────────────────


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.
    Returns (raw_key, key_prefix, key_hash).
    raw_key is shown once; key_hash is stored; key_prefix is safe to display.
    """
    random_part = secrets.token_urlsafe(32)  # 256-bit entropy
    raw_key = f"{API_KEY_PREFIX}{random_part}"
    key_prefix = raw_key[:8]
    key_hash = hash_api_key(raw_key)
    return raw_key, key_prefix, key_hash


def hash_api_key(raw_key: str) -> str:
    """SHA-256 hex digest. Never store raw keys."""
    return hashlib.sha256(raw_key.encode()).hexdigest()
