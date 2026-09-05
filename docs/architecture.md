# Customizable Voice Agent — Architecture Specification
### AssemblyAI x lablab.ai Voice AI Agents Challenge (Realtime STT API path)

This document is a complete implementation spec, not code. It describes every file in the
project, what it is responsible for, how it thinks internally, and exactly how it talks to
every other file. Hand this whole document to an implementing LLM/engineer and the codebase
it produces should be structurally consistent regardless of who writes the actual lines.

---

## 1. Project Summary

A backend service that lets an end user **configure a voice agent** (persona, system
instructions, chosen tools, TTS voice, language) and then **talk to it in real time** over a
WebSocket: the user's mic audio streams up, AssemblyAI's Realtime Speech-to-Text transcribes
it, a LangChain agent reasons over the transcript and calls tools as needed, and a
streaming TTS voice speaks the reply back — with the ability to be interrupted mid-sentence
(barge-in) without breaking the conversation.

**Stack:** Python, FastAPI (async WebSocket + REST), AssemblyAI Realtime STT v3 (WebSocket),
LangChain (agent + tool orchestration), a pluggable streaming TTS provider, a thin browser
client for the demo.

**Path chosen from the challenge:** "Realtime Speech-to-Text API" — AssemblyAI is used purely
as the transcription layer; the team owns the orchestration, the LLM/agent layer, and the TTS
layer end-to-end. This is the correct choice given the stack already includes LangChain and a
"bring your own everything else" requirement (customizable persona/tools/voice).

---

## 2. Core Design Principles

These principles are non-negotiable constraints the file-level design below exists to satisfy.
They come from how production voice agents avoid falling apart under real-time concurrency.

1. **Event-driven finite-state machine, not a linear script.** The conversation is modeled as
   explicit states (`IDLE`, `LISTENING`, `PROCESSING`, `SPEAKING`) with defined transitions,
   not a `while True` loop that assumes turns happen one at a time cleanly.
2. **Every turn has an ID. Every async result is checked against it.** LLM generation, tool
   calls, and TTS synthesis are all asynchronous and can outlive the turn that started them
   (the user may interrupt). Every result callback compares its `turn_id` to the session's
   *current* `turn_id` before being allowed to affect state. Stale results are discarded, not
   applied.
3. **Cancellation is explicit and cooperative.** Python doesn't have `AbortController`, so this
   role is played by a per-turn `asyncio.Event` ("cancel flag") combined with
   `asyncio.Task.cancel()` on the generation/TTS tasks. Long-running loops (token streaming,
   audio chunk streaming) check the flag between iterations.
4. **Spoken state ≠ generated state.** If the agent generates 40 words but the user interrupts
   after 10 are actually played back to them, the conversation history must record roughly
   "10 words spoken," not "40 words generated." This requires tracking a playback cursor on the
   outbound audio path, separate from tracking how much text the LLM produced.
5. **Interruption is a pipeline flush, not a crash.** Barge-in must: (a) stop audio playback
   immediately, (b) cancel the in-flight LLM/TTS tasks for the stale turn, (c) truncate history
   using the spoken-state rule above, (d) start a new turn cleanly — all without tearing down
   the AssemblyAI connection or the session.
6. **STT is a boundary, not part of the brain.** AssemblyAI's job ends at "here is a transcript
   and whether the turn is over." All conversational intelligence (what to say, when to allow
   interruption, how to recover) lives in this app's orchestrator, not in the STT layer.

---

## 3. High-Level Architecture

```
                    ┌───────────────────────────────────────────────┐
                    │                Browser Client                 │
                    │  mic capture → PCM16 frames → WebSocket (up)   │
                    │  audio/caption/state events ← WebSocket (down) │
                    └───────────────────┬────────────────────────────┘
                                        │ single WebSocket connection
                                        ▼
                    ┌───────────────────────────────────────────────┐
                    │        websocket/connection_handler.py         │
                    │   transport glue: 3 concurrent async loops     │
                    │   (audio-in, control-in, events-out)           │
                    └───────┬───────────────────────────┬────────────┘
                            │                            │
                 audio bytes│                            │outbound events
                            ▼                            ▲
              ┌─────────────────────────┐                │
              │  stt/audio_buffer.py    │                │
              │  stt/assemblyai_client  │                │
              │  (AssemblyAI v3 WS)     │                │
              └───────────┬─────────────┘                │
                          │ transcript events              │
                          ▼                                │
              ┌─────────────────────────────────────────┐  │
              │            core/orchestrator.py           │──┘
              │  the FSM: IDLE / LISTENING / PROCESSING /  │
              │  SPEAKING, owns turn_id, owns session.py   │
              └───────┬─────────────────────────┬─────────┘
                      │ start generation         │ start synthesis
                      ▼                          ▼
        ┌───────────────────────────┐  ┌───────────────────────────┐
        │ llm/agent_factory.py       │  │ tts/tts_client.py          │
        │ llm/streaming_handler.py   │  │ tts/audio_playback_mgr.py  │
        │ llm/tools/*                │  │ tts/elevenlabs_provider.py │
        └───────────────────────────┘  └───────────────────────────┘
                      ▲
                      │ reads agent config
        ┌───────────────────────────────┐
        │ customization/agent_profile.py │
        │ customization/profile_store.py │
        │ customization/api_routes.py    │
        └───────────────────────────────┘
```

