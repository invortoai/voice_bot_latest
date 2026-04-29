"""Middleware to set request context for log correlation and error handling."""

from uuid import uuid4

from fastapi import Request
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.context import set_log_context
from app.utils.exceptions import set_request_context


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Generate a request_id and inject it (+ any caller-supplied one) into
    the log context for every request. Also stores the request object for
    error-handler use."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        set_log_context(request_id=request_id)
        set_request_context(request)
        if request.url.path != "/health":
            logger.info(f"Request received: {request.method} {request.url.path}")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
