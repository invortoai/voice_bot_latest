# STT, TTS & VAD Configuration Reference

Complete reference for configuring speech-to-text (Deepgram), text-to-speech (ElevenLabs),
and voice activity detection (Silero VAD + SmartTurn) on an assistant.
All settings are stored in the `assistants` table and applied at call time.

---

## How Configuration Maps to Database Columns

| What you're configuring | DB Column | Type |
|---|---|---|
| Deepgram model | `transcriber_model` | `VARCHAR(100)` |
| Deepgram language | `transcriber_language` | `VARCHAR(10)` |
| All other Deepgram options | `transcriber_settings` | `JSONB` |
| ElevenLabs voice | `voice_id` | `VARCHAR(255)` |
| ElevenLabs model | `voice_model` | `VARCHAR(100)` |
| All other ElevenLabs options | `voice_settings` | `JSONB` |
| VAD + SmartTurn options | `vad_settings` | `JSONB` |

`transcriber_settings`, `voice_settings`, and `vad_settings` accept any key from the tables below as a flat JSON object.

---

## Part 1 — STT: Deepgram LiveOptions

### Top-level columns (not in `transcriber_settings`)

#### `transcriber_model`
The Deepgram model used for transcription. This is the single most impactful setting for accuracy.

| Value | Description | Best for |
|---|---|---|
| `nova-3-general` | Latest generation, highest accuracy | General English voice bots (recommended default) |
| `nova-2` | Previous generation, still excellent | Good fallback, slightly cheaper |
| `nova-2-phonecall` | Tuned for phone audio (mulaw, noise) | Twilio / MCube calls |
| `nova-2-conversational` | Tuned for casual speech patterns | Informal assistants |
| `nova-2-meeting` | Tuned for multi-speaker meetings | Not suitable for single-caller bots |
| `enhanced` | Older high-accuracy model | Legacy use only |
| `base` | Fast but lower accuracy | Not recommended for production |

**Default:** `nova-2`
**Recommended for telephony:** `nova-3-general` or `nova-2-phonecall`

---

#### `transcriber_language`
BCP-47 language code. Tells Deepgram which language to expect.

| Code | Language |
|---|---|
| `en` | English (auto-detects US/UK/AU accent) |
| `en-US` | English — US accent prioritised |
| `en-IN` | English — Indian accent prioritised |
| `hi` | Hindi |
| `es` | Spanish |
| `fr` | French |
| `de` | German |
| `pt` | Portuguese |
| `ar` | Arabic |
| `ja` | Japanese |
| `ko` | Korean |
| `zh` | Chinese (Mandarin) |

> Full list: https://developers.deepgram.com/docs/models-languages-overview

**Default:** `en`
**Impact:** Wrong language = near-zero accuracy. Always set this explicitly.

---

### `transcriber_settings` JSONB options

#### `interim_results` (bool)
Stream partial transcriptions before the speaker finishes. Enables the pipeline to start
processing user intent earlier and reduces perceived latency.

- **Default:** `true`
- **Recommended:** `true` — required for low-latency voice bots
- **Impact if false:** Bot only receives complete utterances, adding 200–800ms extra delay

---

#### `punctuate` (bool)
Add punctuation (commas, periods, question marks) to transcripts automatically.

- **Default:** `true`
- **Recommended:** `true`
- **Impact:** LLM prompt quality improves significantly with punctuation. "yes i want the plan" vs "Yes, I want the plan."

---

#### `smart_format` (bool)
Auto-format numbers, dates, times, currencies, phone numbers, and addresses.
"twenty five dollars" → "$25", "january fifteenth" → "January 15th".

- **Default:** `true`
- **Recommended:** `true` for most bots; `false` if your LLM prompt is sensitive to formatted symbols
- **Impact:** Cleaner LLM input, especially for bots handling orders, bookings, or payments

---

#### `profanity_filter` (bool)
Replace profane words with `****` in the transcript.

- **Default:** `true`
- **Recommended:** `true` for customer-facing bots; `false` for internal/research bots
- **Impact:** Prevents profanity from reaching the LLM context

---

