#!/usr/bin/env python3

import uvicorn
from loguru import logger

from app.config import WORKER_PORT, IS_LOCAL, ENVIRONMENT
from app.core.log_setup import setup_logging


def main():
    setup_logging("worker", environment=ENVIRONMENT)

    logger.info(f"Starting bot worker on port {WORKER_PORT}")

    uvicorn.run(
        "app.worker.main:app",
        host="0.0.0.0",  # nosec B104
        port=WORKER_PORT,
        log_level="info",
        reload=IS_LOCAL,
    )


if __name__ == "__main__":
    main()
