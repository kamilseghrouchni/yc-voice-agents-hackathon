# Pipecat Diary

> Working reference for extending the YC voice agents hackathon bot. Source: https://docs.pipecat.ai/pipecat (crawled 2026-05-30). Pipecat 1.0+ (`pipecat-ai >= 1.3.0`).

## TL;DR — how the bot works in one diagram

```
caller audio                                                 caller audio
   │                                                              ▲
   ▼                                                              │
┌────────────────┐  bytes  ┌─────┐  TranscriptionFrame  ┌──────────────┐
│transport.input │ ──────▶ │ STT │ ──────────────────▶  │user_aggregat.│
└────────────────┘         └─────┘                      └──────┬───────┘
                                                               │ LLMContextFrame
                                                               ▼
                                                        ┌──────────────┐
                                                        │     LLM      │
                                                        │ (tool calls) │
                                                        └──────┬───────┘
                                                               │ LLMTextFrame
                                                               ▼
┌────────────────┐  audio   ┌─────┐                   ┌──────────────┐
│transport.output│ ◀──────  │ TTS │ ◀──────────────── │              │
└──────┬─────────┘         └─────┘                    │              │
       │ TTSTextFrame                                 │              │
       ▼                                              │              │
┌──────────────────┐                                  │              │
│assistant_aggregat│                                  │              │
└──────────────────┘                                  └──────────────┘
```

Wired in `server/bot-nemotron.py::run_bot` (also `bot-gpt.py`). The two aggregators share one `LLMContext` so user-side + assistant-side speak to the same conversation history.

Where each piece lives in this repo:

| Layer            | File                                 | Class                         |
|------------------|--------------------------------------|-------------------------------|
| Transport        | `server/bot-nemotron.py::bot`        | `SmallWebRTCTransport` / `FastAPIWebsocketTransport` |
| STT              | `server/nvidia_stt.py`               | `NVidiaWebSocketSTTService` (subclass of `WebsocketSTTService`) |
| LLM              | `server/nemotron_llm.py`             | `VLLMOpenAILLMService` (subclass of `OpenAILLMService`) |
| TTS              | (pipecat builtin)                    | `GradiumTTSService`           |
| Aggregators      | `server/bot-nemotron.py::run_bot`    | `LLMContextAggregatorPair`    |
| Tools            | `server/bot-nemotron.py::run_bot`    | `ToolsSchema(standard_tools=…)` + `llm.register_direct_function(fn)` |
| Mock backend     | `server/mock_backend.py`             | `BOUQUETS`, `KNOWN_CUSTOMERS` |
| Worker / runner  | `server/bot-nemotron.py`             | `PipelineWorker`, `WorkerRunner` |
| Deploy           | `server/Dockerfile`, `server/pcc-deploy.toml` | Pipecat Cloud (`agent-1x`) |

---

## Frames

A `Frame` is the unit of data on the pipeline. Each processor receives every frame, may transform/swallow/emit, then **must call `super().process_frame(...)` and `push_frame(...)` for anything it doesn't actively consume** or downstream stops.

### Two lanes
- **SystemFrames** — high priority, never discarded on interruption, can travel UPSTREAM.
- **DataFrames + ControlFrames** — queued in order, *discarded on user interruption* unless mixed with `UninterruptibleFrame`.

### Direction
```python
from pipecat.processors.frame_processor import FrameDirection
class FrameDirection(Enum):
    DOWNSTREAM = 1   # input → output (default)
    UPSTREAM = 2     # output → input  (used for EndTaskFrame)
```

### Frames we already use

| Frame                          | What it does                                                                 | When |
|--------------------------------|------------------------------------------------------------------------------|------|
| `LLMRunFrame`                  | Tells the LLM service to consume current context and start generating       | Sent in `on_client_connected` to kick the bot off |
| `EndTaskFrame`                 | System frame. Converted to `EndFrame` and propagated; graceful shutdown.    | `end_call` tool, push UPSTREAM |
| `FunctionCallResultProperties` | Helper dataclass passed to `result_callback`; `run_llm=False` skips the LLM after a tool result | `end_call` tool returns this so the LLM doesn't re-speak after goodbye |

### Frames we will likely want

| Frame                          | What it does                                                                              | Use case |
|--------------------------------|-------------------------------------------------------------------------------------------|----------|
| `TTSSpeakFrame(text, append_to_context=None)` | Speak text directly via TTS, bypassing the LLM. Set `append_to_context=True` to also append to conversation history. | Have a tool say "looking that up..." while it works |
| `StopTaskFrame`                | Stop pipeline immediately                                                                 | Crash / hangup with no goodbye |
| `CancelFrame`                  | System frame. Skips queued non-system frames. Pushed by `worker.cancel()`.                | Client disconnect handler |
| `TranscriptionFrame(user_id, timestamp, text, finalized=False)` | Final STT result                                              | Logging, transcript saving |
| `InterimTranscriptionFrame(text, user_id, timestamp)` | Partial STT result                                                | Showing live captions |
| `LLMMessagesAppendFrame([msg], run_llm=True)` | Inject a developer message mid-call and (optionally) run the LLM right after | Re-prompt idle user, system reminder |
| `LLMUpdateSettingsFrame(delta=OpenAILLMService.Settings(temperature=0.2))` | Change LLM settings mid-call                                  | Hot-switch model knobs |
| `UserIdleTimeoutUpdateFrame(timeout=10.0)` | Change/disable the idle timer at runtime                                      | Long form-fill steps |
| `StartFrame`                   | Pipeline init — sample rates, metrics                                                     | You don't emit this, it's emitted on startup |
| `InterruptionFrame` / `StartInterruptionFrame` / `StopInterruptionFrame` | Wraps the moment a user barge-in happens                          | Observe, don't emit |
| `LLMFullResponseStartFrame` / `LLMFullResponseEndFrame` | Brackets each LLM response                                            | Logging / observers |
| `FunctionCallInProgressFrame` / `FunctionCallResultFrame` | Function call lifecycle                                                  | Tool-call logging |
| `LLMContextFrame`              | Carries the full LLM context; signals LLM to ingest + respond                              | Mostly emitted by aggregators |