#### `filler_words` (bool)
Include filler words — "um", "uh", "hmm", "like" — in the transcript.

- **Default:** `false`
- **Recommended:** `false` — these words add noise to the LLM context without meaning
- **When to enable:** If your LLM is specifically trained to detect hesitation or uncertainty from filler words

---

#### `numerals` (bool)
Convert spoken numbers to digits. "forty two" → "42".
Note: `smart_format: true` supersedes this for most cases.

- **Default:** `false`
- **Recommended:** Use `smart_format: true` instead — it covers numerals plus more

---

#### `endpointing` (int | false)
Milliseconds of silence Deepgram waits after the last word before finalising an utterance
and emitting a final transcript. Controls how quickly the bot "cuts in".

- **Default:** `10` ms (Deepgram default)
- **Recommended for voice bots:** `200`–`400` ms
- **Too low (< 100ms):** Bot interrupts mid-sentence during natural pauses
- **Too high (> 600ms):** Bot feels sluggish and unresponsive
- **Set to `false`:** Disable Deepgram endpointing entirely — use only pipeline VAD (Silero)

> This interacts with the pipeline's Silero VAD `stop_secs` setting. Together they determine
> turn-taking responsiveness. Typically set `endpointing` to match or slightly exceed `stop_secs * 1000`.

---

#### `utterance_end_ms` (string, e.g. `"1000"`)
Time in milliseconds to wait after the last word before emitting an `UtteranceEnd` event.
Only relevant when `vad_events: true` (deprecated — use Silero VAD instead).

- **Default:** Not set
- **Recommended:** Leave unset; use Silero VAD for endpointing

---

#### `diarize` (bool)
Speaker diarization — label each word with which speaker said it (Speaker 0, Speaker 1, etc.).

- **Default:** `false`
- **Recommended for voice bots:** `false` — single-caller scenario, adds unnecessary latency
- **When to enable:** Conference call or multi-participant scenarios

---

#### `channels` (int)
Number of audio channels to process.

- **Default:** `1`
- **Recommended:** `1` — telephony audio is always mono
- **Do not change** unless your audio transport sends stereo

---

#### `encoding` (string)
Audio encoding format. **Do not set this in `transcriber_settings`** — it is automatically
set from the telephony provider:

| Provider | Encoding set automatically |
|---|---|
| Twilio | `mulaw` |
| Jambonz | `linear16` |
| MCube | Read from WebSocket start message |

Setting this in `transcriber_settings` would override the provider value and likely break audio.

---

#### `redact` (string | list | bool)
Redact sensitive information from transcripts.

| Value | What is redacted |
|---|---|
| `"pci"` | Credit card numbers |
| `"ssn"` | Social Security Numbers |
| `"numbers"` | All numeric sequences |
| `true` | All of the above |
| `["pci", "ssn"]` | Multiple categories |

- **Default:** Not set (no redaction)
- **Recommended:** `["pci"]` for bots handling payments; `false` otherwise
- **Impact:** Redacted words appear as `[redacted]` in transcript — LLM cannot use them

---

#### `replace` (string | list)
Word substitution in transcripts. Format: `"original:replacement"`.
Example: `["gonna:going to", "wanna:want to"]`

- **Default:** Not set
- **Use case:** Normalise brand names ("eyevel" → "ElevenLabs"), fix common ASR errors
- **Impact:** Applied before LLM sees the text — useful for domain-specific vocabulary

---

#### `keywords` (string | list)
Boost recognition probability for specific words. Format: `"word:intensifier"` (intensifier 1–5).
Example: `["Invorto:3", "invoice:2"]`

- **Default:** Not set
- **Recommended:** Add your product name, domain terms, and customer-specific vocabulary
- **Impact:** Reduces misrecognition of proper nouns and technical terms

---

#### `keyterm` (list of strings)
Newer alternative to `keywords` — boost recognition of specific terms without an intensifier.
Example: `["Invorto", "GST invoice"]`

- **Default:** Not set
- **Recommended:** Prefer `keyterm` over `keywords` for `nova-3` models

---

#### `search` (string | list)
Highlight specific words/phrases in the response metadata. Does not affect transcript text.

