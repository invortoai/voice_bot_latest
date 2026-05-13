-- Migration 002: Silence nudge + bot_speaks_first fields on assistants
-- Adds per-assistant silence detection and greeting control columns.

ALTER TABLE assistants
    ADD COLUMN IF NOT EXISTS silence_response_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS silence_timeout_seconds   INT     NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS silence_response_type     TEXT    NOT NULL DEFAULT 'static',
    ADD COLUMN IF NOT EXISTS silence_response_message  TEXT,
    ADD COLUMN IF NOT EXISTS bot_speaks_first          BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN assistants.silence_response_enabled IS
    'When TRUE the worker fires a nudge after silence_timeout_seconds of user silence.';
COMMENT ON COLUMN assistants.silence_timeout_seconds IS
    'Seconds of user silence before the nudge fires. Default 5.';
COMMENT ON COLUMN assistants.silence_response_type IS
    '''static'' — speak silence_response_message verbatim; ''ai_generated'' — let LLM compose the nudge.';
COMMENT ON COLUMN assistants.silence_response_message IS
    'Message spoken verbatim when silence_response_type=''static''.';
COMMENT ON COLUMN assistants.bot_speaks_first IS
    'When TRUE (default) the bot plays the greeting on connect. When FALSE the user speaks first.';
