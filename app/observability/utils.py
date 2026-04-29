"""Defensive wrapper for observability emit calls.

In local/dev environments exceptions propagate immediately so bugs surface
during development. In production they are logged at DEBUG and swallowed —
a telemetry failure never touches the application call path.

Usage:
    from app.observability.utils import safe_observe

    safe_observe(emit_call_started, call_sid=..., org_id=..., provider=...)
"""

import logging
from typing import Callable

_log = logging.getLogger(__name__)


def safe_observe(fn: Callable, /, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        from app.config import IS_LOCAL

        if IS_LOCAL:
            raise
        _log.debug(
            "observability emit failed [%s]: %s",
            getattr(fn, "__name__", repr(fn)),
            exc,
        )
