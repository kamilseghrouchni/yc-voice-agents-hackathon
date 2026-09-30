# Diaries

Working notes on tools and frameworks we're using to build the procurement agent. Companion to `pipecat-diary.md` (which is the verbatim Pipecat API reference).

---

## Cekura — evalset-driven agent development

### The hack in one line

Cekura runs simulated callers against your agent over and over, scores each conversation on metrics you define, and gives you a structured report of what failed and where. Claude Code reads that report, edits your system prompt / `soul.md` / tool definitions, and re-runs. The "evalset passes" gate is just a numerical threshold (e.g. ≥80% of scenarios green) that you decide.

### The loop, mapped to the skills

| Step | Skill |
|---|---|
| 1. Connect your agent to Cekura (pick provider: **Pipecat**) | `cekura-create-agent` |
| 2. Decide what "good" means — metrics (CSAT, task completion, "never names suppliers", "captures all 10 priority fields", etc.) | `cekura-metric-design` + `cekura-predefined-metrics` for the free ones |
| 3. Build the evalset — scenarios like "researcher demands binding quote", "vague qty, won't commit", "IRB not approved", "tries goodbye mid-spec" | `cekura-eval-design` (strategy) + `autogen-eval` (bulk) or `manual-create-update-eval` (hand-tune) |
| 4. Execute against your bot | `run-evals` |
| 5. Pull results, group failures | `eval-results` |
| 6. **The iteration loop** — Claude Code diagnoses failures → edits prompt/tools → re-runs evals | `cekura-self-improving-agent` |
| 7. When a specific scenario keeps failing, debug that call | `cekura-fixing-prod-issues` (works for sim-failures too) |

Skill 6 is the "magic" — it's literally a `diagnose → propose → apply → re-validate` loop. For our case (self-hosted Pipecat bot, not VAPI), it operates on the system prompt + tool definitions in `bot-crovi.py` and `soul.md`.

### End-to-end run, concretely

```
/cekura:cekura-onboarding              # one-time wiring, pick Pipecat as provider
/cekura:cekura-create-agent            # registers our local bot
/cekura:cekura-eval-design             # we co-author ~10–15 scenarios
/cekura:run-evals                      # baseline run, see where we are
/cekura:cekura-self-improving-agent    # the loop until threshold is hit
```

### Why this fits the procurement agent specifically

The form (`docs/suppliers/biospecimen-procurement-form.pdf`) gives us a ground-truth checklist of fields to capture, and the `soul.md` gives us behavioral rules ("never name suppliers", "ballpark not quote") — both are *measurable*. That's exactly the shape Cekura is built for, vs. agents where "good" is fuzzy.

### Gotcha — Cekura needs to reach your bot

Local `localhost:7860` won't be reachable from Cekura's cloud. Options:
- **ngrok tunnel** (fastest for dev loop)
- **Deploy to Pipecat Cloud first** (`pc cloud deploy`) — more realistic for the eval loop but slower iteration

The `cekura-create-agent` skill walks through it. Plan on doing one or the other before step 4.

### Recommended sequence for our build

1. Finish the four procurement design questions (inbound/outbound, wedge scope, quote source, soul.md content)
2. Write v0 of `bot-crovi.py` + `soul.md`
3. Run locally, sanity-check the golden path
4. Expose via ngrok
5. `cekura:cekura-onboarding` → `cekura:cekura-eval-design`
6. Let `cekura-self-improving-agent` tighten it

The eval suite becomes the spec we couldn't have written upfront.

### MCP / install state (as of 2026-05-30)

- HTTP MCP server `cekura` added at `https://api.cekura.ai/mcp` (user scope)
- Plugin marketplace `cekura-ai/cekura-skills` added
- Plugin `cekura@cekura-skills` installed (run `/reload-plugins` to apply)

---

## Voice agent + visible UI — design exploration (parked, 2026-05-30)

Picking back up here. Goal: while the caller talks to the biobank agent, the audience sees it navigating referencemedicine.com — "agent reasons live on the site." Three threads converged into one decision.

### The spectrum (pick one architecture, don't blend early)

