# Invorto AI Voice Bot Platform

A distributed AI-powered voice bot platform for handling intelligent phone conversations. Built with FastAPI and Pipecat, the system coordinates warm worker pools that process real-time voice conversations using Deepgram (STT), OpenAI (LLM), and ElevenLabs (TTS). Supports Twilio, Jambonz, and MCube telephony providers with PostgreSQL for state management.

---

## Documentation

- [Local Development Guide](docs/LOCAL_DEVELOPMENT.md)
- [Production Deployment Guide](docs/PRODUCTION_DEPLOYMENT.md)
- [Jambonz Setup Guide](docs/JAMBONZ_SETUP.md)
- [STT/TTS Configuration](docs/STT_TTS_CONFIGURATION.md)
- [Langfuse Tracing](docs/LANGFUSE_TRACING.md)
- [CI/CD Pipeline](bitbucket-pipelines.yml)

---

## Quick Start

```bash
make dev       # Create venv, install all dependencies
make migrate   # Run PostgreSQL migrations

make runner    # Terminal 1 — start runner on port 7860
make worker    # Terminal 2 — start worker on port 8765
make ngrok     # Terminal 3 — expose runner for webhooks
```

API docs: http://localhost:7860/docs

---

## Architecture

### Two-Process Model

```
                        ┌─────────────────────────────────────┐
Telephony Provider ────▶│  Runner  (port 7860 / FastAPI)      │
  (Twilio / Jambonz /   │                                     │
   MCube webhook)       │  • Manages assistants, phone nums   │
                        │  • Routes calls to workers          │
                        │  • Worker pool health checks        │
                        └───────────┬─────────────────────────┘
                                    │ HTTP (health, prewarm, cancel)
                                    ▼
                        ┌─────────────────────────────────────┐
Telephony Provider ────▶│  Worker  (port 8765 / FastAPI)      │
  (WebSocket audio)     │                                     │
                        │  • Pipecat STT → LLM → TTS pipeline │
                        │  • Single-tenant (one call at once) │
                        │  • /ws (Twilio), /ws/jambonz,       │
                        │    /ws/mcube/{call_sid}             │
                        └─────────────────────────────────────┘
```

### Component Roles

| Component | Location | Responsibility |
|-----------|----------|----------------|
| **Runner** | `app/main.py` | FastAPI app, webhook handlers, worker pool, DB CRUD |
| **Worker** | `app/worker/main.py` | WebSocket audio handling, Pipecat pipeline lifecycle |
| **Worker Pool** | `app/services/worker_pool.py` | Worker discovery, health checks, atomic assignment |
| **Pipeline** | `app/worker/pipeline.py` | Pipecat STT→LLM→TTS pipeline construction |
| **AssistantConfig** | `app/worker/config.py` | Merges DB config + WebSocket custom params |

---

## Technology Stack

| Layer | Technology | Version / Notes |
|-------|------------|-----------------|
| **Web framework** | FastAPI | Async, ASGI |
| **Voice pipeline** | Pipecat | Pinned to `0.0.99` (Smart Turn v3) |
| **STT** | Deepgram | `nova-2` model, mulaw/linear16 @ 8kHz |
| **LLM** | OpenAI | `gpt-4o-mini` default |
| **TTS** | ElevenLabs | `eleven_flash_v2_5` default |
| **VAD** | Silero | Configurable confidence/stop_secs |
| **Database** | PostgreSQL | psycopg2, ThreadedConnectionPool |
| **Telephony** | Twilio / Jambonz / MCube | WebSocket audio streaming |
| **Infra** | AWS EC2 / ECS | EC2 tag-based worker discovery |
| **Tracing** | Langfuse | Optional, toggled via env var |

---

## Project Structure

