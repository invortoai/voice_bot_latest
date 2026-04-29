"""Span decorators and library instrumentors.

Primitives
----------
@traced                         — wrap one function/coroutine in an OTEL span
@trace_class                    — wrap every public method of a class
register_library_instrumentors  — activate OTel community instrumentors
                                  for third-party libs (psycopg2, httpx, openai, requests)

Span names default to "{module}.{qualname}" e.g. "worker_pool.EC2WorkerPool.assign"
and can be overridden with the `name=` kwarg.

The tracer is resolved lazily so this module is safe to import before
setup_otel() has been called.
"""

import asyncio
import inspect
from contextlib import contextmanager
from functools import wraps
from typing import Callable, Optional


def _get_tracer():
    from opentelemetry import trace

    return trace.get_tracer("invorto")


def _span_name(fn: Callable, override: Optional[str]) -> str:
    if override:
        return override
    module = fn.__module__.split(".")[-1]
    return f"{module}.{fn.__qualname__}"


def traced(
    fn: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    attrs: Optional[Callable] = None,
):
    """Wrap a function or coroutine in an OTEL span.

    Usage:
        @traced
        @traced(name="worker_pool.assign")
        @traced(attrs=lambda a, kw: {"org_id": kw.get("org_id")})
    """

    def decorator(func: Callable) -> Callable:
        span_name = _span_name(func, name)

        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                with _get_tracer().start_as_current_span(span_name) as span:
                    if attrs:
                        for k, v in (attrs(args, kwargs) or {}).items():
                            if v is not None:
                                span.set_attribute(k, str(v))
                    return await func(*args, **kwargs)

            return async_wrapper

        if inspect.isgeneratorfunction(func):

            @wraps(func)
            @contextmanager
            def gen_wrapper(*args, **kwargs):
                with _get_tracer().start_as_current_span(span_name) as span:
                    if attrs:
                        for k, v in (attrs(args, kwargs) or {}).items():
                            if v is not None:
                                span.set_attribute(k, str(v))
                    yield from func(*args, **kwargs)

            return gen_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            with _get_tracer().start_as_current_span(span_name) as span:
                if attrs:
                    for k, v in (attrs(args, kwargs) or {}).items():
                        if v is not None:
                            span.set_attribute(k, str(v))
                return func(*args, **kwargs)

        return sync_wrapper

    if fn is not None:
        return decorator(fn)
    return decorator


def trace_class(
    cls=None,
    *,
    prefix: Optional[str] = None,
    exclude: Optional[set] = None,
):
    """Auto-trace every public method of a class.

    Usage:
        @trace_class
        class WorkerPool: ...

        @trace_class(prefix="svc", exclude={"health_check"})
        class AssistantService: ...

    Span names: "{prefix}.{method_name}"
    """

    def decorator(klass):
        _exclude = exclude or set()
        _prefix = prefix or klass.__name__.lower()

        for attr_name, attr_val in inspect.getmembers(
            klass, predicate=inspect.isfunction
        ):
            if attr_name.startswith("_") or attr_name in _exclude:
                continue
            setattr(klass, attr_name, traced(name=f"{_prefix}.{attr_name}")(attr_val))

        return klass

    if cls is not None:
        return decorator(cls)
    return decorator


# ── Library instrumentors ─────────────────────────────────────────────────────

_INSTRUMENTORS = [
    ("psycopg2", "opentelemetry.instrumentation.psycopg2", "Psycopg2Instrumentor"),
    ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    # openai intentionally excluded: the community OpenAI instrumentor (Traceloop)
    # captures prompt content and completion text as span attributes by default,
    # which would send conversation data (including system prompts) to the APM
    # backend. Token counts and TTFB are captured via Pipecat MetricsFrame instead.
    ("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
]


def register_library_instrumentors() -> None:
    """Activate OTel instrumentors for third-party libraries.

    Each instrumentor is skipped silently if its package is not installed.
    """
    import importlib
    import logging

    _log = logging.getLogger(__name__)
    for label, module_path, class_name in _INSTRUMENTORS:
        try:
            mod = importlib.import_module(module_path)
            getattr(mod, class_name)().instrument()
            _log.debug("OTel instrumentor activated: %s", label)
        except ImportError:
            pass
        except Exception as exc:
            _log.warning("Failed to activate %s instrumentor: %s", label, exc)
