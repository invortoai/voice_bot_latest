# Jambonz Setup Guide for Invorto AI

Complete guide for integrating Jambonz SIP telephony with the Invorto AI Voice Bot Platform. This enables full-duplex voice conversations over WebSocket without traditional IVR commands.

## Table of Contents
- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Environment Configuration](#environment-configuration)
- [Creating Jambonz Application](#creating-jambonz-application)
- [Inbound Call Setup](#inbound-call-setup)
- [Twilio Integration](#twilio-integration)
- [Outbound Call Setup](#outbound-call-setup)
- [Testing Your Setup](#testing-your-setup)
- [Troubleshooting](#troubleshooting)

---

## Overview

### What is Jambonz?

Jambonz is an open-source CPaaS (Communications Platform as a Service) that provides SIP-based telephony capabilities. Unlike Twilio's traditional `<Say>` and `<Gather>` verbs, Jambonz uses a single bidirectional WebSocket connection for full-duplex audio streaming.

### How It Works

```
Caller → Jambonz → WebSocket → Invorto AI Worker → AI Pipeline
         ↑                                           ↓
         └───────────── Audio Response ──────────────┘
```

**Call Flow:**
1. Incoming call arrives at Jambonz (via DID or SIP Domain)
2. Jambonz sends HTTP POST to your `/jambonz/call` webhook
3. Your bot responds with JSON containing `listen` verb pointing to WebSocket URL
4. Jambonz opens bidirectional WebSocket connection to `/ws/jambonz`
5. Audio streams through: Deepgram (STT) → OpenAI (LLM) → ElevenLabs (TTS)
6. Call status updates posted to `/jambonz/status` webhook

**Key Files:**
- `app/routes/jambonz.py` - Webhook endpoints for call and status
- `app/worker/jambonz/transport.py` - WebSocket transport layer
- `app/worker/jambonz/pipeline.py` - Audio processing pipeline
- `.env` - Environment configuration

---

## Prerequisites

### Required Accounts

1. **Jambonz Account**
   - Cloud: Sign up at https://jambonz.cloud
   - Self-hosted: Deploy your own Jambonz instance
   - You'll need access to the Jambonz Console

2. **API Keys**
   - OpenAI API key (for LLM)
   - Deepgram API key (for speech-to-text)
   - ElevenLabs API key and Voice ID (for text-to-speech)

3. **Optional: Twilio Account**
   - If using Twilio as your carrier/trunk
   - Elastic SIP Trunking configured

### Local Development Tools

- Python 3.11+ with virtual environment
- Ngrok (for local webhook testing)
- PostgreSQL database (already configured)
- Bot Runner and Bot Worker running

---

## Environment Configuration

### 1. Jambonz Environment Variables

Add the following to your `.env` file:

```bash
# Jambonz Configuration
JAMBONZ_API_URL=https://jambonz.cloud/api
JAMBONZ_ACCOUNT_SID=your-account-sid
JAMBONZ_API_KEY=your-api-key
JAMBONZ_APPLICATION_SID=your-application-sid

# API Keys (if not already set)
OPENAI_API_KEY=sk-proj-...
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_VOICE_ID=your-voice-id
DEEPGRAM_API_KEY=...

# Public URLs (set after ngrok is running)
PUBLIC_URL=https://your-ngrok-url.ngrok-free.app
PUBLIC_WS_URL=your-worker-ngrok-url.ngrok-free.dev
```

### 2. Get Jambonz Credentials

**For Jambonz Cloud:**

1. Login to https://jambonz.cloud
2. Navigate to **Account** → **Settings**
3. Copy your **Account SID** → `JAMBONZ_ACCOUNT_SID`
4. Navigate to **Developers** → **API Keys**
5. Create a new API key or copy existing one → `JAMBONZ_API_KEY`

**For Self-Hosted Jambonz:**

1. Login to your Jambonz admin panel
2. Find API credentials in the admin settings
3. API URL should be your Jambonz host: `https://your-jambonz-host.com/api`

### 3. Verify Configuration

```bash
# Test that Jambonz API is accessible
curl -X GET \
  ${JAMBONZ_API_URL}/Accounts/${JAMBONZ_ACCOUNT_SID} \
  -H "Authorization: Bearer ${JAMBONZ_API_KEY}"
```

Expected: 200 OK with account details in JSON.

---

## Creating Jambonz Application

A Jambonz **Application** defines what happens when a call arrives. It points to your webhook endpoints.

### Step 1: Start Your Services

```bash
# Terminal 1: Start Bot Runner
make runner
# Or: python app/run_runner.py

# Terminal 2: Start Bot Worker
make worker
# Or: python app/run_worker.py

# Terminal 3: Start Ngrok
ngrok start --all
# Or: ngrok http 7860
```

**Important:** Copy your ngrok URLs:
- Runner URL: `https://abc123.ngrok-free.app` → Update `PUBLIC_URL` in `.env`
- Worker URL: `https://xyz789.ngrok-free.app` → Update `PUBLIC_WS_URL` in `.env`

Restart the runner after updating `.env` to pick up the new URLs.

### Step 2: Create Application in Jambonz Console

1. **Navigate to Applications**
   - Jambonz Console → **Applications** → **Add Application**

2. **Basic Information**
   - **Name**: `Invorto AI` (or any descriptive name)
   - **Description**: `Full-duplex AI voice assistant`

3. **Calling Webhook Configuration**
   - **Method**: `POST`
   - **URL**: `https://<your-ngrok-url>/jambonz/call`
   - Example: `https://abc123.ngrok-free.app/jambonz/call`

4. **Call Status Webhook Configuration**
   - **Method**: `POST`
   - **URL**: `https://<your-ngrok-url>/jambonz/status`
   - Example: `https://abc123.ngrok-free.app/jambonz/status`

5. **Optional: Add Authentication Header**
   - If you want to secure webhooks, add a custom header:
   - **Header Name**: `X-Jambonz-Secret`
   - **Header Value**: `your-secret-value`
   - Then set in `.env`: `JAMBONZ_WEBHOOK_SECRET=your-secret-value`

6. **Save Application**
   - Copy the **Application SID** that gets generated
   - Add to `.env`: `JAMBONZ_APPLICATION_SID=<copied-sid>`

### Step 3: Verify Webhooks are Working

Test the calling webhook:

```bash
curl -X POST https://<your-ngrok-url>/jambonz/call \
  -H "Content-Type: application/json" \
  -d '{
    "call_sid": "test-123",
    "from": "+1234567890",
    "to": "+0987654321"
  }'
```

Expected response:
```json
[
  {
    "verb": "answer"
  },
  {
    "verb": "listen",
    "url": "wss://<your-worker-ngrok>/ws/jambonz",
    "mixType": "mono",
    "actionHook": "https://<your-ngrok-url>/jambonz/action"
  }
]
```

Test the status webhook:

```bash
curl -X POST https://<your-ngrok-url>/jambonz/status \
  -H "Content-Type: application/json" \
  -d '{
    "call_sid": "test-123",
    "call_status": "in-progress"
  }'
```

Expected response:
```json
{
  "status": "ok"
}
```

---

## Inbound Call Setup

Choose one of two methods to route inbound calls to your application:

### Option A: Using a Phone Number (DID)

**Best for:** Production use with PSTN phone numbers

1. **Obtain a Phone Number**
   - Jambonz Console → **Phone Numbers** → **Buy a Number**
   - Or configure a number from your carrier

2. **Assign Application to Number**
   - Jambonz Console → **Phone Numbers**
   - Select your phone number
   - **Application**: Choose `Invorto AI` (the application you created)
   - **Save**

3. **Test the Setup**
   - Call the phone number from your mobile phone
   - You should hear the AI greeting message
   - Check logs in Terminal 1 (runner) and Terminal 2 (worker)

### Option B: Using a SIP Domain

**Best for:** Development, testing, or SIP-only deployments

1. **Create SIP Domain**
   - Jambonz Console → **SIP Domains** → **Add SIP Domain**
   - **Domain Name**: `yourbrand.sip.jambonz.cloud`
   - **Application**: Choose `Invorto AI`
   - **Save**

2. **Optional: Create SIP User for Testing**
   - Jambonz Console → **SIP Credentials** → **Add Credential**
   - **Username**: `testuser`
   - **Password**: `secure-password`
   - **Domain**: `yourbrand.sip.jambonz.cloud`
   - **Save**

3. **Configure a Softphone (for testing)**
   - Download Zoiper, MicroSIP, or similar SIP client
   - **Account Configuration:**
     - Username: `testuser`
     - Password: `secure-password`
     - Domain/Server: `yourbrand.sip.jambonz.cloud`
     - Transport: TLS (Port 5061) or UDP (Port 5060)

4. **Route External Calls to SIP Domain**
   - If you have an upstream SIP trunk or carrier
   - Point INVITEs to: `sip:yourbrand.sip.jambonz.cloud`
   - Jambonz will route to your application

---

## Twilio Integration

Use Twilio Elastic SIP Trunking as your carrier to connect PSTN calls to Jambonz.

### Architecture

```
PSTN Caller → Twilio → Jambonz SIP Domain → Jambonz App → Invorto Worker
```

### Step 1: Create/Configure Twilio Trunk

1. **Login to Twilio Console**
   - Go to **Elastic SIP Trunking** → **Trunks**

2. **Create or Select a Trunk**
   - Click **Create new SIP Trunk** or select existing
   - **Friendly Name**: `Invorto-Jambonz`

3. **Configure Origination (Inbound)**
   - Go to trunk → **Origination** tab
   - Click **Add Origination SIP URI**
   - **Origination SIP URI**: `sip:yourbrand.sip.jambonz.cloud`
   - **Priority**: 10
   - **Weight**: 10
   - **Protocol**: TLS
   - **Port**: 5061
   - **Save**

4. **Assign a Phone Number**
   - Trunk settings → **Numbers** tab
   - Click **Buy a Number** or assign an existing one
   - This number will now route calls to Jambonz

### Step 2: Configure Jambonz to Accept from Twilio

Your SIP Domain in Jambonz should already be configured (from Option B in Inbound Setup). Ensure:
- Domain exists: `yourbrand.sip.jambonz.cloud`
- Application is assigned: `Invorto AI`

### Step 3: Test Inbound via Twilio

1. Call the Twilio phone number you assigned to the trunk
2. Twilio routes to Jambonz SIP domain
3. Jambonz triggers your application webhooks
4. You should hear the AI assistant

### Step 4: Optional - Configure Twilio as Outbound Carrier

To make outbound calls from Jambonz through Twilio:

1. **Get Twilio Termination Credentials**
   - Twilio Console → Elastic SIP Trunking → Your Trunk
   - **Termination** tab → **Credentials**
   - Note down:
     - Termination SIP URI: `yourtrunk.pstn.twilio.com`
     - Username and Password (from Credential List)

2. **Add Twilio as Carrier in Jambonz**
   - Jambonz Console → **Carriers** → **Add Carrier**
   - **Name**: `twilio`
   - **Protocol**: SIP
   - **SIP Gateway Configuration:**
     - **Host**: `yourtrunk.pstn.twilio.com`
     - **Port**: 5061
     - **Transport**: TLS
     - **Username**: (from Twilio credentials)
     - **Password**: (from Twilio credentials)
   - **Save**

3. **Create Outbound Route**
   - Jambonz Console → **Outbound Routes** → **Add Route**
   - **Name**: `to-twilio`
   - **Pattern**: `^\+?1?\d{10}$` (for US numbers) or `^\+?\d{7,15}$` (international)
   - **Carrier**: Select `twilio`
   - **Save**

---

## Outbound Call Setup

Enable your application to make outbound calls programmatically.

### Prerequisites

Ensure these are set in `.env`:
```bash
JAMBONZ_API_URL=https://jambonz.cloud/api
JAMBONZ_ACCOUNT_SID=your-account-sid
JAMBONZ_API_KEY=your-api-key
JAMBONZ_APPLICATION_SID=your-application-sid
```

### Making Outbound Calls via API

Use the `/call/outbound` endpoint:

```bash
curl -X POST http://localhost:7860/call/outbound \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "to_number": "+19995551234",
    "phone_number_id": "<phone-number-uuid>",
    "assistant_id": "<assistant-uuid>"
  }'
```

**Parameters:**
- `to_number`: Destination phone number (E.164 format)
- `phone_number_id`: UUID of phone number record with Jambonz credentials
- `assistant_id`: UUID of assistant to use for the call

**Response:**
```json
{
  "call_id": "uuid",
  "status": "initiated",
  "provider": "jambonz",
  "provider_sid": "call-sid-from-jambonz"
}
```

### Outbound Call Flow

1. Your API receives outbound call request
2. System looks up phone number and assistant configurations
3. Makes REST API call to Jambonz `/Calls` endpoint
4. Jambonz initiates call through configured carrier (e.g., Twilio)
5. When call answers, Jambonz hits your webhook
6. Application returns `listen` verb pointing to WebSocket
7. Audio processing begins (same as inbound)

---

## Testing Your Setup

### 1. Verify All Services Running

```bash
# Check runner health
curl http://localhost:7860/health

# Check worker health
curl http://localhost:8765/health

# Check ngrok tunnels
curl http://localhost:4040/api/tunnels
```

### 2. Test Jambonz Webhooks

**Test Call Webhook:**
```bash
curl -X POST https://<ngrok-url>/jambonz/call \
  -H "Content-Type: application/json" \
  -d '{
    "call_sid": "test-incoming-123",
    "from": "+11234567890",
    "to": "+10987654321",
    "call_id": "test-call-id"
  }'
```

Should return JSON with `answer` and `listen` verbs.

**Test Status Webhook:**
```bash
curl -X POST https://<ngrok-url>/jambonz/status \
  -H "Content-Type: application/json" \
  -d '{
    "call_sid": "test-incoming-123",
    "call_status": "completed",
    "duration": 45
  }'
```

Should return `{"status": "ok"}`.

### 3. Place a Test Call

**Inbound Test:**
- Call your Jambonz phone number
- Or use your SIP softphone to call the configured target

**Expected Logs (Terminal 1 - Runner):**
```
INFO: Jambonz incoming call webhook received
INFO: Assigned worker: <worker-ip>:8765
INFO: Returning listen verb for WebSocket
```

**Expected Logs (Terminal 2 - Worker):**
```
INFO: WebSocket connection established for call: <call-sid>
INFO: Starting Jambonz media bridge
INFO: Deepgram STT initialized
INFO: OpenAI LLM initialized
INFO: ElevenLabs TTS initialized
INFO: Audio pipeline started
DEBUG: Received audio frame: 160 samples
DEBUG: STT transcript: "hello"
DEBUG: LLM response: "Hi! How can I help you today?"
DEBUG: Sending TTS audio frame
```

### 4. Monitor Jambonz Console

- Jambonz Console → **Recent Calls**
- Click on your test call
- Verify:
  - Call connected successfully
  - Application webhook was invoked
  - WebSocket connection established
  - Call duration and status

---

## Troubleshooting

### Issue: Webhook Returns 404

**Symptoms:** Jambonz can't reach your webhooks

**Solutions:**
1. Verify ngrok is running: `curl http://localhost:4040/api/tunnels`
2. Check `PUBLIC_URL` in `.env` matches your ngrok URL
3. Restart runner after updating `.env`
4. Test webhook directly:
   ```bash
   curl https://<ngrok-url>/jambonz/call
   ```

### Issue: WebSocket Connection Fails

**Symptoms:** Call connects but no audio processing

**Logs:** `WebSocket connection failed` or `Connection refused`

**Solutions:**
1. Verify worker is running: `curl http://localhost:8765/health`
2. Check `PUBLIC_WS_URL` points to correct ngrok tunnel
3. Ensure worker ngrok tunnel is WebSocket-capable
4. Check worker logs for connection errors
5. Verify no firewall blocking WebSocket connections

### Issue: No Audio from AI

**Symptoms:** Caller hears silence, no AI responses

**Diagnostic Steps:**
1. Check worker logs for audio processing errors
2. Verify API keys are set correctly:
   ```bash
   echo $OPENAI_API_KEY
   echo $DEEPGRAM_API_KEY
   echo $ELEVENLABS_API_KEY
   ```
3. Test individual services:
   - Deepgram: Check usage dashboard
   - OpenAI: Verify API key is active
   - ElevenLabs: Confirm voice ID exists

**Solutions:**
- Update API keys in `.env`
- Restart both runner and worker
- Check for rate limiting in provider dashboards
- Review audio codec configuration

### Issue: AI Not Hearing Caller

**Symptoms:** AI doesn't respond to what caller says

**Diagnostic Steps:**
1. Check Deepgram logs for transcription activity
2. Verify audio format matches expected:
   - Sample rate: 8000 Hz (Jambonz default)
   - Encoding: PCM16
   - Channels: Mono
3. Look for STT errors in worker logs

**Solutions:**
- Confirm Deepgram API key is valid
- Check Deepgram quota/usage limits
- Verify audio encoding configuration in Jambonz
- Test with different audio input

### Issue: Twilio → Jambonz Connection Fails

**Symptoms:** Calls to Twilio number fail or never reach Jambonz

**Diagnostic Steps:**
1. Twilio Console → **Monitor** → **Logs**
2. Look for SIP errors (403, 488, etc.)
3. Verify trunk configuration

**Solutions:**
- Ensure Origination URI is correct: `sip:yourbrand.sip.jambonz.cloud`
- Use TLS with port 5061 (not UDP 5060)
- Verify SIP domain exists in Jambonz
- Check Jambonz application is assigned to domain
- Review Jambonz logs for rejected INVITEs

### Issue: Authentication Errors (401)

**Symptoms:** Webhooks return 401 Unauthorized

**Solutions:**
1. If using webhook secret:
   - Ensure `JAMBONZ_WEBHOOK_SECRET` is set in `.env`
   - Add `X-Jambonz-Secret` header in Jambonz application config
   - Header value must match `.env` value exactly
2. If using API key authentication:
   - Verify `API_KEY` in `.env` matches request header
   - Include `X-API-Key` header in requests

### Issue: Database Errors

**Symptoms:** Call records not saving, assistant lookup fails

**Solutions:**
1. Check database connection:
   ```bash
   psql $DATABASE_URL -c "SELECT 1"
   ```
2. Run migrations:
   ```bash
   cd migrations
   python migrate.py
   ```
3. Verify tables exist:
   ```bash
   psql $DATABASE_URL -c "\dt"
   ```

### Getting More Help

1. **Check Logs**
   - Runner logs: Terminal 1 or `docker logs <runner-container>`
   - Worker logs: Terminal 2 or `docker logs <worker-container>`
   - Jambonz logs: Jambonz Console → System Logs

2. **Enable Debug Logging**
   ```bash
   # Add to .env
   LOG_LEVEL=DEBUG
   ```

3. **Jambonz Resources**
   - Documentation: https://docs.jambonz.org
   - Community Forum: https://community.jambonz.org
   - GitHub Issues: https://github.com/jambonz

---

## Production Deployment

### SSL/TLS Requirements

For production, you need:
- Valid SSL certificate for your runner domain
- WebSocket endpoint must support WSS (not WS)
- Use a proper domain instead of ngrok

### Recommended Configuration

```bash
# Production .env
ENVIRONMENT=production

# Use your production domain
PUBLIC_URL=https://api.yourdomain.com
PUBLIC_WS_URL=wss://worker.yourdomain.com

# Secure with webhook secret
JAMBONZ_WEBHOOK_SECRET=long-random-secure-value

# Jambonz production API
JAMBONZ_API_URL=https://api.jambonz.cloud
JAMBONZ_ACCOUNT_SID=prod-account-sid
JAMBONZ_API_KEY=prod-api-key
JAMBONZ_APPLICATION_SID=prod-app-sid
```

### Update Jambonz Application

After deploying to production:
1. Jambonz Console → **Applications** → Your App
2. Update webhook URLs to production domains
3. Add webhook secret header if configured
4. Test with a live call

### Monitoring

Monitor these metrics:
- Call success rate (Jambonz Console)
- WebSocket connection stability (worker logs)
- Audio processing latency (worker logs)
- API provider usage (Deepgram, OpenAI, ElevenLabs dashboards)
- Database query performance

---

## Additional Resources

- **Jambonz Documentation**: https://docs.jambonz.org
- **Jambonz API Reference**: https://api.jambonz.cloud/docs
- **Twilio SIP Trunking**: https://www.twilio.com/docs/sip-trunking
- **Pipecat Framework**: https://github.com/pipecat-ai/pipecat
- **Deepgram API**: https://developers.deepgram.com
- **ElevenLabs API**: https://docs.elevenlabs.io

---

