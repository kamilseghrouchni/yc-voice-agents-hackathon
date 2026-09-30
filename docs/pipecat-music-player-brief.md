# Pipecat Music Player — UI Architecture Brief

> Source: https://github.com/pipecat-ai/pipecat-music-player (crawled 2026-05-30)
> Purpose: blueprint for the biobank concierge live-browser UI.

## TL;DR — three sentences

The music-player ships a React/Vite SPA driven entirely by **RTVI `UICommand` messages** the backend pushes over the existing Pipecat WebRTC data channel — there is no separate WebSocket, no SSE, and no REST polling. Voice and UI are decoupled into two Pipecat workers: a `PipelineWorker` runs STT → LLM → TTS with a single `handle_request` tool, which delegates every utterance to a `UIWorker` whose own LLM picks a tool (e.g. `navigate_to_artist`, `play`) and emits `send_command("screen", {...})` payloads that the React client renders as screens, toasts, and now-playing state. Client clicks travel back as `client.sendUIEvent("nav", payload)`, dispatched server-side to `@ui_event` handlers — same channel, same protocol — so the architecture is a clean bidirectional event bus on top of one WebRTC peer connection.

**This IS the right reference for the biobank UI.** It maps almost 1:1 onto what we want: tool calls produce structured server state, server pushes that state to a React UI as typed messages, voice stays primary. The only piece that does NOT carry over cleanly is the dual-LLM "voice agent → UI agent" split — for the biobank we can keep our single Nemotron pipeline and just emit `send_command(...)` directly from each existing tool function.

## Repo layout

```
pipecat-music-player/
├── server/                    # Python / Pipecat backend
│   ├── bot.py                 # entry point: PipelineWorker (transport + RTVI + voice LLM)
│   ├── ui_agent.py            # MusicUIWorker: tools + @ui_event click handlers + send_command()
│   ├── catalog_agent.py       # CatalogWorker: Deezer catalog (no LLM, @job handlers)
│   ├── discovery_agent.py     # 3 background workers for "related artists" fan-out
│   ├── deezer.py              # Deezer REST adapter
│   ├── llm.py                 # shared OpenAI/Cartesia/Soniox config
│   ├── descriptions.py        # LLM-grounded artist/album answers
│   ├── pyproject.toml         # pipecat-ai[webrtc,daily,silero,soniox,cartesia,openai,runner]>=1.3.0
│   ├── Dockerfile             # Pipecat Cloud image
│   └── pcc-deploy.toml        # `pc cloud deploy` config (agent_name, krisp filter, scaling)
│
└── client/                    # React 19 + Vite 8 + TypeScript SPA
    ├── index.html             # single root div
    ├── vite.config.ts         # @vitejs/plugin-react; no proxy (client points straight at /start)
    ├── package.json           # @pipecat-ai/{client-js, client-react, voice-ui-kit, small-webrtc-transport, daily-transport}
    ├── src/
    │   ├── main.tsx           # <PipecatAppBase> wrapper from voice-ui-kit (provides client + connect/disconnect)
    │   ├── App.tsx            # top-level layout: Header + screen switch + Toast; wires useServerMessages
    │   ├── config.ts          # VITE_BOT_START_URL + VITE_TRANSPORT (smallwebrtc vs daily)
    │   ├── types.ts           # tagged-union Screen / ServerMessage / ClickEvent — load-bearing
    │   ├── index.css          # all styling, one file
    │   ├── hooks/
    │   │   ├── useServerMessages.ts   # THE reducer: RTVIEvent.UICommand → screen/toast/playback state
    │   │   ├── useClickSender.ts      # client.sendUIEvent(kind, payload) wrapper
    │   │   └── usePreviewPlayer.ts    # tiny <audio> wrapper for 30s preview URLs
    │   ├── components/
    │   │   ├── Grid.tsx       # generic 8-col grid + GridCell (image + title + onClick)
    │   │   ├── Header.tsx     # Back / Home / VoiceVisualizer / ConnectButton / Now Playing
    │   │   └── Toast.tsx      # dismissible card auto-closed on BotStoppedSpeaking
    │   └── screens/
    │       ├── Welcome.tsx    # pre-connect card with example phrases
    │       ├── Home.tsx       # 3 stacked grids (trending / new / favorites)
    │       ├── Artist.tsx     # tabbed page (albums / songs / related)
    │       ├── Detail.tsx     # album/song hero + tracklist with per-row play buttons
    │       └── Trending.tsx   # single grid
```

