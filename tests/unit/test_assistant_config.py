"""Unit tests for AssistantConfig (app/worker/config.py)."""

import pytest

from app.worker.config import AssistantConfig


def _make_config(overrides=None, phone_overrides=None, custom_overrides=None):
    """Build a minimal AssistantConfig with sensible defaults."""
    assistant = {
        "id": "asst-001",
        "system_prompt": "You are helpful.",
        "llm_provider": "openai",
        "model": "gpt-4.1-nano",
        "llm_settings": {"temperature": 0.7, "max_completion_tokens": 150},
        "greeting_message": "Hello!",
        "end_call_phrases": ["goodbye", "bye"],
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "voice_model": "eleven_flash_v2_5",
        "voice_settings": {},
        "transcriber_provider": "deepgram",
        "transcriber_model": "nova-2",
        "transcriber_language": "en",
        "transcriber_settings": {},
        "vad_settings": {},
    }
    phone = {
        "provider_credentials": {"account_sid": "AC123", "auth_token": "secret"},
        "max_call_duration_seconds": 3600,
    }
    custom = {"call_sid": "CA123", "call_type": "inbound", "caller": "+14155551234"}

    assistant.update(overrides or {})
    phone.update(phone_overrides or {})
    custom.update(custom_overrides or {})

    return AssistantConfig(
        custom_params=custom,
        assistant_config=assistant,
        phone_config=phone,
    )


class TestAssistantConfigBasics:
    def test_loads_model_and_system_prompt(self):
        cfg = _make_config()
        assert cfg.llm_provider == "openai"
        assert cfg.model == "gpt-4.1-nano"
        assert cfg.system_prompt == "You are helpful."

    def test_temperature_float_normal(self):
        cfg = _make_config({"llm_settings": {"temperature": 0.5}})
        assert cfg.temperature == 0.5

    def test_temperature_zero_not_treated_as_falsy(self):
        """temperature=0.0 must be honoured, not replaced by the default."""
        cfg = _make_config({"llm_settings": {"temperature": 0.0}})
        assert cfg.temperature == 0.0

    def test_temperature_none_uses_default(self):
        cfg = _make_config({"llm_settings": {}})
        assert cfg.temperature == 0.7

    def test_max_completion_tokens(self):
        cfg = _make_config({"llm_settings": {"max_completion_tokens": 300}})
        assert cfg.max_completion_tokens == 300

    def test_max_completion_tokens_default_when_absent(self):
        cfg = _make_config({"llm_settings": {}})
        assert cfg.max_completion_tokens == 150

    def test_max_completion_tokens_default_when_llm_settings_null(self):
        cfg = _make_config({"llm_settings": None})
        assert cfg.max_completion_tokens == 150

    def test_service_tier(self):
        cfg = _make_config({"llm_settings": {"service_tier": "priority"}})
        assert cfg.service_tier == "priority"

    def test_service_tier_none_when_absent(self):
        cfg = _make_config()
        assert cfg.service_tier is None

    def test_greeting_message(self):
        cfg = _make_config({"greeting_message": "Hi there!"})
        assert cfg.get_greeting() == "Hi there!"

    def test_empty_greeting_returns_empty_string(self):
        cfg = _make_config({"greeting_message": None})
        assert cfg.get_greeting() == ""

    def test_end_call_phrases_stripped(self):
        cfg = _make_config({"end_call_phrases": ["  goodbye  ", " bye ", ""]})
        assert cfg.end_call_phrases == ["goodbye", "bye"]

    def test_end_call_phrases_none(self):
        cfg = _make_config({"end_call_phrases": None})
        assert cfg.end_call_phrases == []

    def test_vad_settings_loaded(self):
        cfg = _make_config({"vad_settings": {"confidence": 0.8}})
        assert cfg.vad_settings["confidence"] == 0.8

    def test_vad_settings_json_string_parsed(self):
        """vad_settings stored as JSON string should be parsed."""
        import json

        cfg = _make_config({"vad_settings": json.dumps({"stop_secs": 1.2})})
        assert cfg.vad_settings["stop_secs"] == pytest.approx(1.2)

    def test_vad_settings_none_defaults_to_empty_dict(self):
        cfg = _make_config({"vad_settings": None})
        assert cfg.vad_settings == {}


class TestAssistantConfigSystemMessage:
    def test_inbound_includes_caller(self):
        cfg = _make_config(
            custom_overrides={"call_type": "inbound", "caller": "+14155551234"}
        )
        msg = cfg.get_system_message()
        assert "+14155551234" in msg

    def test_inbound_unknown_caller_not_included(self):
        cfg = _make_config(
            custom_overrides={"call_type": "inbound", "caller": "Unknown"}
        )
        msg = cfg.get_system_message()
        assert "Unknown" not in msg

    def test_outbound_includes_direction_note(self):
        cfg = _make_config(
            custom_overrides={"call_type": "outbound", "to_number": "+15005550006"}
        )
        msg = cfg.get_system_message()
        assert "outbound" in msg.lower()
        assert "+15005550006" in msg

    def test_system_prompt_always_present(self):
        cfg = _make_config({"system_prompt": "My custom prompt."})
        msg = cfg.get_system_message()
        assert msg.startswith("My custom prompt.")


class TestAssistantConfigPhoneConfig:
    def test_twilio_credentials_loaded(self):
        cfg = _make_config(
            phone_overrides={
                "provider_credentials": {
                    "account_sid": "AC_ABC",
                    "auth_token": "TOKEN_XYZ",
                }
            }
        )
        assert cfg.twilio_account_sid == "AC_ABC"
        assert cfg.twilio_auth_token == "TOKEN_XYZ"

    def test_missing_credentials_are_none(self):
        cfg = _make_config(phone_overrides={"provider_credentials": {}})
        assert cfg.twilio_account_sid is None
        assert cfg.twilio_auth_token is None

    def test_max_call_duration(self):
        cfg = _make_config(phone_overrides={"max_call_duration_seconds": 1800})
        assert cfg.max_call_duration == 1800
