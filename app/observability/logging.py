"""Structured logging setup with OTEL log bridge.

Two output formats controlled by LOG_FORMAT env var:

  LOG_FORMAT=text  (default for local/dev)
      Human-readable coloured one-liner:
      2026-04-09 10:23:45.123 | INFO     | message | MainThread | main.py._handle_call:697 | trace_id=abc span_id=xyz org_id=x

  LOG_FORMAT=json  (default for production)
      Flat JSON — one field per top level, no nesting under 'attributes.*'.
      Works natively with Coralogix and Grafana Loki.

Log routing:
  otlp_endpoint set   → structured logs go to OTLP endpoint (+ stdout)
  otlp_endpoint unset → stdout only

Context fields (org_id, call_sid, etc.) are injected per-record by the
optional context_fn callable: pass your app's get_log_context function.
OTEL trace_id/span_id are injected from the active span automatically.
"""

import json
import logging
import os
import socket
import sys
import traceback as _tb
from typing import IO, Callable, Optional

from loguru import logger

_ANSI_RESET = "\033[0m"
_LEVEL_COLORS = {
    "TRACE": "\033[2m",
    "DEBUG": "\033[96m",
    "INFO": "",
    "SUCCESS": "\033[92m",
    "WARNING": "\033[93m",
    "ERROR": "\033[91m",
    "CRITICAL": "\033[1;91m",
}

# Context fields printed in this order in text format; trace_id/span_id first
# for quick copy-paste into APM search.
_ORDERED_CTX = (
    "trace_id",
    "span_id",
    "request_id",
    "event_type",
    "org_id",
    "call_sid",
    "provider",
)

# Top-level JSON fields that must not be overwritten by extra fields.
_JSON_PROTECTED = frozenset(
    {
        "timestamp",
        "severity",
        "level",
        "message",
        "env",
        "thread",
        "code.file",
        "code.function",
        "code.line",
    }
)


class InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn, fastapi, …) through loguru."""

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


# ── Sinks ─────────────────────────────────────────────────────────────────────


def _make_text_sink(stream: IO[str], colorize: bool = False) -> Callable:
    def _sink(message):
        record = message.record
        ts = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        level = record["level"].name
        thread = record["thread"].name
        location = f"{record['file'].name}.{record['function']}:{record['line']}"
        msg = record["message"]

        extra = record["extra"]
        ctx_pairs: list[str] = [f"{k}={extra[k]}" for k in _ORDERED_CTX if extra.get(k)]
        seen = set(_ORDERED_CTX)
        for k, v in extra.items():
            if k not in seen and v is not None:
                ctx_pairs.append(f"{k}={v}")

        parts = [ts, f"{level:<8}", msg, thread, location]
        if ctx_pairs:
            parts.append(" ".join(ctx_pairs))
        line = " | ".join(parts)

        exc = record["exception"]
        if exc and exc.value:
            line += (
                "\n"
                + "".join(
                    _tb.format_exception(exc.type, exc.value, exc.traceback)
                ).rstrip()
            )

        if colorize:
            color = _LEVEL_COLORS.get(level, "")
            if color:
                line = f"{color}{line}{_ANSI_RESET}"

        stream.write(line + "\n")
        stream.flush()

    return _sink


def _build_json_entry(record: dict, resource: dict) -> dict:
    """Build a flat JSON log entry from a loguru record.

    All fields at the top level — no nesting. This means every field is
    a first-class citizen in Coralogix (column selectable, filterable without
    nested-path syntax) and in CloudWatch Insights (queryable with `fields`).
    """
    level_name = record["level"].name
    entry: dict = {
        "timestamp": record["time"].isoformat(),
        "severity": level_name,
        "level": level_name,
        "message": record["message"],
    }

    entry.update(resource)
    entry["env"] = resource.get("deployment.environment", "")

    entry["thread"] = record["thread"].name
    entry["code.file"] = record["file"].name
    entry["code.function"] = record["function"]
    entry["code.line"] = record["line"]

    for k, v in record["extra"].items():
        if v is not None and k not in _JSON_PROTECTED:
            entry[k] = v

    exc = record["exception"]
    if exc and exc.value:
        entry["exception.type"] = type(exc.value).__name__
        entry["exception.message"] = str(exc.value)
        entry["exception.stacktrace"] = "".join(
            _tb.format_exception(exc.type, exc.value, exc.traceback)
        )

    return entry


def _make_json_sink(
    resource: dict, stream: IO[str], colorize: bool = False
) -> Callable:
    def _sink(message):
        entry = _build_json_entry(message.record, resource)
        line = json.dumps(entry, default=str)
        if colorize:
            color = _LEVEL_COLORS.get(message.record["level"].name, "")
            if color:
                line = f"{color}{line}{_ANSI_RESET}"
        stream.write(line + "\n")
        stream.flush()

    return _sink


def _make_otlp_log_sink(service_name: str) -> Callable:
    """Bridge loguru records to the OTEL LoggerProvider.

    The LoggerProvider is resolved lazily at emit time — safe to register before
    setup_otel() is called. Only fires when a real SDK LoggerProvider is installed.

    In the OTLP LogRecord trace_id and span_id are integer fields (→ $l.traceId
    in Coralogix). Other fields land in attributes (→ $.attributes.field).
    Complex values (lists, dicts) are JSON-encoded so DataPrime can parse them.
    """
    _SEV: dict = {
        "TRACE": 1,
        "DEBUG": 5,
        "INFO": 9,
        "SUCCESS": 9,
        "WARNING": 13,
        "ERROR": 17,
        "CRITICAL": 21,
    }
    _last_error: list[str] = [""]  # mutable cell for dedup without nonlocal

    def _sink(message):
        try:
            from opentelemetry._logs import get_logger_provider
            from opentelemetry.sdk._logs import LoggerProvider as _SDKLoggerProvider

            provider = get_logger_provider()
            if not isinstance(provider, _SDKLoggerProvider):
                return

            from opentelemetry import trace as _trace
            from opentelemetry._logs import LogRecord, SeverityNumber

            record = message.record
            span_ctx = _trace.get_current_span().get_span_context()
            trace_id = span_ctx.trace_id if span_ctx.is_valid else None
            span_id = span_ctx.span_id if span_ctx.is_valid else None
            trace_flags = span_ctx.trace_flags if span_ctx.is_valid else None

            attrs: dict = {
                "code.filepath": record["file"].name,
                "code.function": record["function"],
                "code.lineno": record["line"],
                "thread": record["thread"].name,
            }
            for k, v in record["extra"].items():
                if v is None or k in ("trace_id", "span_id"):
                    continue
                # Complex types JSON-encoded so DataPrime can parse/query them.
                attrs[k] = (
                    v
                    if isinstance(v, (str, int, float, bool))
                    else json.dumps(v, default=str)
                )

            exc = record["exception"]
            if exc and exc.value:
                attrs["exception.type"] = type(exc.value).__name__
                attrs["exception.message"] = str(exc.value)
                attrs["exception.stacktrace"] = "".join(
                    _tb.format_exception(exc.type, exc.value, exc.traceback)
                )

            level_name = record["level"].name
            provider.get_logger(service_name).emit(
                LogRecord(
                    timestamp=int(record["time"].timestamp() * 1e9),
                    observed_timestamp=int(record["time"].timestamp() * 1e9),
                    trace_id=trace_id,
                    span_id=span_id,
                    trace_flags=trace_flags,
                    severity_text=level_name,
                    severity_number=SeverityNumber(_SEV.get(level_name, 9)),
                    body=record["message"],
                    attributes=attrs,
                )
            )
        except Exception as exc:
            msg = f"[OTLP sink] {type(exc).__name__}: {exc}"
            if msg != _last_error[0]:
                _last_error[0] = msg
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()

    return _sink


# ── Helpers ───────────────────────────────────────────────────────────────────


class _SuppressHealthCheckFilter(logging.Filter):
    """Drop uvicorn access log entries for GET /health."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "GET /health" not in record.getMessage()