## Connection model

Same transport flow we already use. `client/src/config.ts` builds an `APIRequest` pointing at the bot's `/start` endpoint; `PipecatAppBase` from `@pipecat-ai/voice-ui-kit` handles the SmallWebRTC handshake (POST offer → SDP answer → peer connection up).

```ts
// client/src/config.ts
const smallWebRTCConfig: APIRequest = {
  endpoint: botStartUrl,                       // VITE_BOT_START_URL || http://localhost:7860/start
  requestData: {
    createDailyRoom: false,
    enableDefaultIceServers: true,
    transport: "webrtc",
  },
};

export const TRANSPORT_CONFIG = {
  daily: dailyConfig,
  smallwebrtc: smallWebRTCConfig,
};
```

```tsx
// client/src/main.tsx — voice-ui-kit owns the client lifecycle
<PipecatAppBase
  connectParams={connectParams}
  initDevicesOnMount
  transportType={transportType}   // "smallwebrtc" or "daily"
  noThemeProvider
>
  {({ client, handleConnect, handleDisconnect, error }) => (
    <App client={client} handleConnect={handleConnect} handleDisconnect={handleDisconnect} />
  )}
</PipecatAppBase>
```

**Key takeaway:** they did NOT open a second channel. Voice frames AND structured UI messages all flow over the one RTVI WebRTC data channel that Pipecat already maintains. No `ws://`, no `EventSource`, no REST polling — `RTVIEvent.UICommand` is just another framed message on the same peer connection.

## Bot → UI state pushes

The mechanism is **`UIWorker.send_command(name, payload)`** on the server side and **`RTVIEvent.UICommand`** on the client side. Internally, `send_command` publishes a `BusUICommandMessage`; the `PipelineWorker` translates it into an `RTVIUICommandFrame` that the SmallWebRTC/Daily transport ships down the same data channel as voice frames.

**Server side** (`server/ui_agent.py`):

```python
async def _emit_artist(self, artist: dict) -> None:
    tab = self._get_artist_tab(artist["id"])
    await self.send_command(
        "screen",
        {
            "screen": "artist",
            "artist": artist,
            "active_tab": tab,
            "back_enabled": len(self._state.stack) > 1,
        },
    )
    self._screen_state = self._describe_artist_screen(artist)
```

```python
# Playback push — same pattern, different command name
await self.send_command(
    "playback",
    {
        "state": "playing",
        "item_title": item["title"],
        "item_id": item["id"],
        "preview_url": preview_url,
    },
)
```

**Client side** (`client/src/hooks/useServerMessages.ts`):

```ts
useRTVIClientEvent(RTVIEvent.UICommand, (data: unknown) => {
  // The server drives the UI with UI commands: the command name is the
  // message type and the payload carries the rest. Rebuild the tagged
  // ServerMessage the reducer below already understands.
  const { command, payload } = data as UICommandData;
  const msg = {
    type: command,
    ...(payload as Record<string, unknown>),
  } as unknown as ServerMessage;

  if (msg.type === "screen") {
    if (msg.screen === "home") {
      setScreen({ kind: "home", artists: msg.artists, new_releases: msg.new_releases, favorites: msg.favorites });
    } else if (msg.screen === "artist") {
      setScreen({ kind: "artist", artist: msg.artist, activeTab: msg.active_tab, backEnabled: msg.back_enabled });
    } /* ... */
  } else if (msg.type === "toast") { /* showToast(...) */ }
    else if (msg.type === "playback") { /* play <audio> preview */ }
    else if (msg.type === "favorite_added") { /* setFavorites(...) */ }
    /* scroll_to, playback_control, favorite_removed ... */
});
```

