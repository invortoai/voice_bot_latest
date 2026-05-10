"""OTEL metric instruments for Invorto voice calls.

All instruments are initialised once (lazy singleton) on first call to
_instruments(). Every public function is a no-op when the MeterProvider is
not configured (local dev without OTLP_ENDPOINT).

Naming follows OTEL semantic conventions:
  Namespace   : invorto.*
  AI services : gen_ai.system / gen_ai.request.model  (OTel GenAI semconv)
  Telephony   : telephony.provider
  Tenant      : org_id  (low-cardinality; safe as a metric dimension)

call_sid is intentionally NOT a metric attribute (high-cardinality → time-series
explosion). It lives on traces (call.id span attribute) for per-call APM queries.
"""

from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger(__name__)

# Backing store for the active-call ObservableGauge. Module-level so the
# gauge callback reads the true live state on each poll. An UpDownCounter
# would drift permanently on worker crash (finally block never runs on SIGKILL);
# an ObservableGauge always reflects reality.
_active_call_attrs: Optional[dict] = None

_instr: Optional[dict] = None


def _instruments() -> Optional[dict]:
    """Return the metric instrument dict, or None if OTEL metrics not active."""
    global _instr
    if _instr is not None:
        return _instr

    try:
        from opentelemetry import metrics

        meter = metrics.get_meter("invorto.worker", version="1.0.0")

        def _observe_active(options):
            if _active_call_attrs is not None:
                yield metrics.Observation(1, _active_call_attrs)
            else:
                yield metrics.Observation(0, {})

        meter.create_observable_gauge(
            name="invorto.call.active",
            callbacks=[_observe_active],
            unit="{call}",
            description="1 when a voice call is active on this worker, 0 otherwise.",
        )
        duration = meter.create_histogram(
            name="invorto.call.duration",
            unit="s",
            description="Duration of active voice call (client connected → disconnected).",
        )
        initial_latency = meter.create_histogram(
            name="invorto.call.initial_latency",
            unit="ms",
            description=(
                "End-to-end latency from telephony webhook to first bot audio (ms). "
                "Covers runner webhook processing + network hop + worker pipeline startup."
            ),
        )
        turn_latency = meter.create_histogram(
            name="invorto.turn.latency",
            unit="ms",
            description="Time from user stopped speaking to bot started speaking (ms).",
        )
        stt_ttfb = meter.create_histogram(
            name="invorto.stt.ttfb",
            unit="ms",
            description="STT time-to-first-byte per utterance (ms).",
        )
        llm_ttfb = meter.create_histogram(
            name="invorto.llm.ttfb",
            unit="ms",
            description="LLM time-to-first-token per turn (ms).",
        )
        tts_ttfb = meter.create_histogram(
            name="invorto.tts.ttfb",
            unit="ms",
            description="TTS time-to-first-audio-chunk per turn (ms).",
        )
        llm_tokens = meter.create_counter(
            name="invorto.llm.tokens",
            unit="{token}",
            description="LLM tokens consumed. Attribute gen_ai.token.type=input|output.",
        )
        tts_characters = meter.create_counter(
            name="invorto.tts.characters",
            unit="{char}",
            description="Characters sent to TTS (proxy for TTS cost).",
        )
        service_errors = meter.create_counter(
            name="invorto.service.errors",
            unit="{error}",
            description="Errors from AI services (STT/LLM/TTS). Attributes: service, error.type.",
        )
        prewarm_duration = meter.create_histogram(
            name="invorto.prewarm.duration",
            unit="ms",
            description="Time to complete worker prewarm (ms). Attributes: stt_hit, tts_hit.",
        )

        _instr = {
            "duration": duration,
            "initial_latency": initial_latency,
            "turn_latency": turn_latency,
            "stt_ttfb": stt_ttfb,
            "llm_ttfb": llm_ttfb,
            "tts_ttfb": tts_ttfb,
            "llm_tokens": llm_tokens,
            "tts_characters": tts_characters,
            "service_errors": service_errors,
            "prewarm_duration": prewarm_duration,
        }
        return _instr

    except Exception as exc:
        _log.warning("Failed to initialise OTEL metric instruments: %s", exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def record_call_start(org_id: str, provider: str, call_type: str = "inbound") -> None:
    """Mark this worker as active. Initialises the lazy instrument singleton."""
    global _active_call_attrs
    _active_call_attrs = {
        "telephony.provider": provider.lower(),
        "org_id": org_id or "unknown",
        "call.type": call_type or "inbound",
    }
    _instruments()


def record_call_end(
    d: dict, org_id: str, provider: str, call_type: str = "inbound"
) -> None:
    """Record all end-of-call metrics from the completed call metrics dict.

    Accepts a pre-built dict (from CallMetrics.to_dict()) so the caller
    serialises once and passes the same dict to OTEL metrics, log event, and DB.

    _active_call_attrs is cleared first so the ObservableGauge reads 0 even if
    the rest of this function raises.
    """
    global _active_call_attrs
    _active_call_attrs = None

    instr = _instruments()
    if instr is None:
        return

    base_attrs = {
        "telephony.provider": provider.lower(),
        "org_id": org_id or "unknown",
        "call.type": call_type or "inbound",
    }

    ended_by = d.get("ended_by") or "unknown"
    outcome_attrs = {**base_attrs, "call.ended_by": ended_by}
    if d.get("error_type"):
        outcome_attrs["error.type"] = d["error_type"]

    if (duration_s := d.get("call_active_seconds")) is not None:
        instr["duration"].record(duration_s, outcome_attrs)

    il = d.get("initial_latency") or {}
    if (total_ms := il.get("total_ms")) is not None:
        pw = d.get("prewarm_used") or {}
        instr["initial_latency"].record(
            total_ms,
            {
                **base_attrs,
                "prewarm_used": str(
                    pw.get("stt", False) or pw.get("tts", False)
                ).lower(),
            },
        )

    llm = d.get("llm") or {}
    llm_attrs = {
        "gen_ai.system": llm.get("provider") or "openai",
        "gen_ai.request.model": llm.get("model") or "unknown",
        "org_id": org_id or "unknown",
    }
    if tokens_in := llm.get("tokens_in") or 0:
        instr["llm_tokens"].add(tokens_in, {**llm_attrs, "gen_ai.token.type": "input"})
    if tokens_out := llm.get("tokens_out") or 0:
        instr["llm_tokens"].add(
            tokens_out, {**llm_attrs, "gen_ai.token.type": "output"}
        )

    tts = d.get("tts") or {}
    if chars := tts.get("characters") or 0:
        instr["tts_characters"].add(
            chars,
            {
                "gen_ai.system": tts.get("provider") or "elevenlabs",
                "gen_ai.request.model": tts.get("model") or "unknown",
                "org_id": org_id or "unknown",
            },
        )


def record_stt_ttfb(model: str, system: str, org_id: str, value_ms: float) -> None:
    instr = _instruments()
    if instr is None:
        return
    instr["stt_ttfb"].record(
        value_ms,
        {
            "gen_ai.system": system,
            "gen_ai.request.model": model,
            "org_id": org_id or "unknown",
        },
    )


def record_llm_ttfb(model: str, system: str, org_id: str, value_ms: float) -> None:
    instr = _instruments()
    if instr is None:
        return
    instr["llm_ttfb"].record(
        value_ms,
        {
            "gen_ai.system": system,
            "gen_ai.request.model": model,
            "org_id": org_id or "unknown",
        },
    )


def record_tts_ttfb(model: str, system: str, org_id: str, value_ms: float) -> None:
    instr = _instruments()
    if instr is None:
        return
    instr["tts_ttfb"].record(
        value_ms,
        {
            "gen_ai.system": system,
            "gen_ai.request.model": model,
            "org_id": org_id or "unknown",
        },
    )


def record_turn_latency(org_id: str, provider: str, value_ms: float) -> None:
    instr = _instruments()
    if instr is None:
        return
    instr["turn_latency"].record(
        value_ms,
        {"telephony.provider": provider.lower(), "org_id": org_id or "unknown"},
    )


def record_service_error(
    service: str, system: str, error_type: str, org_id: str
) -> None:
    """Record one service error (e.g. STT empty transcript, LLM timeout, TTS failure).

    service   : 'stt' | 'llm' | 'tts'
    system    : gen_ai.system value (e.g. 'deepgram', 'openai', 'elevenlabs')
    error_type: short error class string (e.g. 'EmptyTranscript', 'Timeout')
    """
    instr = _instruments()
    if instr is None:
        return
    instr["service_errors"].add(
        1,
        {
            "service": service,
            "gen_ai.system": system,
            "error.type": error_type,
            "org_id": org_id or "unknown",
        },
    )


def record_prewarm(prewarm_metrics: dict, org_id: str) -> None:
    """Record prewarm duration and service hit/miss outcome.

    prewarm_metrics: dict from PrewarmEntry.prewarm_metrics (set by _fill_prewarm)
    """
    instr = _instruments()
    if instr is None:
        return
    total_ms = prewarm_metrics.get("total_ms")
    if total_ms is None:
        return
    instr["prewarm_duration"].record(
        total_ms,
        {
            "stt_hit": str(prewarm_metrics.get("stt_ready", False)).lower(),
            "tts_hit": str(prewarm_metrics.get("tts_ready", False)).lower(),
            "org_id": org_id or "unknown",
        },
    )