---

## 4. Conversation State Machine

| State | Meaning | Entered from | Exits to |
|---|---|---|---|
| `IDLE` | Session open, nothing being said | session start / end of turn with silence | `LISTENING` |
| `LISTENING` | Partial transcripts arriving from AssemblyAI | `IDLE`, or interrupting `SPEAKING` | `PROCESSING` |
| `PROCESSING` | Final transcript received; agent generating (LLM + tools) | `LISTENING` | `SPEAKING` or `LISTENING` (if interrupted before audio starts) |
| `SPEAKING` | TTS audio streaming to client | `PROCESSING` | `LISTENING` (barge-in) or `IDLE` (turn completes naturally) |

Barge-in is not a separate state; it is a **transition rule**: any new final transcript (or,
optionally, a confident partial transcript) arriving while in `PROCESSING` or `SPEAKING`
immediately fires the interruption sequence (Section 6) and moves the FSM to `LISTENING` for
the new input.

---

## 5. Turn IDs & Cancellation Strategy

- `session.py` holds `current_turn_id` (monotonically increasing int, or UUID) and
  `cancel_event: asyncio.Event` for the active turn.
- `orchestrator.py` is the **only** file allowed to increment `current_turn_id` or replace
  `cancel_event` — this happens exactly when a new turn begins (fresh final transcript) or
  when an interruption is detected.
- Every task the orchestrator spawns (LLM generation task, TTS synthesis task) is created with
  a **closure over the turn_id it was started for** and a reference to that turn's
  `cancel_event`.
- Before applying any result from those tasks (a generated sentence, a synthesized audio
  chunk, a tool result), the receiving code checks: *is this turn_id still the session's
  current_turn_id, and is cancel_event still unset?* If either check fails, the result is
  dropped silently and logged, never applied to state.
- On interruption: orchestrator sets `cancel_event`, calls `.cancel()` on the active
  generation/TTS `asyncio.Task`s, asks `audio_playback_manager.py` for the playback cursor
  (how much was actually sent to the client), truncates the assistant's turn text down to
  roughly what was spoken, appends that truncated turn to `conversation_history`, then starts
  a brand-new turn for the interrupting input.
- Tools carry an `interruptible: bool` flag (Section 9, `tools/__init__.py`). Non-interruptible
  tools (e.g., "send the email") are allowed to finish in the background and their result is
  still committed to history even if the turn was superseded; interruptible tools' late
  results are discarded like any other stale callback.

---

## 6. WebSocket Wire Protocol (client ⇄ server)

**Client → Server**
| Type | Payload | Purpose |
|---|---|---|
| binary frame | raw PCM16 mono audio bytes | mic audio, forwarded to AssemblyAI |
| `start_session` (JSON) | `{ agent_profile_id }` | picks which configured agent to talk to |
| `end_session` (JSON) | — | graceful teardown |
| `client_interrupt` (JSON) | — | optional explicit "stop talking" button, in addition to voice barge-in |

**Server → Client**
| Type | Payload | Purpose |
|---|---|---|
| `partial_transcript` (JSON) | `{ text }` | live captions of what the user is saying |
| `final_transcript` (JSON) | `{ text }` | committed user utterance |
| `agent_text_delta` (JSON) | `{ text }` | live caption of what the agent is saying (optional, for UI) |
| binary frame | raw TTS audio bytes | agent's spoken audio, tagged internally with turn_id (not sent to client) |
| `state_change` (JSON) | `{ state }` | FSM state, lets the UI show "listening / thinking / speaking" |
| `interrupted` (JSON) | — | tells the client to flush its own audio playback buffer immediately |
| `error` (JSON) | `{ message }` | STT/LLM/TTS provider failure surfaced to the client |