The full set of UICommand names emitted by the server: `screen`, `toast`, `playback`, `playback_control`, `favorite_added`, `favorite_removed`, `scroll_to`. Each maps to a branch in the `useServerMessages` reducer.

## UI ↔ Tool calls

**This is the most important section for us.** The music-player does NOT surface raw tool-call args/results to the UI (no `RTVIEvent.LLMFunctionCall` listener anywhere in the client). Instead, the pattern is:

1. The LLM picks a tool (e.g. `navigate_to_artist`).
2. The tool handler does the work, then calls `self.send_command("screen", {...full structured payload...})` AND `self.respond_to_job(spoken_reply, tts_speak=True)`.
3. The client receives the `screen` UICommand and rerenders.

So the UI sees a **rendered representation of the tool result**, not the function call envelope. The server is responsible for translating "tool fired → structured screen state" before pushing.

```python
# server/ui_agent.py — every UI tool follows this shape
@tool
async def navigate_to_artist(self, params: FunctionCallParams, artist_name: str):
    """Push the artist screen for the named artist."""
    artist = await self._catalog_find_artist(artist_name)
    if not artist:
        await self.respond_to_job(f"I could not find {artist_name} in the library.", tts_speak=True)
        await params.result_callback(None)
        return
    await self._do_navigate_to_artist(artist)         # ← internally calls send_command("screen", ...)
    await self.respond_to_job(f"Here's {artist['name']}.", tts_speak=True)
    await params.result_callback(None)
```

**Click → tool flow (UI → server, no LLM turn).** Click events go the other way using `client.sendUIEvent(kind, payload)`; on the server they hit `@ui_event` handlers that mutate state directly (no LLM call, low latency):

```ts
// client/src/hooks/useClickSender.ts
export function useClickSender() {
  const client = usePipecatClient();
  return useCallback((event: OutboundMessage) => {
    if (!client) return;
    const { kind, ...payload } = event;
    client.sendUIEvent(kind, payload);           // ← the entire client→server protocol
  }, [client]);
}
```

```python
# server/ui_agent.py — server-side dispatch
@ui_event("nav")
async def on_nav(self, message) -> None:
    await self._handle_nav_click(message.payload or {})

@ui_event("action")
async def on_action(self, message) -> None:
    await self._handle_action_click(message.payload or {})

# _handle_action_click then re-emits screen state via send_command, same as the voice path
```

**For our biobank port:** we do NOT need the two-worker split. We can keep `bot-biobank.py`'s single pipeline and just give each tool a `transport` (or a `send_command`-equivalent) handle. Each tool emits a structured UI message before its `result_callback`. For example, `search_cases` would push:

```python
await transport.send_ui_command("case_results", {
    "query": query,
    "cases": [{"id": ..., "indication": ..., "specimens": [...]}, ...],
})
```

…and the React `useServerMessages` reducer would render those as cards.

## Transcript + speaking state

The music-player does NOT render a transcript pane. It surfaces "is the bot talking?" exclusively through the `VoiceVisualizer` in the header and uses `RTVIEvent.BotStoppedSpeaking` to auto-dismiss the description toast in sync with the voice. No `UserStartedSpeaking`/`UserStoppedSpeaking` UI is wired.

```tsx
// client/src/components/Header.tsx — speaking indicator
<VoiceVisualizer
  participantType="bot"
  barCount={5}
  barMaxHeight={20}
  barWidth={4}
  barGap={6}
  barOrigin="center"
  backgroundColor="transparent"
  barColor="#7a5aff"
/>
```

