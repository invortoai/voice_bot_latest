from typing import Optional, Tuple

from loguru import logger

from app.core.database import get_cursor


def get_assistant_by_id(assistant_id: str) -> Optional[dict]:
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT * FROM assistants 
                WHERE id = %s AND is_active = true
                """,
                (assistant_id,),
            )
            row = cur.fetchone()
            if row:
                result = dict(row)
                logger.debug(f"Fetched assistant config for {assistant_id}")
                return result
            else:
                logger.warning(f"Assistant not found or inactive: {assistant_id}")
                return None
    except Exception as e:
        logger.error(f"Error fetching assistant {assistant_id}: {e}")
        return None


def get_phone_number_config(phone_number: str) -> Optional[dict]:
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT * FROM phone_numbers 
                WHERE phone_number = %s AND is_active = true
                """,
                (phone_number,),
            )
            row = cur.fetchone()
            if row:
                result = dict(row)
                logger.debug(f"Fetched phone config for {phone_number}")
                return result
            else:
                logger.warning(f"Phone number not found or inactive: {phone_number}")
                return None
    except Exception as e:
        logger.error(f"Error fetching phone number config {phone_number}: {e}")
        return None


def get_inbound_call_config(
    called_number: str,
) -> Tuple[Optional[dict], Optional[dict]]:
    phone_config = get_phone_number_config(called_number)
    if not phone_config:
        logger.error(f"No phone number config found for: {called_number}")
        return None, None

    if not phone_config.get("is_inbound_enabled", True):
        logger.warning(f"Inbound calls disabled for: {called_number}")
        return None, None

    assistant_id = phone_config.get("assistant_id")
    if not assistant_id:
        logger.error(f"No assistant configured for phone number: {called_number}")
        return phone_config, None

    assistant_config = get_assistant_by_id(str(assistant_id))
    if not assistant_config:
        logger.error(f"Failed to fetch assistant config for: {assistant_id}")
        return phone_config, None

    logger.info(
        f"Loaded inbound call config: phone={called_number}, "
        f"assistant={assistant_config.get('name', assistant_id)}"
    )
    return phone_config, assistant_config
