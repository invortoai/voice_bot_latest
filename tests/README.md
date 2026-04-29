# Test Suite — Invorto Voice AI

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Directory Structure](#directory-structure)
- [Test Types](#test-types)
  - [Unit Tests](#unit-tests)
  - [Integration Tests — Runner](#integration-tests--runner)
  - [Integration Tests — Worker](#integration-tests--worker)
- [How to Run Tests](#how-to-run-tests)
- [Test Reports](#test-reports)
- [Infrastructure & Fixtures](#infrastructure--fixtures)
- [Known Gotchas](#known-gotchas)
- [Guidelines for Adding New Tests](#guidelines-for-adding-new-tests)

---

## Overview

The test suite covers two services:

| Service | Description |
|---------|-------------|
| **Runner** (`app/`) | FastAPI service — assistants, phone numbers, call routing, webhooks |
| **Worker** (`app/worker/`) | Voice pipeline — Pipecat STT→LLM→TTS, worker pool management |

**Total: ~350 tests** across 18 test files.

External AI service calls (Deepgram, ElevenLabs, OpenAI) are never made in tests. Telephony
provider APIs (Twilio, Jambonz, MCube) are also never called. The only real external dependency
is a **Postgres container** (started automatically via Docker for integration tests).

External AI service calls (Deepgram, ElevenLabs, OpenAI) are never made in tests. Telephony
provider APIs (Twilio, Jambonz, MCube) are also never called. The only real external dependency
is a **Postgres container** (started automatically via Docker for integration tests).

---

## Architecture

```
tests/
├── README.md                          ← this file
├── conftest.py                        ← shared fixtures for the whole suite
├── unit/                              ← pure Python tests, no Docker needed
│   ├── test_auth.py
│   ├── test_assistant_config.py
│   ├── test_jambonz_schemas.py
│   ├── test_mcube_helpers.py
│   ├── test_outbound_registry.py
│   ├── test_pipeline_utils.py
│   ├── test_schemas.py
│   ├── test_twilio_twiml.py
│   ├── test_worker_state_unit.py
│   └── test_worker_status_unit.py
└── integration/
    ├── runner/                        ← HTTP endpoint tests with real Postgres
    │   ├── test_assistants.py
    │   ├── test_call_service.py
    │   ├── test_calls.py
    │   ├── test_jambonz_webhook.py
    │   ├── test_mcube_webhook.py
    │   ├── test_outbound_call.py
    │   ├── test_phone_numbers.py
    │   ├── test_twilio_webhook.py
    │   └── test_workers_endpoint.py
    └── worker/                        ← worker pool & state machine tests
        └── test_worker_state.py
```

### Design Principles

- **Isolation** — each test starts with empty tables; the `clean_tables` auto use fixture
  runs `TRUNCATE … CASCADE` before every test.
- **No live AI/telephony calls** — all external service keys are set to dummy values in
  `pytest.ini`.
- **No lifespan side effects** — the FastAPI app is mounted via `ASGITransport` which does
  **not** trigger the ASGI lifespan. Worker health checks and EC2 discovery never run during
  tests.
- **Real database** — integration tests use a real Postgres 15 container started by
  `testcontainers`. All migrations are applied once at session start.

---

## Directory Structure

```
tests/
├── conftest.py                    # Session-scoped DB container, per-test cleanup,
│                                  # runner HTTP client, mock worker fixtures
├── unit/
│   ├── __init__.py
│   ├── test_auth.py               # verify_api_key() — auth enabled/disabled, 401/403
│   ├── test_assistant_config.py   # AssistantConfig: field loading, system message
│   ├── test_jambonz_schemas.py    # JambonzStatusWebhookRequest, extended JambonzWebhookRequest
│   ├── test_mcube_helpers.py      # MCube route helper functions
│   ├── test_outbound_registry.py  # get_provider() factory, TwilioProvider credential validation
│   ├── test_pipeline_utils.py     # is_audio_url(), EndCallProcessor frame processing
│   ├── test_schemas.py            # Pydantic request/response schemas
│   ├── test_twilio_twiml.py       # _build_twiml() XML construction
│   ├── test_worker_state_unit.py  # WorkerState lifecycle (app/worker/state.py)
│   └── test_worker_status_unit.py # WorkerStatus URL resolution (get_ws_url, get_health_url)
└── integration/
    ├── __init__.py
    ├── runner/
    │   ├── __init__.py
    │   ├── test_assistants.py     # /assistants CRUD
    │   ├── test_call_service.py   # call_service CRUD + all status transitions
    │   ├── test_calls.py          # GET /calls (filter/pagination) + /calls/{id}
    │   ├── test_jambonz_webhook.py# /jambonz/call (inbound/outbound) + /jambonz/status
    │   ├── test_mcube_webhook.py  # /mcube/call CONNECTING + hangup flows
    │   ├── test_outbound_call.py  # POST /call/outbound validation + happy path + cleanup
    │   ├── test_phone_numbers.py  # /phone-numbers CRUD
    │   ├── test_twilio_webhook.py # /twilio/incoming TwiML + /twilio/status worker release
    │   └── test_workers_endpoint.py # GET/POST /workers management endpoints
    └── worker/
        ├── __init__.py
        └── test_worker_state.py   # WorkerPool: assignment, release, health demotion
```

---

## Test Types

### Unit Tests

**Location:** `tests/unit/`
**Requires:** nothing (no Docker, no DB, no network)
**Run time:** ~60 s (dominated by Pipecat import time, not the tests themselves)

| File | What is tested |
|------|---------------|
| `test_auth.py` | `verify_api_key()`: empty API_KEY disables auth, 401 on missing key, 403 on wrong key, case sensitivity |
| `test_assistant_config.py` | `AssistantConfig` class: field loading, temperature=0.0 edge case, `vad_settings` JSON parsing, system message generation, phone config mapping |
| `test_jambonz_schemas.py` | `JambonzStatusWebhookRequest` camelCase mapping, duration=0 edge case, `JambonzAmdWebhookRequest`, extended `JambonzWebhookRequest` scenarios |
| `test_mcube_helpers.py` | `_map_dial_status`, `_is_terminal_status`, `_is_connect_hangup`, `_determine_call_identifiers` — pure functions with no side effects |
| `test_outbound_registry.py` | `get_provider()` returns correct instances; unknown provider → 400; `TwilioOutboundProvider.validate_credentials()` edge cases |
| `test_pipeline_utils.py` | `is_audio_url` for all supported audio formats + negative cases; `EndCallProcessor` phrase matching, buffer accumulation, EndFrame scheduling |
| `test_schemas.py` | Pydantic schemas: required fields, camelCase→snake_case mapping, optional defaults, validation errors |
| `test_twilio_twiml.py` | `_build_twiml()`: valid XML output, correct `<Stream>` URL, `<Parameter>` elements, `<Pause>` duration |
| `test_worker_state_unit.py` | `WorkerState`: start/end call lifecycle, `get_health_snapshot()`, concurrent cycles, task cleanup |
| `test_worker_status_unit.py` | `WorkerStatus.get_ws_url()`: PUBLIC_WS_URL override, sslip.io suffix, custom template, private IP fallback, host fallback; `get_health_url()` |

#### Example

```python
# tests/unit/test_assistant_config.py
def test_temperature_zero_not_treated_as_falsy():
    """temperature=0.0 must be honoured, not replaced by the default."""
    cfg = _make_config({"temperature": 0.0})
    assert cfg.temperature == 0.0
```

---

### Integration Tests — Runner

**Location:** `tests/integration/runner/`
**Requires:** Docker (Postgres 15 container started automatically)
**Run time:** ~6 s after container is up (container start: ~10 s first run)

These tests send real HTTP requests to the FastAPI runner app (via `httpx.AsyncClient` +
`ASGITransport`) and verify against a real Postgres database.

| File | Endpoints / Layer covered | Test classes |
|------|--------------------------|--------------|
| `test_assistants.py` | `POST/GET/PATCH/DELETE /assistants` | `TestCreateAssistant`, `TestListAssistants`, `TestGetAssistant`, `TestUpdateAssistant`, `TestDeleteAssistant` |
| `test_phone_numbers.py` | `POST/GET/PATCH/DELETE /phone-numbers` | `TestCreatePhoneNumber`, `TestListPhoneNumbers`, `TestGetPhoneNumber`, `TestUpdatePhoneNumber`, `TestDeletePhoneNumber` |
| `test_calls.py` | `GET /calls`, `GET /calls/{id}` | `TestListCallsEndpoint`, `TestGetCallByIdEndpoint` |
| `test_call_service.py` | `call_service.*` direct DB calls | `TestCallServiceCreate`, `TestCallServiceGetBySid`, `TestCallServiceUpdateStatus`, `TestCallServiceTranscript`, `TestCallServiceGetMany`, … |
| `test_twilio_webhook.py` | `POST /twilio/incoming`, `POST /twilio/status` | `TestTwilioIncomingWebhook`, `TestTwilioStatusWebhook` |
| `test_jambonz_webhook.py` | `POST /jambonz/call` (inbound+outbound), `POST /jambonz/status` | `TestJambonzCallWebhookInbound`, `TestJambonzCallWebhookOutbound`, `TestJambonzStatusWebhook` |
| `test_mcube_webhook.py` | `POST /mcube/call` (CONNECTING, BUSY, ANSWER, CANCEL) | `TestMcubeConnectWebhook`, `TestMcubeWebhookHangupVariants` |
| `test_workers_endpoint.py` | `GET /workers`, `POST /workers/refresh`, `POST /workers/{id}/release` | `TestListWorkersEndpoint`, `TestRefreshWorkersEndpoint`, `TestReleaseWorkerEndpoint` |
| `test_outbound_call.py` | `POST /call/outbound`, legacy alias | `TestOutboundCallValidation`, `TestOutboundCallWorkerCapacity`, `TestOutboundCallHappyPath`, `TestOutboundCallWorkerCleanup`, `TestOutboundCallLegacyAlias` |

#### Notable scenarios covered

- `temperature=0.0` is persisted and returned correctly (not silently replaced by 0.7)
- `vad_settings` JSONB round-trip
- MCube CONNECTING → worker assigned → `wss_url` returned
- MCube BUSY/ANSWER hangup → worker released → call status updated
- Twilio incoming call → TwiML XML with `<Stream>` returned → call record created in DB
- Twilio status `completed`/`failed`/`busy`/`no-answer`/`canceled` → worker released
- Jambonz inbound → verb list with `answer` + `listen` → metadata includes `call_type`
- Jambonz status mapping: `ended`/`hangup` → `completed`; `error` → `failed`; etc.
- Outbound call: missing phone → 404; outbound disabled → 400; no workers → 503
- Outbound call: provider failure → worker released (cleanup)
- Workers endpoint: busy/unhealthy workers not counted as available
- 404 when no assistant is configured for the incoming phone number
- 503 when all workers are busy

---

### Integration Tests — Worker

**Location:** `tests/integration/worker/`
**Requires:** nothing (no Docker needed — no DB access)
**Run time:** ~3 s

These tests exercise `WorkerPool` logic directly without going through HTTP.

| File | Test classes |
|------|--------------|
| `test_worker_state.py` | `TestWorkerAssignment`, `TestWorkerRelease`, `TestWorkerReassign`, `TestWorkerHealthDemotion`, `TestWorkerStatus` |

#### Notable scenarios covered

- Atomic assignment: two concurrent calls get different workers
- No capacity: `get_and_assign_worker` returns `None` when all workers are busy
- Health demotion: workers with ≥3 consecutive failures are skipped during assignment
- `reassign_call_sid`: temp reservation ID replaced by real call SID
- WebSocket URL construction with/without `PUBLIC_WS_URL`

---

## How to Run Tests

### Prerequisites

- Python virtual environment activated (`.venv/`)
- Docker running (required for integration/runner tests only)
- Test dependencies installed:

```bash
pip install -r requirements-test.txt
```

### Make targets (recommended)

| Command | What it does |
|---------|-------------|
| `make test` | Run all 155 tests, short pass/fail output |
| `make test-unit` | Unit tests only — no Docker needed, fastest feedback |
| `make test-report` | All tests + HTML report, opens in browser automatically |
| `make test-cov` | All tests + coverage report, opens in browser automatically |
| `make test-full` | All tests + HTML report + coverage + JUnit XML |

### Raw pytest commands

```bash
# Run the entire test suite
.venv/bin/pytest tests/

# Run only unit tests (no Docker needed)
.venv/bin/pytest tests/unit/

# Run only runner integration tests
.venv/bin/pytest tests/integration/runner/

# Run only worker integration tests
.venv/bin/pytest tests/integration/worker/

# Run a single file
.venv/bin/pytest tests/unit/test_mcube_helpers.py

# Run a single test class
.venv/bin/pytest tests/unit/test_assistant_config.py::TestAssistantConfigBasics

# Run a single test
.venv/bin/pytest tests/unit/test_assistant_config.py::TestAssistantConfigBasics::test_temperature_zero_not_treated_as_falsy

# Run with verbose output (shows each test name)
.venv/bin/pytest tests/ -v

# Stop on first failure
.venv/bin/pytest tests/ -x

# Run with captured output printed (useful for debugging)
.venv/bin/pytest tests/ -s

# Run tests matching a keyword
.venv/bin/pytest tests/ -k "mcube"
.venv/bin/pytest tests/ -k "temperature"
```

---

## Test Reports

Both `pytest-html` and `pytest-cov` are included in `requirements-test.txt` — no extra
install steps. All reports are written to the `reports/` directory (git-ignored).

### Quick reference

| Command | Output |
|---------|--------|
| `make test-report` | `reports/test-report.html` — opens automatically |
| `make test-cov` | `reports/coverage/index.html` — opens automatically |
| `make test-full` | All three reports below + opens HTML report |

### HTML Test Report (`pytest-html`)

A single self-contained HTML file showing pass/fail per test, duration, captured logs,
and failure tracebacks. Useful for sharing results without a CI dashboard.

```
reports/test-report.html   (≈ 180 KB, fully self-contained)
```

Screenshot of what the report contains:
- Summary bar: total passed / failed / errors / skipped
- Filterable table of every test with status, duration, and expandable log output
- Environment section with Python version, pytest version, platform, and plugin versions

### Coverage Report (`pytest-cov`)

An interactive HTML site showing line-by-line coverage per file.

```
reports/coverage/index.html   (directory with one HTML page per module)
```

The terminal also prints a `--cov-report=term-missing` summary immediately after the run:

```
Name                              Stmts   Miss  Cover   Missing
---------------------------------------------------------------
app/routes/assistants.py             28      0   100%
app/routes/mcube.py                 131     52    60%   …
app/worker/config.py                 66     10    85%   57-60, 68-71
…
TOTAL                              3412   2402    30%
```

> Current overall coverage is **~30 %**. The low figure is expected — the worker's
> Pipecat pipeline, WebSocket handlers, and telephony transports are not yet covered
> by integration tests (they require live audio streams to exercise meaningfully).

### JUnit XML (CI/CD)

```
reports/junit.xml   (standard JUnit format, ≈ 21 KB)
```

Readable by GitHub Actions, Bitbucket Pipelines, Jenkins, GitLab CI, and most other CI
systems without any plugins.

#### Bitbucket Pipelines example

```yaml
pipelines:
  default:
    - step:
        name: Test
        image: python:3.11-slim
        services:
          - docker
        script:
          - pip install -r requirements-test.txt
          - mkdir -p reports
          - |
            .venv/bin/pytest tests/ -q \
              --junitxml=reports/junit.xml \
              --cov=app \
              --cov-report=xml:reports/coverage.xml
        artifacts:
          - reports/**
```

#### GitHub Actions example

```yaml
- name: Run tests
  run: |
    pip install -r requirements-test.txt
    pytest tests/ -q \
      --junitxml=reports/junit.xml \
      --cov=app \
      --cov-report=xml:reports/coverage.xml

- name: Publish test results
  uses: actions/upload-artifact@v4
  with:
    name: test-reports
    path: reports/
```

---

## Infrastructure & Fixtures

All shared fixtures live in `tests/conftest.py`.

### `pg_container` (session scope)

Starts a `postgres:15-alpine` Docker container once for the entire test session.

- Patches `app.core.database.DATABASE_URL` to point at the container
- Resets the lazy connection pool so it reconnects to the container on first use
- Runs all SQL migration files in `migrations/*.sql` in sorted order
- Tears down automatically when the session ends

> **Why not use a mock DB?** Real Postgres catches JSONB serialisation issues, constraint
> violations, and migration regressions that a mock would miss.

### `clean_tables` (function scope, autouse)

Runs `TRUNCATE assistants, phone_numbers, calls RESTART IDENTITY CASCADE` before every test.
This gives every test a perfectly clean slate without restarting the container.

### `runner_client` (function scope)

Returns an `httpx.AsyncClient` wired to the FastAPI app via `ASGITransport`. This bypasses
the network stack completely and does **not** trigger the ASGI lifespan (no worker pool health
checks, no EC2 discovery). The DB pool is created lazily on first request using the patched
`DATABASE_URL`.

### `mock_worker` / `worker_in_pool` (function scope)

`mock_worker` returns a `WorkerStatus` instance pre-configured for tests.
`worker_in_pool` inserts the mock worker into the live `worker_pool.workers` dict and cleans
up afterwards. Use this in any test that needs an available worker for call assignment.

### Auth Bypass

The `.env` file may have `API_KEY` set. To prevent this from leaking into tests, `conftest.py`
forcefully sets `os.environ["API_KEY"] = ""` at **module level** — before any app module is
imported. This means `load_dotenv()` in `app/config.py` sees the empty value already in the
environment and does not override it, so authentication is disabled for all tests.

---

## Known Gotchas

### 1. `app.services.worker_pool` module shadowing

`app/services/__init__.py` does:
```python
from .worker_pool import worker_pool  # imports the instance, not the module
```
This shadows the module name. If you need to patch a module-level variable in
`worker_pool.py`, use the **string path form** of `unittest.mock.patch`:

```python
# CORRECT
with patch("app.services.worker_pool.PUBLIC_WS_URL", "wss://example.com"):
    ...

# WRONG — targets the LocalWorkerPool instance, not the module
monkeypatch.setattr(wp_module, "PUBLIC_WS_URL", ...)
```

### 2. MCube phone number lookup uses last 10 digits

`_determine_call_identifiers` strips the number down to its last 10 digits before querying
the DB. In tests, store the phone number as the bare 10-digit value and send the full
`+91XXXXXXXXXX` form in the webhook payload:

```python
PHONE_NUMBER_DIGITS = "8001234567"       # stored in DB
TO_NUMBER_FULL = "+91" + PHONE_NUMBER_DIGITS  # sent by MCube
```

### 3. `load_dotenv()` runs at import time

`app/config.py` calls `load_dotenv()` when first imported. Any env override must happen
**before** that import. The `conftest.py` handles this by calling `os.environ["API_KEY"] = ""`
at module level (the first lines of the file), ensuring it's set before pytest imports any
`app.*` module.

### 4. ASGITransport does not run ASGI lifespan

Using `ASGITransport(app=app)` in `httpx.AsyncClient` does **not** start or stop the
application lifespan. This means `worker_pool.start()` (health check loop) never runs in
tests. This is intentional — use the `worker_in_pool` fixture to add workers manually.

---

## Guidelines for Adding New Tests

### Choose the right test type

| Scenario | Test type |
|----------|-----------|
| Pure function / class method with no I/O | Unit test |
| Pydantic schema validation | Unit test |
| HTTP endpoint + DB interaction | Integration — runner |
| Worker pool logic / state machine | Integration — worker |
| Full call lifecycle (STT→LLM→TTS) | End-to-end (not yet implemented) |

### Unit tests

1. Place the file in `tests/unit/test_<module_name>.py`.
2. Group related tests in a class (`class TestMyFeature:`).
3. No fixtures needed beyond standard pytest. Don't import `pg_container`.
4. Mock any I/O: `from unittest.mock import AsyncMock, patch`.
5. Keep each test focused on one behaviour. Prefer `@pytest.mark.parametrize` for
   variations of the same logic.

```python
# Good — one assertion per test
def test_map_dial_status_cancel(self):
    assert _map_dial_status("CANCEL", None) == "canceled"

# Avoid — tests multiple things at once
def test_map_dial_status_all(self):
    assert _map_dial_status("CANCEL", None) == "canceled"
    assert _map_dial_status("BUSY", None) == "busy"
    assert _map_dial_status("ANSWER", "2026-01-01") == "completed"
```

### Integration tests — runner

1. Place the file in `tests/integration/runner/test_<feature>.py`.
2. Use the `runner_client` fixture for HTTP calls — do not create `httpx.AsyncClient`
   yourself.
3. Use `worker_in_pool` for any test that needs a worker to be available.
4. All DB state set up in a test is automatically cleaned up before the next test by
   `clean_tables` (autouse). You don't need teardown logic.
5. For tests that create prerequisite data (e.g., an assistant before creating a phone
   number), use a local `@pytest.fixture`.

```python
@pytest.fixture
async def created_assistant(runner_client):
    resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
    return resp.json()

async def test_phone_number_linked_to_assistant(self, runner_client, created_assistant):
    payload = {**PHONE_PAYLOAD, "assistant_id": created_assistant["id"]}
    resp = await runner_client.post("/phone-numbers", json=payload)
    assert resp.json()["assistant_id"] == created_assistant["id"]
```

### Integration tests — worker

1. Place the file in `tests/integration/worker/test_<feature>.py`.
2. Instantiate `LocalWorkerPool` directly and populate `pool.workers` manually — do not
   call `discover_workers()`.
3. These tests do not need `pg_container` or `runner_client`.

### Naming conventions

- File: `test_<module_or_feature>.py`
- Class: `Test<FeatureName>` (e.g., `TestCreateAssistant`)
- Method: `test_<what>_<expected_outcome>` (e.g., `test_create_missing_required_field_returns_422`)

### Testing new API endpoints

When adding a new route, write tests that cover at minimum:
- Happy path with valid input
- 404 for a non-existent resource
- 422 for missing/invalid required fields
- Any provider-specific behaviour (e.g., different credential formats)

### Testing new webhook events

When adding a new webhook event type:
- Cover the happy path end-to-end (event → DB update → correct response body)
- Cover missing/unknown call IDs (should not crash)
- Cover all distinct `status` values using `@pytest.mark.parametrize`
