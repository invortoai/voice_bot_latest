from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.config import ENVIRONMENT, IS_LOCAL, CORS_ORIGINS
from app.version import __version__
from app.core.database import get_pool, close_pool
from app.core.auth import verify_api_key
from app.core.log_setup import setup_logging
from app.core.rate_limiter import limiter

# Set up structured JSON logging before importing services so that
# module-level log calls (e.g. worker_pool initialisation) use the
# correct format from the start.
setup_logging("runner", environment=ENVIRONMENT)

from app.observability.otel import setup_otel  # noqa: E402
from app.core.tracing import register_library_instrumentors  # noqa: E402

setup_otel(service_name="invorto-runner", environment=ENVIRONMENT)
register_library_instrumentors()

from app.middleware import RequestContextMiddleware, HttpMetricsMiddleware  # noqa: E402
from app.services.webhook_worker import WebhookWorker  # noqa: E402
from app.services.worker_pool import worker_pool  # noqa: E402
from app.routes import (  # noqa: E402
    assistants_router,
    phone_numbers_router,
    calls_router,
    call_stats_router,
    twilio_router,
    jambonz_router,
    mcube_router,
    workers_router,
    auth_router,
    api_keys_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting bot runner ({ENVIRONMENT} mode)...")

    try:
        get_pool()
        logger.info("Database connection pool initialized")
    except Exception as e:
        logger.warning(f"Database not configured: {e}")

    # Warn at startup if security-critical env vars are missing
    from app.config import (
        JAMBONZ_WEBHOOK_SECRET,
        WORKER_AUTH_TOKEN,
        WORKER_POOL_TYPE,
        PUBLIC_WS_URL,
        WORKER_PUBLIC_WS_HOST_TEMPLATE,
    )

    if not JAMBONZ_WEBHOOK_SECRET:
        logger.warning(
            "JAMBONZ_WEBHOOK_SECRET is not set — all Jambonz webhooks will return 503"
        )
    if not WORKER_AUTH_TOKEN:
        logger.warning(
            "WORKER_AUTH_TOKEN is not set — all worker management endpoints will return 503"
        )

    # K8s WebSocket URL routing sanity checks
    if WORKER_POOL_TYPE == "k8s":
        if PUBLIC_WS_URL and not WORKER_PUBLIC_WS_HOST_TEMPLATE:
            logger.warning(
                "WORKER_POOL_TYPE=k8s with PUBLIC_WS_URL set: all workers will return the "
                "SAME WebSocket URL. Jambonz/Twilio will connect to a random pod, not the "
                "assigned one — per-pod call routing is broken. "
                "Set WORKER_PUBLIC_WS_HOST_TEMPLATE={instance_id}.workers.example.com instead "
                "so each worker returns its own URL routable through Envoy."
            )
        if not PUBLIC_WS_URL and not WORKER_PUBLIC_WS_HOST_TEMPLATE:
            logger.warning(
                "WORKER_POOL_TYPE=k8s but neither PUBLIC_WS_URL nor WORKER_PUBLIC_WS_HOST_TEMPLATE "
                "is set. Worker WebSocket URLs will fall back to private pod IPs (10.x.x.x:8765) "
                "which are not reachable from Jambonz/Twilio. "
                "Set WORKER_PUBLIC_WS_HOST_TEMPLATE={instance_id}.workers.example.com and configure "
                "Envoy to route *.workers.example.com per-pod."
            )
        if WORKER_PUBLIC_WS_HOST_TEMPLATE:
            logger.info(
                f"K8s WebSocket routing: WORKER_PUBLIC_WS_HOST_TEMPLATE={WORKER_PUBLIC_WS_HOST_TEMPLATE!r} "
                f"— ensure Envoy/ingress routes each pod subdomain to the correct pod IP:8765"
            )

    await worker_pool.discover_workers()
    worker_pool.start()

    webhook_worker = WebhookWorker()
    webhook_worker.start()

    yield

    logger.info("Shutting down bot runner...")
    await webhook_worker.stop()
    await worker_pool.stop()
    close_pool()


# ── Customer-friendly error handlers (defined before app so they can be passed
#    into FastAPI() constructor — only way to reliably override defaults) ────────

import json as _json  # noqa: E402


async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """Flatten Pydantic v2 validation errors into a clean {success, error, details} shape."""
    details = []
    for err in exc.errors():
        loc = err.get("loc", ())
        field = ".".join(str(p) for p in loc if p not in ("body", "query", "path"))
        details.append(
            {"field": field or "request", "message": err.get("msg", "Invalid value")}
        )
    return JSONResponse(
        status_code=422,
        content={"success": False, "error": "Validation failed", "details": details},
    )


async def _json_decode_exception_handler(request: Request, exc: Exception):
    """Return a clean 400 for malformed JSON bodies; re-raise everything else."""
    if isinstance(exc, _json.JSONDecodeError):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid JSON in request body",
                "details": [{"field": "body", "message": str(exc)}],
            },
        )
    raise exc


