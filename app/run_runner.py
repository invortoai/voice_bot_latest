#!/usr/bin/env python3

import uvicorn
from loguru import logger

from app.config import RUNNER_PORT, ENVIRONMENT, IS_LOCAL
from app.core.log_setup import setup_logging


def main():
    setup_logging("runner", environment=ENVIRONMENT)

    logger.info(f"Starting bot runner on port {RUNNER_PORT} ({ENVIRONMENT} mode)")

    if IS_LOCAL:
        logger.info(
            f"Configure Twilio webhook: http://localhost:{RUNNER_PORT}/twilio/incoming"
        )
        logger.info("Use ngrok for external access: ngrok http 7860")

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # nosec B104
        port=RUNNER_PORT,
        log_level="info",
        reload=IS_LOCAL,
    )


if __name__ == "__main__":
    main()
