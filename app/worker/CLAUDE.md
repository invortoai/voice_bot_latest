# Worker CLAUDE.md

## What
Voice call processor running Pipecat pipeline. Receives WebSocket audio streams, processes through STT→LLM→TTS, returns audio.

## Guardrails

**Single-Tenant**: One call per worker at a time. `state.py` manages availability—never bypass `worker_state.start_call()`/`end_call()`.

**Twilio vs Jambonz—Don't Mix**:
- Twilio: `/ws` endpoint, `TwilioFrameSerializer`, mulaw @ 8kHz
- Jambonz: `/ws/jambonz` endpoint, `JambonzFrameSerializer`, linear16 @ 8kHz (configurable)
- See `pipeline.py` for `create_pipeline()` vs `create_jambonz_pipeline()`

**Audio Encoding**: Twilio expects mulaw; Jambonz expects linear16. Wrong encoding = garbled audio. Check `DeepgramSTTService(encoding=...)` matches the provider.

**Config Priority**: `AssistantConfig` loads from database (`assistant_config`) first, falls back to WebSocket `custom_params`. See `config.py` for field mappings.

**Greeting Audio URLs**: `is_audio_url()` detects URLs ending in audio extensions. If greeting is a URL, it fetches and streams raw PCM; use `TTSSpeakFrame` instead for TTS.

## Key Files
- `main.py` – WebSocket handlers and pipeline lifecycle
- `pipeline.py` – Pipeline construction for both providers
- `config.py` – `AssistantConfig` class (model, voice, prompts)
- `state.py` – Worker availability tracking
- `jambonz/` – Jambonz-specific transport and serializer
