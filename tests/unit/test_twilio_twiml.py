"""Unit tests for Twilio TwiML construction and outbound provider credential validation.

_build_twiml() is a pure function that produces TwiML XML for outbound calls.
These tests verify it produces valid, well-formed XML with the correct parameters.
"""

import xml.etree.ElementTree as ET


def _build(**kwargs):
    """Helper: call _build_twiml with sensible defaults, allow overrides."""
    from app.services.outbound.twilio import _build_twiml

    defaults = dict(
        ws_url="wss://worker.example.com/ws",
        call_id="call-123",
        to_number="+15005550006",
        max_duration=3600,
    )
    defaults.update(kwargs)
    return _build_twiml(**defaults)


class TestBuildTwimlStructure:
    def test_output_is_parseable_xml(self):
        twiml = _build()
        root = ET.fromstring(twiml)  # raises if invalid XML
        assert root is not None

    def test_root_element_is_response(self):
        root = ET.fromstring(_build())
        assert root.tag == "Response"

    def test_has_connect_child(self):
        root = ET.fromstring(_build())
        connect = root.find("Connect")
        assert connect is not None

    def test_has_stream_inside_connect(self):
        root = ET.fromstring(_build())
        stream = root.find("./Connect/Stream")
        assert stream is not None

    def test_stream_url_attribute(self):
        root = ET.fromstring(_build(ws_url="wss://worker.example.com/ws"))
        stream = root.find("./Connect/Stream")
        assert stream.get("url") == "wss://worker.example.com/ws"

    def test_has_pause_element(self):
        root = ET.fromstring(_build())
        pause = root.find("Pause")
        assert pause is not None

    def test_pause_length_equals_max_duration(self):
        root = ET.fromstring(_build(max_duration=1800))
        pause = root.find("Pause")
        assert pause.get("length") == "1800"


class TestBuildTwimlParameters:
    def test_call_type_outbound_parameter_present(self):
        twiml = _build()
        assert "call_type" in twiml
        assert "outbound" in twiml

    def test_call_id_parameter_present(self):
        twiml = _build(call_id="my-call-id-abc")
        assert "my-call-id-abc" in twiml

    def test_to_number_parameter_present(self):
        twiml = _build(to_number="+15005550999")
        assert "+15005550999" in twiml

    def test_custom_ws_url_in_output(self):
        twiml = _build(ws_url="wss://custom-worker.example.com/ws")
        assert "wss://custom-worker.example.com/ws" in twiml

    def test_parameters_are_xml_stream_parameters(self):
        """Parameters should be <Parameter> elements inside <Stream>."""
        root = ET.fromstring(_build())
        stream = root.find("./Connect/Stream")
        params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
        assert "call_type" in params
        assert params["call_type"] == "outbound"

    def test_call_id_is_stream_parameter(self):
        root = ET.fromstring(_build(call_id="CALL-TEST-XYZ"))
        stream = root.find("./Connect/Stream")
        params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
        assert "call_id" in params
        assert params["call_id"] == "CALL-TEST-XYZ"


class TestBuildTwimlVariations:
    def test_default_max_duration_3600(self):
        root = ET.fromstring(_build(max_duration=3600))
        pause = root.find("Pause")
        assert pause.get("length") == "3600"

    def test_short_max_duration(self):
        root = ET.fromstring(_build(max_duration=60))
        pause = root.find("Pause")
        assert pause.get("length") == "60"

    def test_different_worker_paths(self):
        for path in ["/ws", "/ws/jambonz"]:
            url = f"wss://worker.example.com{path}"
            root = ET.fromstring(_build(ws_url=url))
            stream = root.find("./Connect/Stream")
            assert stream.get("url") == url
