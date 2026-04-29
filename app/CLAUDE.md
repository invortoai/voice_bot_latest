# Runner CLAUDE.md

## What
FastAPI service (port 7860) managing assistants, phone numbers, call routing, and worker pool coordination. Entry point: `main.py`.

## Guardrails

**Webhook Auth**: `/twilio/*` and `/jambonz/*` routes have NO authentication—telephony providers don't send API keys. Protected routes require `X-API-Key` header.

**Database**: Always use `get_cursor()` from `app.core.database`, never raw connections. Context manager handles commit/rollback.

**Worker Pool** — full reference: `services/worker_pool/WORKER_POOL.md`

Quick reference:
- `WORKER_POOL_TYPE=local`  → static `WORKER_HOSTS` list (local dev)
- `WORKER_POOL_TYPE=ec2`    → boto3 EC2 tag discovery (default, production)
- `WORKER_POOL_TYPE=k8s`    → kubernetes_asyncio pod discovery (EKS V2)
- Set `REDIS_HOST` to enable atomic multi-runner assignment coordination
- `WorkerStatus.is_accepting_calls` is the single source of truth for availability
  (computed: `current_call_sid is None and consecutive_failures < 3`)
- Discovery runs at startup, every `HEALTH_CHECK_INTERVAL` seconds, and on-demand
- Drain safety: pods/instances with active calls are kept in the pool even after
  they disappear from the discovery API; removed on next cycle once call ends
- Redis assignment is atomic via a Lua script (single EVAL writes both keys);
  falls back to local asyncio.Lock if Redis is unavailable

**Phone Numbers**: E.164 format required (`+1234567890`). Database lookups fail without the `+` prefix.

## Routes

**Protected Routes** (require `X-API-Key`):
- `/assistants` – CRUD for AI assistants (system prompt, voice, model config)
- `/phone-numbers` – CRUD for phone numbers (provider, credentials, assistant link)
- `/calls` – List calls, stats; `/call/outbound` (Twilio), `/call/outbound/jambonz` (Jambonz)
- `/workers` – Worker pool status

**Twilio Webhooks** (`routes/twilio.py`):
- `/twilio/incoming` – Returns TwiML with `<Stream>` pointing to worker WebSocket
- `/twilio/status` – Status callbacks; releases worker on terminal states
- Response format: XML (TwiML)

**Jambonz Webhooks** (`routes/jambonz.py`):
- `/jambonz/call` – Handles BOTH inbound AND answered outbound calls (check `customerData.call_type`)
- `/jambonz/status` – Status callbacks; maps Jambonz states to Twilio-like statuses
- Response format: JSON array of verb objects (`[{"verb": "answer"}, {"verb": "listen", ...}]`)

**Outbound Call Flow Differences**:
- Twilio: Worker assigned immediately at `/call/outbound`; TwiML embedded in API call
- Jambonz: Worker assigned later when `/jambonz/call` webhook fires (after call answered)

## Key Files
- `main.py` – FastAPI app, router setup, lifespan
- `routes/twilio.py` – TwiML generation, Stream parameters
- `routes/jambonz.py` – Jambonz verbs, listen/bidirectionalAudio config
- `routes/calls.py` – Outbound call initiation logic
- `services/worker_pool/` – Worker discovery and assignment (factory, base, ec2, k8s, local, redis_state)
- `core/database.py` – `get_cursor()` context manager
