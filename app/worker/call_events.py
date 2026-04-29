"""Structured log events for voice call lifecycle.

Each function emits one loguru INFO log with a fixed message string and all
event fields in the `extra` dict — flat structure, no nested JSON.

All numeric summaries (avg, p50, p95, max) are computed in Python before emit
so DataPrime queries are simple field filters — no jsonparse() or unnest needed.
Raw per-turn breakdown stays in calls.metrics JSONB for deep per-call forensics.

Example DataPrime queries:

    filter $l.applicationName == 'invorto-worker'
    | filter $.attributes.event_type == 'invorto.call.completed'
    | select $.attributes.call_sid, $.attributes.org_id,
             $.attributes.duration_s, $.attributes.total_turns,
             $.attributes.turn_latency_p95_ms, $.attributes.llm_tokens_out

    # Per-org p95 turn latency — no jsonparse/unnest needed
    filter $.attributes.event_type == 'invorto.call.completed'
    | groupby $.attributes.org_id
    | aggregate percentile($.attributes.turn_latency_p95_ms, 95)
"""

from __future__ import annotations

from loguru import logger


def _pct(values: list[float], p: int) -> float | None:
    """Return the p-th percentile of values (nearest-rank method)."""
    if not values:
        return None
    s = sorted(values)
    idx = max(0, int(len(s) * p / 100) - 1)
    return round(s[idx], 1)


def _turn_stats(turns: list[dict] | None, field: str) -> dict:
    """Compute avg/p50/p95/max for a single numeric field across all turns."""
    if not turns:
        return {}
    vals = [t[field] for t in turns if t.get(field) is not None]
    if not vals:
        return {}
    avg = round(sum(vals) / len(vals), 1)
    return {
        f"{field}_avg": avg,
        f"{field}_p50": _pct(vals, 50),
        f"{field}_p95": _pct(vals, 95),
        f"{field}_max": round(max(vals), 1),
    }


def emit_turn_completed(
    turn_index: int,
    turn: dict,
    org_id: str,
    provider: str,
) -> None:
    """Log one event per completed bot turn.

    call_sid is injected automatically from the loguru contextvar patcher —
    no need to pass it explicitly.

    DataPrime per-call drill-down:
        filter $.attributes.event_type == 'invorto.turn.completed'
        | filter $.attributes.call_sid == 'CA123'
        | select $.attributes.turn_index, $.attributes.total_ms,
                 $.attributes.stt_ttfb_ms, $.attributes.llm_ttfb_ms,
                 $.attributes.tts_ttfb_ms

    DataPrime cross-call aggregate (no jsonparse/unnest needed):
        filter $.attributes.event_type == 'invorto.turn.completed'
        | groupby $.attributes.org_id
        | aggregate percentile($.attributes.total_ms, 95)
    """
    logger.info(
        "invorto.turn.completed",
        event_type="invorto.turn.completed",
        org_id=org_id or "unknown",
        provider=provider.lower(),
        turn_index=turn_index,
        total_ms=turn.get("total_ms"),
        stt_ttfb_ms=turn.get("stt_ttfb_ms"),
        llm_ttfb_ms=turn.get("llm_ttfb_ms"),
        tts_ttfb_ms=turn.get("tts_ttfb_ms"),
        smart_turn_ms=turn.get("smart_turn_ms"),
    )


def emit_call_started(
    call_sid: str,
    org_id: str,
    provider: str,
    caller: str = "",
    callee: str = "",
    call_type: str = "inbound",
) -> None:
    logger.info(
        "invorto.call.started",
        event_type="invorto.call.started",
        call_sid=call_sid,
        org_id=org_id or "unknown",
        provider=provider.lower(),
        caller=caller or "",
        callee=callee or "",
        call_type=call_type or "inbound",
    )


