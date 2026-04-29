# Invorto AI - Local Development Guide

Complete guide for setting up and running the Invorto AI Voice Bot Platform locally.

## Table of Contents
- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Initial Setup](#initial-setup)
- [Database Setup](#database-setup)
- [Environment Configuration](#environment-configuration)
- [Running the Application](#running-the-application)
- [Development Workflow](#development-workflow)
- [Testing with Ngrok](#testing-with-ngrok)
- [API Documentation](#api-documentation)
- [Troubleshooting](#troubleshooting)

---

## Architecture Overview

Invorto AI is a distributed voice bot platform with two main components:

### Bot Runner (Port 7860)
- Central coordinator service that's always running
- Manages warm worker pool (discovery, health checks, assignment)
- Receives incoming call webhooks from Twilio and Jambonz
- Routes calls to available workers
- Provides REST API for CRUD operations on assistants, phone numbers, and calls
- Serves API documentation at `/docs`

### Bot Worker (Port 8765)
- Pre-warmed worker instance that handles individual calls
- Processes AI voice conversations using Pipecat framework
- Runs speech-to-text (Deepgram), LLM (OpenAI), and text-to-speech (ElevenLabs) pipelines
- Accepts WebSocket connections from Twilio Media Streams
- Reports health and availability to bot runner
- Handles one call at a time per worker instance

### Technology Stack
- **Backend**: FastAPI, Uvicorn (ASGI), Python 3.11
- **AI/Voice**: Pipecat framework, PyTorch, Silero VAD
- **Speech Processing**: Deepgram (STT), OpenAI (LLM), ElevenLabs (TTS)
- **Telephony**: Twilio SDK, Jambonz
- **Database**: PostgreSQL with connection pooling
- **Audio**: pydub, ffmpeg
- **Logging**: Loguru, CloudWatch (production)
- **Containerization**: Docker, Docker Compose

---

## Prerequisites

### Required Software

1. **Python 3.11+**
   ```bash
   python3.11 --version
   ```

2. **Supabase project** (replaces local PostgreSQL)

   This service shares a Supabase-hosted Postgres database with `invorto-ui`.
   You do **not** need a locally installed PostgreSQL server.
   - Create a project at https://supabase.com if you don't have one
   - Install the Supabase CLI for running migrations: `brew install supabase/tap/supabase`
   - `psql` client is optional but useful for debugging: `brew install libpq`

3. **Docker & Docker Compose** (Optional but recommended)
   ```bash
   # macOS
   brew install --cask docker

   # Ubuntu/Debian
   sudo apt-get install docker.io docker-compose

   # Verify installation
   docker --version
   docker-compose --version
   ```

4. **FFmpeg** (Required for audio processing)
   ```bash
   # macOS
   brew install ffmpeg

   # Ubuntu/Debian
   sudo apt-get install ffmpeg

   # Verify installation
   ffmpeg -version
   ```

5. **Git**
   ```bash
   git --version
   ```

6. **Ngrok** (For testing Twilio webhooks locally)
   ```bash
   # macOS
   brew install ngrok

   # Ubuntu/Debian
   curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | \
     sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && \
     echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | \
     sudo tee /etc/apt/sources.list.d/ngrok.list && \
     sudo apt update && sudo apt install ngrok

   # Sign up at https://ngrok.com and get auth token
   ngrok config add-authtoken <your-token>
   ```

### API Keys Required

1. **OpenAI API Key** - https://platform.openai.com/api-keys
2. **Deepgram API Key** - https://console.deepgram.com/
3. **ElevenLabs API Key** - https://elevenlabs.io/
4. **ElevenLabs Voice ID** - From ElevenLabs voice library
5. **Twilio Account** - https://www.twilio.com/
6. **Jambonz Account** - https://jambonz.cloud/

---

## Initial Setup

### 1. Clone the Repository (with submodule)

```bash
# Recommended — clones repo and hydrates the invorto-db submodule in one step
git clone --recurse-submodules git@bitbucket.org:leadsquaredengg/invorto-voice-ai.git
cd invorto-voice-ai

# If you already cloned without --recurse-submodules, run this to hydrate db/
git submodule update --init
```

### 2. Set up the Supabase project

This service shares a Postgres database with `invorto-ui` via Supabase.
There is no separate local database to create.

1. Create (or reuse) a Supabase project at https://supabase.com
2. Copy the **Direct connection** string from Supabase Dashboard → Project Settings → Database → URI (port 5432)
3. Paste it as `DATABASE_URL` in this repo's `.env`
4. Set `SUPABASE_PROJECT_REF` to your project ref (e.g. `jcazvdqmxlzpdwgzlyph`)

See the [Database Setup](#database-setup) section for applying migrations.

### 3. Install Python Dependencies

#### Option A: Using Virtual Environment (Recommended)

```bash
# Create virtual environment
python3.11 -m venv .venv

# Activate virtual environment
# On macOS/Linux:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate

# Install dependencies (both runner + worker)
pip install --upgrade pip
pip install -r requirements.runner.txt -r requirements.worker.txt
```

#### Option B: Using Make (Convenience)

```bash
make dev
source .venv/bin/activate
```

#### Option C: System-wide Installation

```bash
pip install --upgrade pip
pip install -r requirements.runner.txt -r requirements.worker.txt
```

---

## Database Setup

All migrations are managed in the **`invorto-db`** shared repo, included here
as a git submodule at `db/`. This service no longer owns any migration files.

### Step 1 — Ensure submodule is hydrated

```bash
git submodule update --init
# db/supabase/migrations/ is now populated
```

### Step 2 — Point DATABASE_URL at Supabase Postgres

```bash
# In .env — use direct connection (port 5432), not the pooler (port 6543)
DATABASE_URL=postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres
SUPABASE_PROJECT_REF=jcazvdqmxlzpdwgzlyph
```

### Step 3 — Apply all migrations (one command)

```bash
make migrate          # runs: supabase db push from db/ submodule

make migrate-status   # show applied / pending
make migrate-dry-run  # print SQL without applying
```

### Schema ownership

All tables are defined in `invorto-db/supabase/migrations/` in a single
ordered sequence. The circular FK dependency between org and voice-AI tables
is resolved by ordering — `organizations` is created before `assistants`,
so the cross-FKs in `0011` and `0012` are always safe to apply.

See [invorto-db README](https://bitbucket.org/leadsquaredengg/invorto-db) for
the full migration list and schema change workflow.

---

## Environment Configuration

### 1. Create Environment File

```bash
cp .env.example .env
```

### 2. Configure Environment Variables

Edit `.env` with your configuration:

```bash
# Public URLs (set after ngrok setup)
PUBLIC_URL=https://your-ngrok-url.ngrok-free.app
WORKER_HOSTS=your-worker-ngrok-url.ngrok-free.dev
PUBLIC_WS_URL=your-worker-ngrok-url.ngrok-free.dev

# Authentication (optional - leave empty to disable)
API_KEY=

# Database — Supabase Postgres direct connection (port 5432, not pooler 6543)
# Get from: Supabase Dashboard → Project Settings → Database → URI
DATABASE_URL=postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres
SUPABASE_PROJECT_REF=jcazvdqmxlzpdwgzlyph

# API Keys (required)
OPENAI_API_KEY=sk-...
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_VOICE_ID=...
DEEPGRAM_API_KEY=...

# Jambonz Configuration (optional)
JAMBONZ_API_URL=https://jambonz.cloud/api
JAMBONZ_ACCOUNT_SID=
JAMBONZ_API_KEY=
JAMBONZ_APPLICATION_SID=

# Langfuse tracing (optional – see docs/LANGFUSE_TRACING.md)
# LANGFUSE_PUBLIC_KEY=pk-lf-...
# LANGFUSE_SECRET_KEY=sk-lf-...
```

### 3. Configure Ngrok (for webhook testing)

Edit `ngrok.yml`:

```yaml
version: "2"
authtoken: YOUR_NGROK_TOKEN
tunnels:
  bot-runner:
    proto: http
    addr: 7860
    domain: your-custom-domain.ngrok-free.app  # Optional: use reserved domain
    inspect: true
  bot-worker:
    proto: http
    addr: 8765
    inspect: true
```

---

## Running the Application

### Option 1: Docker Compose (Recommended for Testing)

```bash
# Build and start services
docker-compose up -d

# View logs
docker-compose logs -f

# Check service health
curl http://localhost:7860/health
curl http://localhost:8765/health

# Stop services
docker-compose down

# Rebuild after code changes
docker-compose down
docker-compose build
docker-compose up -d
```

**Services:**
- Bot Runner: http://localhost:7860
- Bot Worker: http://localhost:8765
- API Docs: http://localhost:7860/docs

### Option 2: Direct Python Execution (Recommended for Development)

#### Terminal 1: Start Bot Runner

```bash
# Activate virtual environment
source .venv/bin/activate

# Start runner
make runner
# Or: python app/run_runner.py
# Or: ENVIRONMENT=local python app/run_runner.py

# With hot reload (auto-restart on code changes)
make runner-reload
```

#### Terminal 2: Start Bot Worker

```bash
# Activate virtual environment
source .venv/bin/activate

# Start worker
make worker
# Or: python app/run_worker.py
# Or: ENVIRONMENT=local python app/run_worker.py

# With hot reload
make worker-reload
```

#### Terminal 3: Start Ngrok (for webhook testing)

```bash
make ngrok
# Or: ngrok start --config ngrok.yml --all
```

**After ngrok starts:**
1. Copy the runner ngrok URL (e.g., `https://abc123.ngrok-free.app`)
2. Copy the worker ngrok URL (e.g., `https://xyz789.ngrok-free.app`)
3. Update `.env` file:
   ```bash
   PUBLIC_URL=https://abc123.ngrok-free.app
   PUBLIC_WS_URL=wss://xyz789.ngrok-free.app
   ```
4. Restart the runner service to pick up new URLs

### Verify Services

```bash
# Check health
make health

# Or manually:
curl http://localhost:7860/health
curl http://localhost:8765/health

# Check worker pool status
make workers
# Or: curl http://localhost:7860/workers | python -m json.tool
```

---

## Development Workflow

### Project Structure

```
callWorker/
├── app/
│   ├── main.py                   # Runner FastAPI app
│   ├── run_runner.py             # Runner entry point
│   ├── run_worker.py             # Worker entry point
│   ├── config.py                 # Configuration
│   ├── core/
│   │   ├── database.py           # PostgreSQL connection pool
│   │   ├── auth.py               # API key validation
│   │   └── cloudwatch.py         # CloudWatch logging
│   ├── models/
│   │   └── schemas.py            # Pydantic models
│   ├── routes/
│   │   ├── twilio.py             # Twilio webhooks
│   │   ├── jambonz.py            # Jambonz webhooks
│   │   ├── calls.py              # Call management API
│   │   ├── assistants.py         # Assistant management API
│   │   ├── phone_numbers.py      # Phone number management API
│   │   └── workers.py            # Worker pool API
│   ├── services/
│   │   ├── call.py               # Call database operations
│   │   ├── assistant.py          # Assistant database operations
│   │   ├── phone_number.py       # Phone number database operations
│   │   └── worker_pool.py        # Worker discovery and management
│   └── worker/
│       ├── main.py               # Worker FastAPI app
│       ├── pipeline.py           # Voice pipeline construction (Pipecat)
│       ├── config.py             # Assistant configuration
│       ├── state.py              # Call state management
│       ├── services.py           # TTS/STT service factories
│       └── jambonz/              # Jambonz-specific adapters
├── migrations/
│   ├── migrate.py                # Migration runner
│   └── *.sql                     # SQL migration files
├── terraform/                    # AWS infrastructure
├── scripts/
│   └── push_to_ecr.sh            # ECR deployment script
├── requirements.runner.txt       # Runner dependencies
├── requirements.worker.txt       # Worker dependencies
├── Dockerfile.runner             # Runner container
├── Dockerfile.worker             # Worker container
├── docker-compose.yml            # Local orchestration
├── Makefile                      # Development commands
└── .env                          # Environment variables
```

### Common Development Tasks

#### 1. Create a New Assistant

```bash
curl -X POST http://localhost:7860/api/assistants \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sales Assistant",
    "system_prompt": "You are a sales assistant helping customers...",
    "model": "gpt-4o-mini",
    "voice_provider": "elevenlabs",
    "greeting_message": "Hi! I can help you with sales inquiries."
  }'
```

#### 2. Register a Phone Number

```bash
curl -X POST http://localhost:7860/api/phone-numbers \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+1234567890",
    "assistant_id": "<assistant-uuid>",
    "provider": "twilio",
    "twilio_account_sid": "AC...",
    "twilio_auth_token": "...",
    "twilio_sid": "PN..."
  }'
```

#### 3. View Call History

```bash
# List all calls
curl http://localhost:7860/api/calls

# Filter calls
curl "http://localhost:7860/api/calls?status=completed&limit=10"
```

#### 4. Make an Outbound Call

```bash
curl -X POST http://localhost:7860/call/outbound \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+1234567890",
    "phone_number_id": "<phone-number-uuid>",
    "assistant_id": "<assistant-uuid>"
  }'
```

### Code Quality

#### Linting

```bash
# Check code style
make lint
# Or: ruff check app/

# Auto-fix issues
ruff check --fix app/
```

#### Formatting

```bash
# Format code
make format
# Or: ruff format app/
```

> These checks are enforced automatically in CI (Bitbucket Pipelines) on every push. Run them locally before pushing to avoid pipeline failures.

#### Type Checking (optional locally)

```bash
pip install mypy types-requests
mypy app/ --ignore-missing-imports --no-strict-optional --exclude app/worker/
```

#### Security Scan (optional locally)

```bash
pip install bandit
bandit -r app/ -ll
```

#### Clean Cache

```bash
make clean
```

---

## Testing with Ngrok

### Setup Twilio Webhook

1. Start ngrok and get your PUBLIC_URL
2. Login to Twilio Console
3. Go to Phone Numbers → Active Numbers
4. Select your phone number
5. Under "Voice Configuration":
   - **A Call Comes In**: `{PUBLIC_URL}/twilio/incoming` (HTTP POST)
   - **Call Status Changes**: `{PUBLIC_URL}/twilio/status` (HTTP POST)
6. Save changes

### Test Inbound Call

1. Call your Twilio phone number
2. You should hear the greeting message
3. Speak to test the AI conversation
4. Check logs in terminal for debugging

### Monitor Calls

```bash
# Watch runner logs
docker-compose logs -f runner
# Or: tail -f logs in Terminal 1

# Watch worker logs
docker-compose logs -f worker
# Or: tail -f logs in Terminal 2

# View ngrok requests
# Open http://localhost:4040 in browser
```

---

## API Documentation

### Interactive API Docs

Once the runner is running, visit:

- **Swagger UI**: http://localhost:7860/docs
- **ReDoc**: http://localhost:7860/redoc

### Key Endpoints

#### Health & Status
- `GET /health` - Service health check
- `GET /workers` - List worker pool status
- `POST /workers/refresh` - Force worker pool refresh

#### Assistants
- `GET /api/assistants` - List assistants
- `POST /api/assistants` - Create assistant
- `GET /api/assistants/{id}` - Get assistant
- `PATCH /api/assistants/{id}` - Update assistant
- `DELETE /api/assistants/{id}` - Delete assistant

#### Phone Numbers
- `GET /api/phone-numbers` - List phone numbers
- `POST /api/phone-numbers` - Register phone number
- `GET /api/phone-numbers/{id}` - Get phone number
- `PATCH /api/phone-numbers/{id}` - Update phone number
- `DELETE /api/phone-numbers/{id}` - Delete phone number

#### Calls
- `GET /api/calls` - List calls (with filters)
- `GET /api/calls/{id}` - Get call details
- `POST /call/outbound` - Initiate outbound call

#### Webhooks (for Twilio/Jambonz)
- `POST /twilio/incoming` - Incoming call webhook
- `POST /twilio/status` - Call status updates
- `POST /jambonz/call` - Jambonz call webhook (inbound and outbound)
- `POST /jambonz/status` - Jambonz status updates

---

## Next Steps

After successfully running the application locally:

1. **Test thoroughly** - Create assistants, register phone numbers, make test calls
2. **Setup Jambonz** - See JAMBONZ_SETUP.md for complete Jambonz SIP integration guide
3. **Review logs** - Understand the call flow and debugging information
4. **Customize prompts** - Experiment with different system prompts and models
5. **Review production deployment** - See PRODUCTION_DEPLOYMENT.md for AWS deployment

---

## Additional Resources

### Internal Documentation
- **Jambonz Setup Guide**: See JAMBONZ_SETUP.md for complete SIP integration
- **Production Deployment**: See PRODUCTION_DEPLOYMENT.md for AWS deployment guide
- **Langfuse Tracing**: See LANGFUSE_TRACING.md for optional observability and testing steps

### External Documentation
- **Pipecat Documentation**: https://github.com/pipecat-ai/pipecat
- **FastAPI Documentation**: https://fastapi.tiangolo.com/
- **Twilio Media Streams**: https://www.twilio.com/docs/voice/media-streams
- **Jambonz Documentation**: https://docs.jambonz.org/
- **Deepgram API**: https://developers.deepgram.com/
- **ElevenLabs API**: https://docs.elevenlabs.io/
- **OpenAI API**: https://platform.openai.com/docs/