```
app/
├── main.py                    # FastAPI runner app, lifespan, router mounting
├── config.py                  # All environment variable bindings
├── core/
│   ├── auth.py                # X-API-Key verification dependency
│   ├── database.py            # ThreadedConnectionPool, get_cursor() context manager
│   └── cloudwatch.py          # CloudWatch log handler (production only)
├── middleware/
│   └── request_context.py     # Request-scoped context middleware
├── models/
│   └── schemas.py             # All Pydantic request/response models
├── routes/
│   ├── assistants.py          # POST/GET/PATCH/DELETE /assistants
│   ├── calls.py               # GET /calls, POST /call/outbound
│   ├── phone_numbers.py       # POST/GET/PATCH/DELETE /phone-numbers
│   ├── twilio.py              # POST /twilio/incoming, /twilio/status
│   ├── jambonz.py             # POST /jambonz/call, /jambonz/status
│   ├── mcube.py               # POST /mcube/call (single-URL, status + connect)
│   └── workers.py             # GET /workers, POST /workers/refresh, /workers/{id}/release
├── services/
│   ├── assistant.py           # Assistant CRUD (DB layer)
│   ├── call.py                # Call CRUD + status transitions (DB layer)
│   ├── phone_number.py        # Phone number CRUD (DB layer)
│   ├── worker_pool.py         # WorkerStatus, LocalWorkerPool, EC2WorkerPool
│   └── outbound/
│       ├── base.py            # OutboundProvider ABC + OutboundCallResult
│       ├── twilio.py          # Twilio REST call initiation + TwiML builder
│       ├── jambonz.py         # Jambonz REST call initiation
│       ├── mcube.py           # MCube REST call initiation
│       └── registry.py        # get_provider(name) factory
└── worker/
    ├── main.py                # WebSocket endpoints (/ws, /ws/jambonz, /ws/mcube/{sid})
    ├── config.py              # AssistantConfig — merges DB + custom params
    ├── state.py               # WorkerState — single-tenant call lifecycle
    ├── pipeline.py            # Pipecat pipeline, EndCallProcessor, is_audio_url()
    ├── services.py            # STT/TTS service factory functions
    ├── prewarm.py             # Pre-warms STT/TTS connections before call arrives
    ├── pipecat_tracing.py     # Langfuse trace integration
    ├── jambonz/               # Jambonz WebSocket transport + frame serializer
    ├── mcube/                 # MCube WebSocket transport + frame serializer
    └── providers/             # Per-provider WebSocket setup (Twilio, Jambonz, MCube)
```

---

## Database Schema

### `assistants`
Core AI configuration: `system_prompt`, `model`, `temperature`, `max_tokens`, `voice_*`, `transcriber_*`, `vad_settings`, `end_call_phrases`, `greeting_message`.

### `phone_numbers`
Provider configuration: `phone_number` (E.164), `provider` (twilio/jambonz/mcube), `provider_credentials` (JSONB), `assistant_id` (FK), `max_call_duration_seconds`.

### `calls`
Full call lifecycle: `call_sid`, `direction`, `from_number`, `to_number`, `status`, `worker_instance_id`, `worker_host`, `custom_params` (JSONB), `transcript` (JSONB array), `summary`, `provider_metadata` (JSONB).

---

## Webhook Flow Overview

### Inbound Call (all providers)
1. Provider sends webhook → Runner assigns worker → Returns response with WebSocket URL
2. Provider opens WebSocket → Worker runs Pipecat pipeline
3. Call ends → Provider sends status webhook → Runner releases worker

### Outbound Call
1. `POST /call/outbound` → Validate + assign worker → Call provider API
2. Provider calls back via webhook (Jambonz/MCube only) → Worker serves WebSocket
3. Call ends → Status webhook → Runner releases worker

### Provider Differences

| Aspect | Twilio | Jambonz | MCube |
|--------|--------|---------|-------|
| Incoming webhook format | Form data | JSON (camelCase) | JSON (camelCase) |
| Inbound response format | TwiML XML | JSON verb list | JSON `{wss_url}` |
| Worker WS endpoint | `/ws` | `/ws/jambonz` | `/ws/mcube/{call_id}` |
| Audio encoding | mulaw @ 8kHz | linear16 @ 8kHz | configurable |
| Status webhook | Separate `/twilio/status` | Separate `/jambonz/status` | Same `/mcube/call` URL |

