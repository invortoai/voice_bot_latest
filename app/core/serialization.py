"""JSON serialization helpers for psycopg2 types.

psycopg2 with RealDictCursor returns Python types that json.dumps() cannot
handle: Decimal, datetime, UUID, memoryview, timedelta.  Use json_safe()
to recursively convert a dict (or nested structure) before passing it to
any HTTP client or JSON serializer.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from loguru import logger


def json_safe(obj):
    """Recursively convert a psycopg2 result dict to JSON-serializable types.

    Handles every type that PostgreSQL + psycopg2 RealDictCursor can produce:
      - Decimal     -> float
      - datetime    -> ISO-8601 string
      - date        -> ISO-8601 string
      - timedelta   -> total seconds (float)
      - UUID        -> string
      - memoryview  -> bytes -> hex string (bytea columns)
      - bytes       -> hex string
      - set         -> list
      - dict/list   -> recurse
      - everything else passes through (str, int, float, bool, None are safe)
    """
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(i) for i in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, memoryview):
        return obj.tobytes().hex()
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, set):
        return [json_safe(i) for i in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # Fallback: warn and coerce to string so json.dumps() never raises
    logger.warning(
        f"json_safe: unexpected type {type(obj).__name__!r}, coercing to str"
    )
    return str(obj)
