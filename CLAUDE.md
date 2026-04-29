# CLAUDE.md

## What This Is
Invorto AI - A distributed voice bot platform handling phone calls with AI. Uses Pipecat for real-time voice processing with Deepgram (STT), OpenAI (LLM), and ElevenLabs (TTS). Supports Twilio and Jambonz telephony providers.

## Architecture
Two-process model:
- **Runner** (`app/main.py`, port 7860): FastAPI service managing assistants, phone numbers, call routing, and worker pool coordination
- **Worker** (`app/worker/main.py`, port 8765): Handles actual voice calls via WebSocket, runs Pipecat pipeline

Runner discovers workers via EC2 tags (production), Kubernetes API (EKS), or `WORKER_HOSTS` env var (local). Set `WORKER_POOL_TYPE=ec2|local|k8s`. See `app/services/worker_pool/` for pool logic and `app/services/worker_pool/WORKER_POOL.md` for the full reference (discovery flow, assignment, race conditions, Redis state, fallbacks, drain safety, all env vars).

## Key Commands
```bash
make dev          # Create venv and install deps
make migrate      # Run database migrations
make runner       # Start runner (terminal 1)
make worker       # Start worker (terminal 2)
make ngrok        # Expose runner for webhooks (terminal 3)
make lint         # Run ruff linter
make format       # Format code with ruff
```

## Project Structure
- `app/routes/` - API endpoints (assistants, calls, phone_numbers, twilio, jambonz, workers)
- `app/services/` - Business logic (assistant, call, phone_number, worker_pool)
- `app/worker/` - Voice call processing (pipeline.py is the core)
- `app/worker/jambonz/` - Jambonz-specific transport and serializer
- `migrations/` - SQL migrations (run with `make migrate`)
- `terraform/` - AWS infrastructure

## Database
PostgreSQL with three main tables: `assistants`, `phone_numbers`, `calls`. Use `app/core/database.py` `get_cursor()` context manager for queries.

## Key Conventions
- Phone numbers in E.164 format (`+1234567890`)
- Environment: `ENVIRONMENT=local` for dev, auto-detects production
- Webhook routes (`/twilio/*`, `/jambonz/*`) have NO authentication
- Protected routes require `X-API-Key` header (set via `API_KEY` env var)

## Guardrails
- **Twilio vs Jambonz**: Different WebSocket endpoints (`/ws` for Twilio, `/ws/jambonz` for Jambonz) and serializers. Don't mix them.
- **Audio encoding**: Twilio uses mulaw @ 8kHz, Jambonz uses linear16 @ 8kHz. Check `app/worker/pipeline.py` for differences.
- **Worker state**: `app/worker/state.py` tracks single call per worker. Workers are single-tenant during a call.
- **Pipecat version**: Pinned to `0.0.99` in requirements.worker.txt (upgraded from 0.0.82 for Smart Turn v3 support). Don't upgrade without testing.
- **Database connections**: Always use `get_cursor()` context manager, never raw connections.

## Environment Variables
Critical ones (see `app/config.py` for full list):
- `DATABASE_URL` - PostgreSQL connection string
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` - AI service keys
- `PUBLIC_URL` - Public URL for webhooks (ngrok URL in dev)
- `WORKER_HOSTS` - Comma-separated worker addresses (local dev only)
- `API_KEY` - Authentication for protected routes
