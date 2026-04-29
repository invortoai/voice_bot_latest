"""Unit tests for safe TwiML generation using SDK (DAAI-159)."""

import pytest
from xml.etree import ElementTree

from app.services.outbound.twilio import _build_twiml


class TestTwimlSafety:
    def test_basic_twiml_is_valid_xml(self):
        xml = _build_twiml(
            "wss://worker.example.com/ws", "uuid-123", "+14155551234", 3600
        )
        root = ElementTree.fromstring(xml)
        assert root.tag == "Response"

    def test_stream_url_present(self):
        xml = _build_twiml("wss://example.com/ws", "uuid-123", "+1234", 3600)
        root = ElementTree.fromstring(xml)
        stream = root.find(".//{http://www.twilio.com/voice}Stream")
        if stream is None:
            stream = root.find(".//Stream")
        assert stream is not None

    def test_special_chars_in_url_are_escaped(self):
        """XML special characters in ws_url should be properly escaped."""
        xml = _build_twiml(
            "wss://worker.example.com/ws?foo=1&bar=2",
            "uuid-123",
            "+1234",
            3600,
        )
        # Should parse without error — & is properly escaped by SDK
        root = ElementTree.fromstring(xml)
        assert root is not None

    def test_injection_attempt_in_number_is_escaped(self):
        """Even if to_number contained XML, SDK should escape it."""
        malicious = '+1234"/><Redirect>https://evil.com</Redirect><Stream url="x'
        xml = _build_twiml("wss://w.example.com/ws", "uuid-123", malicious, 3600)
        root = ElementTree.fromstring(xml)
        # Should have exactly zero Redirect elements (injection didn't work)
        redirects = root.findall(".//{http://www.twilio.com/voice}Redirect")
        if not redirects:
            redirects = root.findall(".//Redirect")
        assert len(redirects) == 0

    def test_pause_length_present(self):
        xml = _build_twiml("wss://example.com/ws", "uuid-123", "+1234", 1800)
        assert "1800" in xml