---

## Key Design Patterns

### 1. Atomic Worker Assignment (TOCTOU-safe)
`get_and_assign_worker(call_sid)` discovers an available worker and assigns it under a single asyncio lock — eliminating race conditions when multiple concurrent calls arrive.

### 2. Single-Tenant Workers
`WorkerState` (in the worker process) allows only one call at a time. The runner enforces this via the worker pool — a worker with `current_call_sid != None` is never selected for new assignments.

### 3. Provider Abstraction
`OutboundProvider` ABC + registry pattern lets the codebase add new telephony providers without touching route logic. `get_provider("mcube")` returns the correct implementation.

### 4. Config Resolution Priority (Worker)
`AssistantConfig` merges data in priority order:
1. Database `assistants` table (canonical)
2. Database `phone_numbers.provider_credentials`
3. `custom_params` from WebSocket metadata (runtime overrides)

### 5. Database Access Pattern
Always use `get_cursor()` context manager. It handles commit on success and rollback on failure — never use raw connections.

### 6. Phone Number Lookup
Stored and looked up in **E.164 format** (`+1234567890`). MCube sends full international numbers; the route strips to the last 10 digits before lookup (see `_determine_call_identifiers()` in `app/routes/mcube.py`).

### 7. Worker URL Construction (`WorkerStatus.get_ws_url()`)
URL priority chain:
1. `PUBLIC_WS_URL` env override (full `wss://` or bare hostname)
2. `{public_ip}{WORKER_PUBLIC_WS_HOST_SUFFIX}` (e.g., `54.1.2.3.sslip.io`)
3. `WORKER_PUBLIC_WS_HOST_TEMPLATE` with `{public_ip}`, `{private_ip}`, `{instance_id}`
4. `{private_ip}:{WORKER_PORT}` fallback
5. `{host}` final fallback (local dev)

---

## Environment Variables

```bash
# Core
ENVIRONMENT=local|production
DATABASE_URL=postgresql://user:pass@host:5432/db
PUBLIC_URL=https://example.com          # webhook base URL (ngrok in dev)
API_KEY=secret-key                      # empty = auth disabled

# Worker discovery (choose one)
WORKER_HOSTS=localhost:8765             # local dev: static list
AWS_REGION=ap-south-1                   # prod: EC2 discovery
WORKER_POOL_TAG=invorto-ai-worker       # EC2 tag value for workers

# Worker pool tuning
WORKER_TIMEOUT=10                       # health check timeout (seconds)
HEALTH_CHECK_INTERVAL=30                # health check frequency (seconds)
WORKER_STALE_ASSIGNMENT_SECONDS=3600    # force-release after this duration

# WebSocket URL construction (prod)
PUBLIC_WS_URL=wss://example.com         # overrides all other URL logic
WORKER_PUBLIC_WS_SCHEME=wss
WORKER_PUBLIC_WS_PORT=443
WORKER_PUBLIC_WS_HOST_SUFFIX=.sslip.io
WORKER_PUBLIC_WS_HOST_TEMPLATE=         # e.g. {public_ip}.mycompany.com

# AI services
OPENAI_API_KEY=sk-...
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...

# Telephony providers
JAMBONZ_API_URL=https://jambonz.cloud/api
JAMBONZ_ACCOUNT_SID=...
JAMBONZ_API_KEY=...
JAMBONZ_APPLICATION_SID=...
MCUBE_API_URL=https://config.mcube.com/Restmcube-api
MCUBE_AUTH_TOKEN=...

# Features
ENABLE_WORKER_PREWARM=true
LANGFUSE_TRACING_ENABLED=true
LANGFUSE_SECRET_KEY=...
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

---

## Test Suite

### Structure

```
tests/
├── conftest.py                    # Shared fixtures: Postgres container, table cleanup, HTTP client
├── unit/                          # Pure unit tests — no DB, no HTTP, fast
│   ├── test_auth.py               # API key verification logic
│   ├── test_schemas.py            # Pydantic model validation + camelCase mapping
│   ├── test_jambonz_schemas.py    # Extended Jambonz schema tests
│   ├── test_assistant_config.py   # AssistantConfig field loading + system message generation
│   ├── test_pipeline_utils.py     # is_audio_url(), EndCallProcessor frame processing
│   ├── test_mcube_helpers.py      # MCube route helper functions
│   ├── test_outbound_registry.py  # Provider registry + credential validation
│   ├── test_twilio_twiml.py       # TwiML XML construction
│   ├── test_worker_status_unit.py # WorkerStatus URL resolution logic
│   └── test_worker_state_unit.py  # WorkerState call lifecycle
└── integration/
    └── runner/                    # Integration tests against a real Postgres container
        ├── test_assistants.py     # /assistants CRUD endpoints
        ├── test_phone_numbers.py  # /phone-numbers CRUD endpoints
        ├── test_calls.py          # /calls list + /calls/{id} GET endpoints
        ├── test_call_service.py   # call_service direct CRUD + status transitions
        ├── test_twilio_webhook.py # /twilio/incoming and /twilio/status flows
        ├── test_jambonz_webhook.py# /jambonz/call and /jambonz/status flows
        ├── test_mcube_webhook.py  # /mcube/call CONNECTING + hangup flows
        ├── test_workers_endpoint.py # /workers pool management endpoints
        └── test_outbound_call.py  # POST /call/outbound validation + happy path
    └── worker/
        └── test_worker_state.py   # WorkerPool assignment, release, health demotion
