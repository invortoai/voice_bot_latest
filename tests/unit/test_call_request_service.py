"""Unit tests for app/services/call_request.py validation logic.

Tests focus on the _validate() function (no database required).
All edge cases for E.164 format, callback_url, and input_variables are covered.
"""

import pytest

from app.services import call_request as svc

# Convenience alias for the private validator
_validate = svc._validate


class TestE164Validation:
    def test_valid_e164_passes(self):
        _validate("+917022123456", None, None)

    def test_missing_plus_prefix_raises(self):
        with pytest.raises(ValueError, match="E.164"):
            _validate("917022123456", None, None)

    def test_too_short_after_plus_raises(self):
        # E.164 requires at least 7 digits after '+'
        with pytest.raises(ValueError):
            _validate("+12345", None, None)

    def test_exactly_seven_digits_valid(self):
        # Minimum valid: + then 7 digits (e.g. +1234567)
        _validate("+1234567", None, None)

    def test_fourteen_digits_valid(self):
        # Maximum 14 digits after '+'
        _validate("+12345678901234", None, None)

    def test_sixteen_digits_raises(self):
        # The regex allows 7–15 digits after '+'; 16 digits must fail
        with pytest.raises(ValueError):
            _validate("+1234567890123456", None, None)

    def test_leading_zero_after_plus_raises(self):
        # E.164 does not allow '+0...'
        with pytest.raises(ValueError):
            _validate("+01234567890", None, None)

    def test_non_digits_raises(self):
        with pytest.raises(ValueError):
            _validate("+1-800-555-0100", None, None)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _validate("", None, None)


class TestCallbackUrlValidation:
    def test_none_callback_url_passes(self):
        _validate("+917022123456", None, None)

    def test_https_url_passes(self):
        _validate("+917022123456", "https://example.com/webhook", None)

    def test_http_url_raises(self):
        with pytest.raises(ValueError, match="HTTPS"):
            _validate("+917022123456", "http://example.com/webhook", None)

    def test_non_url_string_raises(self):
        with pytest.raises(ValueError):
            _validate("+917022123456", "not-a-url", None)

    def test_empty_string_treated_as_absent(self):
        """Empty string is falsy so the HTTPS check is skipped — no exception raised."""
        _validate("+917022123456", "", None)

    def test_ftp_url_raises(self):
        with pytest.raises(ValueError, match="HTTPS"):
            _validate("+917022123456", "ftp://example.com/data", None)


class TestInputVariablesValidation:
    def test_none_passes(self):
        _validate("+917022123456", None, None)

    def test_empty_dict_passes(self):
        _validate("+917022123456", None, {})

    def test_valid_single_key_passes(self):
        _validate("+917022123456", None, {"name": "Alice"})

    def test_exactly_20_keys_passes(self):
        variables = {f"key_{i}": "value" for i in range(20)}
        _validate("+917022123456", None, variables)

    def test_21_keys_raises(self):
        variables = {f"key_{i}": "value" for i in range(21)}
        with pytest.raises(ValueError, match="20"):
            _validate("+917022123456", None, variables)

    def test_non_string_value_raises(self):
        with pytest.raises(ValueError, match="string"):
            _validate("+917022123456", None, {"count": 42})

    def test_bool_value_raises(self):
        with pytest.raises(ValueError, match="string"):
            _validate("+917022123456", None, {"active": True})

    def test_list_value_raises(self):
        with pytest.raises(ValueError, match="string"):
            _validate("+917022123456", None, {"items": ["a", "b"]})

    def test_none_value_raises(self):
        with pytest.raises(ValueError, match="string"):
            _validate("+917022123456", None, {"key": None})

    def test_exactly_500_char_value_passes(self):
        _validate("+917022123456", None, {"key": "x" * 500})

    def test_501_char_value_raises(self):
        with pytest.raises(ValueError, match="500"):
            _validate("+917022123456", None, {"key": "x" * 501})

    def test_error_message_includes_key_name(self):
        with pytest.raises(ValueError, match="long_field"):
            _validate("+917022123456", None, {"long_field": "x" * 501})

    def test_multiple_valid_variables_pass(self):
        variables = {
            "first_name": "Alice",
            "last_name": "Smith",
            "product": "Pro plan",
            "lead_score": "95",
        }
        _validate("+917022123456", None, variables)