```python
# Speak through TTS without involving the LLM
from pipecat.frames.frames import TTSSpeakFrame
await tts.queue_frame(TTSSpeakFrame("Hold on, looking that up..."))

# End the call (the pattern we already use)
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties
from pipecat.processors.frame_processor import FrameDirection
await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
await params.result_callback({"ok": True},
    properties=FunctionCallResultProperties(run_llm=False))
```

---

## Services

### LLM services

All major providers ship as services: `OpenAILLMService`, `OpenAIResponsesLLMService` (for the new Responses API), `AnthropicLLMService`, `GoogleLLMService`, `NvidiaLLMService`, plus Groq, Mistral, AWS, Azure, Cerebras, Fireworks, Grok, Ollama, OpenRouter, Perplexity, Qwen, SambaNova, Together, etc. (full list: `/api-reference/server/services/llm/*`).

#### `OpenAILLMService` — the base we already extend
```python
OpenAILLMService(
    api_key: str = None,
    base_url: str = None,
    organization: str = None,
    project: str = None,
    default_headers: Mapping[str, str] = None,
    settings: OpenAILLMService.Settings = None,
    retry_timeout_secs: float = 5.0,
    retry_on_timeout: bool = False,
)

OpenAILLMService.Settings(
    model: str = "gpt-4.1",
    system_instruction: str = None,
    temperature: float = NOT_GIVEN,
    max_tokens: int = NOT_GIVEN,
    top_p: float = NOT_GIVEN,
    top_k: int = NOT_GIVEN,
    frequency_penalty: float = NOT_GIVEN,
    presence_penalty: float = NOT_GIVEN,
    seed: int = NOT_GIVEN,
    max_completion_tokens: int = NOT_GIVEN,
)
```
- Streaming: chat-completions streaming is on by default.
- Tool calls: handled by the service; user-facing API is `register_function` / `register_direct_function`.
- Provider-compat (vLLM, LM Studio, Together, OpenRouter…): just override `base_url`. That's what `VLLMOpenAILLMService` does.
- Settings updates mid-call: push `LLMUpdateSettingsFrame(delta=…)`.

#### `NvidiaLLMService` — when to prefer over OpenAI+base_url
Subclass of `OpenAILLMService`. Use it when you want:
- Auto-routing of `reasoning_content` deltas into `LLMThought*Frame` (so chain-of-thought never reaches the TTS).
- NVIDIA's incremental token-reporting accumulation.
- A pre-set cloud URL (`https://integrate.api.nvidia.com/v1`).

Stick with `OpenAILLMService` + `base_url` for generic OpenAI-compatible endpoints (this is what `nemotron_llm.py` does — and the bot's TTFB fix lives there because the stock NVIDIA service wasn't required).

#### `AnthropicLLMService`
```python
AnthropicLLMService(api_key=..., settings=AnthropicLLMService.Settings(
    model="claude-sonnet-4-5-20250929",
    system_instruction="...",
    max_tokens=2048,
    enable_prompt_caching=True,
    thinking=AnthropicThinkingConfig(...),
))
```

#### Universal pattern
- `Settings` dataclass — passed via `settings=...`. Old-style `params=...` / `InputParams` was **deprecated in 0.0.105**.
- `LLMUpdateSettingsFrame(delta=Service.Settings(temperature=0.2))` updates at runtime.

#### Subclassing checklist (when adding a new LLM)
Look at `server/nemotron_llm.py`:
1. Subclass the closest existing service.
2. Override only what's needed (we override token-arming for TTFB).
3. Run the unit tests in `test_nemotron_llm.py` to confirm the subclass still composes with the parent's streaming pipeline.

### STT services

Two architectures:
- **Streaming** (subclass of `STTService` → typically `WebsocketSTTService`): low-latency, persistent connection. Emits `InterimTranscriptionFrame` deltas → final `TranscriptionFrame`. This is the family our `NVidiaWebSocketSTTService` lives in.
- **Segmented** (`SegmentedSTTService`): local VAD chunks audio, HTTPs each segment. Higher latency.

Available: Deepgram, Speechmatics, AssemblyAI, Gladia, Azure, Google, Groq, OpenAI, ElevenLabs, Soniox, Whisper (local), Cartesia, Sarvam, NVIDIA, Gradium, Mistral, Fal, Smallest, xAI.

```python
from deepgram import LiveOptions
from pipecat.services.deepgram.stt import DeepgramSTTService

stt = DeepgramSTTService(
    api_key=os.getenv("DEEPGRAM_API_KEY"),
    live_options=LiveOptions(model="nova-2", interim_results=True, punctuate=True),
)
```

**Sample rate:** set once via `PipelineParams(audio_in_sample_rate=...)` rather than per-service.

**TTFS tuning** — every streaming STT has a `ttfs_p99_latency` knob that says "how long after the final audio chunk before we should give up waiting on the transcript". Tune with measured numbers from `stt-benchmark`:
```python
stt = DeepgramSTTService(api_key=..., ttfs_p99_latency=0.45)
```

**Interim vs final:** STT emits `InterimTranscriptionFrame` continuously then `TranscriptionFrame(finalized=True)` at end-of-turn. Aggregator usually only cares about finals; interims are useful for live captions / interrupt detection.

