from .assistants import router as assistants_router
from .phone_numbers import router as phone_numbers_router
from .calls import router as calls_router
from .call_stats import router as call_stats_router
from .twilio import router as twilio_router
from .jambonz import router as jambonz_router
from .mcube import router as mcube_router
from .workers import router as workers_router
from .auth import router as auth_router
from .api_keys import router as api_keys_router

__all__ = [
    "assistants_router",
    "phone_numbers_router",
    "calls_router",
    "call_stats_router",
    "twilio_router",
    "jambonz_router",
    "mcube_router",
    "workers_router",
    "auth_router",
    "api_keys_router",
]
