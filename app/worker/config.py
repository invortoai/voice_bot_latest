import json
import re
from typing import Optional

from loguru import logger

_E164_RE = re.compile(r"^\+?[0-9]{1,15}$")


def _sanitize_phone(number: Optional[str]) -> str:
    """Sanitize a phone number to prevent LLM prompt injection.

    Strips all non-phone characters (keeps digits and +), then validates
    against E.164-like format.  Returns 'Unknown' if invalid.
    """
    if not number:
        return "Unknown"
    cleaned = re.sub(r"[^0-9+]", "", number)
    if _E164_RE.match(cleaned):
        return cleaned
    return "Unknown"


SYSTEM_PARAM_KEYS = frozenset(
    {
        "call_sid",
        "call_type",
        "caller",
        "called",
        "to_number",
        "assistant_id",
        "system_prompt",
        "llm_provider",
        "model",
        "llm_settings",
        "greeting_message",
        "voice_provider",
        "voice_id",
        "voice_model",
        "voice_settings",
        "max_call_duration",
        "twilio_account_sid",
        "twilio_auth_token",
        "transcriber_provider",
        "transcriber_model",
        "transcriber_language",
        "transcriber_settings",
        "jambonz_audio_in_sample_rate",
        "jambonz_audio_out_sample_rate",
        "jambonz_bidirectional_streaming",
        "custom_params",
        "call_id",
        "phone_number_id",
        "provider_credentials",
        "interruption_strategy",
        "system_prompt_rag_context",
    }
)


class AssistantConfig:
    def __init__(
        self,
        custom_params: dict,
        assistant_config: dict,
        phone_config: Optional[dict] = None,
        user_custom_params: Optional[dict] = None,
    ):
        self.call_sid = custom_params.get("call_sid", "")
        self.call_type = custom_params.get("call_type", "inbound")
        self.caller = custom_params.get("caller", "Unknown")
        self.called = custom_params.get("called", "")
        self.to_number = custom_params.get("to_number", "")

        self.assistant_id = str(assistant_config.get("id", ""))
        self._load_from_assistant_config(assistant_config)
        self._load_from_phone_config(phone_config or {})
        logger.info(f"Config loaded from database for assistant: {self.assistant_id}")

        # RAG context injected by runner at call start (optional)
        self.rag_context: str = custom_params.get("system_prompt_rag_context", "")

        if user_custom_params is not None:
            self.extra_params = dict(user_custom_params)
        else:
            self.extra_params = {
                k: v for k, v in custom_params.items() if k not in SYSTEM_PARAM_KEYS
            }

        logger.info(
            f"Assistant config ready: llm={self.llm_provider}/{self.model}, "
            f"voice={self.voice_provider}/{self.voice_id} (model={self.voice_model}), "
            f"transcriber={self.transcriber_provider}/{self.transcriber_model}"
        )

    def _load_from_assistant_config(self, config: dict):
        self.system_prompt = config.get("system_prompt") or ""
        self.llm_provider = config.get("llm_provider") or "openai"
        self.model = config.get("model") or "gpt-4.1-nano"

        llm = config.get("llm_settings") or {}
        if isinstance(llm, str):
            try:
                llm = json.loads(llm)
            except (json.JSONDecodeError, TypeError):
                llm = {}
        _temp = llm.get("temperature")
        self.temperature = float(_temp) if _temp is not None else 0.7
        _max = llm.get("max_completion_tokens")
        self.max_completion_tokens = int(_max) if _max is not None else 150
        self.service_tier = llm.get("service_tier") or None

        self.greeting_message = config.get("greeting_message") or ""
        self.end_call_phrases = [
            p.strip() for p in (config.get("end_call_phrases") or []) if p and p.strip()
        ]

        self.voice_provider = config.get("voice_provider") or "elevenlabs"
        self.voice_id = config.get("voice_id") or ""
        self.voice_model = config.get("voice_model") or "eleven_flash_v2_5"

        self.voice_settings = config.get("voice_settings") or {}
        if isinstance(self.voice_settings, str):
            try:
                self.voice_settings = json.loads(self.voice_settings)
            except json.JSONDecodeError:
                self.voice_settings = {}

        self.transcriber_provider = config.get("transcriber_provider") or "deepgram"
        self.transcriber_model = config.get("transcriber_model") or "nova-2"
        self.transcriber_language = config.get("transcriber_language") or "en"

        self.transcriber_settings = config.get("transcriber_settings") or {}
        if isinstance(self.transcriber_settings, str):
            try:
                self.transcriber_settings = json.loads(self.transcriber_settings)
            except json.JSONDecodeError:
                self.transcriber_settings = {}

        self.vad_settings = config.get("vad_settings") or {}
        if isinstance(self.vad_settings, str):
            try:
                self.vad_settings = json.loads(self.vad_settings)
            except json.JSONDecodeError:
                self.vad_settings = {}

        # Interruption strategy: flat column on assistants.
        # 'default' (or NULL/empty) → pipecat VAD-based turn start.
        # 'llm_judge' → LLMInterruptionJudgeStrategy.
        # Unknown values fall back to 'default' downstream in the pipeline.
        self.interruption_strategy = (
            (config.get("interruption_strategy") or "default").strip().lower()
        )

    def _load_from_phone_config(self, config: dict):
        # Extract Twilio credentials from provider_credentials JSONB column
        provider_credentials = config.get("provider_credentials", {})
        self.twilio_account_sid = provider_credentials.get("account_sid")
        self.twilio_auth_token = provider_credentials.get("auth_token")

        if not self.twilio_account_sid or not self.twilio_auth_token:
            logger.warning(
                "Twilio credentials not found in phone config - hangup may not work"
            )

        self.max_call_duration = int(config.get("max_call_duration_seconds") or 3600)

    def _replace_placeholders(self, text: str) -> str:
        if not text:
            return text

        def replacer(match):
            key = match.group(1).strip()
            if not key:
                return match.group(0)
            if key in self.extra_params:
                return str(self.extra_params[key])
            logger.warning(
                "Placeholder '{{%s}}' not found in custom_params for call %s; resolving to empty string",
                key,
                self.call_sid,
            )
            return ""

        return re.sub(r"\{\{(.+?)\}\}", replacer, text)

    def get_system_message(self) -> str:
        message = self._replace_placeholders(self.system_prompt)

        if self.call_type == "outbound":
            message += "\n\nThis is an outbound call that you initiated."
            safe_to = _sanitize_phone(self.to_number)
            if safe_to != "Unknown":
                message += f" You called {safe_to}."
        else:
            safe_caller = _sanitize_phone(self.caller)
            if safe_caller != "Unknown":
                message += f"\n\nThe caller's number is {safe_caller}."

        message += f'\n\nEnsure responses are clear, complete, and within the max token limit of {self.max_completion_tokens} tokens. Use natural spoken language. Avoid expressive punctuation like "!" or symbols that affect TTS modulation. Use only commas and full stops for natural pauses. Maintain a neutral tone without over-emphasis.'

        # Append RAG knowledge base context if available
        if self.rag_context:
            message += f"\n\n{self.rag_context}"

        return message

    def get_greeting(self) -> str:
        return self._replace_placeholders(self.greeting_message)