---

## 7. Full Project Structure

```
voice_agent/
├── main.py
├── config.py
├── core/
│   ├── orchestrator.py
│   ├── session.py
│   ├── turn_manager.py
│   └── events.py
├── stt/
│   ├── assemblyai_client.py
│   └── audio_buffer.py
├── llm/
│   ├── agent_factory.py
│   ├── prompt_templates.py
│   ├── streaming_handler.py
│   └── tools/
│       ├── __init__.py
│       ├── web_search_tool.py
│       └── custom_tool_loader.py
├── tts/
│   ├── tts_client.py
│   ├── elevenlabs_provider.py
│   └── audio_playback_manager.py
├── customization/
│   ├── agent_profile.py
│   ├── profile_store.py
│   └── api_routes.py
├── websocket/
│   └── connection_handler.py
├── utils/
│   ├── logger.py
│   └── audio_utils.py
├── frontend/
│   ├── index.html
│   └── client.js
├── .env.example
├── requirements.txt
└── README.md
```

---

## 8. File-by-File Specification

### `main.py`
**Purpose:** application entrypoint.
**Internal logic:** creates the FastAPI app, mounts the WebSocket route to
`websocket/connection_handler.py`, mounts the REST router from
`customization/api_routes.py`, sets up CORS for the demo frontend, loads `config.py` once at
startup, and initializes logging via `utils/logger.py`.
**Connections:** imports everything that needs to be "registered" (the router and the
WebSocket handler); nothing imports `main.py`.
**Motive:** single, obvious place to see how the whole app boots; keeps wiring separate from
logic.

### `config.py`
**Purpose:** centralized settings/credentials loader.
**Internal logic:** reads environment variables (`ASSEMBLYAI_API_KEY`, LLM provider key(s),
TTS provider key, default speech model, default sample rate, default LLM model name) once,
exposes them as a single settings object/namespace.
**Connections:** imported by `stt/assemblyai_client.py`, `llm/agent_factory.py`,
`tts/elevenlabs_provider.py`, and `main.py`. Nothing computes credentials except this file.
**Motive:** prevents `os.environ[...]` calls scattered across the codebase; one file to change
when swapping providers or deploying to a new environment.

### `core/events.py`
**Purpose:** defines the internal message/event vocabulary that flows between components.
**Internal logic:** typed event definitions such as `PartialTranscriptEvent`,
`FinalTranscriptEvent`, `AgentTokenEvent`, `AgentSentenceReadyEvent`, `ToolCallStartedEvent`,
`ToolResultEvent`, `TTSAudioChunkEvent`, `InterruptionEvent` — each carrying at minimum a
`turn_id` and a timestamp. Not tied to any single provider's wire format.
**Connections:** used by every file listed as producing/consuming "events" below —
`stt/assemblyai_client.py`, `llm/streaming_handler.py`, `tts/tts_client.py`, and
`core/orchestrator.py`.
**Motive:** decouples every stage of the pipeline from every other stage's specific vendor
format. If AssemblyAI's JSON schema changes, only `assemblyai_client.py` needs to change —
everything downstream still consumes the same internal event types. This is the practical
version of the "immutable event ledger" idea.

### `core/session.py`
**Purpose:** the in-memory state for one active conversation.
**Internal logic:** holds `session_id`, `agent_profile` (the loaded customization for this
session), `conversation_history` (ordered list of turns with role, text, and how much of an
assistant turn was actually spoken vs generated), `current_turn_id`, `cancel_event` for the
active turn, `fsm_state`, and references to any currently-running generation/TTS
`asyncio.Task`s so they can be cancelled.
**Connections:** created and owned by `websocket/connection_handler.py` at connect time;
read and mutated almost exclusively by `core/orchestrator.py`; read (not mutated) by
`llm/agent_factory.py` to pull conversation memory and by `tts/elevenlabs_provider.py` to pull
the configured voice.
**Motive:** keeps "what do we currently know about this conversation" in one addressable
object instead of passing a dozen loose variables between functions.

