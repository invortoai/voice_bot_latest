"""Re-export tracing primitives from the standalone observability package."""

from app.observability.tracing import (  # noqa: F401
    traced,
    trace_class,
    register_library_instrumentors,
)
