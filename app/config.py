import os
from dotenv import load_dotenv

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
IS_LOCAL = ENVIRONMENT.lower() in ("local", "dev", "development")

# Log output format: "text" (human-readable) or "json" (OTEL-aligned JSON).
# Defaults to "text" locally and "json" in production.
LOG_FORMAT = os.getenv("LOG_FORMAT", "text" if IS_LOCAL else "json").lower()

DATABASE_URL = os.getenv("DATABASE_URL")
DB_MIN_CONNECTIONS = int(os.getenv("DB_MIN_CONNECTIONS", "1"))
DB_MAX_CONNECTIONS = int(os.getenv("DB_MAX_CONNECTIONS", "2"))
DB_SSLMODE = os.getenv("DB_SSLMODE", "disable" if IS_LOCAL else "require")

WORKER_TIMEOUT = int(os.getenv("WORKER_TIMEOUT", "5" if IS_LOCAL else "10"))
HEALTH_CHECK_INTERVAL = int(
    os.getenv("HEALTH_CHECK_INTERVAL", "10" if IS_LOCAL else "30")
)
WORKER_STALE_ASSIGNMENT_SECONDS = int(
    os.getenv("WORKER_STALE_ASSIGNMENT_SECONDS", "3600" if not IS_LOCAL else "0")
)
WORKER_HOSTS = os.getenv("WORKER_HOSTS", "localhost:8765").split(",")
PUBLIC_WS_URL = os.getenv("PUBLIC_WS_URL", "")
WORKER_PORT = int(os.getenv("WORKER_PORT", "8765"))

# Public WebSocket URL construction (used when routing Twilio Media Streams to a worker)
# Twilio uses secure WebSockets; in production you generally need TLS termination on 443.
# Defaults are set up to work well with per-instance TLS termination using "<public_ip>.sslip.io".
WORKER_PUBLIC_WS_SCHEME = os.getenv("WORKER_PUBLIC_WS_SCHEME", "wss")
WORKER_PUBLIC_WS_PORT = int(
    os.getenv("WORKER_PUBLIC_WS_PORT", "443" if not IS_LOCAL else "8765")
)
WORKER_PUBLIC_WS_HOST_SUFFIX = os.getenv(
    "WORKER_PUBLIC_WS_HOST_SUFFIX",
    ".sslip.io" if not IS_LOCAL else "",
)
WORKER_PUBLIC_WS_HOST_TEMPLATE = os.getenv("WORKER_PUBLIC_WS_HOST_TEMPLATE", "")

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
WORKER_POOL_TAG = os.getenv("WORKER_POOL_TAG", "invorto-ai-worker")

# ── Worker Pool Type ──────────────────────────────────────────────────────────
# "ec2"   (default) → AWS EC2 tag-based discovery (production)
# "local"           → static WORKER_HOSTS list (local dev / docker-compose)
# "k8s"             → Kubernetes API pod discovery (EKS V2)
WORKER_POOL_TYPE = os.getenv("WORKER_POOL_TYPE", "ec2")

# K8s worker discovery — only used when WORKER_POOL_TYPE=k8s
WORKER_K8S_NAMESPACE = os.getenv("WORKER_K8S_NAMESPACE", "invorto")
WORKER_K8S_LABEL_SELECTOR = os.getenv(
    "WORKER_K8S_LABEL_SELECTOR", "app.kubernetes.io/name=invorto-worker"
)

# Redis shared-state backend — enables atomic cross-runner worker assignment.
# Leave REDIS_HOST unset for single-runner deployments (EC2 / local dev).
# Set REDIS_HOST for multi-runner EKS deployments.
REDIS_HOST = os.getenv("REDIS_HOST", "")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

PUBLIC_URL = os.getenv("PUBLIC_URL", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

RUNNER_PORT = int(os.getenv("PORT", "7860"))

# ── CORS ──────────────────────────────────────────────────────────────────────
# Comma-separated list of allowed origins.
# Local defaults cover Vite (8080) and common CRA/Next ports (5173, 3000).
# In production set CORS_ORIGINS to your actual UI domain(s).
CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:8080,http://localhost:5173,http://localhost:3000",
    ).split(",")
    if o.strip()
]

