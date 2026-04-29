"""OpenTelemetry provider setup.

Configures all three OTEL signals when OTLP_ENDPOINT is set:
  - Traces  → {endpoint}/v1/traces   (BatchSpanProcessor)
  - Metrics → {endpoint}/v1/metrics  (PeriodicExportingMetricReader, 60 s)
  - Logs    → {endpoint}/v1/logs     (BatchLogRecordProcessor)

A TracerProvider is always created (even without OTLP_ENDPOINT) so that
auto-instrumentation generates valid spans and trace_id/span_id appear in logs.

Collector mode (OTEL_USE_COLLECTOR=true):
  - OTLP_HEADERS are omitted — collector owns backend auth
  - In-process LoggerProvider is skipped — collector ingests logs from stdout,
    so the loguru→OTEL bridge would double-ship every log line
"""

from typing import Optional


def _parse_otlp_headers(raw: str) -> dict:
    """Parse "Key=Value,Key2=Value2" into a dict.

    Only the first '=' per pair is the delimiter so Bearer tokens containing
    '=' are handled correctly. Malformed pairs are skipped with a warning.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)
    headers: dict = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            _log.warning(
                "Skipping malformed OTLP_HEADERS entry (missing '='): %r", pair
            )
            continue
        k, _, v = pair.partition("=")
        headers[k.strip()] = v.strip()
    return headers


def setup_otel(
    service_name: str,
    environment: Optional[str] = None,
) -> None:
    """Configure OTEL TracerProvider, MeterProvider, and LoggerProvider."""
    from app.config import ENVIRONMENT, OTLP_ENDPOINT, OTLP_HEADERS, OTEL_USE_COLLECTOR

    try:
        from app.version import __version__ as service_version
    except Exception:
        service_version = "unknown"

    environment = environment or ENVIRONMENT
    endpoint = OTLP_ENDPOINT.rstrip("/")

    raw_headers = "" if OTEL_USE_COLLECTOR else OTLP_HEADERS
    headers = _parse_otlp_headers(raw_headers) if raw_headers else {}

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as e:
        import logging

        logging.getLogger(__name__).warning(
            "OpenTelemetry SDK not available, skipping setup: %s", e
        )
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": environment,
        }
    )

    # ── Tracing ───────────────────────────────────────────────────────────────
    # Always create a real TracerProvider so auto-instrumentation generates valid
    # spans and trace_id/span_id values appear in every log record.
    tracer_provider = TracerProvider(resource=resource)

    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)
            )
        )

    trace.set_tracer_provider(tracer_provider)

    # ── Metrics ───────────────────────────────────────────────────────────────
    # Explicit histogram bucket boundaries for voice-domain latency metrics.
    # Default SDK buckets lack resolution in the 100–2000 ms range that matters
    # for STT/LLM/TTS TTFB SLO tracking — without these views p95 is meaningless.
    try:
        from opentelemetry.sdk.metrics.aggregation import (
            ExplicitBucketHistogramAggregation,
        )
        from opentelemetry.sdk.metrics.view import View

        _VOICE_MS_BUCKETS = [
            50, 100, 150, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000,
        ]
        _DURATION_S_BUCKETS = [10, 30, 60, 120, 180, 300, 600, 1800]

        _histogram_views = [
            View(
                instrument_name="invorto.turn.latency",
                aggregation=ExplicitBucketHistogramAggregation(_VOICE_MS_BUCKETS),
            ),
            View(
                instrument_name="invorto.stt.ttfb",
                aggregation=ExplicitBucketHistogramAggregation(_VOICE_MS_BUCKETS),
            ),
            View(
                instrument_name="invorto.llm.ttfb",
                aggregation=ExplicitBucketHistogramAggregation(_VOICE_MS_BUCKETS),
            ),
            View(
                instrument_name="invorto.tts.ttfb",
                aggregation=ExplicitBucketHistogramAggregation(_VOICE_MS_BUCKETS),
            ),
            View(
                instrument_name="invorto.call.initial_latency",
                aggregation=ExplicitBucketHistogramAggregation(_VOICE_MS_BUCKETS),
            ),
            View(
                instrument_name="invorto.call.duration",
                aggregation=ExplicitBucketHistogramAggregation(_DURATION_S_BUCKETS),
            ),
        ]
    except ImportError:
        _histogram_views = []

    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers),
            export_interval_millis=60_000,
        )
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[metric_reader],
            views=_histogram_views,
        )
    else:
        meter_provider = MeterProvider(resource=resource, views=_histogram_views)

    metrics.set_meter_provider(meter_provider)

    # ── Logs ──────────────────────────────────────────────────────────────────
    # Skipped in collector mode: the collector ingests logs from stdout so the
    # in-process bridge would double-ship every line. Span correlation still
    # works because trace_id/span_id are injected into stdout JSON by the
    # loguru patcher in logging.py (independent of the LoggerProvider).
    if not OTEL_USE_COLLECTOR:
        try:
            from opentelemetry._logs import set_logger_provider
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

            logger_provider = LoggerProvider(resource=resource)

            if endpoint:
                from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                    OTLPLogExporter,
                )

                logger_provider.add_log_record_processor(
                    BatchLogRecordProcessor(
                        OTLPLogExporter(
                            endpoint=f"{endpoint}/v1/logs", headers=headers
                        )
                    )
                )

            set_logger_provider(logger_provider)

        except ImportError as e:
            import logging

            logging.getLogger(__name__).warning(
                "OTEL log SDK not available — OTLP log export disabled: %s", e
            )
