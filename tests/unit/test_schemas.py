"""Unit tests for Pydantic request/response schemas."""

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    AssistantCreate,
    AssistantUpdate,
    McubeConnectWebhookRequest,
    JambonzWebhookRequest,
    OutboundCallRequest,
)


class TestAssistantCreate:
    def test_minimal_required_fields(self):
        a = AssistantCreate(name="Bot", system_prompt="You are helpful.")
        assert a.name == "Bot"
        assert a.llm_provider == "openai"  # default
        assert a.model == "gpt-4.1-nano"  # default
        assert a.voice_provider == "elevenlabs"  # default

    def test_llm_settings_defaults(self):
        a = AssistantCreate(name="Bot", system_prompt="You are helpful.")
        assert a.llm_settings["temperature"] == pytest.approx(0.7)
        assert a.llm_settings["max_completion_tokens"] == 150

    def test_vad_settings_accepted(self):
        a = AssistantCreate(
            name="Bot",
            system_prompt="You are helpful.",
            vad_settings={"confidence": 0.9},
        )
        assert a.vad_settings == {"confidence": 0.9}

    def test_end_call_phrases_list(self):
        a = AssistantCreate(
            name="Bot",
            system_prompt="x",
            end_call_phrases=["goodbye", "bye"],
        )
        assert a.end_call_phrases == ["goodbye", "bye"]

    def test_missing_required_fields_raises(self):
        with pytest.raises(ValidationError):
            AssistantCreate(name="Bot")  # missing system_prompt


class TestAssistantUpdate:
    def test_all_optional(self):
        u = AssistantUpdate()
        assert u.name is None
        assert u.system_prompt is None
        assert u.vad_settings is None

    def test_partial_update(self):
        u = AssistantUpdate(name="New Name", llm_settings={"temperature": 0.5})
        assert u.name == "New Name"
        assert u.llm_settings["temperature"] == pytest.approx(0.5)
        assert u.model is None


class TestMcubeConnectWebhookRequest:
    def test_camelCase_to_snake_case_mapping(self):
        data = {
            "callId": "CALL-001",
            "callDirection": "inbound",
            "fromNumber": "+14155551234",
            "toNumber": "+18001234567",
            "dialStatus": "CONNECTING",
        }
        req = McubeConnectWebhookRequest(**data)
        assert req.call_id == "CALL-001"
        assert req.call_direction == "inbound"
        assert req.from_number == "+14155551234"
        assert req.to_number == "+18001234567"
        assert req.dial_status == "CONNECTING"

    def test_snake_case_also_accepted(self):
        data = {
            "call_id": "CALL-002",
            "dial_status": "BUSY",
        }
        req = McubeConnectWebhookRequest(**data)
        assert req.call_id == "CALL-002"
        assert req.dial_status == "BUSY"

    def test_call_id_required(self):
        with pytest.raises(ValidationError):
            McubeConnectWebhookRequest()

    def test_optional_fields_default_none(self):
        req = McubeConnectWebhookRequest(call_id="X")
        assert req.call_direction is None
        assert req.from_number is None
        assert req.dial_status is None


class TestJambonzWebhookRequest:
    def test_camelCase_mapping(self):
        data = {
            "callSid": "JSID-001",
            "from": "+14155551234",
            "to": "+18001234567",
        }
        req = JambonzWebhookRequest(**data)
        assert req.call_sid == "JSID-001"
        assert req.from_number == "+14155551234"
        assert req.to_number == "+18001234567"

    def test_all_optional(self):
        req = JambonzWebhookRequest()
        assert req.call_sid is None


_PHONE_ID = "11111111-1111-1111-1111-111111111111"
_ASST_ID = "22222222-2222-2222-2222-222222222222"
_CALL_UUID = "550e8400-e29b-41d4-a716-446655440000"


class TestOutboundCallRequest:
    def test_required_fields(self):
        req = OutboundCallRequest(
            phone_number_id=_PHONE_ID,
            assistant_id=_ASST_ID,
            to_number="+15005550006",
        )
        assert str(req.phone_number_id) == _PHONE_ID
        assert str(req.assistant_id) == _ASST_ID
        assert req.to_number == "+15005550006"
        assert req.custom_params == {}

    def test_custom_params_optional(self):
        req = OutboundCallRequest(
            phone_number_id=_PHONE_ID,
            assistant_id=_ASST_ID,
            to_number="+15005550006",
            custom_params={"key": "value"},
        )
        assert req.custom_params == {"key": "value"}

    def test_call_id_defaults_to_none(self):
        req = OutboundCallRequest(
            phone_number_id=_PHONE_ID,
            assistant_id=_ASST_ID,
            to_number="+15005550006",
        )
        assert req.call_id is None

    def test_call_id_accepted_as_uuid_string(self):
        req = OutboundCallRequest(
            phone_number_id=_PHONE_ID,
            assistant_id=_ASST_ID,
            to_number="+15005550006",
            call_id=_CALL_UUID,
        )
        assert str(req.call_id) == _CALL_UUID

    def test_call_id_non_uuid_rejected(self):
        """call_id must be a UUID; non-UUID strings raise ValidationError."""
        with pytest.raises(ValidationError):
            OutboundCallRequest(
                phone_number_id=_PHONE_ID,
                assistant_id=_ASST_ID,
                to_number="+15005550006",
                call_id="custom-id-not-a-uuid",
            )

    def test_call_id_and_custom_params_together(self):
        call_id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
        req = OutboundCallRequest(
            phone_number_id=_PHONE_ID,
            assistant_id=_ASST_ID,
            to_number="+919999999999",
            call_id=call_id,
            custom_params={"first_name": "Rahul", "lead_id": "LEAD001"},
        )
        assert str(req.call_id) == call_id
        assert req.custom_params["first_name"] == "Rahul"
        assert req.custom_params["lead_id"] == "LEAD001"
