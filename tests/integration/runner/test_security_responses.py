"""Integration tests for API response sanitization.

Covers:
- DAAI-140: Credential masking in phone number responses
- DAAI-142: System prompt not exposed in phone number responses
- DAAI-143: Worker IP redaction from /workers API
- DAAI-146: PII masking in call list API
"""

import pytest


ASSISTANT_PAYLOAD = {
    "name": "Security Test Bot",
    "system_prompt": "SECRET: You are a confidential bot.",
}

PHONE_PAYLOAD = {
    "phone_number": "+15005550099",
    "provider": "twilio",
    "provider_credentials": {
        "account_sid": "AC123",
        "auth_token": "super-secret-token",
    },
    "is_inbound_enabled": True,
}


@pytest.fixture
async def assistant(runner_client):
    resp = await runner_client.post("/assistants", json=ASSISTANT_PAYLOAD)
    return resp.json()


@pytest.fixture
async def phone_number(runner_client, assistant):
    payload = {**PHONE_PAYLOAD, "assistant_id": assistant["id"]}
    resp = await runner_client.post("/phone-numbers", json=payload)
    return resp.json()


class TestPhoneNumberCredentialMasking:
    """DAAI-140: Ensure provider_credentials are masked in responses."""

    async def test_list_masks_auth_token(self, runner_client, phone_number):
        resp = await runner_client.get("/phone-numbers")
        assert resp.status_code == 200
        phones = resp.json()["phone_numbers"]
        assert len(phones) >= 1
        for phone in phones:
            creds = phone.get("provider_credentials", {})
            if "auth_token" in creds:
                assert creds["auth_token"] == "***", "auth_token should be masked"
            # account_sid is NOT secret, should still be visible
            if "account_sid" in creds:
                assert creds["account_sid"] == "AC123"

    async def test_get_by_id_masks_auth_token(self, runner_client, phone_number):
        phone_id = phone_number["id"]
        resp = await runner_client.get(f"/phone-numbers/{phone_id}")
        assert resp.status_code == 200
        creds = resp.json().get("provider_credentials", {})
        assert creds.get("auth_token") == "***"

    async def test_create_returns_masked(self, runner_client, assistant):
        payload = {**PHONE_PAYLOAD, "assistant_id": assistant["id"]}
        payload["phone_number"] = "+15005550098"  # different number
        resp = await runner_client.post("/phone-numbers", json=payload)
        assert resp.status_code == 200
        creds = resp.json().get("provider_credentials", {})
        assert creds.get("auth_token") == "***"


class TestSystemPromptNotExposed:
    """DAAI-142: System prompt should not leak through phone number APIs."""

    async def test_phone_number_response_no_system_prompt(
        self, runner_client, phone_number
    ):
        phone_id = phone_number["id"]
        resp = await runner_client.get(f"/phone-numbers/{phone_id}")
        data = resp.json()
        assert "system_prompt" not in data, (
            "system_prompt should not be in phone number response"
        )


class TestWorkerIpRedaction:
    """DAAI-143: Worker API should not expose IPs."""

    async def test_workers_no_ip_fields(self, runner_client, worker_in_pool):
        resp = await runner_client.get("/workers")
        assert resp.status_code == 200
        for worker in resp.json()["workers"]:
            assert "host" not in worker, "host should be redacted"
            assert "private_ip" not in worker, "private_ip should be redacted"
            assert "public_ip" not in worker, "public_ip should be redacted"
            assert "worker_id" in worker, "worker_id should be present"
            assert "is_available" in worker, "is_available should be present"


class TestCallPiiMasking:
    """DAAI-146: Call list should mask phone numbers."""

    async def test_call_list_masks_phone_numbers(
        self, runner_client, phone_number, worker_in_pool
    ):
        # Create a call via webhook
        form_data = {
            "CallSid": "CA-SEC-001",
            "From": "+14155551234",
            "To": "+15005550099",
        }
        await runner_client.post("/twilio/incoming", data=form_data)

        # List calls — phone numbers should be masked
        resp = await runner_client.get("/calls")
        if resp.json().get("calls"):
            for call in resp.json()["calls"]:
                from_num = call.get("from_number", "")
                if from_num:
                    assert from_num.startswith("***"), (
                        f"from_number should be masked, got: {from_num}"
                    )
                assert "worker_host" not in call, "worker_host should be stripped"
                assert "worker_instance_id" not in call, (
                    "worker_instance_id should be stripped"
                )