def _intercept_stdlib_loggers() -> None:
    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(logging.INFO)
    for name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
        lg = logging.getLogger(name)
        lg.handlers = [InterceptHandler()]
        lg.propagate = False
        lg.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").addFilter(_SuppressHealthCheckFilter())
    # Suppress httpx INFO request logs (e.g. worker health check 200s) — errors still surface.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _build_resource(
    service_name: str, environment: str, host: str, version: str = "unknown"
) -> dict:
    return {
        "service.name": service_name,
        "service.version": version,
        "deployment.environment": environment,
        "host.name": host,
    }


# ── Main setup ────────────────────────────────────────────────────────────────


def setup_logging(
    service_name: str,
    environment: str,
    *,
    otlp_endpoint: str = "",
    log_format: Optional[str] = None,
    log_level: Optional[str] = None,
    context_fn: Optional[Callable[[], dict]] = None,
    service_version: str = "unknown",
) -> None:
    """Configure loguru sinks for the given service.

    Args:
        service_name:    OTEL service.name resource label
        environment:     Deployment environment (controls defaults)
        otlp_endpoint:   If set, logs also go to OTLP endpoint
        log_format:      "text" or "json" (auto-detected from environment)
        log_level:       Minimum log level (auto-detected from environment)
        context_fn:      Callable() → dict injected into every log record
        service_version: service.version resource attribute
    """
    is_local = environment.lower() in ("local", "dev", "development")
    host = socket.gethostname()

    _fmt = (
        log_format or os.getenv("LOG_FORMAT", "text" if is_local else "json")
    ).lower()

    _valid_levels = {
        "TRACE",
        "DEBUG",
        "INFO",
        "SUCCESS",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }
    _lvl_env = (log_level or os.getenv("LOG_LEVEL", "DEBUG")).upper()
    _lvl = _lvl_env if _lvl_env in _valid_levels else "INFO"

    resource = _build_resource(service_name, environment, host, service_version)

    def _patcher(record):
        if context_fn:
            record["extra"].update(context_fn())
        try:
            from opentelemetry import trace as _trace

            span = _trace.get_current_span()
            ctx = span.get_span_context()
            if ctx.is_valid:
                record["extra"]["trace_id"] = format(ctx.trace_id, "032x")
                record["extra"]["span_id"] = format(ctx.span_id, "016x")
        except Exception:
            pass

    logger.configure(patcher=_patcher)
    logger.remove()

    # ── stdout (always active) ────────────────────────────────────────────────
    _colorize = is_local
    if _fmt == "json":
        logger.add(
            _make_json_sink(resource, sys.stdout, colorize=_colorize), level=_lvl
        )
    else:
        logger.add(_make_text_sink(sys.stdout, colorize=_colorize), level=_lvl)

    # ── OTLP log destination (when configured) ────────────────────────────────
    if otlp_endpoint:
        logger.add(_make_otlp_log_sink(service_name), level=_lvl)
        logger.info(
            "Logging: format=%s level=%s destination=otlp endpoint=%s",
            _fmt,
            _lvl,
            otlp_endpoint,
        )
    else:
        logger.info("Logging: format=%s level=%s destination=stdout", _fmt, _lvl)

    _intercept_stdlib_loggers()
