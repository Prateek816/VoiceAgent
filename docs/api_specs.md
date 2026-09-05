# Voice Agent — Complete API Specification
### REST API + WebSocket Protocol (companion to `voice_agent_architecture.md`)

Base URL (dev): `http://localhost:8000`
WebSocket base (dev): `ws://localhost:8000`
All REST payloads: `application/json`. All timestamps: ISO 8601 UTC.

---

## 1. REST API — Agent Profile Management

Implemented in `customization/api_routes.py`, backed by `customization/profile_store.py`,
validated against the `customization/agent_profile.py` schema.

### 1.1 `AgentProfile` object (canonical shape, returned/accepted everywhere below)

```json
{
  "id": "string (uuid, server-generated)",
  "name": "string",
  "persona": {
    "system_prompt": "string",
    "tone": "string (e.g. 'friendly', 'formal', 'concise')",
    "language": "string (BCP-47, e.g. 'en-US')"
  },
  "llm": {
    "provider": "string (e.g. 'openai', 'groq', 'anthropic')",
    "model": "string (e.g. 'gpt-4o-mini')",
    "temperature": "number (0.0-2.0, default 0.7)",
    "max_tokens": "integer (default 512)"
  },
  "tts": {
    "provider": "string (e.g. 'elevenlabs')",
    "voice_id": "string",
    "speaking_rate": "number (0.5-2.0, default 1.0)"
  },
  "tools": {
    "enabled_builtin": ["string"],
    "custom_tools": [
      {
        "name": "string",
        "description": "string",
        "input_schema": { "type": "object", "properties": {} },
        "webhook_url": "string (https, optional)",
        "interruptible": "boolean (default true)"
      }
    ]
  },
  "interruption_sensitivity": "string ('low' | 'medium' | 'high', default 'medium')",
  "created_at": "string (ISO 8601)",
  "updated_at": "string (ISO 8601)"
}
```

### 1.2 `POST /api/profiles` — create a profile
**Request body:** `AgentProfile` object, omit `id`, `created_at`, `updated_at`.
```json
{
  "name": "Support Bot",
  "persona": {
    "system_prompt": "You are a helpful support agent for Acme Corp.",
    "tone": "friendly",
    "language": "en-US"
  },
  "llm": { "provider": "groq", "model": "llama-3.3-70b", "temperature": 0.6, "max_tokens": 400 },
  "tts": { "provider": "elevenlabs", "voice_id": "21m00Tcm4TlvDq8ikWAM", "speaking_rate": 1.0 },
  "tools": { "enabled_builtin": ["web_search"], "custom_tools": [] },
  "interruption_sensitivity": "medium"
}
```
**Response `201 Created`:** full `AgentProfile` object with `id`, `created_at`, `updated_at`
populated.
**Errors:** `400` invalid schema (field-level error list), `422` unprocessable (e.g. unknown
`llm.provider`).

### 1.3 `GET /api/profiles` — list profiles
**Query params:** `limit` (int, default 20), `offset` (int, default 0).
**Response `200 OK`:**
```json
{ "items": [ /* AgentProfile[] */ ], "total": 1, "limit": 20, "offset": 0 }
```

### 1.4 `GET /api/profiles/{profile_id}` — fetch one
**Response `200 OK`:** `AgentProfile` object.
**Errors:** `404` not found.

### 1.5 `PATCH /api/profiles/{profile_id}` — partial update
**Request body:** any subset of `AgentProfile` fields (deep-merged).
```json
{ "tts": { "voice_id": "EXAVITQu4vr4xnSDxMaL" } }
```
**Response `200 OK`:** full updated `AgentProfile`.
**Errors:** `404` not found, `400` invalid field.

### 1.6 `DELETE /api/profiles/{profile_id}`
**Response:** `204 No Content`.
**Errors:** `404` not found.

### 1.7 `GET /api/tools` — list available built-in tools (for UI dropdowns)
**Response `200 OK`:**
```json
{
  "tools": [
    { "name": "web_search", "description": "Search the web for current information.", "interruptible": true }
  ]
}
```

### 1.8 `GET /api/voices` — list available TTS voices (proxied/cached from provider)
**Response `200 OK`:**
```json
{
  "voices": [
    { "voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel", "language": "en-US", "preview_url": "https://.../preview.mp3" }
  ]
}
```

### 1.9 `GET /healthz` — liveness check
**Response `200 OK`:** `{ "status": "ok" }`

---

## 2. WebSocket API — Live Voice Session

**Endpoint:** `ws://localhost:8000/ws/session`
One connection = one conversation session. Binary frames carry audio; text frames carry JSON
control/event messages, each shaped `{ "type": "<name>", ...fields }`.

### 2.1 Client → Server messages

#### `start_session`
Sent once, immediately after connecting, before any audio.
```json
{
  "type": "start_session",
  "agent_profile_id": "b3f1c2...-uuid",
  "audio_config": {
    "encoding": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1
  }
}
```
Server responds with `session_ready` (below) or `error`.