- **Default:** Not set
- **Use case:** Analytics — detect when users mention specific terms
- **Impact on call quality:** None (metadata only)

---

#### `multichannel` (bool)
Transcribe each audio channel independently. Requires `channels > 1`.

- **Default:** `false`
- **Recommended:** `false` for all standard telephony use cases

---

#### `alternatives` (int)
Return N alternative transcriptions ranked by confidence.

- **Default:** `1`
- **Recommended:** `1` — multiple alternatives are not used by the pipeline

---

#### `vad_events` (bool)
Use Deepgram's own VAD for turn detection instead of the pipeline's Silero VAD.

- **Default:** `false`
- **Recommended:** `false` — **deprecated as of Pipecat 0.0.99**. The pipeline uses Silero VAD which is superior. Enabling this may cause conflicts.

---

### Recommended `transcriber_settings` by use case

**Standard English voice bot (Twilio):**
```json
{
  "interim_results": true,
  "punctuate": true,
  "smart_format": true,
  "profanity_filter": true,
  "endpointing": 300
}
```

**Indian English / Hindi bot:**
```json
{
  "interim_results": true,
  "punctuate": true,
  "smart_format": true,
  "profanity_filter": false,
  "endpointing": 350,
  "keywords": ["your-brand:3"]
}
```

**Payment / finance bot (with PCI redaction):**
```json
{
  "interim_results": true,
  "punctuate": true,
  "smart_format": true,
  "profanity_filter": true,
  "endpointing": 300,
  "redact": ["pci", "ssn"]
}
```

**Fast-response bot (prioritise latency over accuracy):**
```json
{
  "interim_results": true,
  "punctuate": false,
  "smart_format": false,
  "endpointing": 150
}
```

---

## Part 2 — TTS: ElevenLabs InputParams

### Top-level columns (not in `voice_settings`)

#### `voice_id`
The ElevenLabs voice ID. Find IDs in the ElevenLabs dashboard under Voices.
This is the most impactful setting for how your bot sounds.

- **Required:** Yes — no fallback default
- **Impact:** Determines the entire character and tone of the bot's voice

---

#### `voice_model`
The ElevenLabs synthesis model. Controls quality vs latency trade-off.

| Value | Latency | Quality | Languages | Notes |
|---|---|---|---|---|
| `eleven_flash_v2_5` | ~75ms | High | 32 | **Best for voice bots** — ultra-low latency |
| `eleven_turbo_v2_5` | ~250ms | Very high | 32 | Good balance of quality and speed |
| `eleven_multilingual_v2` | ~400ms | Highest | 29 | Too slow for real-time bots |
| `eleven_turbo_v2` | ~300ms | High | English only | Legacy |
| `eleven_flash_v2` | ~100ms | Good | English only | Legacy |

**Default:** `eleven_flash_v2_5`
**Recommended:** `eleven_flash_v2_5` for all real-time voice bots

> Only `eleven_flash_v2_5` and `eleven_turbo_v2_5` support the `language` param in `voice_settings`.

---

### `voice_settings` JSONB options

#### `stability` (float, 0.0–1.0)
Controls consistency of the voice across utterances.

- **Low (0.0–0.3):** More expressive, emotional, varied — can sound inconsistent across turns
- **Mid (0.4–0.6):** Natural conversational variation — **recommended range**
- **High (0.7–1.0):** Very consistent and predictable — can sound robotic
- **Default:** Not set (ElevenLabs uses `0.5` internally)
- **Recommended for voice bots:** `0.5`

---

#### `similarity_boost` (float, 0.0–1.0)
How closely the output adheres to the original voice clone/sample.

- **Low (0.0–0.4):** More generative, less tied to original — can drift
- **Mid (0.5–0.75):** Good adherence with naturalness — **recommended range**
- **High (0.76–1.0):** Very close to original — can introduce audio artefacts on some voices
- **Default:** Not set (ElevenLabs uses `0.75` internally)
- **Recommended for voice bots:** `0.75`

---

#### `style` (float, 0.0–1.0)
Amplify the style and expressiveness of the voice.