```ts
// client/src/hooks/useServerMessages.ts — narration-linked toast dismissal
useRTVIClientEvent(RTVIEvent.BotStoppedSpeaking, () => {
  if (!toastFollowsBot.current) return;
  toastFollowsBot.current = false;
  clearTimeout(toastTimer.current);
  setToast(null);
});
```

For the biobank we'll likely want a richer transcript pane. The plumbing is well-trodden — listen for `RTVIEvent.UserTranscript` and `RTVIEvent.BotTranscript` on the same `useRTVIClientEvent` hook and push them into a transcript list. The voice-ui-kit also ships pre-built transcript components we can drop in. TODO — not obvious from this repo, but standard Pipecat RTVI surface.

## Deployment shape

Two artifacts, two hosts:

- **Server** ships as a Pipecat Cloud agent. `server/pcc-deploy.toml` declares `agent_name = "music-player"`, `agent_profile = "agent-1x"`, krisp filter, scaling. Deploy via `pc cloud secrets set ... --file .env` then `pc cloud deploy`. The `Dockerfile` is a vanilla Pipecat base image.
- **Client** is a vanilla Vite SPA — `npm run build` emits `dist/` which deploys to any static host (Vercel/Netlify/CF Pages). No Pipecat-Cloud-specific build hooks. The client knows about the server only via `VITE_BOT_START_URL` + `VITE_BOT_START_PUBLIC_API_KEY` (bearer token to call the cloud `/start` endpoint).

Local dev: server on `:7860`, client on `:5173`. No dev proxy in `vite.config.ts` — the client points directly at the absolute origin.

**Note for us:** our `server/Dockerfile` currently only copies `bot.py` + `mock_backend.py` (per the repo CLAUDE.md). We'd need to either rename `bot-biobank.py` → `bot.py` or update the Dockerfile to ship the biobank bot. Existing pattern already documented in CLAUDE.md.

## What we'd port to biobank-concierge

For the biobank UI, we'd reuse:

- `client/index.html`, `client/vite.config.ts`, `client/package.json` → copy as-is, change `name` to `biobank-client`.
- `client/src/main.tsx` → reuse as-is for the connect/disconnect lifecycle; only the env vars change (`VITE_BOT_START_URL=http://localhost:7860`).
- `client/src/config.ts` → reuse as-is; SmallWebRTC + Daily transport config already matches our bot's transport dispatch.
- `client/src/hooks/useServerMessages.ts` → keep the structure (`useRTVIClientEvent(RTVIEvent.UICommand, ...)`) but replace the music-domain branches (`screen`/`playback`/`favorite_added`) with biobank branches (`case_results`, `specimen_list`, `order_updated`, `quote`, `submitted`, `toast`).
- `client/src/hooks/useClickSender.ts` → reuse as-is. Same `sendUIEvent(kind, payload)` protocol.
- `client/src/types.ts` → adapt the discriminated unions. Replace `Screen`/`ServerMessage`/`ClickEvent` definitions with biobank equivalents (cases, specimens, order line items, quote total).
- `client/src/components/Header.tsx` → adapt: keep `VoiceVisualizer` + `ConnectButton`, drop "Now Playing" / Back / Home, add maybe "Cancel order".
- `client/src/components/Toast.tsx` → reuse as-is for confirmations (e.g. "Added 50 mL plasma to order").
- `client/src/components/Grid.tsx` → adapt grid into a card list for case search results.
- `client/src/screens/Welcome.tsx` → reuse the shape; replace example phrases with "Find me NSCLC FFPE cases", "Add 100 mL plasma", "Quote the order".
- `client/src/screens/Home.tsx` → repurpose as the default "what would you like to search for?" landing.
- `client/src/screens/Detail.tsx` → adapt into a "Case detail" pane (case ID, indication, available specimens, fees per row).