#### Binary audio frame
Raw bytes, PCM16 mono little-endian at the negotiated `sample_rate`. No JSON wrapper. Sent
continuously while the mic is capturing.

#### `client_interrupt`
Optional explicit "stop talking" (e.g. a UI button), in addition to voice barge-in.
```json
{ "type": "client_interrupt" }
```

#### `switch_profile`
Swap the active agent profile mid-session (keeps the WebSocket open).
```json
{ "type": "switch_profile", "agent_profile_id": "a91e4d...-uuid" }
```

#### `end_session`
```json
{ "type": "end_session" }
```
Server responds with `session_closed`, then closes the socket.

### 2.2 Server → Client messages

#### `session_ready`
```json
{ "type": "session_ready", "session_id": "sess_9f8a...", "agent_profile": { /* AgentProfile */ } }
```

#### `state_change`
```json
{ "type": "state_change", "state": "idle" }
```
`state` ∈ `"idle" | "listening" | "processing" | "speaking"`.

#### `partial_transcript`
```json
{ "type": "partial_transcript", "text": "so i wanted to ask", "turn_id": 14 }
```

#### `final_transcript`
```json
{ "type": "final_transcript", "text": "So I wanted to ask about my refund status.", "turn_id": 14 }
```

#### `agent_text_delta`
Streamed caption of the agent's reply (for UI), sentence-chunked to match TTS.
```json
{ "type": "agent_text_delta", "text": "Sure, let me check that for you.", "turn_id": 14 }
```

#### `tool_call_started` / `tool_call_result`
```json
{ "type": "tool_call_started", "tool_name": "web_search", "turn_id": 14, "call_id": "call_1" }
```
```json
{ "type": "tool_call_result", "tool_name": "web_search", "turn_id": 14, "call_id": "call_1", "success": true, "summary": "Refund policy: 14 days." }
```

#### Binary audio frame (agent speech)
Raw PCM16 (or provider-native encoding, documented per deployment) audio bytes. No JSON
wrapper. Client appends to its playback buffer as received.

#### `interrupted`
```json
{ "type": "interrupted", "turn_id": 14 }
```
Client must immediately flush/stop any queued playback audio.

#### `error`
```json
{ "type": "error", "code": "stt_provider_error", "message": "AssemblyAI connection dropped.", "turn_id": 14 }
```
`code` ∈ `"stt_provider_error" | "llm_error" | "tts_provider_error" | "invalid_profile" | "invalid_message"`.

#### `session_closed`
```json
{ "type": "session_closed", "reason": "client_requested" }
```
`reason` ∈ `"client_requested" | "idle_timeout" | "error"`.

### 2.3 Full message sequence (happy path)
```
C → start_session
S → session_ready
C → [binary audio frames...]
S → state_change {listening}
S → partial_transcript ×N
S → final_transcript
S → state_change {processing}
S → agent_text_delta ×N (+ optional tool_call_started/result)
S → state_change {speaking}
S → [binary audio frames...]
S → state_change {idle}
```

### 2.4 Barge-in sequence
```
... S is mid {speaking}, streaming binary audio ...
C → [binary audio frames — user starts talking]
S → state_change {listening}
S → final_transcript (new turn_id)
S → interrupted {turn_id: old}
S → state_change {processing}
... continues as happy path with new turn_id ...
```

---

## 3. Internal Provider Payloads (server-side only, not exposed to client)

### 3.1 AssemblyAI Realtime v3 connection params (used by `stt/assemblyai_client.py`)
```
wss://streaming.assemblyai.com/v3/ws?speech_model=universal-3-5-pro&encoding=pcm_s16le&sample_rate=16000
Header: Authorization: <ASSEMBLYAI_API_KEY>
```
Inbound message shape consumed:
```json
{ "type": "Turn", "transcript": "...", "end_of_turn": true, "turn_is_formatted": true }
```
Outbound control message sent on shutdown:
```json
{ "type": "Terminate" }
```

### 3.2 Internal event objects (produced/consumed across `core/events.py`)
```json
{ "event": "FinalTranscriptEvent", "turn_id": 14, "text": "...", "ts": "2026-09-05T12:00:00Z" }
{ "event": "AgentSentenceReadyEvent", "turn_id": 14, "text": "...", "seq": 2, "ts": "..." }
{ "event": "TTSAudioChunkEvent", "turn_id": 14, "seq": 2, "bytes_len": 3200, "ts": "..." }
{ "event": "InterruptionEvent", "stale_turn_id": 14, "new_turn_id": 15, "spoken_chars": 41, "ts": "..." }
```

---

## 4. Standard Error Envelope (REST)

```json
{
  "error": {
    "code": "string (machine-readable)",
    "message": "string (human-readable)",
    "fields": { "field_name": "what's wrong (optional, validation errors only)" }
  }
}
```
Used for all non-2xx REST responses (`400`, `404`, `422`, `500`).