- **`0.0`:** No style exaggeration — neutral delivery
- **`0.1–0.3`:** Subtle expressiveness — good for professional bots
- **`0.4–0.7`:** Noticeable expressiveness — suitable for friendly/casual bots
- **`0.8–1.0`:** Strong style amplification — can sound overdone; increases latency
- **Default:** Not set (ElevenLabs uses `0.0` internally)
- **Recommended for voice bots:** `0.0`–`0.2` — avoid high values as they add latency

---

#### `use_speaker_boost` (bool)
Apply additional audio processing to enhance speaker clarity.

- **Default:** Not set (ElevenLabs uses `true` internally)
- **Recommended:** `true`
- **Impact:** Slightly cleaner audio over telephony. Minor latency increase (~10ms).

---

#### `speed` (float, 0.7–1.2)
Playback speed multiplier for synthesised speech.

- **`0.7`:** 30% slower — good for elderly callers or complex information
- **`1.0`:** Normal speed
- **`1.1–1.2`:** Slightly faster — suits high-energy or sales bots
- **Default:** Not set (ElevenLabs uses `1.0` internally)
- **Recommended for voice bots:** `1.0`–`1.1`
- **Impact on conversation:** Higher speed = less natural pauses = user may feel rushed

---

#### `language` (string, language code)
Force the synthesis language. Only works with multilingual models
(`eleven_flash_v2_5`, `eleven_turbo_v2_5`).

| Code | Language |
|---|---|
| `en` | English |
| `hi` | Hindi |
| `es` | Spanish |
| `fr` | French |
| `de` | German |
| `pt` | Portuguese |
| `ar` | Arabic |
| `ja` | Japanese |
| `ko` | Korean |
| `zh` | Chinese |

- **Default:** Not set (model auto-detects from text)
- **Recommended:** Set explicitly when you know the language — auto-detection adds latency
- **Impact:** Correct language code improves accent and intonation significantly

---

#### `auto_mode` (bool)
Optimise the streaming pipeline automatically for low latency and quality.

- **Default:** `true`
- **Recommended:** `true` — leave enabled; it is ElevenLabs' own latency optimisation
- **Impact if false:** Slightly higher latency with no quality benefit in most cases

---

#### `apply_text_normalization` (`"auto"` | `"on"` | `"off"`)
Control how text is normalised before synthesis (numbers, abbreviations, symbols).

| Value | Behaviour |
|---|---|
| `"auto"` | ElevenLabs decides (default) |
| `"on"` | Always normalise — "£25" → "twenty-five pounds" |
| `"off"` | No normalisation — pass text as-is |

- **Default:** Not set (`"auto"` behaviour)
- **Recommended:** `"auto"` for most bots; `"on"` for bots with currency/number-heavy responses

---

#### `enable_ssml_parsing` (bool)
Parse SSML tags in TTS input text (e.g. `<break time="500ms"/>`, `<emphasis>`).

- **Default:** Not set (`false`)
- **Recommended:** `false` unless your LLM is explicitly prompted to output SSML
- **Impact:** If enabled without SSML in prompts, angle brackets in normal text may cause errors

---

#### `enable_logging` (bool)
Allow ElevenLabs to log the request on their servers for quality improvement.

- **Default:** Not set (`true` on free tier, enterprise may differ)
- **Recommended:** `false` for production bots handling PII

---

### Recommended `voice_settings` by use case

**Standard professional bot (English):**
```json
{
  "stability": 0.5,
  "similarity_boost": 0.75,
  "style": 0.0,
  "use_speaker_boost": true,
  "speed": 1.0
}
```

**Friendly / casual bot:**
```json
{
  "stability": 0.45,
  "similarity_boost": 0.7,
  "style": 0.2,
  "use_speaker_boost": true,
  "speed": 1.05
}
```

**Hindi / multilingual bot:**
```json
{
  "language": "hi",
  "stability": 0.5,
  "similarity_boost": 0.75,
  "style": 0.0,
  "use_speaker_boost": true,
  "speed": 0.95,
  "apply_text_normalization": "on"
}
```

