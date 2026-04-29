"""API exception utilities for standardized error responses following RFC 7807."""

from contextlib import contextmanager
from contextvars import ContextVar
from http import HTTPStatus
from typing import Any, Generator, Optional

import psycopg2.errors
from fastapi import HTTPException, Request

# Context variable to store current request
_current_request: ContextVar[Optional[Request]] = ContextVar(
    "current_request", default=None
)


def set_request_context(request: Request) -> None:
    """Set the current request in context. Called by middleware.
    Args:
        request: FastAPI Request object
    """
    _current_request.set(request)


def get_request_path() -> str:
    """Get the current request path from context.

    Returns:
        Request path or '/unknown' if not available
    """
    request = _current_request.get()
    if request:
        return request.url.path
    return "/unknown"


def raise_api_error(
    status_code: int,
    detail: str,
    instance: Optional[str] = None,
    title: Optional[str] = None,
    error_type: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Raise a standardized API error following RFC 7807 Problem Details format.

    Args:
        status_code: HTTP status code (e.g., 404, 500)
        detail: Human-readable explanation specific to this occurrence
        instance: URI reference (auto-extracted from request if not provided)
        title: Short, human-readable summary (auto-generated if not provided)
        error_type: URI reference identifying the problem type (auto-generated if not provided)
        **kwargs: Additional members to include in the problem details

    Raises:
        HTTPException: With RFC 7807 compliant error detail structure
    """
    # Auto-extract instance from request if not provided
    if not instance:
        instance = get_request_path()

    # Generate title from HTTPStatus enum if not provided
    if not title:
        try:
            title = HTTPStatus(status_code).phrase
        except ValueError:
            # Fallback for non-standard status codes
            title = "Error"

    # Build RFC 7807 compliant error response
    error_detail = {
        "type": error_type,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
    }

    # Add any additional custom fields
    if kwargs:
        error_detail.update(kwargs)

    raise HTTPException(status_code=status_code, detail=error_detail)


@contextmanager
def handle_db_errors(operation: str) -> Generator:
    """Context manager that maps psycopg2 constraint errors to HTTP responses."""
    try:
        yield
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(
            status_code=409, detail=f"Conflict during {operation}: duplicate value"
        )
    except psycopg2.errors.ForeignKeyViolation:
        raise HTTPException(
            status_code=422, detail=f"Foreign key violation during {operation}"
        )
    except psycopg2.errors.CheckViolation:
        raise HTTPException(
            status_code=422, detail=f"Check constraint violation during {operation}"
        )
