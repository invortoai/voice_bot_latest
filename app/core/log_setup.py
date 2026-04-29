"""App-level logging setup — thin wrapper over observability.logging."""

import os
from typing import Optional


def setup_logging(
    service: str,
    environment: Optional[str] = None,
) -> None:
    from app.config import OTLP_ENDPOINT
    from app.core.context import get_log_context

    environment = environment or os.getenv("ENVIRONMENT", "production")

    try:
        from app.version import __version__ as ver
    except Exception:
        ver = "unknown"

    from app.observability.logging import setup_logging as _setup

    _setup(
        service_name=f"invorto-{service}",
        environment=environment,
        otlp_endpoint=OTLP_ENDPOINT,
        context_fn=get_log_context,
        service_version=ver,
    )