### `core/turn_manager.py`
**Purpose:** pure logic for turn ID lifecycle and staleness checks — no I/O, no network calls.
**Internal logic:** exposes functions to (a) mint a new turn id when a turn begins, (b) check
whether a given callback's turn id is still current, and (c) given a full generated response
and the playback cursor from `audio_playback_manager.py`, compute the truncated "spoken"
version of that response for history purposes.
**Connections:** called by `core/orchestrator.py` at every point where an async result needs
validating or a turn needs starting/ending.
**Motive:** isolates the trickiest, most bug-prone logic (race condition resolution, spoken-vs-
generated truncation) into a small, independently testable unit, separate from the FSM
transition logic itself.

### `core/orchestrator.py`
**Purpose:** the central coordinator — the only file that knows the full state machine.
**Internal logic:** implements the FSM from Section 4. Receives transcript events (from the
STT layer) and drives the LLM and TTS layers in response, entirely through the interfaces
those layers expose (it never touches AssemblyAI, LangChain, or a TTS SDK directly). On a
final transcript: starts a new turn via `turn_manager`, appends the user's utterance to
`session.conversation_history`, kicks off agent generation. As sentence-sized chunks of the
agent's reply become ready (from `llm/streaming_handler.py`), forwards them to
`tts/tts_client.py` for synthesis and queues the resulting audio via
`tts/audio_playback_manager.py`. On any new transcript arriving mid-`PROCESSING`/`SPEAKING`,
runs the interruption sequence from Section 5. Emits `state_change` and other outbound events
that `websocket/connection_handler.py` forwards to the client.
**Connections:** imports `session.py`, `turn_manager.py`, `events.py`; receives calls from
`websocket/connection_handler.py` (inbound audio-derived events arrive via
`stt/assemblyai_client.py` → here) and from `stt/assemblyai_client.py` directly (transcript
callbacks); calls into `llm/agent_factory.py` + `llm/streaming_handler.py` to generate, and
`tts/tts_client.py` + `tts/audio_playback_manager.py` to speak.
**Motive:** every other file in the pipeline is a "dumb" specialist (STT wrapper, LLM wrapper,
TTS wrapper); this file is the only one allowed to make decisions about *when* things are
allowed to happen. Centralizing that avoids the classic bug pattern of two files each
independently deciding it's safe to speak.

### `stt/audio_buffer.py`
**Purpose:** normalizes and queues inbound mic audio before it reaches AssemblyAI.
**Internal logic:** receives raw binary frames pushed by `connection_handler.py` as they
arrive from the browser, chunks them to a consistent frame size, and (via
`utils/audio_utils.py` if needed) ensures the format matches what
`assemblyai_client.py` is configured to send (e.g., PCM16 mono 16kHz). Exposes an async
queue/generator that `assemblyai_client.py` drains.
**Connections:** fed by `websocket/connection_handler.py`; drained by
`stt/assemblyai_client.py`; may call `utils/audio_utils.py` for format conversion.
**Motive:** keeps "how do we get browser audio into the right shape" separate from both raw
WebSocket transport handling and AssemblyAI protocol handling.

### `stt/assemblyai_client.py`
**Purpose:** wraps one AssemblyAI Realtime v3 WebSocket connection for one session.
**Internal logic:** on session start, opens a connection to the v3 streaming endpoint with the
chosen `speech_model` and encoding/sample-rate params (mirroring the provided sample's
connection-parameter pattern, but PCM16 from a live mic instead of an AAC file stream), runs
the AssemblyAI websocket's send/receive loop (as a background thread, matching the reference
sample's threading approach, since the official client library used there is synchronous),
and bridges results back into the app's asyncio event loop via a thread-safe queue. Forwards
every audio chunk it receives from `audio_buffer.py` as a binary frame. Parses incoming
`Turn` messages, distinguishes partial vs. end-of-turn transcripts, and converts them into
`PartialTranscriptEvent` / `FinalTranscriptEvent` objects for the orchestrator. Sends a
`Terminate` control message on clean session shutdown, matching the reference sample.
**Connections:** consumes from `stt/audio_buffer.py`; produces events consumed by
`core/orchestrator.py`; reads credentials/model config from `config.py`.
**Motive:** isolates every AssemblyAI-specific detail (URL construction, auth header, the
`type: Turn` / `end_of_turn` message shape, the threading model needed by the sync client
library) behind a clean event interface. If the STT vendor ever changes, this is the only file
that changes.

