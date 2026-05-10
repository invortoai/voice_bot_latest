from typing import Any, Optional, List, Union, Dict
from uuid import UUID
from pydantic import BaseModel, Field, model_validator

DEFAULT_LLM_SETTINGS: Dict[str, Any] = {
    "temperature": 0.7,
    "max_completion_tokens": 150,
}


class AssistantCreate(BaseModel):
    name: str
    system_prompt: str
    description: Optional[str] = None
    llm_provider: str = "openai"
    model: str = "gpt-4.1-nano"
    llm_settings: Dict[str, Any] = Field(
        default_factory=lambda: dict(DEFAULT_LLM_SETTINGS)
    )
    voice_provider: str = "elevenlabs"
    voice_id: Optional[str] = None
    voice_model: str = "eleven_flash_v2_5"
    voice_settings: Optional[Dict[str, Any]] = None
    greeting_message: Optional[str] = None
    end_call_phrases: Optional[List[str]] = None
    transcriber_provider: str = "deepgram"
    transcriber_model: str = "nova-2"
    transcriber_language: str = "en"
    transcriber_settings: Optional[Dict[str, Any]] = None
    vad_settings: Optional[Dict[str, Any]] = None
    interruption_strategy: Optional[str] = None
    insight_enabled: bool = False
    insights_config_id: Optional[UUID] = None


class AssistantUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    llm_provider: Optional[str] = None
    model: Optional[str] = None
    llm_settings: Optional[Dict[str, Any]] = None
    voice_provider: Optional[str] = None
    voice_id: Optional[str] = None
    voice_model: Optional[str] = None
    voice_settings: Optional[Dict[str, Any]] = None
    greeting_message: Optional[str] = None
    end_call_phrases: Optional[List[str]] = None
    is_active: Optional[bool] = None
    transcriber_provider: Optional[str] = None
    transcriber_model: Optional[str] = None
    transcriber_language: Optional[str] = None
    transcriber_settings: Optional[Dict[str, Any]] = None
    vad_settings: Optional[Dict[str, Any]] = None
    interruption_strategy: Optional[str] = None
    insight_enabled: Optional[bool] = None
    insights_config_id: Optional[UUID] = None


class PhoneNumberCreate(BaseModel):
    phone_number: str = Field(
        ..., description="Phone number in E.164 format (+1234567890)"
    )
    friendly_name: Optional[str] = None
    provider: str = Field(
        default="twilio",
        description="Telephony provider: 'twilio', 'jambonz', or 'mcube'",
    )
    # Provider credentials (JSONB storage for all provider auth data)
    # Twilio: {"account_sid": "...", "auth_token": "...", "sid": "..."}
    # MCube: {"token": "..."}
    # Jambonz: {"trunk_name": "..."}
    provider_credentials: Optional[dict] = Field(
        default={}, description="Provider authentication credentials as JSON"
    )
    # Common fields
    assistant_id: Optional[str] = None
    is_inbound_enabled: bool = True
    is_outbound_enabled: bool = True
    max_call_duration_seconds: int = 3600


class PhoneNumberUpdate(BaseModel):
    friendly_name: Optional[str] = None
    provider: Optional[str] = None
    # Provider credentials (JSONB storage for all provider auth data)
    provider_credentials: Optional[dict] = None
    # Common fields
    assistant_id: Optional[str] = None
    is_inbound_enabled: Optional[bool] = None
    is_outbound_enabled: Optional[bool] = None
    max_call_duration_seconds: Optional[int] = None
    is_active: Optional[bool] = None


class CallInitiateRequest(BaseModel):
    """
    Customer-facing call initiation request.
    Authenticated via X-API-Key (org API key). Enqueues into call_requests;
    the org-queue-processor dispatches the actual call.
    """

    assistant_id: UUID = Field(
        ..., description="UUID of the Invorto assistant (voice bot) to use"
    )
    phone_number_id: UUID = Field(
        ..., description="UUID of the outbound phone number configured in Invorto"
    )
    to_number: str = Field(
        ..., description="Destination phone number in E.164 format (e.g. +917022xxxxxx)"
    )
    input_variables: Optional[Dict[str, str]] = Field(
        default=None,
        description="Key-value pairs injected as {{variable_name}} tokens at call runtime. Max 20 pairs; values must be flat strings ≤ 500 chars",
    )
    external_customer_id: Optional[str] = Field(
        None, description="Unique ID of the lead/contact in the external CRM or system"
    )
    invorto_campaign_id: Optional[UUID] = Field(
        None,
        description="Invorto campaign ID. When provided the call follows the campaign's dialling rules and schedule",
    )
    callback_url: Optional[str] = Field(
        None,
        description="HTTPS webhook URL to receive the post-call stats payload. Must start with https://",
    )
    call_time: Optional[str] = Field(
        None,
        description="ISO 8601 datetime to schedule the call. Omit to trigger immediately",
    )
    priority: Optional[int] = Field(
        default=5,
        ge=1,
        le=100,
        description="Dispatch priority 1 (highest) – 100 (lowest). Default 5",
    )
    additional_data: Optional[dict] = Field(
        None,
        description="Arbitrary metadata stored and returned in the post-call payload. Not injected into the conversation",
    )