```

### Running Tests

```bash
make test            # All tests (requires Docker for Postgres testcontainer)
make test-unit       # Unit tests only — no Docker needed, fast (~10s)
make test-cov        # Coverage report to terminal
make test-report     # HTML coverage report (opens in browser)
make test-full       # All reports + JUnit XML (for CI)

# Run a specific test file
pytest tests/unit/test_auth.py -v

# Run a specific test class
pytest tests/integration/runner/test_calls.py::TestListCallsEndpoint -v

# Run with extra output on failures
pytest -v --tb=short

# Run only tests matching a keyword
pytest -k "twilio" -v
```

### Test Infrastructure

#### Postgres Testcontainer
All integration tests use `testcontainers` to spin up a real Postgres 15 container per session. This ensures:
- Tests exercise actual SQL migrations
- No mocking of database behavior
- Full isolation from any local or production database

#### Table Cleanup
The `clean_tables` fixture (autouse, function-scoped) runs `TRUNCATE ... RESTART IDENTITY CASCADE` before every test. Each test starts with a clean database.

#### Worker Pool Isolation
The `worker_in_pool` fixture registers a fake `WorkerStatus` in the global `worker_pool` dict and removes it after the test. Tests that need workers should use this fixture.

#### No ASGI Lifespan
`runner_client` uses `ASGITransport` without starting the ASGI lifespan. This means:
- Worker pool health-check background tasks do not run
- EC2 discovery does not run
- DB pool is initialized lazily on first request

#### Environment Patching
`conftest.py` sets env vars **before any app module is imported**. This is critical — `load_dotenv(override=False)` in `app/config.py` won't overwrite vars already in `os.environ`. Key overrides:
- `API_KEY=""` — disables authentication
- `DATABASE_URL` — points to testcontainer DSN
- `WORKER_HOSTS=localhost:8765` — prevents EC2 discovery
- `ENABLE_WORKER_PREWARM=false` — disables prewarm HTTP calls

### Writing New Tests

#### Unit Test Checklist
- Put in `tests/unit/`
- No fixtures that require `pg_container` or `runner_client`
- Use `unittest.mock.patch` to isolate from env vars, network, DB
- Import the module under test directly
- One class per logical group, one method per scenario

#### Integration Test Checklist
- Put in `tests/integration/runner/` (for runner) or `tests/integration/worker/` (for worker)
- Tests that need the DB must depend on `pg_container` (or `runner_client` which depends on it)
- Tests that need an HTTP client use `runner_client` fixture
- Tests that need a worker use `worker_in_pool` fixture
- Seed DB state via HTTP endpoints (preferred) or directly via `call_service.create()` etc.
- Clean-up is handled by `clean_tables` autouse fixture — no manual teardown needed

#### Telephony Webhook Tests
- **Twilio**: Send `data={}` (form-encoded), not `json={}`. Match Twilio field names: `CallSid`, `From`, `To`, `CallStatus`, `CallDuration`.
- **Jambonz**: Send `json={}` with camelCase fields: `callSid`, `from`, `to`, `callStatus`.
- **MCube**: Send `json={}` with camelCase fields: `callId`, `dialStatus`, `fromNumber`, `toNumber`.

#### Mocking External Providers
When testing outbound call flows, mock `provider.initiate()` to avoid hitting real Twilio/Jambonz/MCube APIs:
```python
from unittest.mock import AsyncMock, patch
from app.services.outbound.base import OutboundCallResult