**High-clarity bot for elderly / complex info:**
```json
{
  "stability": 0.6,
  "similarity_boost": 0.8,
  "style": 0.0,
  "use_speaker_boost": true,
  "speed": 0.85
}
```

---

## Part 3 — Setting Up an Assistant via API

### Create assistant (POST /assistants)

```json
{
  "name": "Sales Bot - English",
  "system_prompt": "You are a helpful sales assistant...",
  "model": "gpt-4o-mini",
  "temperature": 0.7,
  "max_tokens": 150,

  "transcriber_provider": "deepgram",
  "transcriber_model": "nova-3-general",
  "transcriber_language": "en-IN",
  "transcriber_settings": {
    "interim_results": true,
    "punctuate": true,
    "smart_format": true,
    "profanity_filter": true,
    "endpointing": 300,
    "keywords": ["Invorto:3", "GST:2"]
  },

  "voice_provider": "elevenlabs",
  "voice_id": "YOUR_ELEVENLABS_VOICE_ID",
  "voice_model": "eleven_flash_v2_5",
  "voice_settings": {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": true,
    "speed": 1.0
  }
}
```

### Update assistant (PUT /assistants/{id})
Send only the fields you want to change:

```json
{
  "transcriber_language": "hi",
  "transcriber_settings": {
    "interim_results": true,
    "punctuate": true,
    "smart_format": true,
    "endpointing": 350,
    "keywords": ["Invorto:3"]
  },
  "voice_settings": {
    "language": "hi",
    "stability": 0.5,
    "similarity_boost": 0.75,
    "speed": 0.95,
    "apply_text_normalization": "on"
  }
}
```

### Direct SQL (migrations / seeding)

```sql
INSERT INTO assistants (
    name,
    system_prompt,
    model,
    transcriber_provider,
    transcriber_model,
    transcriber_language,
    transcriber_settings,
    voice_provider,
    voice_id,
    voice_model,
    voice_settings
) VALUES (
    'Sales Bot - English',
    'You are a helpful sales assistant...',
    'gpt-4o-mini',
    'deepgram',
    'nova-3-general',
    'en-IN',
    '{"interim_results": true, "punctuate": true, "smart_format": true, "profanity_filter": true, "endpointing": 300, "keywords": ["Invorto:3"]}',
    'elevenlabs',
    'YOUR_VOICE_ID',
    'eleven_flash_v2_5',
    '{"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": true, "speed": 1.0}'
);
```
---

## Part 4 — Quick Reference: Settings That Matter Most

### STT — ranked by impact on voice bot quality

| Priority | Setting | Recommended value | Why |
|---|---|---|---|
| 1 | `transcriber_model` | `nova-3-general` | Single biggest accuracy lever |
| 2 | `transcriber_language` | Match your callers | Wrong language = broken transcription |
| 3 | `endpointing` | `200`–`400` | Controls turn-taking responsiveness |
| 4 | `punctuate` | `true` | Cleaner LLM input |
| 5 | `smart_format` | `true` | Handles numbers/dates in LLM input |
| 6 | `keywords` / `keyterm` | Your domain terms | Reduces misrecognition of brand names |
| 7 | `interim_results` | `true` | Required for low latency |
| 8 | `redact` | `["pci"]` if payments | Compliance |

### TTS — ranked by impact on voice bot quality

| Priority | Setting | Recommended value | Why |
|---|---|---|---|
| 1 | `voice_id` | Choose carefully | Entire character of the bot |
| 2 | `voice_model` | `eleven_flash_v2_5` | Latency — use nothing slower for real-time |
| 3 | `speed` | `1.0`–`1.1` | Pacing affects user experience significantly |
| 4 | `stability` | `0.5` | Too high = robotic, too low = inconsistent |
| 5 | `similarity_boost` | `0.75` | Faithful to voice without artefacts |
| 6 | `language` | Match `transcriber_language` | Critical for non-English bots |
| 7 | `style` | `0.0`–`0.2` | Keep low — high values add latency |
| 8 | `apply_text_normalization` | `"auto"` or `"on"` | Important for number-heavy responses |
---

## Part 5 — VAD: SileroVADAnalyzer + SmartTurn

### How VAD works in the pipeline