def emit_call_completed(
    call_sid: str,
    org_id: str,
    provider: str,
    d: dict,
    caller: str = "",
    callee: str = "",
    call_type: str = "inbound",
) -> None:
    """Log call-completed event with pre-computed flat stats.

    Accepts a pre-built dict (from CallMetrics.to_dict()) so the caller
    serialises once and passes the same dict to OTEL metrics, this log event,
    and the DB save — all three consumers see identical data.

    Turn latency and TTFB stats are pre-computed here so DataPrime queries
    are simple field accesses (no jsonparse/unnest). Raw turns[] stays only
    in calls.metrics JSONB for per-call drill-down.
    """
    llm = d.get("llm") or {}
    tts = d.get("tts") or {}
    stt = d.get("stt") or {}
    il = d.get("initial_latency") or {}
    pw = d.get("prewarm_used") or {}
    turns = d.get("turns") or []

    # Pre-compute turn-level stats (flat — no arrays in log event)
    tl = _turn_stats(turns, "total_ms")
    stt_ttfb = _turn_stats(turns, "stt_ttfb_ms")
    llm_ttfb = _turn_stats(turns, "llm_ttfb_ms")
    tts_ttfb = _turn_stats(turns, "tts_ttfb_ms")

    logger.info(
        "invorto.call.completed",
        # ── Identity ───────────────────────────────────────────────────────────
        event_type="invorto.call.completed",
        call_sid=call_sid,
        org_id=org_id or "unknown",
        provider=provider.lower(),
        caller=caller or "",
        callee=callee or "",
        call_type=call_type or "inbound",
        # ── Call outcome ───────────────────────────────────────────────────────
        ended_by=d.get("ended_by") or "unknown",
        error_type=d.get("error_type"),
        # ── Duration & conversation ────────────────────────────────────────────
        duration_s=d.get("call_active_seconds"),
        total_turns=d.get("total_turns"),
        interruptions=d.get("interruptions"),
        vad_triggers=d.get("vad_triggers"),
        # ── Turn latency (total user-stopped → bot-started) ────────────────────
        turn_latency_avg_ms=tl.get("total_ms_avg"),
        turn_latency_p50_ms=tl.get("total_ms_p50"),
        turn_latency_p95_ms=tl.get("total_ms_p95"),
        turn_latency_max_ms=tl.get("total_ms_max"),
        # ── STT TTFB per turn ─────────────────────────────────────────────────
        stt_ttfb_avg_ms=stt_ttfb.get("stt_ttfb_ms_avg"),
        stt_ttfb_p95_ms=stt_ttfb.get("stt_ttfb_ms_p95"),
        # ── LLM TTFB per turn ─────────────────────────────────────────────────
        llm_ttfb_avg_ms=llm_ttfb.get("llm_ttfb_ms_avg"),
        llm_ttfb_p95_ms=llm_ttfb.get("llm_ttfb_ms_p95"),
        # ── TTS TTFB per turn ─────────────────────────────────────────────────
        tts_ttfb_avg_ms=tts_ttfb.get("tts_ttfb_ms_avg"),
        tts_ttfb_p95_ms=tts_ttfb.get("tts_ttfb_ms_p95"),
        # ── Initial latency breakdown ──────────────────────────────────────────
        il_total_ms=il.get("total_ms"),
        il_runner_webhook_ms=il.get("runner_webhook_ms"),
        il_transport_hop_ms=il.get("transport_hop_ms"),
        il_ws_msg_recv_ms=il.get("ws_msg_recv_ms"),
        il_config_resolve_ms=il.get("config_resolve_ms"),
        il_pipeline_build_ms=il.get("pipeline_build_ms"),
        il_transport_start_ms=il.get("transport_start_ms"),
        il_greeting_tts_ttfb_ms=il.get("greeting_tts_ttfb_ms"),
        il_worker_ms=il.get("worker_ms"),
        # ── STT ───────────────────────────────────────────────────────────────
        stt_provider=stt.get("provider"),
        stt_model=stt.get("model"),
        stt_language=stt.get("language"),
        stt_transcript_count=stt.get("transcript_count"),
        stt_empty_transcripts=stt.get("empty_transcripts"),
        stt_avg_transcript_len=stt.get("avg_transcript_length"),
        # ── LLM ───────────────────────────────────────────────────────────────
        llm_provider=llm.get("provider"),
        llm_model=llm.get("model"),
        llm_tokens_in=llm.get("tokens_in"),
        llm_tokens_out=llm.get("tokens_out"),
        # ── TTS ───────────────────────────────────────────────────────────────
        tts_provider=tts.get("provider"),
        tts_model=tts.get("model"),
        tts_voice_id=tts.get("voice_id"),
        tts_characters=tts.get("characters"),
        # ── Prewarm ───────────────────────────────────────────────────────────
        prewarm_stt=pw.get("stt", False),
        prewarm_tts=pw.get("tts", False),
    )
