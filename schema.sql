-- Invorto AI - Full Database Schema
-- Generated from app source code analysis

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── organizations ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS organizations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT,
    org_type              TEXT NOT NULL DEFAULT 'standard',
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    max_api_keys          INT NOT NULL DEFAULT 10,
    max_active_api_keys   INT NOT NULL DEFAULT 5,
    default_bot_id        UUID,
    minutes_consumed      INT NOT NULL DEFAULT 0,
    total_minutes_ordered INT NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── org_users ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS org_users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        UUID REFERENCES organizations(id) ON DELETE CASCADE,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    role          TEXT NOT NULL DEFAULT 'member',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    password_hash TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_org_users_org ON org_users(org_id);
CREATE INDEX IF NOT EXISTS idx_org_users_email ON org_users(email);

-- ── refresh_tokens ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES org_users(id) ON DELETE CASCADE,
    org_id     UUID REFERENCES organizations(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_hash ON refresh_tokens(token_hash);

-- ── insights_config ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS insights_config (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                      UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name                        TEXT NOT NULL DEFAULT 'default',
    is_default                  BOOLEAN NOT NULL DEFAULT FALSE,
    stt_provider                TEXT NOT NULL DEFAULT 'deepgram',
    stt_model                   TEXT NOT NULL DEFAULT 'nova-2',
    stt_language                TEXT NOT NULL DEFAULT 'en',
    stt_speaker_index_bot       INT,
    stt_multichannel            BOOLEAN NOT NULL DEFAULT FALSE,
    llm_provider                TEXT NOT NULL DEFAULT 'openai',
    llm_model                   TEXT NOT NULL DEFAULT 'gpt-4o-mini',
    llm_temperature             NUMERIC(3,2) NOT NULL DEFAULT 0.2,
    analysis_prompt             TEXT,
    enable_summary              BOOLEAN NOT NULL DEFAULT TRUE,
    enable_sentiment            BOOLEAN NOT NULL DEFAULT TRUE,
    enable_key_topics           BOOLEAN NOT NULL DEFAULT TRUE,
    enable_call_score           BOOLEAN NOT NULL DEFAULT TRUE,
    enable_call_outcome         BOOLEAN NOT NULL DEFAULT TRUE,
    enable_actionable_insights  BOOLEAN NOT NULL DEFAULT TRUE,
    allowed_call_outcomes       TEXT[] NOT NULL DEFAULT '{}',
    custom_fields_schema        JSONB,
    callback_url                TEXT,
    callback_secret             TEXT,
    force_worker_audio_download BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_insights_config_org ON insights_config(org_id);

-- ── assistants ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assistants (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    description           TEXT,
    system_prompt         TEXT NOT NULL,
    llm_provider          TEXT NOT NULL DEFAULT 'openai',
    model                 TEXT NOT NULL DEFAULT 'gpt-4.1-nano',
    llm_settings          JSONB NOT NULL DEFAULT '{}',
    voice_provider        TEXT NOT NULL DEFAULT 'elevenlabs',
    voice_id              TEXT,
    voice_model           TEXT NOT NULL DEFAULT 'eleven_flash_v2_5',
    voice_settings        JSONB,
    greeting_message      TEXT,
    end_call_phrases      TEXT[],
    transcriber_provider  TEXT NOT NULL DEFAULT 'deepgram',
    transcriber_model     TEXT NOT NULL DEFAULT 'nova-2',
    transcriber_language  TEXT NOT NULL DEFAULT 'en',
    transcriber_settings  JSONB,
    vad_settings          JSONB,
    interruption_strategy TEXT,
    insight_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    insights_config_id    UUID REFERENCES insights_config(id) ON DELETE SET NULL,
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_assistants_org ON assistants(org_id);

-- ── phone_numbers ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS phone_numbers (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                   UUID REFERENCES organizations(id) ON DELETE CASCADE,
    phone_number             TEXT NOT NULL UNIQUE,
    friendly_name            TEXT,
    provider                 TEXT NOT NULL DEFAULT 'twilio',
    provider_credentials     JSONB NOT NULL DEFAULT '{}',
    assistant_id             UUID REFERENCES assistants(id) ON DELETE SET NULL,
    is_inbound_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    is_outbound_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    max_call_duration_seconds INT NOT NULL DEFAULT 3600,
    is_active                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_org ON phone_numbers(org_id);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_number ON phone_numbers(phone_number);

-- ── campaigns ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS campaigns (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name         TEXT,
    callback_url TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_campaigns_org ON campaigns(org_id);

-- ── campaign_phone_numbers ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS campaign_phone_numbers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id     UUID REFERENCES campaigns(id) ON DELETE CASCADE,
    org_id          UUID REFERENCES organizations(id) ON DELETE CASCADE,
    phone_number    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_campaign_phone_numbers_campaign ON campaign_phone_numbers(campaign_id);

-- ── calls ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calls (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_sid             TEXT NOT NULL UNIQUE,
    parent_call_sid      TEXT,
    org_id               UUID REFERENCES organizations(id) ON DELETE CASCADE,
    direction            TEXT NOT NULL DEFAULT 'inbound',
    from_number          TEXT,
    to_number            TEXT,
    phone_number_id      UUID REFERENCES phone_numbers(id) ON DELETE SET NULL,
    assistant_id         UUID REFERENCES assistants(id) ON DELETE SET NULL,
    status               TEXT NOT NULL DEFAULT 'initiated',
    started_at           TIMESTAMPTZ,
    ended_at             TIMESTAMPTZ,
    duration_seconds     INT,
    error_code           TEXT,
    error_message        TEXT,
    recording_url        TEXT,
    summary              TEXT,
    worker_instance_id   TEXT,
    worker_host          TEXT,
    custom_params        JSONB NOT NULL DEFAULT '{}',
    provider_metadata    JSONB NOT NULL DEFAULT '{}',
    provider             TEXT NOT NULL DEFAULT 'twilio',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_calls_org ON calls(org_id);
CREATE INDEX IF NOT EXISTS idx_calls_sid ON calls(call_sid);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);

-- ── call_requests ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS call_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID REFERENCES organizations(id) ON DELETE CASCADE,
    source          TEXT NOT NULL DEFAULT 'api',
    campaign_id     UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    phone_number    TEXT NOT NULL,
    lead_id         TEXT,
    bot_id          UUID REFERENCES assistants(id) ON DELETE SET NULL,
    phone_number_id UUID REFERENCES phone_numbers(id) ON DELETE SET NULL,
    custom_params   JSONB NOT NULL DEFAULT '{}',
    additional_data JSONB,
    callback_url    TEXT,
    scheduled_at    TIMESTAMPTZ,
    priority                INT NOT NULL DEFAULT 100,
    status                  TEXT NOT NULL DEFAULT 'queued',
    call_status             TEXT,
    call_direction          TEXT,
    call_start_time         TIMESTAMPTZ,
    call_end_time           TIMESTAMPTZ,
    call_duration_seconds   INT,
    call_duration_minutes   FLOAT,
    recording_url           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_call_requests_org ON call_requests(org_id);
CREATE INDEX IF NOT EXISTS idx_call_requests_status ON call_requests(status);

-- ── webhook_deliveries ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID REFERENCES organizations(id) ON DELETE CASCADE,
    call_request_id UUID REFERENCES call_requests(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    webhook_url     TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INT NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    next_retry_at   TIMESTAMPTZ,
    response_code   INT,
    response_body   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_org ON webhook_deliveries(org_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status ON webhook_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_retry ON webhook_deliveries(next_retry_at) WHERE status = 'pending';

-- ── org_api_keys ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS org_api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    scopes      JSONB NOT NULL DEFAULT '[]',
    metadata    JSONB NOT NULL DEFAULT '{}',
    expires_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_org_api_keys_org ON org_api_keys(org_id);
CREATE INDEX IF NOT EXISTS idx_org_api_keys_hash ON org_api_keys(key_hash);

-- ── org_api_key_audit_logs ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS org_api_key_audit_logs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        UUID REFERENCES organizations(id) ON DELETE CASCADE,
    key_id        TEXT,
    actor_user_id TEXT,
    action        TEXT NOT NULL,
    key_prefix    TEXT,
    ip_address    TEXT,
    user_agent    TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_org ON org_api_key_audit_logs(org_id);

-- ── call_analysis ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS call_analysis (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_name              TEXT,
    call_id                UUID REFERENCES calls(id) ON DELETE SET NULL,
    insights_config_id     UUID REFERENCES insights_config(id) ON DELETE SET NULL,
    org_id                 UUID REFERENCES organizations(id) ON DELETE CASCADE,
    audio_source_type      TEXT,
    audio_url              TEXT,
    status                 TEXT NOT NULL DEFAULT 'pending',
    api_source             TEXT,
    callback_url           TEXT,
    additional_data        JSONB,
    audio_duration_seconds NUMERIC,
    non_talk_time_seconds  NUMERIC,
    processed_at           TIMESTAMPTZ,
    sentiment_analysis     TEXT,
    agent_sentiment        TEXT,
    customer_sentiment     TEXT,
    transcript_turns       JSONB,
    key_topics             JSONB,
    recommendations        JSONB,
    overall_call_score     NUMERIC,
    overall_summary        TEXT,
    call_outcome           TEXT,
    talk_time_ratio        NUMERIC,
    custom_fields          JSONB,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_call_analysis_org ON call_analysis(org_id);
CREATE INDEX IF NOT EXISTS idx_call_analysis_status ON call_analysis(status);

-- ── insights_jobs ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS insights_jobs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_analysis_id        UUID REFERENCES call_analysis(id) ON DELETE CASCADE,
    org_id                  UUID REFERENCES organizations(id) ON DELETE CASCADE,
    priority                INT NOT NULL DEFAULT 100,
    parent_call_request_id  UUID REFERENCES call_requests(id) ON DELETE SET NULL,
    status                  TEXT NOT NULL DEFAULT 'queued',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_insights_jobs_org ON insights_jobs(org_id);
CREATE INDEX IF NOT EXISTS idx_insights_jobs_status ON insights_jobs(status);
CREATE INDEX IF NOT EXISTS idx_insights_jobs_call_request ON insights_jobs(parent_call_request_id);
