"""
Rate limiter instance (slowapi, Phase 1 — per-IP).

Register in main.py:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

Phase 2 upgrade path: replace get_remote_address with a function that
reads key_id from the resolved org context for true per-key limiting.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
