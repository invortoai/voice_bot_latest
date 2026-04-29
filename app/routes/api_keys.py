"""
/api-keys — Per-org API key management (CRUD + rotate).

All endpoints require a valid JWT Bearer token.
Create / delete / rotate require role=admin.
List is available to any authenticated org user.

Limits (stored per-org in organizations table):
  max_api_keys        — total keys allowed (active + inactive)
  max_active_api_keys — max simultaneously active keys

Security notes:
  - Allowed update fields are explicitly allowlisted to prevent dynamic SQL injection.
  - Count check and INSERT share one transaction to avoid TOCTOU race.
  - Activate (PATCH is_active=True) checks max_active_api_keys before applying.
  - Rotate preserves the key's current is_active status (does not force active).
  - Key name must be unique within an org.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from psycopg2.extras import Json
from pydantic import BaseModel

from app.core.auth import generate_api_key, require_org_admin, verify_jwt_token
from app.core.database import get_cursor
from app.core.rate_limiter import limiter
from app.config import RATE_LIMIT_KEY_MGMT

router = APIRouter(prefix="/api-keys", tags=["API Keys"])

# Fields that may be updated via PATCH — explicit allowlist prevents dynamic SQL abuse
ALLOWED_UPDATE_FIELDS = frozenset(
    {"name", "is_active", "expires_at", "scopes", "metadata"}
)


# ── Schemas ────────────────────────────────────────────────────────────────────


class ApiKeyCreate(BaseModel):
    name: str
    expires_at: Optional[datetime] = None
    scopes: list[str] = []
    metadata: dict = {}


class ApiKeyUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    expires_at: Optional[datetime] = None
    scopes: Optional[list[str]] = None
    metadata: Optional[dict] = None


# ── Audit helper ───────────────────────────────────────────────────────────────


def _write_audit(
    *,
    org_id: str,
    key_id: Optional[str],
    actor_user_id: str,
    action: str,
    key_prefix: Optional[str],
    request: Request,
    metadata: Optional[dict] = None,
) -> None:
    """
    Write an audit row in its OWN transaction.
    Failures are logged but never propagate — the key operation always succeeds.
    """
    try:
        ip = request.client.host if request.client else None
        ua = (request.headers.get("user-agent") or "")[:512]
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO org_api_key_audit_logs
                    (org_id, key_id, actor_user_id, action, key_prefix,
                     ip_address, user_agent, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    org_id,
                    key_id,
                    actor_user_id,
                    action,
                    key_prefix,
                    ip,
                    ua,
                    Json(metadata or {}),
                ),
            )
    except Exception as exc:
        logger.warning(f"Audit log write failed (non-fatal): {exc}")


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("")
@limiter.limit(RATE_LIMIT_KEY_MGMT)
async def list_api_keys(
    request: Request,
    token: dict = Depends(verify_jwt_token),
):
    """List all API keys for the caller's org. Key hashes are never returned."""
    org_id = token["org_id"]
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, name, key_prefix, is_active, scopes, metadata,
                   expires_at, last_used_at, created_by, created_at, updated_at
            FROM   org_api_keys
            WHERE  org_id = %s
            ORDER  BY created_at DESC
            """,
            (org_id,),
        )
        keys = [dict(r) for r in cur.fetchall()]
    return {"api_keys": keys, "total": len(keys)}


@router.post("", status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_KEY_MGMT)
async def create_api_key(
    request: Request,
    req: ApiKeyCreate,
    token: dict = Depends(require_org_admin),
):
    """
    Create a new API key for the caller's org.

    The raw key is returned ONCE in this response only.
    It is never stored — save it immediately.

    Enforces:
      - max_api_keys  : total key count (active + inactive)
      - name uniqueness within the org
    Count check and INSERT share a single transaction to prevent TOCTOU races.
    """
    org_id = token["org_id"]
    user_id = token["user_id"]

    raw_key, key_prefix, key_hash = generate_api_key()

    with get_cursor() as cur:
        # Fetch org limits — FOR UPDATE locks the row for the duration of this transaction
        cur.execute(
            "SELECT max_api_keys, max_active_api_keys FROM organizations WHERE id = %s FOR UPDATE",
            (org_id,),
        )
        org = cur.fetchone()
        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found"
            )

        # Gap #1 fix: count ALL keys (active + inactive) against max_api_keys
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM org_api_keys WHERE org_id = %s",
            (org_id,),
        )
        total_count = cur.fetchone()["cnt"]
        max_total = org["max_api_keys"]
        if max_total > 0 and total_count >= max_total:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Total key limit reached ({total_count}/{max_total}). "
                    "Delete an existing key first."
                ),
            )

        # Gap #12 fix: enforce name uniqueness within the org
        cur.execute(
            "SELECT id FROM org_api_keys WHERE org_id = %s AND LOWER(name) = LOWER(%s)",
            (org_id, req.name.strip()),
        )
        if cur.fetchone():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A key named '{req.name}' already exists in this organisation.",
            )

        # Gap #4 fix: INSERT in the same transaction as the count check
        cur.execute(
            """
            INSERT INTO org_api_keys
                (org_id, name, key_prefix, key_hash, is_active,
                 scopes, metadata, expires_at, created_by)
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s)
            RETURNING id, name, key_prefix, is_active, scopes, metadata,
                      expires_at, created_at, updated_at
            """,
            (
                org_id,
                req.name.strip(),
                key_prefix,
                key_hash,
                Json(req.scopes),
                Json(req.metadata),
                req.expires_at,
                user_id,
            ),
        )
        row = dict(cur.fetchone())

    # Audit in separate transaction — never fails the create
    _write_audit(
        org_id=org_id,
        key_id=str(row["id"]),
        actor_user_id=user_id,
        action="created",
        key_prefix=key_prefix,
        request=request,
    )

    return {
        **row,
        "key": raw_key,
        "warning": "Store this key securely. It will not be shown again.",
    }


@router.patch("/{key_id}")
@limiter.limit(RATE_LIMIT_KEY_MGMT)
async def update_api_key(
    request: Request,
    key_id: str,
    req: ApiKeyUpdate,
    token: dict = Depends(verify_jwt_token),
):
    """
    Update name, active status, expiry, scopes, or metadata.

    Activation (is_active=True) additionally checks max_active_api_keys.
    """
    org_id = token["org_id"]
    user_id = token["user_id"]

    # Activation / scope changes require admin
    if (req.is_active is not None or req.scopes is not None) and token.get(
        "role"
    ) != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required to change key status or scopes",
        )

    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No updates provided"
        )

    # Gap #9 fix: validate every field against the explicit allowlist
    unknown = set(updates.keys()) - ALLOWED_UPDATE_FIELDS
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown update field(s): {', '.join(unknown)}",
        )

    # Gap #2 fix: check active limit before activating
    if updates.get("is_active") is True:
        with get_cursor() as cur:
            cur.execute(
                "SELECT max_active_api_keys FROM organizations WHERE id = %s",
                (org_id,),
            )
            org = cur.fetchone()
            if org:
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM org_api_keys
                    WHERE  org_id = %s AND is_active = TRUE AND id != %s
                    """,
                    (org_id, key_id),
                )
                active_count = cur.fetchone()["cnt"]
                max_active = org["max_active_api_keys"]
                if max_active > 0 and active_count >= max_active:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Active key limit reached ({active_count}/{max_active}). "
                            "Deactivate another key first."
                        ),
                    )

    set_parts: list[str] = []
    values: list = []
    for field, value in updates.items():
        if field in ("scopes", "metadata"):
            set_parts.append(f"{field} = %s")
            values.append(Json(value))
        else:
            set_parts.append(f"{field} = %s")
            values.append(value)

    values.extend([org_id, key_id])

    with get_cursor() as cur:
        cur.execute(
            f"""
            UPDATE org_api_keys
            SET    {", ".join(set_parts)}
            WHERE  org_id = %s AND id = %s
            RETURNING id, name, key_prefix, is_active, scopes, metadata,
                      expires_at, last_used_at, updated_at
            """,
            values,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
            )
        row = dict(row)

    action = (
        "activated"
        if updates.get("is_active") is True
        else "deactivated"
        if updates.get("is_active") is False
        else "updated"
    )
    _write_audit(
        org_id=org_id,
        key_id=key_id,
        actor_user_id=user_id,
        action=action,
        key_prefix=row["key_prefix"],
        request=request,
    )
    return row


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(RATE_LIMIT_KEY_MGMT)
async def delete_api_key(
    request: Request,
    key_id: str,
    token: dict = Depends(require_org_admin),
):
    """Permanently delete an API key. Cannot be undone."""
    org_id = token["org_id"]
    user_id = token["user_id"]

    with get_cursor() as cur:
        cur.execute(
            "SELECT key_prefix FROM org_api_keys WHERE org_id = %s AND id = %s",
            (org_id, key_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
            )
        key_prefix = row["key_prefix"]

    # Audit before delete so key_id FK still exists
    _write_audit(
        org_id=org_id,
        key_id=key_id,
        actor_user_id=user_id,
        action="deleted",
        key_prefix=key_prefix,
        request=request,
    )

    with get_cursor() as cur:
        cur.execute(
            "DELETE FROM org_api_keys WHERE org_id = %s AND id = %s",
            (org_id, key_id),
        )


@router.post("/{key_id}/rotate", status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_KEY_MGMT)
async def rotate_api_key(
    request: Request,
    key_id: str,
    token: dict = Depends(require_org_admin),
):
    """
    Rotate an API key — generate a new secret while keeping the same record.
    Old key is immediately invalidated. New raw key returned once only.

    Gap #3 fix: preserves the key's current is_active status instead of
    forcing is_active=TRUE, preventing silent bypass of the active key limit.
    """
    org_id = token["org_id"]
    user_id = token["user_id"]

    raw_key, key_prefix, key_hash = generate_api_key()

    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE org_api_keys
            SET    key_prefix   = %s,
                   key_hash     = %s,
                   last_used_at = NULL
            WHERE  org_id = %s AND id = %s
            RETURNING id, name, key_prefix, is_active, scopes, metadata,
                      expires_at, created_at, updated_at
            """,
            (key_prefix, key_hash, org_id, key_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
            )
        row = dict(row)

    _write_audit(
        org_id=org_id,
        key_id=key_id,
        actor_user_id=user_id,
        action="rotated",
        key_prefix=key_prefix,
        request=request,
        metadata={"new_key_prefix": key_prefix},
    )

    return {
        **row,
        "key": raw_key,
        "warning": "Store this key securely. It will not be shown again.",
    }
