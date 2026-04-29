"""Unit tests for MCube helper functions (app/routes/mcube.py)."""

import pytest

from app.routes.mcube import (
    _is_connect_hangup,
    _is_terminal_status,
    _map_dial_status,
    _determine_call_identifiers,
)
from app.models.schemas import McubeConnectWebhookRequest


class TestMapDialStatus:
    def test_answer_without_endtime_is_in_progress(self):
        assert _map_dial_status("ANSWER", None) == "in-progress"

    def test_answer_with_endtime_is_completed(self):
        assert _map_dial_status("ANSWER", "2026-01-01T12:00:00") == "completed"

    def test_cancel_is_canceled(self):
        assert _map_dial_status("CANCEL", None) == "canceled"

    def test_executive_busy_is_busy(self):
        assert _map_dial_status("Executive Busy", None) == "busy"

    def test_busy_is_busy(self):
        assert _map_dial_status("Busy", None) == "busy"

    def test_noanswer_is_no_answer(self):
        assert _map_dial_status("NoAnswer", None) == "no-answer"

    def test_unknown_status_lowercased(self):
        assert _map_dial_status("CUSTOM_STATUS", None) == "custom_status"

    def test_endtime_always_returns_completed(self):
        """Any dial_status with an endTime should map to 'completed'."""
        assert _map_dial_status("CANCEL", "2026-01-01") == "completed"
        assert _map_dial_status("NoAnswer", "2026-01-01") == "completed"


class TestIsTerminalStatus:
    @pytest.mark.parametrize(
        "status", ["completed", "canceled", "busy", "no-answer", "failed"]
    )
    def test_terminal_statuses(self, status):
        assert _is_terminal_status(status) is True

    @pytest.mark.parametrize("status", ["initiated", "in-progress", "ringing", ""])
    def test_non_terminal_statuses(self, status):
        assert _is_terminal_status(status) is False


class TestIsConnectHangup:
    @pytest.mark.parametrize(
        "status",
        [
            "BUSY",
            "ANSWER",
            "EXECUTIVE BUSY",
            "CANCEL",
            "NOANSWER",
            "busy",
            "answer",
            "cancel",  # lowercase variants
            " BUSY ",  # with whitespace
        ],
    )
    def test_hangup_statuses_return_true(self, status):
        assert _is_connect_hangup(status) is True

    @pytest.mark.parametrize("status", ["CONNECTING", None, "", "RINGING", "UNKNOWN"])
    def test_non_hangup_statuses_return_false(self, status):
        assert _is_connect_hangup(status) is False


class TestDetermineCallIdentifiers:
    def _make_payload(self, **kwargs):
        defaults = {
            "call_id": "CALL-001",
            "call_direction": "inbound",
            "from_number": "+14155551234",
            "to_number": "+18001234567",
        }
        defaults.update(kwargs)
        return McubeConnectWebhookRequest(**defaults)

    def test_inbound_direction(self):
        payload = self._make_payload(call_direction="inbound")
        ids = _determine_call_identifiers(payload)
        assert ids.direction == "inbound"
        assert ids.is_outbound is False

    def test_outbound_direction(self):
        payload = self._make_payload(call_direction="outbound")
        ids = _determine_call_identifiers(payload)
        assert ids.direction == "outbound"
        assert ids.is_outbound is True

    def test_call_sid_is_call_id(self):
        payload = self._make_payload(call_id="CALL-XYZ")
        ids = _determine_call_identifiers(payload)
        assert ids.call_sid == "CALL-XYZ"

    def test_inbound_lookup_uses_last_10_of_to_number(self):
        payload = self._make_payload(
            call_direction="inbound",
            to_number="+918001234567",  # 13 digits
        )
        ids = _determine_call_identifiers(payload)
        assert ids.lookup_number == "8001234567"

    def test_outbound_lookup_uses_last_10_of_from_number(self):
        payload = self._make_payload(
            call_direction="outbound",
            from_number="+914155551234",  # agent number
            to_number="+919999999999",
        )
        ids = _determine_call_identifiers(payload)
        assert ids.lookup_number == "4155551234"

    def test_missing_direction_defaults_to_inbound(self):
        payload = self._make_payload(call_direction=None)
        ids = _determine_call_identifiers(payload)
        assert ids.direction == "inbound"