Turn detection uses two layers working together:

```
Audio in → Silero VAD → detects speech/silence boundaries
                ↓
         SmartTurn model → decides if user has actually finished their turn
                ↓
         LLM prompt sent
```

- **Silero VAD** (`SileroVADAnalyzer` / `VADParams`) — low-level, fast. Classifies each audio
  chunk as speech or silence based on an ONNX neural model. Controls when the pipeline registers
  that the user started or stopped making sound.

- **SmartTurn** (`LocalSmartTurnAnalyzerV3` / `SmartTurnParams`) — high-level, slower. After
  Silero declares silence, SmartTurn analyses the full utterance audio to decide whether the
  user has genuinely finished their turn (e.g. mid-sentence pause vs. real end-of-turn).
  This prevents the bot from interrupting during natural pauses.

All settings live in the single `vad_settings` JSONB column.

---

### `vad_settings` JSONB options

#### VAD (Silero) settings

##### `confidence` (float, 0.0–1.0)
Minimum probability score from the Silero model to register a chunk as speech.

- **Low (0.3–0.5):** More sensitive — picks up whispers, background speech, noise
- **Mid (0.6–0.75):** Balanced — **recommended range**
- **High (0.8–1.0):** Only registers clear, loud speech — may miss soft-spoken callers
- **Default:** `0.7`
- **Recommended:** `0.7` for telephony; lower to `0.5` if callers speak softly

---

##### `start_secs` (float, seconds)
How long continuous speech must be detected before the pipeline fires a
`UserStartedSpeakingFrame`. Prevents very brief sounds (coughs, clicks) from
triggering a turn.

- **Low (0.05–0.1s):** Very reactive — responds to any sound immediately
- **Mid (0.15–0.25s):** Filters short spurious noises — **recommended range**
- **High (0.3s+):** May delay detecting real speech start; bot feels slow to notice caller
- **Default:** `0.2`
- **Recommended:** `0.2`

---

##### `stop_secs` (float, seconds)
How long silence must persist after speech before the pipeline fires a
`UserStoppedSpeakingFrame`. This is the primary "how long to wait after the caller
stops" setting — it feeds the SmartTurn model.

- **Low (0.2–0.4s):** Bot cuts in quickly — risk of interrupting mid-sentence pauses
- **Mid (0.5–0.8s):** Balanced responsiveness vs. patience — **recommended range**
- **High (1.0s+):** Very patient — bot waits a long time; feels unresponsive
- **Default:** `0.8`
- **Recommended:** `0.5`–`0.8`

> **Important:** The previous hardcoded value was `0.2` — far too aggressive.
> It caused the bot to cut in during mid-sentence pauses. The new default is `0.8`.

---

##### `min_volume` (float, 0.0–1.0)
Minimum RMS audio energy required alongside the confidence score. Acts as a
secondary noise gate — audio below this volume is ignored regardless of model score.

- **Low (0.2–0.4):** Picks up very quiet audio and background hiss
- **Mid (0.5–0.7):** Filters low-level noise on telephony — **recommended range**
- **High (0.8+):** Only responds to loud audio — may miss callers in quiet rooms
- **Default:** `0.6`
- **Recommended:** `0.6`

---

#### SmartTurn settings

##### `smart_turn_stop_secs` (float, seconds)
After `stop_secs` of silence (Silero), SmartTurn waits up to this duration to collect
additional audio context before running its model. Longer values give the model more
data but increase latency.

- **Low (1.0–2.0s):** Fast response — model gets less context, slightly lower accuracy
- **Mid (2.5–3.5s):** Good balance — **recommended range**
- **High (4.0s+):** Very accurate turn detection but noticeable delay
- **Default:** `3.0`
- **Recommended:** `2.0`–`3.0`

---

##### `smart_turn_pre_speech_ms` (float, milliseconds)
How much audio **before** the detected speech start is included in the model's input.
Pre-speech context helps the model understand the utterance beginning.

- **Default:** `500` ms
- **Recommended:** `500` — rarely needs changing
- **Impact:** Too low = model misses utterance start; too high = wastes compute on silence

---

