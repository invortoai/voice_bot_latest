"""
Pipecat OpenTelemetry tracing wired to Langfuse via OTLP HTTP.

When Langfuse keys are set, we attach an OTLP HTTP exporter directly to the
existing global TracerProvider (set up by setup_otel() in worker/main.py).
PipelineTask must use enable_tracing=True so STT/LLM/TTS spans appear in Langfuse.

We do NOT call Pipecat's setup_tracing() because OTEL only allows
set_tracer_provider() once — a second call is silently rejected, leaving the
Langfuse exporter attached to a provider that is never used.

See: https://docs.pipecat.ai/server/utilities/opentelemetry
     https://langfuse.com/docs/opentelemetry/get-started
"""

import base64

from loguru import logger

from app.config import (
    LANGFUSE_BASE_URL,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
    LANGFUSE_TRACING_ENABLED,
)


def _is_langfuse_otlp_enabled() -> bool:
    return bool(
        LANGFUSE_TRACING_ENABLED and LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY
    )


def setup_pipecat_langfuse_tracing() -> bool:
    """
    Attach a Langfuse OTLP exporter to the existing global TracerProvider.

    Langfuse uses Basic auth: base64(public_key:secret_key).
    Returns True if tracing was set up, False otherwise.
    """
    if not _is_langfuse_otlp_enabled():
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning(
            "OTLP tracing dependencies missing; Langfuse OTLP disabled: %s",
            e,
        )
        return False

    # Langfuse OTLP endpoint (HTTP only; gRPC not supported)
    endpoint = f"{LANGFUSE_BASE_URL.rstrip('/')}/api/public/otel/v1/traces"
    auth_string = base64.b64encode(
        f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()
    ).decode()

    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={"Authorization": f"Basic {auth_string}"},
    )

    # Attach to the existing global provider set by setup_otel().
    # Calling set_tracer_provider() a second time is silently rejected by OTEL,
    # so we add the processor directly instead of going through setup_tracing().
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info(
            "Pipecat OpenTelemetry tracing initialized (Langfuse OTLP) — "
            "attached to existing TracerProvider"
        )
        return True

    logger.warning(
        "No SDK TracerProvider found (got %s); Langfuse OTLP not attached. "
        "Ensure setup_otel() is called before setup_pipecat_langfuse_tracing().",
        type(provider).__name__,
    )
    return False


def is_pipecat_tracing_enabled() -> bool:
    """True if we should pass enable_tracing=True to PipelineTask."""
    return _is_langfuse_otlp_enabled()