# ── WebSocket ─────────────────────────────────────────────────────────────────
# Comma-separated allowed origins for WebSocket connections.
# Empty = allow all (server-to-server connections from Twilio/Jambonz don't send Origin).
WS_ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in os.getenv("WS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# ── Authentication ────────────────────────────────────────────────────────────
# Global infra key — kept exclusively for /workers and internal SRE endpoints.
API_KEY = os.getenv("API_KEY", "")

# JWT — issued by POST /auth/login for key-management calls (/api-keys/*)
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(
    os.getenv("JWT_EXPIRE_MINUTES", "30")
)  # access token: short-lived
JWT_REFRESH_EXPIRE_DAYS = int(
    os.getenv("JWT_REFRESH_EXPIRE_DAYS", "30")
)  # refresh token: long-lived

# Org API key settings
API_KEY_PREFIX = os.getenv("API_KEY_PREFIX", "inv_")

# API_KEY also serves as the internal service key for trusted callers
# (Supabase edge functions, queue processors) that present X-API-Key = API_KEY
# plus X-Org-ID to act on behalf of an org. No separate RUNNER_SERVICE_KEY needed.

# Rate limiting (slowapi) — per-IP, Phase 1
RATE_LIMIT_LOGIN = os.getenv("RATE_LIMIT_LOGIN", "10/minute")
RATE_LIMIT_KEY_MGMT = os.getenv("RATE_LIMIT_KEY_MGMT", "30/minute")

# Jambonz Configuration (optional defaults, can be overridden per phone number)
JAMBONZ_API_URL = os.getenv("JAMBONZ_API_URL", "https://jambonz.cloud/api")
JAMBONZ_ACCOUNT_SID = os.getenv("JAMBONZ_ACCOUNT_SID", "")
JAMBONZ_API_KEY = os.getenv("JAMBONZ_API_KEY", "")
JAMBONZ_APPLICATION_SID = os.getenv("JAMBONZ_APPLICATION_SID", "")
JAMBONZ_WEBHOOK_SECRET = os.getenv("JAMBONZ_WEBHOOK_SECRET", "")

# Langfuse (optional - leave unset to disable tracing)
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_BASE_URL = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
LANGFUSE_TRACING_ENABLED = os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# Recording Storage (AWS S3) — used by Jambonz recording fetch
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
S3_REGION = os.getenv("S3_REGION", "ap-south-1")
S3_PRESIGNED_URL_EXPIRY = int(
    os.getenv("S3_PRESIGNED_URL_EXPIRY", str(7 * 24 * 3600))
)  # 7 days default
# MCube Configuration
MCUBE_API_URL = os.getenv("MCUBE_API_URL", "https://config.mcube.com/Restmcube-api")
MCUBE_AUTH_TOKEN = os.getenv("MCUBE_AUTH_TOKEN", "")  # Global fallback token

# Worker-to-runner shared secret for management endpoints (/cancel, /prewarm)
WORKER_AUTH_TOKEN = os.getenv("WORKER_AUTH_TOKEN", "")

# Call metrics: collect per-call performance metrics (latency, usage, TTFBs)
# stored in calls.metrics JSONB column after each call ends
ENABLE_CALL_METRICS = os.getenv("ENABLE_CALL_METRICS", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── Webhook Delivery ─────────────────────────────────────────────────────────
# Background worker in the runner delivers post-call-stat webhooks.
WEBHOOK_ENABLED = os.getenv("WEBHOOK_ENABLED", "true").lower() in ("true", "1", "yes")
WEBHOOK_POLL_INTERVAL_SECONDS = int(os.getenv("WEBHOOK_POLL_INTERVAL_SECONDS", "10"))
WEBHOOK_PENDING_FALLBACK_SECONDS = int(
    os.getenv("WEBHOOK_PENDING_FALLBACK_SECONDS", "30")
)
WEBHOOK_DELIVERY_TIMEOUT_SECONDS = int(
    os.getenv("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", "10")
)
WEBHOOK_MAX_ATTEMPTS = int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "4"))
WEBHOOK_BACKOFF_SECONDS = [
    int(s) for s in os.getenv("WEBHOOK_BACKOFF_SECONDS", "30,120,600").split(",")
]
WEBHOOK_CALLBACK_SECRET = os.getenv(
    "WEBHOOK_CALLBACK_SECRET", ""
)  # global HMAC fallback