##### `smart_turn_max_duration_secs` (float, seconds)
Maximum audio window (in seconds) the SmartTurn model analyses. Long utterances
are truncated to this length. The model was trained on segments up to ~8 seconds.

- **Default:** `8.0`
- **Recommended:** `8.0` — only reduce if callers consistently speak very briefly
- **Impact if too low:** Model may cut off long sentences and misclassify as end-of-turn

---

### Recommended `vad_settings` by use case

**Standard voice bot (balanced responsiveness + accuracy):**
```json
{
  "confidence": 0.7,
  "start_secs": 0.2,
  "stop_secs": 0.6,
  "min_volume": 0.6,
  "smart_turn_stop_secs": 2.5,
  "smart_turn_pre_speech_ms": 500,
  "smart_turn_max_duration_secs": 8.0
}
```

**Fast-response bot (sales, outbound — minimise bot latency):**
```json
{
  "confidence": 0.7,
  "start_secs": 0.1,
  "stop_secs": 0.4,
  "min_volume": 0.6,
  "smart_turn_stop_secs": 1.5,
  "smart_turn_pre_speech_ms": 300,
  "smart_turn_max_duration_secs": 6.0
}
```

**Patient bot (elderly callers, complex info, slow speakers):**
```json
{
  "confidence": 0.6,
  "start_secs": 0.2,
  "stop_secs": 1.0,
  "min_volume": 0.5,
  "smart_turn_stop_secs": 4.0,
  "smart_turn_pre_speech_ms": 500,
  "smart_turn_max_duration_secs": 8.0
}
```

**Noisy environment (call centre background noise):**
```json
{
  "confidence": 0.75,
  "start_secs": 0.25,
  "stop_secs": 0.7,
  "min_volume": 0.7,
  "smart_turn_stop_secs": 3.0,
  "smart_turn_pre_speech_ms": 500,
  "smart_turn_max_duration_secs": 8.0
}
```

---

### How VAD interacts with Deepgram `endpointing`

Both systems influence when the bot cuts in — they must be tuned together:

| Setting | Layer | Effect |
|---|---|---|
| `vad_settings.stop_secs` | Pipeline (Silero) | Silence duration before SmartTurn runs |
| `vad_settings.smart_turn_stop_secs` | Pipeline (SmartTurn) | Max wait for SmartTurn model inference |
| `transcriber_settings.endpointing` | Deepgram (STT) | Silence before Deepgram emits final transcript |

**Rule of thumb:** Set `transcriber_settings.endpointing` to roughly `vad_settings.stop_secs * 1000`
(e.g. `stop_secs=0.6` → `endpointing=600`). This keeps the Deepgram final transcript and the
SmartTurn decision arriving at roughly the same time.

---

### Adding VAD config when creating an assistant (API)

```json
{
  "name": "Sales Bot",
  "system_prompt": "...",
  "transcriber_model": "nova-3-general",
  "transcriber_language": "en-IN",
  "transcriber_settings": {
    "interim_results": true,
    "punctuate": true,
    "smart_format": true,
    "endpointing": 600
  },
  "voice_model": "eleven_flash_v2_5",
  "voice_id": "YOUR_VOICE_ID",
  "voice_settings": {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "speed": 1.0
  },
  "vad_settings": {
    "confidence": 0.7,
    "start_secs": 0.2,
    "stop_secs": 0.6,
    "min_volume": 0.6,
    "smart_turn_stop_secs": 2.5,
    "smart_turn_pre_speech_ms": 500,
    "smart_turn_max_duration_secs": 8.0
  }
}
```

### SQL (direct insert)

```sql
UPDATE assistants
SET vad_settings = '{
  "confidence": 0.7,
  "start_secs": 0.2,
  "stop_secs": 0.6,
  "min_volume": 0.6,
  "smart_turn_stop_secs": 2.5,
  "smart_turn_pre_speech_ms": 500,
  "smart_turn_max_duration_secs": 8.0
}'
WHERE name = 'Sales Bot';
```

---

### VAD — ranked by impact on conversation quality