app = FastAPI(
    title="Invorto AI Bot Runner",
    description="""
## Bot Runner API

A FastAPI-based service that manages AI voice assistants and phone call routing.

### Features
- **Assistants**: Create and manage AI voice assistants with custom configurations
- **Phone Numbers**: Register and configure phone numbers (supports Twilio and Jambonz)
- **Calls**: Initiate outbound calls and handle incoming call webhooks
- **Workers**: Manage worker instances that process voice calls

### Telephony Providers
- **Twilio**: Full support for inbound/outbound calls via Twilio
- **Jambonz**: Full support for inbound/outbound calls via Jambonz
- **MCube**: Full support for inbound/outbound calls via MCube

### Environment Variables
```
# Jambonz
JAMBONZ_API_URL=https://jambonz.cloud/api
JAMBONZ_ACCOUNT_SID=your-account-sid
JAMBONZ_API_KEY=your-api-key
JAMBONZ_APPLICATION_SID=your-app-sid

# MCube
MCUBE_API_URL=https://api.mcube.com/Restmcube-api
MCUBE_AUTH_TOKEN=your-bearer-token
```

### Webhook URLs
- **Twilio Inbound**: `{PUBLIC_URL}/twilio/incoming`
- **Twilio Status**: `{PUBLIC_URL}/twilio/status`
- **Jambonz Call**: `{PUBLIC_URL}/jambonz/call` (handles both inbound and answered outbound)
- **Jambonz Status**: `{PUBLIC_URL}/jambonz/status`
- **MCube Call** (refurl): `{PUBLIC_URL}/mcube/call` (CONNECTING → wss_url; BUSY/ANSWER → status update and release worker)
    """,
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    exception_handlers={
        RequestValidationError: _validation_exception_handler,
        Exception: _json_decode_exception_handler,
    },
    openapi_tags=[
        {
            "name": "Health",
            "description": "Health check endpoint for load balancers and monitoring.",
        },
        {
            "name": "Auth",
            "description": "Login to obtain a short-lived JWT for API key management.",
        },
        {
            "name": "API Keys",
            "description": "Create, list, rotate, and revoke per-org API keys.",
        },
        {
            "name": "Assistants",
            "description": "Manage AI voice assistants with custom system prompts, voices, and configurations.",
        },
        {
            "name": "Phone Numbers",
            "description": "Register and configure phone numbers for Twilio or Jambonz providers.",
        },
        {
            "name": "Calls",
            "description": "Initiate outbound calls and retrieve call history.",
        },
        {
            "name": "Call Stats",
            "description": "Query per-call stats and webhook delivery logs. Authenticated via X-API-Key header.",
        },
        {
            "name": "Insights: Config",
            "description": "Manage per-org AI configuration (STT, LLM, feature toggles, webhook settings).",
        },
        {
            "name": "Workers",
            "description": "Monitor and manage worker instances that process voice calls.",
        },
        {
            "name": "Twilio Webhooks",
            "description": "Webhook endpoints for Twilio call events. Configure these URLs in your Twilio Console.",
        },
        {
            "name": "Jambonz Webhooks",
            "description": "Webhook endpoints for Jambonz call events. Configure these URLs in your Jambonz Portal.",
        },
        {
            "name": "MCube Webhooks",
            "description": "Webhook endpoints for MCube call events. Configure these URLs in your MCube Portal.",
        },
        {
            "name": "Health",
            "description": "Health check endpoint for load balancers and monitoring.",
        },
    ],
)