**Subclassing `WebsocketSTTService`** — docs don't give a verbatim recipe; we have a working one in `server/nvidia_stt.py`. Pattern: wire your provider's WS framing in `_connect`/`_send_audio`/`_handle_message`, emit `InterimTranscriptionFrame` / `TranscriptionFrame`, let the base class do the rest.

### TTS services

Two architectures:
- **WebSocket-based** (lower latency, word timestamps for accurate interruption): Cartesia, ElevenLabs, Rime, Gradium.
- **HTTP-based**: OpenAI TTS, Azure, Google, Deepgram.

**`GradiumTTSService`** (what we use):
```python
GradiumTTSService(
    api_key=os.getenv("GRADIUM_API_KEY"),
    settings=GradiumTTSService.Settings(
        voice="voice-id",   # voice identifier
        model=...,
        language=...,
    ),
)
```
Gradium **always outputs at 48 kHz** — not configurable on the service. The transport handles resample.

**Voice IDs**: provider-specific strings (Gradium, Cartesia, ElevenLabs all use distinct IDs). Changing voice at runtime via `LLMUpdateSettingsFrame`/`UpdateSettingsFrame` reconnects the WS.

**Interruption**: WS providers with word timestamps capture exactly which words were spoken before the user cut in; HTTP providers lose this fidelity.

**Bypass the LLM**: `TTSSpeakFrame("text", append_to_context=True)` queues an utterance directly.

---

## Function calling / tool use

### Two ways to define tools

**Direct functions (what our bot uses):**
```python
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

async def get_current_weather(params: FunctionCallParams, location: str, format: str):
    """Get the current weather.

    Args:
        location: The city and state, e.g. "San Francisco, CA".
        format: Must be "celsius" or "fahrenheit".
    """
    await params.result_callback({"conditions": "sunny", "temperature": "75"})

tools = ToolsSchema(standard_tools=[get_current_weather])
```
Signature → schema; docstring → description. First arg must be `FunctionCallParams`.

**Explicit FunctionSchema:**
```python
from pipecat.adapters.schemas.function_schema import FunctionSchema

weather_function = FunctionSchema(
    name="get_current_weather",
    description="Get the current weather in a location",
    properties={
        "location": {"type": "string", "description": "..."},
        "format": {"type": "string", "enum": ["celsius","fahrenheit"]},
    },
    required=["location", "format"],
)
tools = ToolsSchema(standard_tools=[weather_function])
```