### `llm/prompt_templates.py`
**Purpose:** owns all prompt text and prompt assembly logic.
**Internal logic:** holds a base "voice agent" system-prompt scaffold (instructions to answer
concisely, avoid markdown/formatting since output will be spoken aloud, keep a natural
conversational register), a function that merges that scaffold with the end user's custom
persona/instructions from their `AgentProfile`, and a variant used specifically when resuming
a turn after an interruption (instructing the model to continue naturally rather than
re-explaining or repeating what was already spoken).
**Connections:** consumed by `llm/agent_factory.py` at agent-build time and by
`core/orchestrator.py` when constructing a "repair" turn after a barge-in.
**Motive:** all prompt engineering happens in one file that can be iterated on quickly during
the hackathon without touching orchestration or agent-wiring code; also the natural home for
conversational-repair instructions.

### `llm/tools/__init__.py`
**Purpose:** the tool registry.
**Internal logic:** maintains a mapping of tool name → tool factory/object, plus per-tool
metadata — most importantly an `interruptible: bool` flag used by the orchestrator to decide
whether a tool call in flight during a barge-in should be abandoned (discard late result) or
allowed to finish and still commit its result to history.
**Connections:** read by `llm/agent_factory.py` when assembling the tool list for a given
profile; read by `core/orchestrator.py` when deciding how to handle an in-flight tool call
during interruption.
**Motive:** single place that answers "what can this agent do, and is it safe to interrupt
while it's doing it."

### `llm/tools/web_search_tool.py`
**Purpose:** one concrete example tool, illustrating the pattern all built-in tools follow.
**Internal logic:** wraps a search call behind a LangChain-compatible tool interface (name,
description used by the LLM for tool selection, input schema, and `interruptible=True` since a
search call is cheap and safe to abandon).
**Connections:** registers itself into `llm/tools/__init__.py`.
**Motive:** gives the implementing engineer a template to copy for any additional built-in
tools.

### `llm/tools/custom_tool_loader.py`
**Purpose:** the concrete mechanism behind "the user can customize their voice agent" beyond
persona and voice — lets an end user define a new tool without writing Python.
**Internal logic:** parses a simple tool definition (name, description, input schema, and
either a webhook URL to call or a reference to a predefined built-in behavior) attached to an
`AgentProfile`, and produces a LangChain-compatible tool wrapper for it dynamically at
agent-build time.
**Connections:** invoked by `llm/agent_factory.py` while resolving a profile's enabled tools;
reads custom tool definitions from `customization/profile_store.py`.
**Motive:** this is the single highest-leverage "customization" feature to demo at a
hackathon — it turns "configurable persona" into "configurable capability" with a small,
self-contained file.

### `llm/agent_factory.py`
**Purpose:** builds a ready-to-run LangChain agent for a given session/profile.
**Internal logic:** given an `AgentProfile`, assembles the system prompt (via
`prompt_templates.py`), resolves the profile's enabled tool names into actual tool objects (via
`llm/tools/__init__.py` and `custom_tool_loader.py`), instantiates the chosen chat model
wrapper (provider/model name pulled from the profile or `config.py` defaults), and returns a
runnable that exposes an async streaming interface the orchestrator can drive turn by turn,
fed with `session.conversation_history` as memory. Built once per session and cached on
`session.py`, not rebuilt every turn.
**Connections:** reads `customization/agent_profile.py` schema and
`customization/profile_store.py` data; uses `prompt_templates.py` and `llm/tools/*`; the
resulting runnable is driven by `core/orchestrator.py` through `llm/streaming_handler.py`.
**Motive:** keeps "how do we turn a profile into a working agent" in one place, separate from
both the state machine (orchestrator) and the persona data model (customization/).

### `llm/streaming_handler.py`
**Purpose:** the latency-critical bridge between LLM token output and TTS input.
**Internal logic:** consumes the agent's streaming output (token-by-token or event-by-event),
accumulates tokens into a running text buffer, and applies punctuation-based chunking —
flushing a "TTS-ready" sentence as soon as it hits a sentence boundary (`.`, `?`, `!`, and a
comma-based fallback if a sentence runs long) rather than waiting for the full response. Emits
`AgentSentenceReadyEvent`s as each chunk becomes ready, and optionally raw
`AgentTokenEvent`s for live captioning. On cancellation mid-stream, captures exactly how much
text had been generated up to that point and hands it back to the orchestrator/turn_manager
for the spoken-vs-generated truncation described in Section 5.
**Connections:** driven by `core/orchestrator.py` once `agent_factory.py` has produced a
runnable for the turn; its output events are consumed by `tts/tts_client.py` (to synthesize)
and, for cancellation bookkeeping, by `core/turn_manager.py`.
**Motive:** this is explicitly the most latency-sensitive segment of the whole pipeline (it
determines time-to-first-audio); isolating it as its own unit makes it possible to tune
chunking strategy independently of everything else.