| Mode | Agent tools | Browser library | Latency | Demo story |
|---|---|---|---|---|
| **A. Passive mirror** | only data tools; a FrameProcessor sniffs `FunctionCall…Frame` and fires `mirror.show(...)` to a Playwright sidecar | Playwright headed | brain ~100ms; browser catches up async | "we mirrored the agent's state" — safe, invisible to tool code |
| **B. Actuator tools** | LLM gets explicit `navigate(path)`, `highlight(text)`, `scroll_to(text)` alongside data tools | Playwright headed | brain decides → browser acts ~300-800ms | "agent reasons live on the site" — judges see deliberate clicks |
| **C. Autonomous browsing** | one `browse(goal)` tool; LLM reads DOM, picks next click per turn | `browser-use` / Stagehand / Skyvern | 2-10s per step | overkill for a structured biobank |

**Direction:** B. The actuator tools turn each browser action into a graded LLM decision — exactly the shape Cekura can score, and the auto-improvement loop can tune.

### Pipecat ships nothing for browser control

Pipecat = voice/multimodal pipeline. The composition point is the **function-call frame stream**: every LLM decision leaves the LLM processor as a `FunctionCall…Frame` you can intercept (FrameProcessor) or react to (tool body side-effect). Brain = pipecat. Hands = Playwright. They don't overlap; they compose.

### Reference architecture — `pipecat-ai/pipecat-music-player`

Voice-driven music browsing app, exemplary worker separation. Three workers, two-inference-per-turn.

```
PipelineWorker (bot.py)          MusicUIWorker (ui_agent.py)        CatalogWorker (catalog_agent.py)
─────────────────────────        ─────────────────────────          ─────────────────────────
transport ↔ STT ↔ LLM ↔ TTS      its own LLM                        NO LLM pipeline (BaseWorker)
+ RTVI to React client           owns nav stack + screen state      owns Deezer cache + descriptions
                                 send_command("screen", …)
                                 → RTVI → client renders
ONE tool: handle_request(q)      ~12 tools: navigate_to_artist,     @job(name=…) handlers:
        │                        play, switch_tab, show_info,         list_home, find_artist,
        │                        answer_about_catalog, …              get_album_tracks, …
        │
        ▼                        client clicks → @ui_event           single warm process
worker.job("ui",                 (no LLM — low-latency state edit)   lifetime = whole runner
  name="respond",                       │
  payload={query})                     ▼
        │                        self.job("catalog", name=…)
        └──────────────────────► uniform inter-worker primitive
```

Key contracts to steal:
- **Two inferences per turn.** Voice LLM hears → calls `handle_request(query)` → opens job to UI worker. UI LLM reads `(query, current screen)` → picks action tool → `respond_to_job(answer, tts_speak=True)`. The voice pipeline speaks the UI worker's return value **verbatim** — voice LLM never re-phrases, can't drift.
- **`keep_history=False`** on UI worker. The screen IS the context, auto-injected each turn. No history accretion.
- **Client clicks bypass the LLM** via `@ui_event` handlers — direct state edit, fast UI.
- **One primitive for all inter-worker calls:** `self.job("worker_name", name="handler_name", payload=...)`.

### Current `bot-biobank.py` shape (today)

Single `PipelineWorker`, voice LLM owns 8 tools (data + order + lifecycle). `BIOBANK = load_biobank(BIOBANK_ID)` at module scope; tools close over per-call `order` dict inside `run_bot()`. `prompts/v0.md` mixes voice rules + tool flow + honesty — **one LLM is doing five jobs** (conversation flow, vocabulary, tool args, order state, voice rules).

### Three-worker shape applied to biobank