| Priority | Setting | Recommended | Why |
|---|---|---|---|
| 1 | `stop_secs` | `0.5`–`0.8` | Primary driver of "does bot feel patient or jumpy" |
| 2 | `smart_turn_stop_secs` | `2.0`–`3.0` | Controls SmartTurn accuracy vs. latency trade-off |
| 3 | `endpointing` (in STT) | `stop_secs × 1000` | Must align with `stop_secs` or transcripts arrive late |
| 4 | `confidence` | `0.7` | Tune down if soft-spoken callers are missed |
| 5 | `min_volume` | `0.6` | Tune up if background noise triggers false turns |
| 6 | `start_secs` | `0.2` | Rarely needs changing |
| 7 | `smart_turn_max_duration_secs` | `8.0` | Only reduce for bots with very short expected turns |
| 8 | `smart_turn_pre_speech_ms` | `500` | Leave at default |

---

## Part 6 — LLM: OpenAI Configuration

### Top-level columns

#### `model`
The OpenAI model used for conversation.

| Value | Speed | Quality | Cost | Notes |
|---|---|---|---|---|
| `gpt-4o-mini` | Fast | Good | Low | **Recommended default for voice bots** |
| `gpt-4o` | Medium | Excellent | Medium | Use when reasoning quality matters |
| `gpt-4.1` | Medium | Excellent | Medium | Latest GPT-4 class |
| `gpt-4.1-mini` | Fast | Good | Low | Alternative to gpt-4o-mini |

---

#### `temperature` (float, 0.0–2.0)
Controls randomness of LLM responses.

- **`0.0`:** Fully deterministic — same input always gives same output
- **`0.3–0.5`:** Consistent but not robotic — good for information/FAQ bots
- **`0.6–0.8`:** Natural conversational variation — **recommended for most bots**
- **`1.0+`:** High creativity — responses become less predictable, may go off-script
- **Default (DB):** `0.7`
- **Recommended for voice bots:** `0.6`–`0.7`

---

#### `max_tokens` (int, >= 1)
Maximum number of tokens the LLM can produce per response. Controls response length.

- **Too low (< 50):** Bot gives incomplete answers, gets cut off mid-sentence
- **`100–200`:** Short, concise responses — ideal for voice (listeners can't re-read)
- **`300–500`:** Moderate detail — use for complex information bots
- **Too high (> 500):** Bot speaks too long, callers lose patience
- **Default (DB):** `150`
- **Recommended for voice bots:** `100`–`200`

> **Note:** Both `temperature` and `max_tokens` were previously stored in the DB but
> never passed to the LLM. This is now fixed — they are wired via `InputParams`.

---

### `end_call_phrases` (array of strings)
Phrases that, when spoken by the bot, trigger automatic call termination.
The bot says the phrase fully, then the call ends — it never cuts off mid-sentence.

**How it works:**
1. LLM generates a response containing e.g. "Goodbye, have a great day!"
2. The response is passed to TTS and spoken normally
3. After estimated speaking time, `EndFrame` is queued — call ends cleanly

**Example values:**
```json
["goodbye", "have a great day", "thank you for calling", "take care"]
```

**Tips:**
- Use lowercase — matching is case-insensitive
- Keep phrases unique enough that they don't appear in mid-conversation responses
- The system prompt should instruct the LLM to use these phrases when ending the call

**Example system prompt addition:**
```
When the conversation is complete or the user wants to end the call,
say "Thank you for calling, goodbye!" to end the session.
```

---

### Setting up LLM config via API

```json
{
  "model": "gpt-4o-mini",
  "temperature": 0.7,
  "max_tokens": 150,
  "end_call_phrases": ["goodbye", "have a great day", "thank you for calling"]
}
```

### LLM — ranked by impact on conversation quality

| Priority | Setting | Recommended | Why |
|---|---|---|---|
| 1 | `model` | `gpt-4o-mini` | Quality vs latency vs cost trade-off |
| 2 | `temperature` | `0.6`–`0.7` | Too high = off-script, too low = robotic |
| 3 | `max_tokens` | `100`–`200` | Voice listeners can't absorb long responses |
| 4 | `end_call_phrases` | Set explicitly | Without these, calls never auto-terminate |
