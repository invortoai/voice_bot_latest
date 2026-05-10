"""Log context propagation via Python contextvars.

Call set_log_context() at the earliest point where a value is known:
  - auth dependencies   → org_id
  - webhook handlers    → call_sid, provider, org_id (after phone_config fetch)
  - WebSocket handlers  → call_sid, provider, org_id (after call_record fetch)

Every loguru log call in the same async task then automatically includes those
fields — no need to pass extra={} manually.
"""

from contextvars import ContextVar
from typing import Optional

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_call_sid: ContextVar[str] = ContextVar("call_sid", default="")
_org_id: ContextVar[str] = ContextVar("org_id", default="")
_provider: ContextVar[str] = ContextVar("provider", default="")


def set_log_context(
    *,
    request_id: Optional[str] = None,
    call_sid: Optional[str] = None,
    org_id: Optional[str] = None,
    provider: Optional[str] = None,
) -> None:
    """Set one or more log context fields for the current async task."""
    if request_id:
        _request_id.set(request_id)
    if call_sid:
        _call_sid.set(call_sid)
    if org_id:
        _org_id.set(org_id)
    if provider:
        _provider.set(provider)


def set_span_attrs(**attrs) -> None:
    """Set key=value attributes on the active OTEL span; no-ops if none is recording."""
    try:
        from opentelemetry import trace as _trace

        span = _trace.get_current_span()
        if span.is_recording():
            for k, v in attrs.items():
                if v is not None:
                    span.set_attribute(k, str(v))
    except Exception:
        pass


def get_log_context() -> dict:
    """Return non-empty context fields for the current async task."""
    return {
        k: v
        for k, v in {
            "request_id": _request_id.get(),
            "call_sid": _call_sid.get(),
            "org_id": _org_id.get(),
            "provider": _provider.get(),
        }.items()
        if v
    }
