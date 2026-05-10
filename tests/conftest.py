"""
Shared test fixtures: Postgres testcontainer, DB patching, table cleanup,
and runner HTTP client.

IMPORTANT: env overrides at the top of this file run before any app module is
imported (conftest.py is the first file pytest processes). This ensures that
`load_dotenv()` in app/config.py sees our test values and does NOT override
them from the .env file.
"""

import glob
import hashlib
import os
import secrets

# Override env vars before any app import so that app/config.py picks them up.
# load_dotenv(override=False) does not overwrite keys already present in os.environ.
os.environ.setdefault(
    "ENVIRONMENT", "local"
)  # IS_LOCAL=True → API_KEY="" disables auth
os.environ.setdefault("API_KEY", "")
os.environ["API_KEY"] = ""  # force empty → global auth disabled in tests
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("WORKER_HOSTS", "localhost:8765")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-pytest")
# Disable effective rate-limiting in tests — all requests share 127.0.0.1
os.environ.setdefault("RATE_LIMIT_LOGIN", "1000/minute")
os.environ.setdefault("RATE_LIMIT_KEY_MGMT", "1000/minute")

import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer


# ---------------------------------------------------------------------------
# Session-scoped Postgres container
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Start a Postgres container for the entire test session.

    After the container is up:
    - Patches app.core.database.DATABASE_URL to point at the container.
    - Resets the lazy connection pool so it is re-created on first use.
    - Runs all SQL migrations in order from db/supabase/migrations/.

    Yields the container DSN so other session fixtures can connect directly.
    """
    with PostgresContainer("postgres:15-alpine") as pg:
        dsn = (
            f"postgresql://{pg.username}:{pg.password}"
            f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
            f"?sslmode=disable"
        )

        # Patch the module-level DATABASE_URL binding *before* any DB call.
        import app.core.database as db_module

        original_url = db_module.DATABASE_URL
        original_pool = db_module._pool
        original_sslmode = db_module.DB_SSLMODE

        db_module.DATABASE_URL = dsn
        db_module.DB_SSLMODE = "disable"  # testcontainer postgres has no SSL
        db_module._pool = None  # Force pool re-creation with new URL

        _run_migrations(dsn)

        yield dsn

        # Restore original values (important when running multiple sessions in
        # a single process, e.g., under watch-mode).
        db_module.DATABASE_URL = original_url
        db_module.DB_SSLMODE = original_sslmode
        db_module._pool = original_pool


def _run_migrations(dsn: str) -> None:
    """Run every SQL migration file in sorted order against *dsn*.

    Primary location: db/supabase/migrations/ (git submodule — full Supabase history).
    CI fallback:      schema.sql (base schema) + migrations/*.sql (app-level migrations)
                      used when the Bitbucket submodule is not available.

    Each statement is executed individually so that Supabase-specific DDL
    (GRANT to anon/authenticated roles, INSERT into storage.buckets, etc.)
    can fail gracefully without blocking the rest of the migration file.
    """
    repo_root = os.path.dirname(os.path.dirname(__file__))
    pattern = os.path.join(repo_root, "db", "supabase", "migrations", "*.sql")
    migration_files = sorted(glob.glob(pattern))

    if not migration_files:
        # Submodule not initialised (e.g. CI without Bitbucket access).
        # Fall back to schema.sql + migrations/*.sql checked into this repo.
        fallback_schema = os.path.join(repo_root, "schema.sql")
        fallback_migrations = sorted(
            glob.glob(os.path.join(repo_root, "migrations", "*.sql"))
        )
        migration_files = []
        if os.path.exists(fallback_schema):
            migration_files.append(fallback_schema)
        migration_files.extend(fallback_migrations)

    if not migration_files:
        raise RuntimeError(
            f"No migration files found at {pattern} or schema.sql/migrations/. "
            "Make sure the db submodule is initialised: `git submodule update --init`"
        )

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        for path in migration_files:
            with open(path) as fh:
                sql = fh.read()
            for stmt in _split_sql_statements(sql):
                try:
                    cur.execute(stmt)
                except Exception as exc:
                    # Supabase-specific DDL (storage.buckets, GRANTs to
                    # anon/authenticated) fails harmlessly in plain Postgres.
                    print(
                        f"[migration] {os.path.basename(path)}: "
                        f"{type(exc).__name__}: {str(exc)[:120]}"
                    )
    finally:
        cur.close()
        conn.close()


def _split_sql_statements(sql: str) -> list:
    """Split a SQL script into individual statements.

    Handles:
    - Dollar-quoted strings  ($$...$$, $body$...$body$)
    - Single-quoted strings  ('...')
    - Line comments          (-- ...)
    - Block comments         (/* ... */)

    Statements are delimited by a semicolon that appears outside all quoted
    contexts.  Empty / whitespace-only strings are dropped.
    """
    statements: list = []
    current: list = []
    i = 0
    n = len(sql)
    in_single_quote = False
    in_dollar_quote = False
    dollar_tag = ""
    in_line_comment = False
    in_block_comment = False

    while i < n:
        ch = sql[i]

        # ── line comment ──────────────────────────────────────────────────────
        if not in_single_quote and not in_dollar_quote and not in_block_comment:
            if ch == "-" and i + 1 < n and sql[i + 1] == "-":
                in_line_comment = True

        if in_line_comment:
            current.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        # ── block comment ─────────────────────────────────────────────────────
        if not in_single_quote and not in_dollar_quote and not in_line_comment:
            if ch == "/" and i + 1 < n and sql[i + 1] == "*":
                in_block_comment = True

        if in_block_comment:
            current.append(ch)
            if ch == "*" and i + 1 < n and sql[i + 1] == "/":
                current.append(sql[i + 1])
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue

        # ── single-quoted string ──────────────────────────────────────────────
        if not in_dollar_quote and ch == "'":
            current.append(ch)
            if in_single_quote:
                # '' is an escaped single quote inside a string
                if i + 1 < n and sql[i + 1] == "'":
                    current.append(sql[i + 1])
                    i += 2
                    continue
                in_single_quote = False
            else:
                in_single_quote = True
            i += 1
            continue

        # ── dollar-quoted string ──────────────────────────────────────────────
        if not in_single_quote and ch == "$":
            if in_dollar_quote:
                # Check for closing tag
                closing = dollar_tag
                if sql[i : i + len(closing)] == closing:
                    current.append(closing)
                    i += len(closing)
                    in_dollar_quote = False
                    dollar_tag = ""
                    continue
            else:
                # Check for opening dollar-quote tag  ($tag$ or $$)
                end = sql.find("$", i + 1)
                if end != -1:
                    tag = sql[i : end + 1]
                    inner = tag[1:-1]
                    if all(c.isalnum() or c == "_" for c in inner):
                        in_dollar_quote = True
                        dollar_tag = tag
                        current.append(tag)
                        i += len(tag)
                        continue

        # ── statement separator ───────────────────────────────────────────────
        if not in_single_quote and not in_dollar_quote and ch == ";":
            current.append(ch)
            stmt = "".join(current).strip()
            if stmt and stmt != ";":
                statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    # Remaining text after the last semicolon
    remaining = "".join(current).strip()
    if remaining and remaining != ";":
        statements.append(remaining)

    return statements


# ---------------------------------------------------------------------------
# Session-scoped test tenant (org + admin user + API key)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_tenant(pg_container):
    """Seed a test organisation, admin user, and API key for the entire session.

    Returns a dict with org_id, user_id, api_key (raw), key_prefix, key_hash,
    email, password_hash, and password — everything tests need to authenticate
    via X-API-Key or JWT Bearer.
    """
    # Generate a cryptographically-strong API key inline (avoids importing
    # app.core.auth which triggers FastAPI/Pydantic initialisation side-effects).
    raw_key = f"inv_{secrets.token_urlsafe(32)}"
    key_prefix = raw_key[:8]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    test_password = "test-password-secret-123"
    password_hash = hashlib.sha256(test_password.encode()).hexdigest()

    conn = psycopg2.connect(pg_container)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        # Create test organisation
        cur.execute(
            """
            INSERT INTO organizations (name, org_type, is_active)
            VALUES ('Test Org', 'demo', TRUE)
            RETURNING id
            """
        )
        org_id = str(cur.fetchone()[0])

        # Create admin user for JWT/login tests
        cur.execute(
            """
            INSERT INTO org_users (org_id, email, name, role, is_active, password_hash)
            VALUES (%s, 'admin@testorg.com', 'Test Admin', 'admin', TRUE, %s)
            RETURNING id
            """,
            (org_id, password_hash),
        )
        user_id = str(cur.fetchone()[0])

        # Create the test API key (used as X-API-Key header in all data-endpoint tests)
        cur.execute(
            """
            INSERT INTO org_api_keys (org_id, name, key_prefix, key_hash, is_active, scopes)
            VALUES (%s, 'test-key', %s, %s, TRUE, '[]'::jsonb)
            """,
            (org_id, key_prefix, key_hash),
        )
    finally:
        cur.close()
        conn.close()

    return {
        "org_id": org_id,
        "user_id": user_id,
        "api_key": raw_key,
        "key_prefix": key_prefix,
        "key_hash": key_hash,
        "email": "admin@testorg.com",
        "password_hash": password_hash,
        "password": test_password,
    }


@pytest.fixture(scope="session")
def test_org_id(test_tenant):
    """Return the UUID of the session-level test organisation."""
    return test_tenant["org_id"]


@pytest.fixture(scope="session")
def test_api_key(test_tenant):
    """Return the raw API key for the session-level test organisation."""
    return test_tenant["api_key"]


# ---------------------------------------------------------------------------
# Per-test table cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_tables(pg_container, test_tenant):
    """Delete all test data before every test for full isolation.

    Uses DELETE (not TRUNCATE CASCADE) because organizations.default_bot_id
    references assistants — TRUNCATE assistants CASCADE would drop the org row.
    Deletes in FK-safe order: calls → phone_numbers → assistants.
    Resets org_api_keys to exactly the one session test key.
    """
    with psycopg2.connect(pg_container) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Delete in FK order so no child rows block parent deletes.
            cur.execute("DELETE FROM refresh_tokens")
            # webhook_deliveries → call_requests (FK)
            cur.execute("DELETE FROM webhook_deliveries")
            cur.execute("DELETE FROM call_requests")
            cur.execute("DELETE FROM campaign_phone_numbers")
            cur.execute("DELETE FROM calls")
            cur.execute("DELETE FROM phone_numbers")
            # insights: call_analysis_jobs → call_analysis → insights_config
            cur.execute("DELETE FROM call_analysis")
            cur.execute("DELETE FROM insights_config")
            cur.execute("DELETE FROM assistants")
            # Wipe API keys (cascades to org_api_key_audit_logs via ON DELETE CASCADE)
            cur.execute("DELETE FROM org_api_keys")
            # Restore the session test key
            cur.execute(
                """
                INSERT INTO org_api_keys (org_id, name, key_prefix, key_hash, is_active, scopes)
                VALUES (%s, 'test-key', %s, %s, TRUE, '[]'::jsonb)
                """,
                (
                    test_tenant["org_id"],
                    test_tenant["key_prefix"],
                    test_tenant["key_hash"],
                ),
            )
    yield


# ---------------------------------------------------------------------------
# Runner HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture
async def runner_client(pg_container, test_tenant):
    """Async HTTP client wired directly to the FastAPI runner app.

    Includes both X-API-Key (per-org key for verify_customer_api_key endpoints)
    and X-Org-ID (for verify_global_key_with_org endpoints, which bypass key
    validation when API_KEY is empty in tests).

    The ASGI lifespan is intentionally NOT run so that we avoid
    worker-pool health-check tasks and EC2 discovery. DB pool is
    initialised lazily on first request via the patched DATABASE_URL.
    """
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={
            "X-API-Key": test_tenant["api_key"],
            "X-Org-ID": test_tenant["org_id"],
        },
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Mock worker helper
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_worker():
    """Return a WorkerStatus instance pre-configured for tests."""
    from app.services.worker_pool import WorkerStatus

    return WorkerStatus(host="localhost:8765", instance_id="test-worker-1")


@pytest.fixture
def worker_in_pool(mock_worker):
    """Add a fake worker to the shared pool and clean up afterwards."""
    from app.services.worker_pool import worker_pool

    worker_pool.workers[mock_worker.instance_id] = mock_worker
    yield mock_worker
    worker_pool.workers.pop(mock_worker.instance_id, None)
    # Reset worker state in case a test assigned a call to it
    mock_worker.current_call_sid = None
    mock_worker.assigned_at = None