On the server side, the port is smaller than the music-player's because **we don't need the dual-worker split**. We keep `bot-biobank.py` as is and add ONE thing: a way for each tool function to push a UI command alongside its `result_callback`. Two options:

1. Capture the `transport` (or a small `send_ui` closure) in `run_bot()` the same way order state is captured, and call it from each tool. Cleanest for our existing single-pipeline shape.
2. Subclass `PipelineWorker` to expose `send_command(...)` and call `worker.send_command(...)` from tools. Closer to the music-player API but heavier change.

Either way, the *frame* the client receives is `RTVIEvent.UICommand` — the client code is identical regardless of which approach we pick server-side.

### Concrete suggested scaffold for our use case

- **Left pane:** live transcript (RTVI `UserTranscript` + `BotTranscript` events) + `VoiceVisualizer` for speaking state.
- **Center:** results of the latest tool call. `search_cases` → grid of case cards; `get_specimens_for_case` → specimen list; `get_order_summary` → order receipt; `submit_order` → submission confirmation card.
- **Right pane:** running order (specimens added with fee per row, subtotal, fees total). Updated by an `order_updated` UICommand pushed from `add_to_order` / `remove_from_order`.
- **Persistent Toast** for "Added X to order", "Order submitted", error states — exactly the music-player pattern.

## Open questions / things we still don't know

- **Streaming partial tool results.** The music-player's `_run_related_discovery` shows the pattern (mutate state in place, re-call `send_command("screen", ...)` as each worker finishes). That maps to e.g. paginating large `search_cases` results, but we'd need to confirm Pipecat will let a long-running tool emit multiple UI commands before its `result_callback`. TODO — not obvious from the code.
- **Transcript surface.** The music-player intentionally doesn't render transcript. For us it's a primary feature. We'd need to confirm RTVI `UserTranscript`/`BotTranscript` event payload shape and timing (interim vs final), or fall back to `voice-ui-kit`'s built-in transcript components. TODO — not in this repo.
- **Tool-call visibility for debugging.** No example in this repo of listening to `RTVIEvent.LLMFunctionCall` directly on the client. For developer UX it might be useful to see "agent is calling `search_cases({query: ...})`" inline, but the music-player deliberately abstracts that away.
- **Auth on the data channel.** Music-player relies on the bot start endpoint being protected by `VITE_BOT_START_PUBLIC_API_KEY`. Once the WebRTC pc is up, every `sendUIEvent` is implicitly trusted. We'd want to think about whether biobank tools (especially `submit_order`) need a higher-trust path, or if a session-scoped order is fine.
- **Reconnect behavior.** The music-player handles reconnect by sending `{kind: "hello"}` on `RTVIEvent.BotReady`; the server's `@ui_event("hello")` re-emits the top of the nav stack. We'd want the same hello → "re-emit current order + last search results" handshake.
- **No transcript persistence.** Music-player drops state on disconnect. For the biobank we may want the order to survive a refresh, which would push us toward server-persisted sessions keyed off the WebRTC connection id.

## Links

- Repo root: https://github.com/pipecat-ai/pipecat-music-player
- Server entry: https://github.com/pipecat-ai/pipecat-music-player/blob/main/server/bot.py
- UI worker (tools + send_command + @ui_event): https://github.com/pipecat-ai/pipecat-music-player/blob/main/server/ui_agent.py
- Client app shell: https://github.com/pipecat-ai/pipecat-music-player/blob/main/client/src/App.tsx
- **`useServerMessages` reducer** (the most important client file): https://github.com/pipecat-ai/pipecat-music-player/blob/main/client/src/hooks/useServerMessages.ts
- `useClickSender` (client → server): https://github.com/pipecat-ai/pipecat-music-player/blob/main/client/src/hooks/useClickSender.ts
- Typed message contract: https://github.com/pipecat-ai/pipecat-music-player/blob/main/client/src/types.ts
- Live deploy reference: https://pipecat-music-player.vercel.app/
