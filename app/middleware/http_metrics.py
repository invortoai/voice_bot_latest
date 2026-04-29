"""HTTP request duration metrics with org_code dimension.

Records http.server.request.duration (OTEL semantic convention) as a histogram
so per-org APM dashboards can filter by org_code.

Design notes:
- Meter is created in __init__ (after setup_otel) so the real MeterProvider
  is already in place when the middleware is added.
- org_code is read from _org_id contextvar *after* call_next returns — by that
  point the auth dependency or webhook handler has already called
  set_log_context(org_id=...) for every authenticated and webhook route.
- /health is excluded (same as FastAPIInstrumentor) to avoid polling spam.
- Route template (e.g. /calls/{call_id}) is read from scope after routing.
"""

import time

from opentelemetry import metrics as otel_metrics
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.context import _org_id

_EXCLUDED_PATHS = frozenset({"/health"})


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    """Emit http.server.request.duration histogram with org_code label."""

    def __init__(self, app) -> None:
        super().__init__(app)
        meter = otel_metrics.get_meter("invorto.http", version="1.0.0")
        self._duration = meter.create_histogram(
            name="http.server.request.duration",
            unit="s",
            description="Duration of HTTP server requests.",
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _EXCLUDED_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            http_route = getattr(route, "path", request.url.path)
            self._duration.record(
                elapsed,
                {
                    "http.request.method": request.method,
                    "http.response.status_code": status_code,
                    "http.route": http_route,
                    "url.scheme": request.url.scheme,
                    "org_code": _org_id.get() or "unknown",
                },
            )