class CallInitiateResponse(BaseModel):
    """Response returned immediately after a call request is accepted."""

    request_id: str = Field(
        ...,
        description="Unique Invorto call identifier. Use this to poll GET /call-stats/{request_id}",
    )
    status: str = Field(default="queued", description="Initial request status")
    message: str = Field(
        default="Call request accepted", description="Human-readable confirmation"
    )


class OutboundCallRequest(BaseModel):
    """Request to initiate an outbound call. Provider is resolved from the phone number config."""

    phone_number_id: UUID = Field(
        ..., description="ID of the phone number to use for the call"
    )
    assistant_id: UUID = Field(
        ..., description="ID of the assistant to handle the call"
    )
    to_number: str = Field(
        ..., description="Destination phone number in E.164 format (+1234567890)"
    )
    custom_params: Optional[dict] = Field(
        default={}, description="Optional custom parameters to pass to the call"
    )
    call_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Pre-assigned call ID (UUID) from call_requests.id. "
            "When provided this UUID is used as calls.id so the same identifier "
            "flows end-to-end: call_requests → backend calls table → API response "
            "→ webhook deliveries. When omitted a new UUID is generated."
        ),
    )


# Backward-compatible aliases — identical to OutboundCallRequest.
JambonzOutboundCallRequest = OutboundCallRequest


# =============================================================================
# Jambonz Webhook Schemas (for API documentation)
# =============================================================================


class JambonzWebhookRequest(BaseModel):
    """Jambonz webhook request payload for incoming/outbound calls."""

    call_sid: Optional[str] = Field(None, description="Jambonz unique call identifier")
    call_id: Optional[str] = Field(None, description="SIP Call-ID header")
    direction: Optional[str] = Field(
        None, description="Call direction: inbound or outbound"
    )
    from_number: Optional[str] = Field(None, description="Caller phone number")
    to_number: Optional[str] = Field(None, description="Called phone number")
    caller_id: Optional[str] = Field(None, description="Caller ID")
    account_sid: Optional[str] = Field(None, description="Jambonz account SID")
    application_sid: Optional[str] = Field(None, description="Jambonz application SID")
    originating_sip_ip: Optional[str] = Field(
        None, description="Originating SIP IP address"
    )
    originating_sip_trunk_name: Optional[str] = Field(
        None, description="SIP trunk name"
    )
    sample_rate: Optional[int] = Field(8000, description="Audio sample rate in Hz")
    tag: Optional[Union[str, dict]] = Field(
        None, description="Custom metadata passed when creating the call"
    )
    customer_data: Optional[Union[str, dict]] = Field(
        None, description="Customer data (for outbound calls)"
    )

    @model_validator(mode="before")
    @classmethod
    def map_camel_case_fields(cls, data: dict) -> dict:
        """Map camelCase fields from Jambonz to snake_case."""
        if not isinstance(data, dict):
            return data

        # Mapping: snake_case_field -> camelCase field from Jambonz
        field_mappings = {
            "call_sid": "callSid",
            "call_id": "callId",
            "from_number": "from",
            "to_number": "to",
            "caller_id": "callerId",
            "account_sid": "accountSid",
            "application_sid": "applicationSid",
            "originating_sip_ip": "originatingSipIp",
            "originating_sip_trunk_name": "originatingSipTrunkName",
            "sample_rate": "sampleRate",
            "customer_data": "customerData",
        }

        for snake_case, camel_case in field_mappings.items():
            # If snake_case already exists, keep it
            if snake_case in data and data[snake_case] is not None:
                continue
            # Otherwise, map from camelCase if it exists
            if camel_case in data and data[camel_case] is not None:
                data[snake_case] = data[camel_case]

        return data