### `tts/tts_client.py`
**Purpose:** vendor-agnostic streaming TTS interface.
**Internal logic:** defines the contract every concrete provider must implement — given a text
chunk (and a voice id from the profile), stream back audio bytes as they're generated — and
owns per-turn cancellation of the active synthesis call/connection so the orchestrator can stop
it immediately on barge-in.
**Connections:** implemented by `tts/elevenlabs_provider.py` (or any alternative provider
added later); called by `core/orchestrator.py` per sentence chunk emitted from
`llm/streaming_handler.py`; its output is consumed by `tts/audio_playback_manager.py`.
**Motive:** swapping TTS vendors — a very plausible mid-hackathon decision — only requires
writing a new provider file, not touching the orchestrator.

### `tts/elevenlabs_provider.py`
**Purpose:** the concrete TTS implementation actually used at demo time.
**Internal logic:** opens/reuses a streaming synthesis connection for the voice id configured
on the session's `AgentProfile` — this is literally what "customizable voice" means in
practice — streams each incoming sentence chunk's text and yields audio bytes as they arrive,
tagging every chunk internally with the turn_id it belongs to so stale audio can be identified
downstream.
**Connections:** implements `tts/tts_client.py`'s interface; reads voice/language settings
from the session's `AgentProfile` (`customization/agent_profile.py`); reads credentials from
`config.py`.
**Motive:** concrete vendor detail lives in exactly one file, matching the pattern already used
for the STT vendor.

### `tts/audio_playback_manager.py`
**Purpose:** owns the outbound audio stream to the client and the "how much was actually
heard" bookkeeping.
**Internal logic:** receives ordered, turn_id-tagged audio chunks from `tts_client.py` and
forwards them to the client over the WebSocket, in order, only while the chunk's turn_id
matches the session's current turn_id (dropping stale chunks per Race Condition 1 in Section
10). Tracks a running playback cursor (approximate ms or bytes actually sent) so that, on
interruption, `turn_manager.py` can compute the spoken-vs-generated truncation. On receiving an
interruption signal from the orchestrator, immediately stops forwarding any further queued
audio for the stale turn and pushes an `interrupted` control message so the client flushes its
own playback buffer too.
**Connections:** consumes from `tts/tts_client.py`; sends outbound frames via
`websocket/connection_handler.py`; reports the playback cursor to `core/orchestrator.py` /
`core/turn_manager.py`.
**Motive:** this file is the only place that knows the physical reality of what the user has
actually heard, which is the piece of information the entire spoken-vs-generated design
principle depends on.

### `customization/agent_profile.py`
**Purpose:** the schema for one configurable voice agent.
**Internal logic:** defines the data model for a profile: id, display name, persona/system
instructions, LLM provider + model + temperature, enabled built-in tool names, any custom tool
definitions, TTS voice id, language/locale, and an interruption-sensitivity setting.
**Connections:** this is the single source of truth read by `llm/agent_factory.py`,
`llm/prompt_templates.py`, `llm/tools/custom_tool_loader.py`, and
`tts/elevenlabs_provider.py` — every customizable seam in the pipeline reads from this one
schema rather than re-deriving configuration in multiple places.
**Motive:** guarantees "what makes this agent this agent" has exactly one definition.

### `customization/profile_store.py`
**Purpose:** persistence for `AgentProfile` objects.
**Internal logic:** basic CRUD (create/get/update/delete by profile id). For hackathon scope
this can be a JSON-file or SQLite-backed store; the interface is written so it can later be
swapped for Postgres/Redis without touching callers.
**Connections:** used by `customization/api_routes.py` (REST create/edit) and by
`websocket/connection_handler.py` at session start (to load the profile the client asked for).
**Motive:** keeps storage swappable and out of both the REST layer and the WebSocket layer.

### `customization/api_routes.py`
**Purpose:** REST surface for managing agent profiles, used by the customization UI before a
voice session starts.
**Internal logic:** exposes create/list/update/delete endpoints for `AgentProfile`s, plus
read-only endpoints listing available tools/voices for populating a settings UI's dropdowns.
**Connections:** imports `customization/profile_store.py` and `agent_profile.py`; mounted into
`main.py`.
**Motive:** separates "configure an agent" (ordinary request/response) from "talk to an agent"
(stateful, streaming) — they have completely different lifecycles and don't belong in the same
file.

