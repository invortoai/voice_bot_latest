# Worker Pool — Complete Reference

This document is the authoritative reference for the worker pool subsystem.
It covers architecture, data flow, every race condition and how it is handled,
all fallback paths, and the full environment-variable contract.

---

## Table of Contents

1. [Role in the System](#1-role-in-the-system)
2. [Package Structure](#2-package-structure)
3. [WorkerStatus: the unit of state](#3-workerstatus-the-unit-of-state)
4. [Pool Types and Selection](#4-pool-types-and-selection)
5. [Discovery Flow](#5-discovery-flow)
6. [Health-Check Loop](#6-health-check-loop)
7. [Call Assignment Flow](#7-call-assignment-flow)
8. [Redis State Backend](#8-redis-state-backend)
9. [Call Release Flow](#9-call-release-flow)
10. [Race Conditions and Mitigations](#10-race-conditions-and-mitigations)
11. [Fallback Paths](#11-fallback-paths)
12. [Drain Safety (pod/instance termination mid-call)](#12-drain-safety)
13. [Startup Race: fresh runner with calls in flight](#13-startup-race)
14. [Stale Assignment Reaping](#14-stale-assignment-reaping)
15. [Pre-warm Flow](#15-pre-warm-flow)
16. [Environment Variable Reference](#16-environment-variable-reference)
17. [Redis Key Reference](#17-redis-key-reference)
18. [Failure Mode Matrix](#18-failure-mode-matrix)

---

## 1. Role in the System

```
Telephony provider          Runner (port 7860)           Worker (port 8765)
(Twilio / Jambonz)    ──►  /twilio/incoming          ──► WebSocket /ws
                            /jambonz/call                  Pipecat pipeline
                            /call/outbound                 STT / LLM / TTS
                                  │
                         worker_pool.get_and_assign_worker()
                                  │
                     ┌────────────┴─────────────┐
                     │     BaseWorkerPool        │
                     │  self.workers: dict       │  ← registry (IPs, health)
                     │  self._redis: optional    │  ← assignment authority
                     └──────────────────────────-┘
```

The runner is a **router**. It receives webhooks from the telephony provider,
selects an idle worker from the pool, hands off the WebSocket URL to the
provider, and tracks which worker owns which call.

Workers are **single-tenant during a call**: one call per worker process.
The pool enforces this invariant.

### Design principle: Redis as single source of truth

Assignment state (`current_call_sid`) lives **only in Redis**.
`WorkerStatus` holds a **best-effort local cache** of the last assignment this
runner performed — it is never synced from Redis after the initial write.

This eliminates the old `_sync_assignments_from_redis` loop and all the
timing-window bugs it introduced.  Redis atomically arbitrates concurrent
assignment attempts via Lua scripts; the local cache is only a pre-filter
(reduces Redis round-trips by skipping workers already known to be busy).

---

## 2. Package Structure

```
app/services/worker_pool/
├── __init__.py        # Calls create_worker_pool(); exposes worker_pool singleton
├── factory.py         # create_worker_pool() — reads env vars, wires Redis
├── base.py            # BaseWorkerPool + WorkerStatus — all shared logic
├── local.py           # LocalWorkerPool — static WORKER_HOSTS list (dev)
├── ec2.py             # EC2WorkerPool   — boto3 tag-based discovery (prod EC2)
├── k8s.py             # K8sWorkerPool   — kubernetes_asyncio pod discovery (EKS)
└── redis_state.py     # RedisStateBackend — atomic assignment state via Lua
```

**`worker_pool` singleton** is created at import time in `__init__.py`.
FastAPI imports it in `main.py` which triggers `create_worker_pool()` before
the lifespan hook runs. The lifespan then calls `worker_pool.discover_workers()`
and `worker_pool.start()`.

---

## 3. WorkerStatus: the unit of state

```python
class WorkerStatus:
    # Immutable registry info (set on discovery, updated on IP change)
    host: str
    instance_id: str
    private_ip: Optional[str]
    public_ip: Optional[str]

    # Runner-local health metrics (never shared via Redis)
    consecutive_failures: int       # resets to 0 on success
    last_health_check: float        # unix timestamp

    # Best-effort assignment cache — written by assign/release/reassign on THIS runner.
    # Never synced from Redis. Redis is authoritative; this is a pre-filter only.
    current_call_sid: Optional[str]
    assigned_at: Optional[float]
```

**`is_accepting_calls`** — `current_call_sid is None and consecutive_failures < 3`.
Used as a pre-filter before the atomic Redis assignment check.  A worker that
passes this may still fail the Redis Lua check (another runner claimed it first).

**`to_dict()` / `to_safe_dict()`** — returns local cache only.  For accurate
assignment state use `get_all_workers_state()` which reads from Redis.

---

## 4. Pool Types and Selection

| `WORKER_POOL_TYPE` | Class             | Discovery mechanism         |
|--------------------|-------------------|-----------------------------|
| `ec2` (default)    | `EC2WorkerPool`   | `boto3` EC2 tag filter      |
| `local`            | `LocalWorkerPool` | `WORKER_HOSTS` env var      |
| `k8s`              | `K8sWorkerPool`   | Kubernetes pod API          |

Set `REDIS_HOST` to enable shared assignment coordination.  Without it, a
single asyncio.Lock provides mutual exclusion (single-runner only).

---

## 5. Discovery Flow

`discover_workers()` is called at startup and on every health-check cycle.

```
K8s / EC2 API
     │
     ▼
discovered = { pod_name/instance_id: WorkerStatus(...) }
     │
     ▼
async with self._lock:
   ┌─ add new workers
   ├─ update IP if changed (skip if mid-call — drain safety)
   └─ collect stale_ids = [ids not in discovered]

for each stale_id:
   has_call = _has_active_call(worker)   ← checks local cache + Redis
   if has_call:
       keep in pool (draining)
   else:
       del self.workers[stale_id]
```

**`_has_active_call(worker)`** — returns `True` if:
1. Local `current_call_sid` is set, OR
2. Redis HASH `current_call_sid` field is non-empty (post-restart: local blank, Redis has truth)

This ensures pods/instances that disappeared from the API while mid-call stay
reachable until `release_worker()` fires.

**On-demand discovery** — `_on_demand_discover()` is triggered when
`get_and_assign_worker` finds no available workers.  Debounced to 5 s so
concurrent "pool empty" callers share one API round-trip.

---

## 6. Health-Check Loop

Runs every `HEALTH_CHECK_INTERVAL` seconds (default 30 s).

```python
while True:
    await discover_workers()
    await asyncio.gather(*[health_check_worker(w) for w in workers.values()])
    await _release_stale_assignments()
    await asyncio.sleep(HEALTH_CHECK_INTERVAL)
```

### health_check_worker

1. `GET /health` on the worker
2. Update `consecutive_failures` (increments on failure, resets on success)
3. If worker reports `current_call = null` but runner has `current_call_sid`:
   - **Within** `_STARTUP_GRACE_SECONDS` (90 s): skip — call may not have connected yet
   - **Beyond** 90 s: release (missed status webhook or multi-runner skew)
4. If worker reports a call SID that differs from local cache:
   - Within 90 s of assignment: skip (reassignment in progress, worker connecting new WS)
   - Beyond 90 s: update local cache to match worker (worker is authoritative for its own state)

### No more _sync_assignments_from_redis

The old periodic Redis→local sync loop is removed.  It caused the "Restored
assignment" log noise and introduced timing windows where a worker appeared busy
on one runner after another had already released it.  With Redis as the sole
assignment authority there is nothing to sync.

---

## 7. Call Assignment Flow

### Redis path (multi-runner)

```
get_and_assign_worker(call_sid)
    │
    ├─ candidates = [w for w in workers if w.is_accepting_calls]
    │   (local pre-filter: skips locally-known-busy and unhealthy workers)
    │
    └─ _redis.find_and_assign(candidate_ids, call_sid, ttl)
            │
            └─ Lua script (atomic):
               for each candidate HASH:
                   if HGET current_call_sid == '' or nil:
                       HSET current_call_sid = call_sid, assigned_at = now
                       EXPIRE hash ttl
                       SET invorto:worker:call:{call_sid} = worker_id  EX ttl
                       return worker_id
               return false
```

The Lua script runs atomically on Redis — no two runners can claim the same
worker even under concurrent load.

On Redis error: retry up to `_REDIS_ASSIGN_RETRIES` (3) times with linear
backoff (0.5 s × attempt).  Reject after exhaustion — falling through to the
local lock path would cause split-brain (both runners see the same worker as
free and double-assign it).

### Local path (no Redis)

Single asyncio.Lock protects the full scan-and-claim sequence.  Safe for
single-runner deployments only.

---

## 8. Redis State Backend

### Schema

```
invorto:worker:state:{worker_id}  HASH
    current_call_sid: str    # empty/absent = worker is free
    assigned_at:      str    # unix timestamp string, empty/absent = free
    TTL: 24 h (set on assignment; safety net if release never fires)

invorto:worker:call:{call_sid}    STRING
    value:  worker_id
    TTL:    24 h (same safety net)
```

### Lua scripts

**`_LUA_FIND_AND_ASSIGN`** — iterate candidate HASHes, claim first free one.
Atomically writes both the HASH fields and the reverse-mapping key.

**`_LUA_RELEASE_ASSIGNMENT`** — `HDEL` state fields + `DEL` call key.
Atomic: a crash between the two deletes is impossible.

**`_LUA_REASSIGN`** — `DEL` old call key, `SET` new call key, `HSET` new SID.
Three operations in one atomic script.

### Retry

All `RedisStateBackend` public methods wrap their operations in `_with_retry()`
— linear-backoff retry (3 attempts, 0.3 s × attempt) on any exception.

### GET /workers accuracy

`get_all_workers_state()` reads all worker HASHes in a **single pipeline
round-trip** (`batch_get_states`).  This gives callers the live Redis state
rather than potentially stale local cache, without N individual round-trips.

---

## 9. Call Release Flow

```
release_worker(call_sid)
    │
    ├─ for attempt in range(4):          # RC-8: retry for call-mapping race
    │       worker_id = GET invorto:worker:call:{call_sid}
    │       if found: break
    │       sleep 200 ms
    │
    ├─ if worker_id found:
    │       release_assignment(worker_id, call_sid)   ← Lua: HDEL + DEL
    │       local: worker.current_call_sid = None
    │
    └─ else:                             # Key missing after all retries
            scan local pool for matching current_call_sid
            clear if found (covers Redis miss / expired key)

release_worker_by_id(worker_id)         # admin / drain endpoint
    │
    ├─ read call_sid from local cache
    ├─ if call_sid: release_assignment(worker_id, call_sid)
    │  else:        clear_worker_state(worker_id)
    └─ clear local cache
```

---

## 10. Race Conditions and Mitigations

### RC-1: Two runners assign the same worker simultaneously
**Scenario**: Both runners pass `is_accepting_calls` pre-filter; both try to
claim the same worker.

**Mitigation**: Lua `_LUA_FIND_AND_ASSIGN` is atomic. Only one HSET succeeds
for a given HASH field. The second runner's Lua call sees a non-empty
`current_call_sid` and moves to the next candidate.

---

### RC-2: Worker crashes mid-call
**Scenario**: Worker process dies; WebSocket drops; telephony provider hangs up.

**Mitigation**: Provider sends a `completed`/`failed` status webhook →
`release_worker()` fires → Redis + local cleared. Health check body reader
provides a second path: next health-check cycle detects worker returns
`current_call = null` → releases.

---

### RC-3: Status webhook goes to a different runner pod
**Scenario**: Twilio load-balances the status webhook to `runner-B`; call was
assigned by `runner-A`.

**Mitigation**: `release_worker()` on `runner-B` looks up
`invorto:worker:call:{call_sid}` in Redis → gets the worker_id → atomically
releases both Redis keys → clears `runner-B`'s local cache.  `runner-A`'s
local cache is stale until its next health-check body reader cycle (≤30 s),
but the worker is free for reassignment immediately because **Redis is
authoritative**.

---

### RC-4: Runner restart with calls in flight
**Scenario**: Runner restarts; local state is blank; but Redis still has active
assignments.

**Mitigation**: On assignment, `find_and_assign` Lua checks the Redis HASH —
workers that are busy in Redis will have a non-empty `current_call_sid` field
and will be skipped, even if the runner's local cache shows them as free.
Drain safety (`_has_active_call`) also checks Redis so pods mid-call are not
evicted from the pool.

When the call ends, the status webhook fires → `release_worker()` → Redis
lookup succeeds → release.

---

### RC-5 (removed): Local/Redis assignment divergence
**Old scenario**: `_sync_assignments_from_redis` restored Redis assignments into
local state on every health-check cycle.  A completed call could get its worker
"re-assigned" on a second runner pod that ran `_sync` before seeing the release.

**Resolution**: `_sync_assignments_from_redis` is removed entirely.  Local cache
is written only by explicit assign/release/reassign operations.  There is no
periodic sync to reintroduce stale assignments.

---

### RC-6: Status webhook before reassign_call_sid (outbound calls)
**Scenario**: Outbound call flow: worker is assigned a temp UUID → Twilio API
returns real SID → `reassign_call_sid(UUID → real_SID)`.  If Twilio fires a
`no-answer`/`failed` webhook with the real SID **before** `reassign_call_sid`
writes `invorto:worker:call:{real_SID}`, `release_worker(real_SID)` finds no
call-mapping key.

**Mitigation**: `release_worker` retries the Redis lookup up to 4 times at
200 ms intervals (total 600 ms window), which is well beyond any realistic
reassign latency.  If the key appears during the retry window, release proceeds
normally.  If not (genuinely missing), falls back to local pool scan.

---

## 11. Fallback Paths

| Scenario                             | Path                                         |
|--------------------------------------|----------------------------------------------|
| Redis down during assignment         | Retry 3× with backoff → reject call          |
| Redis down during release lookup     | Retry 4× → local pool scan → warn if missing |
| Redis down during `get_all_workers_state` | Return local cache with warning          |
| Redis down during drain check        | `_has_active_call` returns True (safe default)|
| No REDIS_HOST configured             | Local asyncio.Lock path throughout           |

The fallback to local assignment (when Redis is unavailable) is intentionally
**rejected**.  Two runners hitting a Redis blip simultaneously would both fall
back to local locking, see the same worker as free, and double-assign it.

---

## 12. Drain Safety

When a pod/instance disappears from the discovery API (K8s deletionTimestamp
set, EC2 instance terminated) while a call is in progress:

1. `discover_workers` collects the stale ID.
2. `_has_active_call(worker)` is called **outside the lock** (it may await Redis).
   - Returns True if local `current_call_sid` is set, OR Redis HASH is non-empty.
3. If has_call → **keep** the entry in `self.workers` so `release_worker()` and
   `get_worker_for_call()` can still resolve it.
4. On the next discovery cycle after the call ends, `_has_active_call` returns
   False → entry is removed.

**Post-restart drain**: if the runner restarted while a pod was mid-call, local
`current_call_sid` is None, but `_has_active_call` checks Redis and returns True.
This prevents incorrectly evicting a draining pod from the pool.

---

## 13. Startup Race: fresh runner with calls in flight

When a runner starts fresh (or restarts) while calls are active:

1. `discover_workers()` populates `self.workers` with blank `WorkerStatus` objects
   (all fields None/0).
2. `get_and_assign_worker()` calls `find_and_assign` Lua which reads Redis HASHes.
   Workers with active calls have a non-empty `current_call_sid` → Lua skips them.
   New calls are only assigned to genuinely free workers.
3. When the active calls end, status webhooks fire → `release_worker()` → Redis
   lookup succeeds regardless of which runner handles the webhook.

There is no grace period or sync needed.  Redis is always authoritative.

---

## 14. Stale Assignment Reaping

`_release_stale_assignments()` runs every health-check cycle as a safety net for
assignments that outlive `WORKER_STALE_ASSIGNMENT_SECONDS` (disabled when ≤ 0).

**Redis mode**: batch-fetch worker state HASHes (`batch_get_states`).  If Redis
returns empty/absent for a locally-busy worker → clear local cache.  The call
mapping key in Redis is already gone (another runner already released it); local
state just hasn't caught up yet.

**No-Redis mode**: make an HTTP `/health` call to the worker.  Only release if
the worker confirms `current_call = null` to avoid killing a legitimately long call.

---

## 15. Pre-warm Flow

Pre-warm is always active (no toggle env var).

```
get_and_assign_worker() → worker assigned
    │
    ├─ send_prewarm_and_wait(worker, call_sid, config)   [inbound: after WS URL built]
    │       POST /prewarm  { call_sid, wait: true, ...config }
    │       Returns: { status: "ready" | "error" }
    │
    └─ trigger_prewarm_nowait(worker, call_sid)          [outbound: fire-and-forget]
            asyncio.create_task(POST /prewarm { call_sid })

reassign_call_sid(old → new):
    trigger_prewarm_reassign_nowait(worker, old, new)
        POST /prewarm/reassign { old_key: old, new_key: new }

release_worker(call_sid):
    asyncio.create_task(_cancel_prewarm(worker, call_sid))
        DELETE /prewarm/{call_sid}
```

---

## 16. Environment Variable Reference

| Variable                        | Default        | Description                                                  |
|---------------------------------|----------------|--------------------------------------------------------------|
| `WORKER_POOL_TYPE`              | `ec2`          | `ec2`, `local`, or `k8s`                                     |
| `REDIS_HOST`                    | (none)         | Redis host; enables multi-runner coordination                |
| `REDIS_PORT`                    | `6379`         | Redis port                                                   |
| `WORKER_HOSTS`                  | (none)         | Comma-separated `host:port` list (local pool only)           |
| `WORKER_PORT`                   | `8765`         | Worker HTTP/WS port                                          |
| `HEALTH_CHECK_INTERVAL`         | `30`           | Seconds between health-check cycles                          |
| `WORKER_TIMEOUT`                | `5.0`          | HTTP timeout for worker health-check requests                |
| `WORKER_STALE_ASSIGNMENT_SECONDS` | `0`          | Force-release workers assigned longer than this (0 = off)   |
| `WORKER_K8S_NAMESPACE`          | `default`      | K8s namespace to list pods from                              |
| `WORKER_K8S_LABEL_SELECTOR`     | (none)         | K8s label selector to filter worker pods                     |
| `WORKER_PUBLIC_WS_SCHEME`       | `wss`          | Scheme for public WebSocket URLs                             |
| `WORKER_PUBLIC_WS_PORT`         | `443`          | Port for public WebSocket URLs (0 or 443 = omit)             |
| `WORKER_PUBLIC_WS_HOST_SUFFIX`  | (none)         | sslip.io-style suffix appended to public_ip                  |
| `WORKER_PUBLIC_WS_HOST_TEMPLATE`| (none)         | Template for per-worker WS hostname (`{instance_id}`, etc.)  |
| `PUBLIC_WS_URL`                 | (none)         | Shared WS base URL (ngrok in dev, proxy in prod)             |

---

## 17. Redis Key Reference

| Key pattern                              | Type   | Value       | TTL  | Purpose                          |
|------------------------------------------|--------|-------------|------|----------------------------------|
| `invorto:worker:state:{worker_id}`       | HASH   | `{ current_call_sid, assigned_at }` | 24 h (set on assignment) | Authoritative assignment state per worker |
| `invorto:worker:call:{call_sid}`         | STRING | `worker_id` | 24 h | Reverse lookup: call → worker    |

**TTL semantics**: The 24 h TTL is a last-resort safety net in case a release
never fires (runner crash, network partition).  Normal releases use the Lua
scripts which explicitly `HDEL` / `DEL` the keys.  TTL expiry never fires during
a live call (real calls are orders of magnitude shorter than 24 h).

---

## 18. Failure Mode Matrix

| Failure                              | Detection                          | Recovery                                      |
|--------------------------------------|------------------------------------|-----------------------------------------------|
| Worker crashes mid-call              | Health-check body reader (≤30 s)   | Release via body reader; telephony sends webhook too |
| Status webhook to wrong runner       | Immediate (webhook fires to that runner) | Redis lookup → release regardless of assigning runner |
| Runner restarts                      | Next assignment attempt             | Redis HASH blocks double-assignment; drain check keeps mid-call pods |
| Redis transient error (assignment)   | Exception in find_and_assign       | Retry 3× with backoff; reject on exhaustion   |
| Redis transient error (release)      | Exception in get_worker_for_call   | Retry 4× with backoff; fall back to local scan |
| Redis transient error (drain check)  | Exception in get_worker_state      | Return True (safe default: keep worker)        |
| Redis down (assignment)              | All retries exhausted              | Call rejected; no split-brain                 |
| Redis down (GET /workers)            | batch_get_states raises            | Return local cache with warning               |
| Pod disappears mid-call (K8s drain)  | discover_workers sees pod missing  | _has_active_call → keep entry until call ends |
| Long call exceeds STALE_ASSIGNMENT   | _release_stale_assignments         | Redis: clear if Redis shows free; no-Redis: HTTP confirm |