with patch(
    "app.services.outbound.twilio.TwilioOutboundProvider.initiate",
    new=AsyncMock(return_value=OutboundCallResult(call_sid="CA-MOCK", from_number="+15005550006")),
):
    resp = await runner_client.post("/call/outbound", json={...})
```

#### Testing Worker Pool Behavior
Tests that check worker assignment or release should use the `worker_in_pool` fixture, which registers a real `WorkerStatus` in the global pool:
```python
async def test_worker_assigned(self, runner_client, worker_in_pool):
    # ... make HTTP call that triggers worker assignment ...
    assert worker_in_pool.current_call_sid == "expected-call-sid"
    assert worker_in_pool.is_available is False
```

---

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| Phone number lookup fails | Use E.164 format (`+1234567890`), including `+` prefix |
| Twilio audio garbled | Check encoding: must be **mulaw @ 8kHz** for Twilio |
| Jambonz audio garbled | Check encoding: must be **linear16 @ 8kHz** for Jambonz |
| Worker never assigned | `consecutive_failures >= 3` demotes workers — reset or fix health endpoint |
| Pipecat upgrade breaks pipeline | Pinned to `0.0.99` — test Smart Turn v3 before upgrading |
| DB query returns wrong row | All service functions use `RETURNING *` — check column names match schema |
| Outbound worker not found | Jambonz/MCube: worker is pre-assigned by call_id, not call_sid — check `reassign_call_sid()` |
| Tests fail on DB not ready | Ensure test depends on `pg_container` (directly or via `runner_client`) |

---

## Code Style

```bash
make lint     # Check with ruff
make format   # Auto-fix with ruff
```

PEP 8, type hints encouraged but not mandatory for internal helpers. These checks run automatically in CI on every push — see [CI/CD Pipeline](#cicd-pipeline) below.

---

## CI/CD Pipeline

Automated quality checks run via Bitbucket Pipelines (`bitbucket-pipelines.yml`).

| Trigger | Steps |
|---------|-------|
| Every push | Lint & format (ruff), security scan (bandit), dependency audit (pip-audit), unit tests (≥40% coverage), type check (mypy) |
| Pull requests | All of the above + integration tests (≥30% coverage) |
| Merge to `main` | Full suite + Teams notification |

**Steps at a glance:**

| Step | Tool | What it catches |
|------|------|-----------------|
| Lint & Format | `ruff` | Style issues, formatting drift |
| Security Scan | `bandit` | Unsafe subprocess, insecure crypto, hardcoded secrets |
| Dependency Audit | `pip-audit` | CVEs in runner dependencies |
| Unit Tests | `pytest --cov` | Regressions, coverage gate |
| Type Check | `mypy` | Type errors in `app/` (excludes `app/worker/` — pipecat has no stubs) |
| Integration Tests | `pytest` + Postgres service | DB-backed flows end-to-end |

> **Note**: Worker dependencies (`torch`, `torchaudio`, pipecat) are not installed in CI due to size (~2GB). Build validation for the worker is commented out in the pipeline.