### `websocket/connection_handler.py`
**Purpose:** the per-connection entrypoint and transport glue.
**Internal logic:** on connect, reads which `agent_profile_id` the client wants (via a
`start_session` control message), loads it from `profile_store.py`, creates a
`core/session.py` object, wires up a dedicated `stt/assemblyai_client.py` instance and a
`core/orchestrator.py` instance for this session, then runs three concurrent async loops for
the lifetime of the connection: (1) inbound binary audio frames → `audio_buffer.py` →
`assemblyai_client.py`; (2) inbound JSON control messages (explicit interrupt, end session,
mid-session profile switch) → `orchestrator.py`; (3) drains the orchestrator's outbound event
queue (transcripts, agent captions, TTS audio, state changes, interruption notices) and writes
them out as WebSocket frames per the protocol in Section 6. On disconnect, tears down the
AssemblyAI connection (sending `Terminate`) and cancels any in-flight tasks cleanly.
**Connections:** the top-level glue — imports `session.py`, `orchestrator.py`,
`stt/assemblyai_client.py`, `customization/profile_store.py`; is itself only referenced by
`main.py`, which registers it as the WebSocket route handler.
**Motive:** keeps all raw-transport concerns (framing, connect/disconnect lifecycle) in one
file, fully separate from the FSM logic in `orchestrator.py`, so the orchestrator stays
transport-agnostic and could in principle sit behind a different transport (e.g. WebRTC) later
without changes.

### `utils/logger.py`
**Purpose:** structured logging setup.
**Internal logic:** configures log output (ideally structured/JSON) so every log line can
include `session_id` and `turn_id`, which is essential for debugging race conditions during
development.
**Connections:** imported by nearly every file above for consistent log formatting.
**Motive:** correlating logs by turn is the single most useful debugging tool for this kind of
concurrent system; worth setting up once, correctly, on day one.

### `utils/audio_utils.py`
**Purpose:** audio format helper functions.
**Internal logic:** small, stateless functions for converting/resampling audio between what
the browser captures and what AssemblyAI's realtime endpoint expects (e.g., ensuring PCM16
mono at the configured sample rate), and, symmetrically, packaging outbound TTS audio into a
format the browser can play directly.
**Connections:** used by `stt/audio_buffer.py` (inbound) and
`tts/audio_playback_manager.py` (outbound).
**Motive:** keeps format-conversion arithmetic out of both the STT-protocol file and the
playback-bookkeeping file, which have enough responsibility already.

### `frontend/index.html` + `frontend/client.js`
**Purpose:** the minimal demo client used for judging.
**Internal logic:** captures mic audio (via `getUserMedia` + an `AudioWorkletNode` producing
PCM16 frames, which avoids needing browser-side Opus/WebM-to-PCM conversion), opens the
WebSocket to `connection_handler.py`, streams audio frames up, listens for and renders
transcript/caption/state messages, plays back incoming TTS audio, and renders a small
customization form (backed by `customization/api_routes.py`) for picking a persona/voice/tools
before starting a session.
**Connections:** talks to `websocket/connection_handler.py` and
`customization/api_routes.py` over the network only — not imported by any backend file.
**Motive:** gives judges something to click during the demo, and doubles as the reference
implementation of the wire protocol for any future non-browser client.

### `.env.example`, `requirements.txt`, `README.md`
**Purpose:** standard project scaffolding.
**Internal logic:** `.env.example` lists every required key (`ASSEMBLYAI_API_KEY`, chosen LLM
provider key, chosen TTS provider key); `requirements.txt` pins dependencies (`fastapi`,
`uvicorn`, a WebSocket client library for the AssemblyAI connection, `langchain` + the relevant
provider integration packages, `python-dotenv`, `pydantic`, the chosen TTS SDK);
`README.md` documents setup/run steps and briefly restates the wire protocol from Section 6 for
anyone reviewing or judging the project.
**Motive:** reduces setup friction for teammates and judges alike.

---

## 9. End-to-End Sequence Walkthroughs

### 9.1 Normal turn (no interruption)
1. Client streams mic audio → `connection_handler` → `audio_buffer` → `assemblyai_client`.
2. `assemblyai_client` emits `PartialTranscriptEvent`s as the user talks → orchestrator moves
   FSM to `LISTENING`, forwards captions to client.
