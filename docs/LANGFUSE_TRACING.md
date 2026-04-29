# Langfuse Tracing (Optional)

Voice calls are traced via **Pipecat OpenTelemetry** and sent to [Langfuse](https://langfuse.com) over OTLP HTTP. When enabled, each call appears in Langfuse with conversation → turns → STT / LLM / TTS spans, token usage, and latency.

## Configuration

Add to `.env` (optional; omit to disable tracing):

```bash
# Langfuse – get keys from https://cloud.langfuse.com (or your self-hosted project)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...

# Optional: default is EU cloud
# LANGFUSE_BASE_URL=https://cloud.langfuse.com
# LANGFUSE_BASE_URL=https://us.cloud.langfuse.com

# Optional: set to false to disable without removing keys
# LANGFUSE_TRACING_ENABLED=true
```

- **Worker only**: Tracing is initialized in the worker at startup. The runner does not send traces.
- **No keys**: If `LANGFUSE_SECRET_KEY` (or public key) is unset, tracing is disabled and the worker runs normally.

## What Gets Traced

- One **conversation** per call with `conversation_id` = Twilio/Jambonz call SID.
- **Turns**: each user/assistant exchange.
- **Spans**: STT (Deepgram), LLM (OpenAI), TTS (ElevenLabs) with:
  - Token usage (input/output) for LLM
  - Time-to-first-byte and duration
  - Model and provider
- **Attributes**: `assistant_name`, `provider` (twilio / jambonz) on the conversation span.

## Steps to Test

### 1. Enable tracing

1. Create a project at [Langfuse Cloud](https://cloud.langfuse.com) (or use self-hosted).
2. In the project, open **Settings → API Keys** and create a key pair (public + secret).
3. Add to `.env`:
   ```bash
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   ```
4. Restart the **worker** so it picks up the new env and runs `setup_pipecat_langfuse_tracing()`.

### 2. Place a test call

1. Start runner and worker (see [LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md)).
2. Place an inbound or outbound call so the worker handles it (Twilio or Jambonz).
3. Talk for a few turns so there is STT → LLM → TTS activity.

### 3. Verify in Langfuse

1. Open your Langfuse project → **Traces** (or **Sessions**).
2. You should see a new trace per call, with:
   - **Trace/Conversation**: `conversation_id` = your call SID; attributes `assistant_name`, `provider`.
   - **Spans**: conversation → turns → `stt`, `llm`, `tts` with token counts and timings.

### 4. Optional: confirm worker logs

On worker startup you should see:

```text
Pipecat OpenTelemetry tracing initialized (Langfuse OTLP)
```

If keys are missing or tracing is disabled, this line does not appear and no traces are sent.

## Disabling tracing

- Remove `LANGFUSE_SECRET_KEY` and `LANGFUSE_PUBLIC_KEY` from `.env`, or  
- Set `LANGFUSE_TRACING_ENABLED=false` in `.env`,  
then restart the worker.

## References

- [Pipecat OpenTelemetry](https://docs.pipecat.ai/server/utilities/opentelemetry)
- [Langfuse OTLP](https://langfuse.com/docs/opentelemetry/get-started)
- [Pipecat Langfuse example](https://github.com/pipecat-ai/pipecat-examples/tree/main/open-telemetry/langfuse)