class JambonzStatusWebhookRequest(BaseModel):
    """Jambonz call status webhook payload."""

    call_sid: Optional[str] = Field(None, description="Jambonz unique call identifier")
    call_id: Optional[str] = Field(None, description="SIP Call-ID header")
    call_status: Optional[str] = Field(None, description="Current call status")
    direction: Optional[str] = Field(
        None, description="Call direction: inbound or outbound"
    )
    from_number: Optional[str] = Field(None, description="Caller phone number")
    to_number: Optional[str] = Field(None, description="Called phone number")
    duration: Optional[int] = Field(None, description="Call duration in seconds")
    sip_status: Optional[int] = Field(None, description="SIP status code")
    sip_reason: Optional[str] = Field(None, description="SIP status reason")
    call_termination_by: Optional[str] = Field(
        None, description="Who terminated the call: caller or callee"
    )
    account_sid: Optional[str] = Field(None, description="Jambonz account SID")
    application_sid: Optional[str] = Field(None, description="Jambonz application SID")
    originating_sip_ip: Optional[str] = Field(
        None, description="Originating SIP IP address"
    )
    originating_sip_trunk_name: Optional[str] = Field(
        None, description="SIP trunk name"
    )

    @model_validator(mode="before")
    @classmethod
    def map_camel_case_fields(cls, data: dict) -> dict:
        """Map camelCase fields from Jambonz to snake_case."""
        if not isinstance(data, dict):
            return data

        # Mapping: snake_case_field -> camelCase field from Jambonz
        field_mappings = {
            "call_sid": "callSid",
            "call_id": "callId",
            "call_status": "callStatus",
            "from_number": "from",
            "to_number": "to",
            "sip_status": "sipStatus",
            "sip_reason": "sipReason",
            "call_termination_by": "callTerminationBy",
            "account_sid": "accountSid",
            "application_sid": "applicationSid",
            "originating_sip_ip": "originatingSipIp",
            "originating_sip_trunk_name": "originatingSipTrunkName",
        }

        for snake_case, camel_case in field_mappings.items():
            # If snake_case already exists, keep it
            if snake_case in data and data[snake_case] is not None:
                continue
            # Otherwise, map from camelCase if it exists
            if camel_case in data and data[camel_case] is not None:
                data[snake_case] = data[camel_case]

        return data


class JambonzAmdWebhookRequest(BaseModel):
    """Jambonz Answering Machine Detection webhook payload."""

    callSid: Optional[str] = Field(None, description="Unique call identifier")
    callId: Optional[str] = Field(None, description="Alternative call identifier")
    amd: Optional[dict] = Field(
        None,
        description="AMD result containing 'type' (human/machine) and 'reason'",
        json_schema_extra={"example": {"type": "human", "reason": "speech detected"}},
    )


class JambonzVerb(BaseModel):
    """A single Jambonz call-control verb."""

    verb: str = Field(
        ..., description="Jambonz verb name (answer, say, listen, hangup, etc.)"
    )

    model_config = {"extra": "allow"}  # Allow additional fields per verb type


class OutboundCallResponse(BaseModel):
    """Response after initiating an outbound call."""

    call_sid: str = Field(..., description="Unique call identifier from the provider")
    call_id: str = Field(..., description="Internal call ID")
    to_number: str = Field(..., description="Destination phone number")
    from_number: str = Field(..., description="Source phone number")
    phone_number_id: str = Field(..., description="Phone number configuration ID")
    worker_id: Optional[str] = Field(
        None,
        description="Assigned worker instance ID (None for Jambonz/MCube until call webhook)",
    )
    status: str = Field(..., description="Initial call status")
    provider: Optional[str] = Field(
        None, description="Telephony provider (twilio/jambonz/mcube)"
    )


class CallStatRecord(BaseModel):
    """Per-call stats record returned by GET /call-stats endpoints."""

    request_id: str = Field(
        ..., description="Unique Invorto call identifier (call_requests.id)"
    )
    call_status: str = Field(
        ...,
        description="Final status: answered | missed | busy | failed | rejected",
    )
    call_direction: Optional[str] = Field(None, description="outbound or inbound")
    call_start_time: Optional[str] = Field(
        None, description="ISO 8601 — when the call was initiated"
    )
    call_end_time: Optional[str] = Field(
        None, description="ISO 8601 — when the call ended"
    )
    total_duration_seconds: Optional[int] = Field(
        None, description="Elapsed time from start to end"
    )
    recording_url: Optional[str] = Field(
        None, description="URL to call recording; null if not answered or disabled"
    )
    initiation_payload: dict = Field(
        ..., description="Full copy of the original call initiation request payload"
    )


