"""Unit tests for Jambonz webhook schemas — extended coverage.

Covers JambonzStatusWebhookRequest, extended JambonzWebhookRequest scenarios,
and JambonzAmdWebhookRequest.
"""

import json
from app.models.schemas import (
    JambonzStatusWebhookRequest,
    JambonzWebhookRequest,
    JambonzAmdWebhookRequest,
)


class TestJambonzStatusWebhookRequest:
    """Tests for the status callback schema (used by /jambonz/status)."""

    def test_camelCase_fields_mapped(self):
        data = {
            "callSid": "JSID-STATUS-001",
            "callStatus": "completed",
            "from": "+14155551234",
            "to": "+18001234567",
            "duration": 120,
        }
        req = JambonzStatusWebhookRequest(**data)
        assert req.call_sid == "JSID-STATUS-001"
        assert req.call_status == "completed"
        assert req.from_number == "+14155551234"
        assert req.to_number == "+18001234567"
        assert req.duration == 120

    def test_all_fields_optional(self):
        req = JambonzStatusWebhookRequest()
        assert req.call_sid is None
        assert req.call_status is None
        assert req.duration is None
        assert req.direction is None

    def test_sip_status_and_reason_mapped(self):
        data = {"sipStatus": 200, "sipReason": "OK"}
        req = JambonzStatusWebhookRequest(**data)
        assert req.sip_status == 200
        assert req.sip_reason == "OK"

    def test_call_termination_by_mapped(self):
        data = {"callTerminationBy": "caller"}
        req = JambonzStatusWebhookRequest(**data)
        assert req.call_termination_by == "caller"

    def test_snake_case_also_accepted(self):
        data = {"call_sid": "JSID-002", "call_status": "failed", "duration": 0}
        req = JambonzStatusWebhookRequest(**data)
        assert req.call_sid == "JSID-002"
        assert req.call_status == "failed"
        assert req.duration == 0

    def test_zero_duration_accepted(self):
        """duration=0 is a valid value (very short call), must not be dropped."""
        data = {"callSid": "JSID-003", "duration": 0}
        req = JambonzStatusWebhookRequest(**data)
        assert req.duration == 0

    def test_account_and_application_sid_mapped(self):
        data = {"accountSid": "ACC-001", "applicationSid": "APP-001"}
        req = JambonzStatusWebhookRequest(**data)
        assert req.account_sid == "ACC-001"
        assert req.application_sid == "APP-001"

    def test_originating_sip_fields_mapped(self):
        data = {
            "originatingSipIp": "192.168.1.1",
            "originatingSipTrunkName": "trunk-1",
        }
        req = JambonzStatusWebhookRequest(**data)
        assert req.originating_sip_ip == "192.168.1.1"
        assert req.originating_sip_trunk_name == "trunk-1"

    def test_terminal_statuses_accepted(self):
        for status in [
            "completed",
            "ended",
            "hangup",
            "failed",
            "busy",
            "no-answer",
            "canceled",
        ]:
            req = JambonzStatusWebhookRequest(call_sid="S1", call_status=status)
            assert req.call_status == status

    def test_non_terminal_statuses_accepted(self):
        for status in ["in-progress", "ringing", "trying", "early-media"]:
            req = JambonzStatusWebhookRequest(call_sid="S2", call_status=status)
            assert req.call_status == status


class TestJambonzWebhookRequestExtended:
    """Extended tests for the call webhook schema (used by /jambonz/call)."""

    def test_customer_data_as_dict(self):
        data = {
            "callSid": "JSID-003",
            "customerData": {"assistant_id": "asst-001", "call_type": "outbound"},
        }
        req = JambonzWebhookRequest(**data)
        assert req.customer_data == {
            "assistant_id": "asst-001",
            "call_type": "outbound",
        }

    def test_customer_data_as_json_string(self):
        """Jambonz may send customerData as a JSON-encoded string."""
        tag_str = json.dumps({"assistant_id": "asst-001"})
        data = {"callSid": "JSID-004", "customerData": tag_str}
        req = JambonzWebhookRequest(**data)
        assert req.customer_data == tag_str

    def test_tag_field_accepted(self):
        data = {"callSid": "JSID-005", "tag": {"assistant_id": "asst-002"}}
        req = JambonzWebhookRequest(**data)
        assert req.tag == {"assistant_id": "asst-002"}

    def test_sample_rate_defaults_to_8000(self):
        req = JambonzWebhookRequest()
        assert req.sample_rate == 8000

    def test_custom_sample_rate_16000(self):
        data = {"sampleRate": 16000}
        req = JambonzWebhookRequest(**data)
        assert req.sample_rate == 16000

    def test_direction_field_inbound(self):
        req = JambonzWebhookRequest(direction="inbound", callSid="JSID-006")
        assert req.direction == "inbound"

    def test_direction_field_outbound(self):
        req = JambonzWebhookRequest(direction="outbound", callSid="JSID-007")
        assert req.direction == "outbound"

    def test_call_id_mapped_from_camelCase(self):
        data = {"callId": "SIP-CALL-ID-001"}
        req = JambonzWebhookRequest(**data)
        assert req.call_id == "SIP-CALL-ID-001"

    def test_snake_case_call_sid_accepted(self):
        req = JambonzWebhookRequest(call_sid="SNAKE-CASE-SID")
        assert req.call_sid == "SNAKE-CASE-SID"


class TestJambonzAmdWebhookRequest:
    """Tests for the Answering Machine Detection webhook schema."""

    def test_human_amd_result(self):
        data = {
            "callSid": "JSID-AMD-001",
            "amd": {"type": "human", "reason": "speech detected"},
        }
        req = JambonzAmdWebhookRequest(**data)
        assert req.callSid == "JSID-AMD-001"
        assert req.amd["type"] == "human"

    def test_machine_amd_result(self):
        data = {
            "callSid": "JSID-AMD-002",
            "amd": {"type": "machine", "reason": "voicemail greeting"},
        }
        req = JambonzAmdWebhookRequest(**data)
        assert req.amd["type"] == "machine"

    def test_all_fields_optional(self):
        req = JambonzAmdWebhookRequest()
        assert req.callSid is None
        assert req.amd is None