# Recording fetch: retry backoff in seconds (no initial delay)
S3_RECORDING_FETCH_RETRY_DELAY = int(os.getenv("S3_RECORDING_FETCH_RETRY_DELAY", "30"))

# ── Direct file upload via POST /insights/analyse/upload (multipart/form-data) ─
# Supported formats: mp3, mp4, wav, ogg, flac, m4a
# Default 25 MB; hard ceiling 500 MB enforced in code regardless of env var.
RECORDING_UPLOAD_MAX_FILE_SIZE_MB: int = min(
    int(os.getenv("RECORDING_UPLOAD_MAX_FILE_SIZE_MB", "25")),
    500,
)
RECORDING_UPLOAD_ALLOWED_MIME_TYPES: list[str] = [
    t.strip()
    for t in os.getenv(
        "RECORDING_UPLOAD_ALLOWED_MIME_TYPES",
        "audio/mpeg,audio/mp4,video/mp4,audio/wav,audio/x-wav,"
        "audio/ogg,audio/flac,audio/x-flac,audio/m4a,audio/x-m4a",
    ).split(",")
    if t.strip()
]
# S3 key prefix: file_uploads/{org_id}/{job_id}.{ext}
RECORDING_UPLOAD_S3_PREFIX = os.getenv("RECORDING_UPLOAD_S3_PREFIX", "file_uploads")

# ── LLM Interruption Judge ───────────────────────────────────────────────────
# Model, timeout, word-count threshold, and system prompt for the
# BACKCHANNEL / INTERRUPT classifier that runs while the bot is speaking.
LLM_JUDGE_MODEL = os.getenv("LLM_JUDGE_MODEL", "gpt-4.1-nano")
LLM_JUDGE_TIMEOUT = float(os.getenv("LLM_JUDGE_TIMEOUT", "2.0"))
LLM_JUDGE_INSTANT_INTERRUPT_WORD_COUNT = int(
    os.getenv("LLM_JUDGE_INSTANT_INTERRUPT_WORD_COUNT", "5")
)
LLM_JUDGE_SYSTEM_PROMPT = os.getenv(
    "LLM_JUDGE_SYSTEM_PROMPT",
    "You classify user speech during a voice call as BACKCHANNEL or INTERRUPT.\n\n"
    "BACKCHANNEL: acknowledgements, filler words, listening signals that do not "
    "require a response.\n"
    "Examples: ok, hmm, haan, haanji, accha, right, yeah, uh-huh, I see, go on, "
    "theek hai, sure, mhm, ha, ji, okay, alright, got it, yes, hello\n\n"
    "INTERRUPT: questions, disagreements, new topics, requests to stop or repeat, "
    "corrections, or anything that demands the bot respond differently.\n"
    "Examples: wait, stop, what did you say, no that's wrong, I have a question, "
    "can you repeat that, actually, listen, hold on, ruko, nahi\n\n"
    "The user said this {stt_output}\n\n while the bot was {bot_status}.\n\n"
    "Reply with exactly one word: BACKCHANNEL or INTERRUPT",
)

# ── OpenTelemetry ─────────────────────────────────────────────────────────────
# Leave OTLP_ENDPOINT unset to disable OTEL export (local dev default).
# For Coralogix AP1 (direct export, no collector):
#   OTLP_ENDPOINT=https://ingress.coralogix.in:443
#   OTLP_HEADERS=Authorization=Bearer <SendDataKey>,CX-Application-Name=invorto,CX-Subsystem-Name=runner
# For EKS with OTEL Collector sidecar:
#   OTLP_ENDPOINT=http://localhost:4318
#   OTEL_USE_COLLECTOR=true  (collector owns auth — omit OTLP_HEADERS)
OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "")
OTLP_HEADERS = os.getenv("OTLP_HEADERS", "")
OTEL_USE_COLLECTOR = os.getenv("OTEL_USE_COLLECTOR", "false").lower() == "true"
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "")