class WebhookDeliveryRecord(BaseModel):
    """One webhook delivery attempt for a call-stat event."""

    id: str = Field(..., description="Unique delivery record ID")
    call_request_id: str = Field(..., description="The call this delivery belongs to")
    event_type: str = Field(
        ...,
        description="call.completed | call.failed | call.no_answer | call.busy | call.cancelled",
    )
    webhook_url: str = Field(..., description="Destination URL")
    status: str = Field(..., description="pending | delivered | failed | exhausted")
    attempt_number: int = Field(..., description="Current attempt count (starts at 1)")
    max_attempts: int = Field(..., description="Maximum attempts allowed")
    response_status_code: Optional[int] = Field(
        None, description="HTTP status code returned by the destination"
    )
    response_time_ms: Optional[int] = Field(
        None, description="Round-trip time in milliseconds"
    )
    error_message: Optional[str] = Field(
        None, description="Error detail when status is failed or exhausted"
    )
    created_at: str = Field(
        ..., description="ISO 8601 — when this delivery was enqueued"
    )
    last_attempted_at: Optional[str] = Field(
        None, description="ISO 8601 — timestamp of the most recent attempt"
    )
    delivered_at: Optional[str] = Field(
        None, description="ISO 8601 — timestamp when successfully delivered"
    )
    next_retry_at: Optional[str] = Field(
        None,
        description="ISO 8601 — when the next retry is scheduled (null if delivered/exhausted)",
    )


# =============================================================================
# MCube Webhook Schemas
# =============================================================================


class McubeConnectWebhookRequest(BaseModel):
    """MCube connect webhook - single refurl for connect and status.

    Action is driven by dial_status:
    - CONNECTING: assign worker, create/update call, return wss_url.
    - BUSY / ANSWER (hangup events): update call status, release worker, return status ok.
    """

    call_direction: Optional[str] = Field(
        None, description="Call direction: inbound or outbound"
    )
    from_number: Optional[str] = Field(None, description="Caller phone number")
    to_number: Optional[str] = Field(None, description="Called phone number")
    call_id: str = Field(..., description="MCube unique call identifier")
    dial_status: Optional[str] = Field(
        None,
        description="CONNECTING: return wss_url; ANSWER/BUSY: hangup (update status, release worker)",
    )
    call_provider: Optional[str] = Field(None, description="Call provider e.g. MCUBE")
    account_id: Optional[str] = Field(None, description="Account ID")
    filename: Optional[str] = Field(None, description="Recording path")
    startTime: Optional[str] = Field(None, description="Call Start Time")
    endTime: Optional[str] = Field(None, description="Call End Time")
    answeredTime: Optional[str] = Field(None, description="Call duration in seconds")

    @model_validator(mode="before")
    @classmethod
    def map_camel_case_fields(cls, data: dict) -> dict:
        """Map camelCase fields from MCube to snake_case."""
        if not isinstance(data, dict):
            return data
        mappings = {
            "call_direction": "callDirection",
            "from_number": "fromNumber",
            "to_number": "toNumber",
            "call_id": "callId",
            "dial_status": "dialStatus",
            "call_provider": "callProvider",
            "account_id": "accountId",
        }
        result = dict(data)
        for snake, camel in mappings.items():
            if camel in result and snake not in result:
                result[snake] = result[camel]
        return result


class McubeConnectWebhookResponse(BaseModel):
    """Response for MCube connect webhook - WebSocket URL for call streaming."""

    wss_url: str = Field(..., description="WebSocket URL for MCube to connect")


class McubeStatusOkResponse(BaseModel):
    """Response for MCube connect webhook when handling hangup (dial_status BUSY/ANSWER)."""

    status: str = Field(default="ok", description="Acknowledgment status")


class McubeWebhookRequest(BaseModel):
    """MCube status webhook - call status and event updates."""

    starttime: Optional[str] = Field(None, description="Start time of call")
    callid: str = Field(..., description="Unique MCube call ID")
    emp_phone: Optional[str] = Field(None, description="Agent/executive phone number")
    clicktocalldid: Optional[str] = Field(None, description="MCube DID number")
    callto: Optional[str] = Field(None, description="Customer number")
    dialstatus: Optional[str] = Field(
        None, description="ANSWER/CANCEL/Executive Busy/Busy/NoAnswer"
    )
    direction: str = Field(..., description="Call direction: inbound or outbound")
    groupname: Optional[str] = Field(None, description="Call group name")
    agentname: Optional[str] = Field(None, description="Name of the agent")
    refid: Optional[str] = Field(
        None, description="Reference ID (our call_id for outbound)"
    )
    # Status-specific fields (present in status updates)
    filename: Optional[str] = Field(None, description="Call recording URL")
    endtime: Optional[str] = Field(None, description="End time of call")
    disconnectedby: Optional[str] = Field(None, description="Customer or Agent")
    answeredtime: Optional[str] = Field(
        None, description="Call answered duration (HH:MM:SS)"
    )