**Provider-native (e.g. OpenAI's web_search):**
```python
from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
tools = ToolsSchema(
    standard_tools=[weather_function],
    custom_tools={AdapterType.OPENAI: [provider_tool]},
)
```

### Registration
Direct + ToolsSchema only declares the tool to the LLM. Register the handler:
```python
llm.register_direct_function(
    get_current_weather,
    cancel_on_interruption=False,
    timeout_secs=60.0,
)
# or:
llm.register_function("get_current_weather", handler, cancel_on_interruption=True, timeout_secs=30.0)
```
**Both** `ToolsSchema(standard_tools=…)` **and** `register_direct_function` are required — declared but not registered = LLM hallucinates tool name with no handler.

### `FunctionCallParams`
```python
@dataclass
class FunctionCallParams:
    function_name: str
    tool_call_id: str
    arguments: Mapping[str, Any]
    llm: LLMService                              # for push_frame, etc.
    context: LLMContext                          # for messages
    result_callback: FunctionCallResultCallback
    app_resources: Any                           # whatever you pass to PipelineWorker(app_resources=...)
```

### `FunctionCallResultProperties`
```python
@dataclass
class FunctionCallResultProperties:
    run_llm: bool | None = None              # skip post-call LLM round
    on_context_updated: Callable | None = None
    is_final: bool = True                    # for streamed/partial results
```
- `run_llm=False` — what we use in `end_call`; also useful for chained tool calls so the LLM doesn't narrate intermediate steps.
- `is_final=False` — push interim status updates from a long-running tool (only meaningful with `cancel_on_interruption=False`).

### Async tools / cancellation
- `cancel_on_interruption=True` (default): tool aborts on user barge-in; LLM waits for result.
- `cancel_on_interruption=False`: tool keeps running; LLM proceeds without waiting.
- `enable_async_tool_cancellation=True` on the LLM service auto-injects a `cancel_async_tool_call` tool the model can call to drop a stale in-flight call.

### Intermediate / streaming results
```python
async def track_delivery(params: FunctionCallParams):
    await params.result_callback({"status": "picked_up"},
        properties=FunctionCallResultProperties(is_final=False))
    await params.result_callback({"status": "delivered"})  # is_final=True by default
```

### Error handling
```python
async def fetch_weather_from_api(params: FunctionCallParams):
    try:
        ...
        await params.result_callback({...})
    except Exception as e:
        await params.result_callback({"error": f"Failed to get weather: {e}"})
```

### `@tool` decorator + `LLMWorker` (modern pattern, alternative)
```python
class MyAgent(LLMWorker):
    @tool
    async def get_weather(self, params: FunctionCallParams, city: str):
        """Get the current weather for a city.

        Args:
            city (str): The city name (e.g. 'San Francisco').
        """
        await params.result_callback(await fetch_weather(city))
```
Our bot still uses the function-list + `register_direct_function` pattern (1.3.x supports both).

### MCP tools
```python
async with MCPClient(server_params=StdioServerParameters(...)) as mcp:
    tools = await mcp.register_tools(llm)
    context = LLMContext(tools=tools)
```
Combine multiple MCP servers by concatenating their `ToolsSchema`s.

---

## Aggregators & `LLMContext`

`LLMContext` is the universal, OpenAI-shaped context container (replaces the per-provider contexts that existed before 1.0). It owns:
- `messages` (list of role/content dicts; roles: `system`/`developer`/`user`/`assistant`/`tool`)
- `tools` (`ToolsSchema`)
- `tool_choice`

### Pairing
```python
context = LLMContext(tools=tools)
user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        user_turn_strategies=FilterIncompleteUserTurnStrategies(),
    ),
)
```
- User aggregator: place **after STT**. Collects transcripts → user messages.
- Assistant aggregator: place **after `transport.output()`**. Captures spoken text word-by-word so interruptions truncate the recorded utterance correctly.

### System instruction vs developer messages
Pick the right home:
- `system_instruction=...` in the LLM Settings — survives context summarization, single source of truth.
- `{"role": "developer", "content": "..."}` injected into messages — participates in normal flow; gets summarized.

### Mutating context
```python
# Kick off a turn (what we do in on_client_connected)
context.add_message({"role": "user", "content": "Greet the caller."})
await worker.queue_frames([LLMRunFrame()])

# Append + run mid-call
await worker.queue_frame(LLMMessagesAppendFrame(
    [{"role": "developer", "content": "..."}],
    run_llm=True,
))

# Read it back
msgs = user_aggregator.context.messages
```

### Auto-summarization
Off by default. Default thresholds (when on): 8000 tokens or 20 messages.
```python
from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregatorParams
from pipecat.utils.context.llm_context_summarization import (
    LLMAutoContextSummarizationConfig, LLMContextSummaryConfig,
)
LLMContextAggregatorPair(
    context,
    assistant_params=LLMAssistantAggregatorParams(
        enable_auto_context_summarization=True,
        auto_context_summarization_config=LLMAutoContextSummarizationConfig(
            max_context_tokens=4000,
            max_unsummarized_messages=10,
            summary_config=LLMContextSummaryConfig(
                target_context_tokens=3000,
                min_messages_after_summary=2,
            ),
        ),
    ),
)
```
System message survives. Function-call pairs are kept intact. Anything important that must never be summarized goes in `system_instruction`, not in messages.

### Transcript capture (events)
```python
from pipecat.processors.aggregators.llm_response_universal import (
    UserTurnStoppedMessage, AssistantTurnStoppedMessage,
)

@user_aggregator.event_handler("on_user_turn_stopped")
async def _(aggregator, strategy, m: UserTurnStoppedMessage):
    log(f"[{m.timestamp}] user: {m.content}")

@assistant_aggregator.event_handler("on_assistant_turn_stopped")
async def _(aggregator, m: AssistantTurnStoppedMessage):
    if m.content:
        log(f"[{m.timestamp}] assistant: {m.content} (interrupted={m.interrupted})")
```

---

## Turn-taking & VAD

### `SileroVADAnalyzer`
```python
SileroVADAnalyzer(
    sample_rate=None,             # auto from pipeline; must be 8000 or 16000
    params=VADParams(
        confidence=0.7,           # 0..1, speech-detection threshold
        start_secs=0.2,           # speech secs before "speaking"
        stop_secs=0.2,            # silence secs before "quiet"
        min_volume=0.6,           # 0..1, minimum volume
    ),
)
```

### Turn-strategy stack
Default = VAD start + `LocalSmartTurnAnalyzerV3` stop. The aggregator accepts a `user_turn_strategies` that combines `start=[...]` and `stop=[...]` strategies.

**Start strategies** (any one fires the turn):
- `VADUserTurnStartStrategy` (speech detected)
- `TranscriptionUserTurnStartStrategy` (text arrived)
- `MinWordsUserTurnStartStrategy`
- `WakePhraseUserTurnStartStrategy`
- `ExternalUserTurnStartStrategy`

**Stop strategies**:
- `SpeechTimeoutUserTurnStopStrategy`
- `TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())` — ML model on last ~8s of audio, runs in <100ms ONNX/CPU, supports 23 languages.
- `LLMTurnCompletionUserTurnStopStrategy` — used by `FilterIncompleteUserTurnStrategies`.
- `ExternalUserTurnStopStrategy`.

### `FilterIncompleteUserTurnStrategies` (what we use)
LLM-gated turn completion. The LLM is auto-prompted to tag each response with `✓` (complete) / `○` (incomplete short) / `◐` (incomplete long, "thinking"). If incomplete, the bot stays quiet and re-prompts after `incomplete_short_timeout` (5s) or `incomplete_long_timeout` (10s).
```python
FilterIncompleteUserTurnStrategies(
    config=UserTurnCompletionConfig(
        incomplete_short_timeout=3.0,
        incomplete_long_timeout=20.0,
        incomplete_short_prompt="...",
        incomplete_long_prompt="...",
    ),
)
```
Markers are stripped from spoken/transcribed text automatically.

### Idle re-engagement
Set `user_idle_timeout=5.0` on `LLMUserAggregatorParams`; then:
```python
@user_aggregator.event_handler("on_user_turn_idle")
async def _(aggregator):
    await aggregator.push_frame(LLMMessagesAppendFrame(
        [{"role": "developer", "content": "User has been quiet — politely check in."}],
        run_llm=True,
    ))
```
At runtime: `await worker.queue_frame(UserIdleTimeoutUpdateFrame(timeout=10.0))` (or `timeout=0` to disable).

---

## Transports

`transport.input()` and `transport.output()` are the bookends of every pipeline. All transports take a `TransportParams`:
```python
TransportParams(
    audio_in_enabled=False,                 # turn audio in on/off
    audio_in_sample_rate=None,              # None = pipeline default
    audio_in_channels=1,
    audio_in_filter=None,                   # e.g. KrispVivaFilter()
    audio_in_stream_on_start=True,
    audio_in_passthrough=True,
    audio_out_enabled=False,
    audio_out_sample_rate=None,             # None = TTS service rate
    audio_out_channels=1,
    audio_out_bitrate=96000,
    audio_out_10ms_chunks=4,
    audio_out_mixer=None,
    audio_out_destinations=[],
    audio_out_end_silence_secs=2,
    audio_out_auto_silence=True,
    # video_* fields if needed
)
```
> The transports doc page lists `vad_analyzer` in `TransportParams` examples; the dedicated params reference doesn't enumerate it. In practice both forms are seen; configuring VAD on `LLMUserAggregatorParams` (as we do) is the canonical 1.x location.

### `SmallWebRTCTransport` (local dev)
```python
SmallWebRTCTransport(
    webrtc_connection: SmallWebRTCConnection,
    params: TransportParams,
    input_name: str = None,
    output_name: str = None,
)
```
Sample rates: 16 kHz in, 24 kHz out (what we use).
Events: `on_client_connected`, `on_client_disconnected`, `on_app_message`.

### `FastAPIWebsocketTransport` (Twilio + other telephony)
```python
FastAPIWebsocketTransport(
    websocket: WebSocket,
    params: FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        add_wav_header=False,       # Twilio = raw mu-law
        serializer=TwilioFrameSerializer(...),
    ),
    input_name=None,
    output_name=None,
)
```
Sample rates for Twilio: **8 kHz in/out** (we override both in `bot()`). Audio is μ-law (PCMU); the serializer converts to PCM internally.
Events: `on_client_connected`, `on_client_disconnected`, `on_session_timeout`.

### `TwilioFrameSerializer`
```python
TwilioFrameSerializer(
    stream_sid=stream_sid,
    call_sid=call_sid,                 # needed for auto hangup
    account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
)
```
With creds, it automatically hangs the call on `EndFrame`/`CancelFrame` — so our `EndTaskFrame` path actually drops the call. Disable with `auto_hang_up=False`.

### TwiML
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://your-url.ngrok.io/ws" />
  </Connect>
</Response>
```

### Other transports
`DailyTransport` (Daily WebRTC), `LiveKitTransport`, `WebsocketTransport` (generic), `HeyGenTransport` / `TavusTransport` (avatar video), `WhatsApp`, `Vonage`. Telephony serializers exist for Telnyx, Plivo, Exotel, Genesys.

---

## Pipeline & worker plumbing

### Pipeline
Linear list of processors. Frame is pushed in order:
```python
pipeline = Pipeline([
    transport.input(),
    stt,
    user_aggregator,
    llm,
    tts,
    transport.output(),
    assistant_aggregator,
])
```
Each processor must call `super().process_frame(frame, direction)` and `push_frame(frame, direction)` for any frame it isn't deliberately consuming. Order matters — each processor needs the frame types its upstream is emitting.

`ParallelPipeline` lets you fan out → fan in (used by `VoicemailDetector`, RTVI, etc.).

### `PipelineParams`
```python
PipelineParams(
    audio_in_sample_rate=16000,
    audio_out_sample_rate=24000,
    enable_heartbeats=False,
    heartbeats_period_secs=1.0,
    heartbeats_monitor_secs=10.0,
    enable_metrics=False,
    enable_usage_metrics=False,
    report_only_initial_ttfb=False,
    send_initial_empty_metrics=True,
    start_metadata={},
)
```
(Observers, idle-timeout, allow-interruptions configs are accepted by `PipelineWorker`/aggregators directly — see below.)

### `PipelineWorker` (1.x successor to `PipelineTask`)
```python
worker = PipelineWorker(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[...],                                  # list of BaseObserver
    idle_timeout_secs=300,
    idle_timeout_frames=(BotSpeakingFrame, UserSpeakingFrame),
    cancel_on_idle_timeout=True,
    app_resources=...,                                # exposed as params.app_resources in tools
    enable_tracing=False,                             # OpenTelemetry
    enable_turn_tracking=False,
    conversation_id=None,
)

# Lifecycle
await worker.queue_frame(frame, direction)
await worker.queue_frames([f1, f2])
await worker.stop_when_done()
await worker.cancel()
worker.has_finished()
```

Worker event handlers: `on_pipeline_started`, `on_pipeline_finished`, `on_pipeline_error`, `on_frame_reached_upstream/downstream`, `on_idle_timeout`. Filter with `set_reached_upstream_filter()`.

### `WorkerRunner`
```python
runner = WorkerRunner(
    name=None,
    bus=None,
    handle_sigint=True,   # we pass False because the runner.main wraps us
    handle_sigterm=False,
    force_gc=False,
    loop=None,
)
await runner.add_workers(worker)
await runner.run()        # blocks until pipelines finish
```
For multi-agent setups (handoffs), `WorkerRunner` is what owns the bus.

### `pipecat.runner.run.main`
The harness in our `__main__`: parses CLI, builds the appropriate `RunnerArguments` (`SmallWebRTCRunnerArguments`, `WebSocketRunnerArguments`, `DailyRunnerArguments`), and calls our `bot(runner_args)`. Local dev opens a FastAPI server on port 7860 with a debug HTML UI.

`parse_telephony_websocket(websocket)` — handshake helper for Twilio/Telnyx/Plivo/Exotel that pulls out `call_id`, `stream_id`, `from_number`, etc.

---

## Events & lifecycle

### Per-transport
- `on_client_connected(transport, client)` — kick off the conversation.
- `on_client_disconnected(transport, client)` — call `worker.cancel()`.
- `on_app_message(...)` (SmallWebRTC only) — incoming data-channel messages.
- `on_session_timeout(...)` (FastAPI WS only).

### Per-worker
`on_pipeline_started` / `_finished` / `_error` / `_idle_timeout` / `_frame_reached_upstream/downstream`.

### Per-aggregator
`on_user_turn_started`, `on_user_turn_stopped(strategy, UserTurnStoppedMessage)`, `on_user_turn_idle`, `on_assistant_turn_stopped(AssistantTurnStoppedMessage)`.

### Session-start canonical pattern (what we use)
```python
@transport.event_handler("on_client_connected")
async def on_client_connected(transport, client):
    context.add_message({"role": "user", "content": "Greet the caller."})
    await worker.queue_frames([LLMRunFrame()])
```
For room-based WebRTC (Daily) use `on_client_ready` instead — `on_client_connected` fires before the client is ready to hear.

---

## Metrics & observability

### Built-in metrics
Set `enable_metrics=True` and/or `enable_usage_metrics=True` on `PipelineParams`. Pipecat then emits `MetricsFrame`s carrying:
- `TTFBMetricsData` (time-to-first-byte per service)
- `ProcessingMetricsData`
- `TextAggregationMetricsData` (first-LLM-token → first-complete-sentence)
- `LLMUsageMetricsData` (prompt/completion tokens)
- `TTSUsageMetricsData` (chars)
- `TurnMetricsData`

Console output:
```
AnthropicLLMService#0 TTFB: 0.8378
CartesiaTTSService#0 text aggregation time: 0.2134
AnthropicLLMService#0 prompt tokens: 104, completion tokens: 53
```

**TTFB note for reasoning models:** stock pipecat stops the TTFB clock on the first chunk with `choices`. For a thinking-enabled model, that's a `reasoning_content` delta — not a user-visible token. Our `VLLMOpenAILLMService` patches this (see `server/nemotron_llm.py`).

### Observers
```python
from pipecat.observers.base_observer import BaseObserver, FramePushed, FrameProcessed

class MyObserver(BaseObserver):
    async def on_push_frame(self, data: FramePushed): ...
    async def on_process_frame(self, data: FrameProcessed): ...
    async def on_pipeline_started(self): ...

worker = PipelineWorker(pipeline,
    params=PipelineParams(observers=[MyObserver()]))
```

Built-ins:
- `DebugLogObserver(frame_types=(TranscriptionFrame, InterimTranscriptionFrame))` — log selected frame types.
- `LLMLogObserver`, `TranscriptionLogObserver` — verbatim service activity.
- `UserBotLatencyObserver` — user-to-bot latency, first-bot-speech, per-service breakdown. Emits `on_latency_measured`.
- `TurnTrackingObserver` — `on_turn_started(turn_number)`, `on_turn_ended(turn_number, duration, was_interrupted)`.
- `StartupTimingObserver` — measures processor startup.
- `RTVIObserver` / `GoogleRTVIObserver` — converts internal frames to RTVI protocol for client SDKs.

### OpenTelemetry tracing
```python
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from pipecat.utils.tracing.setup import setup_tracing

exporter = OTLPSpanExporter(endpoint="http://localhost:4317", insecure=True)
setup_tracing(service_name="my-voice-app", exporter=exporter)

worker = PipelineWorker(
    pipeline,
    params=PipelineParams(enable_metrics=True),
    enable_tracing=True,
    enable_turn_tracking=True,
    conversation_id="customer-123",
)
```
Exporters supported: OTLP/gRPC (Jaeger, Grafana), OTLP/HTTP (Langfuse), Console, AWS X-Ray, Google Cloud Trace, Azure Monitor, Datadog APM, OpenInference (Arize), traceAI (Future AGI).
Install: `uv add "pipecat-ai[tracing]"`.

### Datadog / Pipecat Cloud
`/pipecat-cloud/guides/using-datadog` covers Datadog APM specifically when on PCC.

---

## Deployment

### Pipecat Cloud — basics
Profiles:
- `agent-1x` (default) — 0.5 vCPU, 1 GB — voice agents (what we use)
- `agent-2x` — 1 vCPU, 2 GB — voice/video
- `agent-3x` — 1.5 vCPU, 3 GB — heavy video

Regions: `us-west` (default), `us-east`, etc. Agent names are **globally unique across all regions** — suffix with region if multi-region.

### `pcc-deploy.toml`
```toml
agent_name = "flower-bot"
secret_set = "flower-bot-secrets"
agent_profile = "agent-1x"
region = "us-west"

[scaling]
    min_agents = 1
```
Optional `websocket_auth = "token"` for HMAC token auth on the generic WS endpoint.

### Dockerfile pattern (uv)
```dockerfile
FROM dailyco/pipecat-base:latest      # Python 3.12 by default; -py3.11 / -py3.13 / -py3.14 also available

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY ./bot.py bot.py
# COPY any other modules you use (mock_backend.py, nvidia_stt.py, nemotron_llm.py, etc.)
```
Rules: base image expects `bot.py` with `async def bot(runner_args): ...`. No CMD needed. Build for ARM:
```bash
docker build --platform linux/arm64 -t my-agent:latest .
```
**Our repo gotcha**: `server/Dockerfile` only copies `bot.py` — when shipping `bot-gpt.py` / `bot-nemotron.py` either rename or add `COPY` lines for them and the extra modules.

### CLI
```bash
pipecat cloud deploy                          # build + deploy per pcc-deploy.toml
pipecat cloud deploy --region us-east
pipecat cloud deploy --profile agent-2x
pipecat cloud deploy --min-agents 2
pipecat cloud deploy my-agent my-image:0.1    # bring-your-own image
pipecat cloud agent status flower-bot
pipecat cloud agent delete flower-bot

# Secrets
pipecat cloud secrets set flower-bot-secrets KEY1=value KEY2=value2
pipecat cloud secrets list
pipecat cloud secrets list flower-bot-secrets
pipecat cloud secrets unset flower-bot-secrets KEY_NAME
pipecat cloud secrets delete flower-bot-secrets
```
Secrets become env vars in the agent process. (`pc cloud secrets set <name> --file .env` is what the repo README shows; that flag isn't documented on the secrets reference page — flag if drift, but the README usage clearly works.)

### Local dev runner
`pipecat.runner.run.main()` boots a FastAPI dev server on `:7860` with a built-in WebRTC test UI. `ENV=local` distinguishes dev (we use this to skip Krisp).

---

## Patterns we'll likely need

### Per-call state (confirm: closure is the right pattern)
The bot already does this:
```python
async def run_bot(transport, ...):
    order = {"items": [], "delivery": None}   # one per call

    async def add_to_order(params, bouquet_name, quantity=1):
        order["items"].append({...})           # closes over `order`
```
This is correct. The alternative — `app_resources=...` on `PipelineWorker` — is for **shared** resources (DB pools, API clients) where each call gets the same handle; not for per-call mutable state. Per-call state belongs in a closure created inside `run_bot`.

### Inject a system-level reminder mid-conversation
```python
await worker.queue_frame(LLMMessagesAppendFrame(
    [{"role": "developer", "content": "Reminder: don't forget to ask about delivery date."}],
    run_llm=True,
))
```
Use the idle-event hook for time-based reminders (above in Turn-taking section).

### End the call gracefully (the goodbye + hangup pattern)
This is what `end_call` does:
```python
async def end_call(params: FunctionCallParams):
    await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
    await params.result_callback(
        {"ok": True},
        properties=FunctionCallResultProperties(run_llm=False),
    )
```
- `EndTaskFrame` pushed UPSTREAM is converted to `EndFrame` by the source and propagated downstream — every processor flushes cleanly. TTS finishes the goodbye that's already in-flight (because the goodbye line is in the same LLM turn).
- `run_llm=False` prevents the model from generating a stray follow-up after the tool result.
- For Twilio: `TwilioFrameSerializer` (with creds) auto-hangs on `EndFrame`. Done.
- Instant abort (no goodbye): `await worker.cancel()` — pushes a `CancelFrame`.

### Multi-step intake without read-back chatter
Two levers:
1. **System-prompt discipline** — what we already do: "Ask ONE thing at a time. Don't restate what the customer just said."
2. **`run_llm=False` on intermediate tool calls** — when you chain `set_delivery_details` → `place_order`, you can call `place_order` programmatically without an LLM round trip if you set `run_llm=False` on the first result.

### Structured logging of tool calls
Use `DebugLogObserver` filtered to tool frames, or use the `FunctionCallInProgressFrame` / `FunctionCallResultFrame` lifecycle to attach a `BaseObserver`:
```python
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.frames.frames import FunctionCallInProgressFrame, FunctionCallResultFrame

class ToolCallLogger(BaseObserver):
    async def on_push_frame(self, data: FramePushed):
        if isinstance(data.frame, FunctionCallInProgressFrame):
            log(f"TOOL CALL  {data.frame.function_name}({data.frame.arguments})")
        elif isinstance(data.frame, FunctionCallResultFrame):
            log(f"TOOL RESULT {data.frame.function_name} -> {data.frame.result}")
```

### Save the whole transcript
Use the `on_user_turn_stopped` / `on_assistant_turn_stopped` aggregator events shown above. Or capture audio with `AudioBufferProcessor`:
```python
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

audiobuffer = AudioBufferProcessor(num_channels=1, enable_turn_audio=False)
# add to pipeline after transport.output()
await audiobuffer.start_recording()

@audiobuffer.event_handler("on_audio_data")
async def _(buffer, audio, sample_rate, num_channels):
    with wave.open(f"recordings/{ts}.wav", "wb") as wf:
        wf.setnchannels(num_channels); wf.setsampwidth(2); wf.setframerate(sample_rate)
        wf.writeframes(audio)
```

### Voicemail detection
For outbound, drop in `VoicemailDetector`:
```python
voicemail_detector = VoicemailDetector(llm=OpenAILLMService(...), voicemail_response_delay=2.0)

pipeline = Pipeline([
    transport.input(), stt,
    voicemail_detector.detector(),
    user_aggregator, llm, tts,
    voicemail_detector.gate(),
    transport.output(), assistant_aggregator,
])

@voicemail_detector.event_handler("on_voicemail_detected")
async def _(p):
    await p.push_frame(TTSSpeakFrame("This is Field & Flower calling about your order..."))
    await p.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
```

### Agent handoff (when one bot persona isn't enough)
Multi-agent uses `LLMWorker` subclasses connected via a `BusBridgeProcessor` on the transport-owning agent. The `activate_worker(name, deactivate_self=True)` call from a tool swaps which agent owns the floor:
```python
class IntakeAgent(LLMWorker):
    bridged = ()
    @tool
    async def hand_off_to_billing(self, params: FunctionCallParams):
        await self.activate_worker("billing", deactivate_self=True)
```

### Hot-swap LLM settings
```python
await worker.queue_frame(LLMUpdateSettingsFrame(
    delta=OpenAILLMService.Settings(temperature=0.2)
))
```

---

## Gotchas

- **Sample rates**: set `audio_in_sample_rate` / `audio_out_sample_rate` once on `PipelineParams` (or the transport-specific overrides we use in `bot()`). Don't set them on each service. Twilio is 8 kHz; WebRTC defaults to 16 in / 24 out.
- **Frame direction**: `EndTaskFrame` must be pushed **UPSTREAM**, not downstream. Downstream EndFrame is what every other processor sees after the source converts it.
- **Processor ordering**: each processor needs the frame types its upstream emits. Assistant aggregator goes after `transport.output()` so it captures word-by-word truncation under interruption — moving it ahead of `transport.output()` will desync your transcript on barge-ins.
- **Tools require BOTH `ToolsSchema` and `register_direct_function`.** Declared but not registered = LLM "calls" a no-op.
- **System instruction belongs on the LLM, not in messages**, if you want it to survive auto-context-summarization.
- **Krisp filter is only available when deployed to Pipecat Cloud** — the bot guards with `if ENV != "local"`. Don't try to instantiate `KrispVivaFilter` locally.
- **Push every frame you don't consume.** Custom `FrameProcessor` must call `super().process_frame()` and `push_frame(frame, direction)` or the pipeline starves below it.
- **Settings vs InputParams**: `params=` is deprecated since 0.0.105. Use `settings=...`.
- **Reasoning models and TTFB**: stock TTFB clock stops on first `choices` chunk, which for a thinking model is `reasoning_content`, not user-visible text. The `VLLMOpenAILLMService` in this repo patches that. Hidden assumption: requires the server to emit `reasoning_content` deltas (i.e. vLLM with `--reasoning-parser nemotron_v3`). Without that parser, CoT leaks into `content` and gets spoken.
- **Gradium TTS is fixed at 48 kHz output** — sample rate is not configurable on the service. Pipeline resampling handles it.
- **`pc cloud secrets set --file .env`** — used in the repo's deploy README but not in the secrets reference page. The reference shows `KEY=value` pairs. Both forms appear to work; verify behavior if it ever stops syncing.
- **Dockerfile only copies `bot.py`** by default — if shipping a different filename, edit the Dockerfile or rename.
- **Default base image is Python 3.12** (`dailyco/pipecat-base:latest`). For 3.11, use `:latest-py3.11`.
- **`on_client_connected` fires before client is ready** in room-based WebRTC (Daily). Use `on_client_ready` instead. For SmallWebRTC + Twilio (our cases), `on_client_connected` is the right hook.
- **Turn-completion markers (`✓`/`○`/`◐`) eat your first few tokens** when `FilterIncompleteUserTurnStrategies` is on — that's normal; they're stripped from the spoken output. But if the LLM forgets to emit them, pipecat logs a warning and just runs.

---

## Links

- Pipeline + frame processing: https://docs.pipecat.ai/pipecat/learn/pipeline
- Frames overview: https://docs.pipecat.ai/api-reference/server/frames/overview
- Function calling guide: https://docs.pipecat.ai/pipecat/learn/function-calling
- Context management: https://docs.pipecat.ai/pipecat/learn/context-management
- Turn strategies: https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-turn-strategies
- Filter-incomplete-turns: https://docs.pipecat.ai/api-reference/server/utilities/turn-management/filter-incomplete-turns
- Silero VAD: https://docs.pipecat.ai/api-reference/server/utilities/audio/silero-vad-analyzer
- Smart turn: https://docs.pipecat.ai/api-reference/server/utilities/turn-detection/smart-turn-overview
- SmallWebRTC transport: https://docs.pipecat.ai/api-reference/server/services/transport/small-webrtc
- FastAPI WS transport: https://docs.pipecat.ai/api-reference/server/services/transport/fastapi-websocket
- Twilio serializer: https://docs.pipecat.ai/api-reference/server/services/serializers/twilio
- Twilio guide: https://docs.pipecat.ai/pipecat/telephony/twilio-websockets
- Runner guide: https://docs.pipecat.ai/api-reference/server/utilities/runner/guide
- PipelineWorker: https://docs.pipecat.ai/api-reference/server/pipeline/pipeline-worker
- PipelineParams: https://docs.pipecat.ai/api-reference/server/pipeline/pipeline-params
- WorkerRunner: https://docs.pipecat.ai/api-reference/server/workers/runner
- Metrics: https://docs.pipecat.ai/pipecat/fundamentals/metrics
- OpenTelemetry: https://docs.pipecat.ai/api-reference/server/utilities/opentelemetry
- Observers: https://docs.pipecat.ai/api-reference/server/utilities/observers/observer-pattern
- Pipeline termination: https://docs.pipecat.ai/pipecat/learn/pipeline-termination
- Idle detection: https://docs.pipecat.ai/pipecat/fundamentals/detecting-user-idle
- Saving transcripts: https://docs.pipecat.ai/pipecat/fundamentals/saving-transcripts
- Audio buffer / recording: https://docs.pipecat.ai/pipecat/fundamentals/recording-audio
- Voicemail detector: https://docs.pipecat.ai/pipecat/fundamentals/voicemail
- Context summarization: https://docs.pipecat.ai/pipecat/fundamentals/context-summarization
- Service settings: https://docs.pipecat.ai/pipecat/fundamentals/service-settings
- Custom FrameProcessor: https://docs.pipecat.ai/pipecat/fundamentals/custom-frame-processor
- OpenAI LLM service: https://docs.pipecat.ai/api-reference/server/services/llm/openai
- NVIDIA LLM service: https://docs.pipecat.ai/api-reference/server/services/llm/nvidia
- Anthropic LLM service: https://docs.pipecat.ai/api-reference/server/services/llm/anthropic
- Gradium TTS: https://docs.pipecat.ai/api-reference/server/services/tts/gradium
- NVIDIA STT: https://docs.pipecat.ai/api-reference/server/services/stt/nvidia
- MCP tools: https://docs.pipecat.ai/api-reference/server/utilities/mcp/mcp
- Agent handoff: https://docs.pipecat.ai/pipecat/learn/agent-handoff
- Pipecat Cloud deploy: https://docs.pipecat.ai/pipecat-cloud/fundamentals/deploy
- Pipecat Cloud secrets: https://docs.pipecat.ai/pipecat-cloud/fundamentals/secrets
- Pipecat Cloud agent images / Dockerfile: https://docs.pipecat.ai/pipecat-cloud/fundamentals/agent-images
- 1.0 migration: https://docs.pipecat.ai/pipecat/migration/migration-1.0
- Full URL index: https://docs.pipecat.ai/llms.txt