# Auto-instrumentation (no-op when OTLP_ENDPOINT is unset)
try:
    import socket as _socket
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    import logging as _logging

    FastAPIInstrumentor.instrument_app(app, excluded_urls="/health")

    _runner_meter = _otel_metrics.get_meter("invorto.runner", version="1.0.0")
    _runner_host = _socket.gethostname()

    def _observe_workers(options):
        try:
            available = sum(
                1 for w in worker_pool.workers.values() if w.is_accepting_calls
            )
            total = len(worker_pool.workers)
            _base = {"host.name": _runner_host}
            yield _otel_metrics.Observation(available, {**_base, "state": "available"})
            yield _otel_metrics.Observation(
                total - available, {**_base, "state": "busy"}
            )
        except Exception as exc:
            _logging.getLogger(__name__).debug("worker gauge callback error: %s", exc)

    _runner_meter.create_observable_gauge(
        name="invorto.runner.workers",
        callbacks=[_observe_workers],
        unit="{worker}",
        description="Worker count by state (available / busy).",
    )
except ImportError:
    pass

# Add middleware for request context (for error handling)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(HttpMetricsMiddleware)

# slowapi rate-limiting middleware
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


app.add_middleware(SlowAPIMiddleware)

# CORS — must be added LAST so Starlette places it outermost (handles
# browser preflight OPTIONS before rate-limiting or auth run).
# Covers: /auth/login, /api-keys/*, /assistants/*, /phone-numbers/*, /calls/*
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    expose_headers=["X-Request-ID"],
)

# Auth + API key management (JWT-based, rate-limited — no global auth dep)
app.include_router(auth_router)
app.include_router(api_keys_router)

# Org-scoped data routes (each route verifies org API key inline via Depends)
app.include_router(assistants_router)
app.include_router(phone_numbers_router)
app.include_router(calls_router)
app.include_router(call_stats_router)

# Worker management (global infra API key — SRE only)
app.include_router(workers_router, dependencies=[Depends(verify_api_key)])

# Twilio webhooks (no authentication - Twilio doesn't send API keys)
app.include_router(twilio_router)

# Jambonz webhooks (no authentication - carrier/SBC doesn't send API keys)
app.include_router(jambonz_router)

# MCube webhooks (no authentication - MCube doesn't send API keys)
app.include_router(mcube_router)

from app.routes.insights_config import router as insights_config_router  # noqa: E402
from app.routes.insights_analyse import router as insights_analyse_router  # noqa: E402
from app.routes.knowledge import router as knowledge_router  # noqa: E402

app.include_router(insights_config_router)
app.include_router(insights_analyse_router)
app.include_router(knowledge_router)


# Public health endpoint (no authentication - used by load balancers and monitoring)
@app.get("/health", tags=["Health"])
async def health_check():
    available_workers = sum(
        1 for w in worker_pool.workers.values() if w.is_accepting_calls
    )
    return {
        "status": "healthy",
        "version": __version__,
        "environment": ENVIRONMENT,
        "total_workers": len(worker_pool.workers),
        "available_workers": available_workers,
    }


if __name__ == "__main__":
    import uvicorn
    from app.config import RUNNER_PORT

    logger.info(f"Starting bot runner on port {RUNNER_PORT} ({ENVIRONMENT} mode)")

    if IS_LOCAL:
        logger.info(
            f"Configure Twilio webhook: http://localhost:{RUNNER_PORT}/twilio/incoming"
        )
        logger.info("Use ngrok for external access: ngrok http 7860")

    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104
        port=RUNNER_PORT,
        log_level="info",
    )