```
PipelineWorker (bot.py, ~120 LoC)             BiobankUIWorker (NEW, ~250 LoC)         BiobankCatalogWorker (NEW, ~80 LoC)
─────────────────────────────────             ─────────────────────────────────       ─────────────────────────────────
transport → STT → voice LLM → TTS             UIWorker subclass                       BaseWorker — no LLM pipeline
ONE tool: handle_request(q)                   own LLM + prompts/v{N}.md               wraps existing BiobankBackend
                                              owns: per-call order dict               @job handlers:
+ PlaywrightSubscriber peer                   owns: current view intent                 search_cases
  subscribes to UI commands                                                             get_case_details
  drives long-lived Chromium                  tools:                                    get_specimens_for_case
  on referencemedicine.com                      data:      search_cases (→ catalog)     quote_total
                                                           get_case_details (→ catalog)
voice LLM prompt: ~10 lines,                              get_specimens (→ catalog)    loaded once per process,
"hear → handle_request →                       actuator:  navigate, highlight,         warm cache,
speak return verbatim →                                   scroll_to                    lifetime = whole runner
never re-phrase"                              order/life: add_to_order, set_buyer_info,
                                                          get_order_summary,
                                                          submit_order, end_call
                                              respond_to_job(answer, tts_speak=True)
```

Per-file diff:

| File | Today | After |
|---|---|---|
| `bot-biobank.py` | 467 LoC, owns everything | ~120 LoC: transport dispatch + voice pipeline + 1 tool + `runner.add_workers(catalog, ui, voice)` |
| **NEW** `ui_worker.py` | — | all 8 tools currently in `run_bot()` move here + 3 actuator tools |
| **NEW** `catalog_worker.py` | — | thin `BaseWorker` adapter exposing `BiobankBackend` methods as `@job` handlers |
| `biobank_backend.py` | unchanged | unchanged — pure data layer the catalog worker holds |
| **NEW** `playwright_subscriber.py` | — | peer worker, subscribes to UI command frames, drives Chromium |
| `prompts/v0.md` | mixes everything | becomes UI worker's prompt; voice LLM gets its own ~10-line prompt |
| `soul.md` | unchanged | injected into UI worker prompt only |

### Alignment with CLAUDE.md "generalize, don't hardcode per-biobank" rule

All three workers are biobank-agnostic. Biobank identity stays in `soul.md` + the loaded inventory the catalog worker holds. Auto-improvable artifact stays `prompts/v{N}.md`, now owned by the UI worker. Adding a second biobank = drop a folder under `server/biobanks/` + set `BIOBANK_ID`. No code changes.

### Two-phase plan

- **Phase A (1-2h, low risk):** keep single worker. Add a `FrameProcessor` browser-mirror peer + 3 actuator tools (`navigate`, `highlight`, `scroll_to`) registered alongside existing tools. Voice LLM is now planning browser actions visibly. Cekura can grade "did the agent pick the right page?" today.
- **Phase B (3-5h, after A proves out):** refactor to three-worker pattern. Sharper Cekura attribution (failed call → *which* LLM was wrong?) + cleaner narrative for judges.

### Gating facts before committing to Phase B

1. **Pipecat 1.3.0 API surface.** Current `pyproject.toml` pins `pipecat-ai>=1.3.0`. `PipelineWorker` + `WorkerRunner.add_workers` confirmed present (bot-biobank.py already imports them). Need to verify `UIWorker`, `BaseWorker`, `@job(name=…)`, `@ui_event`, `respond_to_job(…, tts_speak=True)`, `send_command(…)`, `self.job("name", name=…, payload=…)` are in this version. 5-min grep through installed pkg, or check what version the music player pins.
2. **Two-inference latency.** Each user turn now hits an LLM twice. Est +400-800ms TTFSW on Nemotron 120B cold paths. Measure before committing — Phase A keeps single-inference latency.

### Library landscape (for the actuator)

- **Playwright headed (Chromium)** — recommended. Long-lived browser + context, fire-and-forget commands.
- **Daily video transport** — could stream Chromium into the call as video. Overkill for an audience watching a second monitor.
- `browser-use` / Stagehand / Skyvern — autonomous browsing; not needed for a known site we control the harvester for.

### Next concrete steps when picking this back up

1. Grep installed pipecat for `UIWorker` / `BaseWorker` / `@job` / `@ui_event` / `respond_to_job`. Lock the version question.
2. Decide A-only vs. A→B based on remaining hackathon clock.
3. Either way, start by writing `playwright_subscriber.py` standalone — long-lived Playwright headed + tiny POST endpoint. Testable in isolation by curling `/show`.
4. Then wire either (Phase A) FrameProcessor + 3 tools into `bot-biobank.py`, or (Phase B) the three-worker split.
