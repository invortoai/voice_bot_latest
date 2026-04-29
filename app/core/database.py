from typing import Optional
from contextlib import contextmanager

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from loguru import logger

from app.config import DATABASE_URL, DB_MIN_CONNECTIONS, DB_MAX_CONNECTIONS, DB_SSLMODE


_pool: Optional[ThreadedConnectionPool] = None


def get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL environment variable is not set")
        kwargs = {}
        if DB_SSLMODE and DB_SSLMODE != "disable":
            kwargs["sslmode"] = DB_SSLMODE
        _pool = ThreadedConnectionPool(
            DB_MIN_CONNECTIONS, DB_MAX_CONNECTIONS, DATABASE_URL, **kwargs
        )
        logger.info(
            f"Database connection pool created (min={DB_MIN_CONNECTIONS}, max={DB_MAX_CONNECTIONS})"
        )
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("Database connection pool closed")


@contextmanager
def get_connection():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(dict_cursor: bool = True):
    with get_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            yield cur