3. AssemblyAI signals end-of-turn → `assemblyai_client` emits `FinalTranscriptEvent`.
4. Orchestrator: mints new `turn_id`, appends user turn to history, moves FSM to `PROCESSING`,
   calls `agent_factory`-built runnable through `streaming_handler`.
5. `streaming_handler` chunks the LLM's streamed tokens into sentences, emitting
   `AgentSentenceReadyEvent`s in order.
6. Orchestrator forwards each sentence to `tts_client` → `elevenlabs_provider`, which streams
   back turn_id-tagged audio chunks.
7. `audio_playback_manager` forwards each chunk to the client (FSM: `SPEAKING`), advancing its
   playback cursor.
8. When generation and synthesis both finish with no interruption, orchestrator appends the
   full assistant turn to history and returns FSM to `IDLE`/`LISTENING`.

### 9.2 Barge-in turn (interruption)
1. Same as above through step 6 — agent is mid-`SPEAKING`.
2. `assemblyai_client` emits a new `FinalTranscriptEvent` for user speech detected mid-playback.
3. Orchestrator detects this while FSM is `PROCESSING`/`SPEAKING`: sets the active turn's
   `cancel_event`, cancels the generation and TTS `asyncio.Task`s for the stale `turn_id`.
4. Orchestrator asks `audio_playback_manager` for the current playback cursor, asks
   `turn_manager` to truncate the stale assistant turn's text down to what was actually spoken,
   appends that truncated turn to history.
5. `audio_playback_manager` stops forwarding any further stale-turn audio and sends
   `interrupted` to the client so it flushes local playback immediately.
6. Orchestrator mints a fresh `turn_id`, moves FSM to `LISTENING`, and proceeds exactly like a
   normal turn (Section 9.1, step 4 onward) using `prompt_templates`'s repair-turn variant so
   the model doesn't repeat itself.

---

## 10. Race Conditions → Responsible File

| Race | Scenario | File that resolves it |
|---|---|---|
| TTS startup interruption | First audio chunk arrives after the turn was already cancelled | `tts/audio_playback_manager.py` (checks turn_id before forwarding) |
| In-flight LLM generation | Tokens still streaming when interrupted | `llm/streaming_handler.py` + `core/turn_manager.py` (only spoken text is committed) |
| Tool execution interruption | User interrupts while a tool call is running | `llm/tools/__init__.py` (`interruptible` flag) + `core/orchestrator.py` (decides discard vs. commit) |
| Late callback / ghost state | A stale async result resolves during a later turn | `core/turn_manager.py`'s turn_id check, enforced at every entry point in `core/orchestrator.py` |

---

## 11. Phased Build Plan (fits a month-long hackathon)

- **Week 1 — Spine:** `config.py`, `stt/assemblyai_client.py`, `stt/audio_buffer.py`,
  `core/session.py`, minimal `core/orchestrator.py` (no interruption yet), a hardcoded single
  `AgentProfile`, `llm/agent_factory.py` with one tool-less LangChain agent. Goal: mic in →
  transcript → LLM text reply printed to console.
- **Week 2 — Voice out:** `llm/streaming_handler.py` (sentence chunking),
  `tts/tts_client.py` + `tts/elevenlabs_provider.py`, `tts/audio_playback_manager.py`,
  `websocket/connection_handler.py`, minimal `frontend/`. Goal: full voice-in → voice-out loop
  with no interruption handling.
- **Week 3 — Make it robust:** `core/turn_manager.py`, full turn_id plumbing, the barge-in
  transition in `orchestrator.py`, spoken-vs-generated truncation. Goal: the agent can be
  talked over mid-sentence without breaking.
- **Week 4 — Customization + polish:** `customization/*` (profiles, REST API, frontend
  settings form), `llm/tools/*` including `custom_tool_loader.py`, logging polish, README, demo
  script.

---

## 12. Out of Scope for the Hackathon MVP

- Building a custom VAD/turn-detection model — fully delegated to AssemblyAI, as the research
  underpinning this design explicitly treats that as a boundary concern, not an orchestration
  concern.
- Multi-user auth/accounts — a single shared profile store is fine for a demo.
- Horizontal scaling / multi-instance session affinity — single-process `asyncio` is sufficient
  for a hackathon demo.
- Perfect backchannel detection (distinguishing "uh-huh" from real barge-in) — treat any
  final transcript during `SPEAKING` as an interruption for MVP; this is a documented open
  problem industry-wide, not something to solve in a month.