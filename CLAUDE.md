# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Core principle: generalize from the site, do not hardcode per-biobank fixes

This project's thesis is that **a voice agent for any biobank can be built from that biobank's website and structured inventory** — the system harvests context (process, case studies, inventory schema, pricing) and the agent generalizes from it. Any biobank-specific patch — Reference-Medicine-only column names, hardcoded specimen-type maps, vendor-name lookup tables, RM-specific clinical vocabulary — is an anti-pattern.

When a failure mode shows up in one biobank (e.g. the agent can't filter Reference Medicine cases by specimen type), the fix is NOT to add that filter mapping in code. The fix is one of:

1. **Improve the harvester** — extract a richer schema / capability descriptor from the biobank's site or inventory file so the agent learns the vocabulary at boot time.
2. **Tighten the auto-improvement loop** — let Cekura evals catch the failure and the loop refine `prompts/v{N}.md` (biobank-agnostic) until the agent uses available tools correctly.
3. **Improve the agent-facing tool docstrings** — so the LLM picks better arguments without needing a special-case backend function.

Hardcoding a `_CASE_HAS_SPECIMEN_MAP` keyed on Reference Medicine's column names (e.g. "Plasma (mL)", "Tumor, malignant blocks") is exactly the wrong move — it does not transfer to a second biobank, it embeds vendor schema in code, and it short-circuits the loop the hackathon brief is asking us to build.

If you find yourself writing a `if biobank == "reference_medicine": ...` branch, or mapping caller vocabulary to specific column names of one biobank, **stop and re-route through the harvester or the auto-improvement loop instead.**

## What this repo is

Hackathon starter for a Pipecat voice agent. Two parallel implementations of the same "Field & Flower" flower-shop bot live in `server/`:

- `bot-gpt.py` — Gradium STT → OpenAI Responses (GPT-4.1) → Gradium TTS
- `bot-nemotron.py` — NVIDIA Parakeet STT (`nvidia_stt.py`) → Nemotron-3-Super via vLLM (`nemotron_llm.py`) → Gradium TTS

The two bots are deliberately near-duplicates so they can be A/B'd; if you change tool definitions, system prompt, or pipeline shape in one, mirror it in the other (or factor out the shared piece).

## Commands

All commands run from `server/`.

```bash
uv sync                       # install deps (uv is required, not pip)
uv run bot-gpt.py             # run GPT bot locally on http://localhost:7860
uv run bot-nemotron.py        # run Nemotron bot locally
uv run pytest test_nemotron_llm.py    # unit tests (only nemotron_llm has tests)
uv run pytest test_nemotron_llm.py::test_ttfb_armed_only_on_first_content_token -v
uv run ruff check .           # lint (ruff configured in pyproject.toml: line-length 100, rules I+UP)
uv run ruff format .
uv run pyright                # typecheck
```

First launch takes ~20s — Pipecat downloads Silero VAD and turn-detection models on cold start.

### Deploy (Pipecat Cloud + Twilio)

```bash
pc cloud secrets set flower-bot-secrets --file .env    # sync .env to cloud secrets
pc cloud deploy                                         # build + deploy per pcc-deploy.toml
```

`pcc-deploy.toml` declares `agent_name = "flower-bot"`, `agent_profile = "agent-1x"`, and the secret-set name. The Dockerfile only copies `bot.py` and `mock_backend.py` — **edit it before deploying** if you're shipping `bot-gpt.py` or `bot-nemotron.py` (or rename your bot to `bot.py`).

## Architecture

### Pipeline shape (both bots, identical)

```
transport.input() → STT → user_aggregator → LLM → TTS → transport.output() → assistant_aggregator
```

Wired in `run_bot()`. The aggregators (`LLMContextAggregatorPair`) hold the conversation context that both the user-side (with VAD + turn strategy) and assistant-side processors share. Tools are registered two ways and **both are required**:
1. `ToolsSchema(standard_tools=tool_functions)` → describes tools to the LLM.
2. `llm.register_direct_function(fn)` per function → wires the actual async handler.

### Per-call state

Each call's order dict is created inside `run_bot()` and captured as a closure by the tool functions. Don't move tools to module scope — that would share order state across calls.

### Transport dispatch (`bot()` entrypoint)

`bot(runner_args: RunnerArguments)` matches on the runner type to build the right transport:
- `SmallWebRTCRunnerArguments` → browser dev at 16k/24k Hz
- `WebSocketRunnerArguments` → Twilio media stream at 8k/8k μ-law; also parses the websocket to fetch `call_sid` and looks up the caller's number via Twilio REST for known-customer personalization

Krisp noise filter is added when `ENV != "local"` (i.e. on Pipecat Cloud).

### Mock backend

`mock_backend.py` exports `BOUQUETS` (catalog) and `KNOWN_CUSTOMERS` (phone → profile for returning-caller personalization). This is the file to swap when customizing the demo — the tool functions in `bot-*.py` read these dicts directly. Phone numbers must be E.164 to match Twilio's `from_number`. Bouquet keys are lowercase; the tools lowercase user input before lookup.

### Nemotron-specific: TTFB fix

`nemotron_llm.py` (`VLLMOpenAILLMService`) subclasses `OpenAILLMService` solely to fix a TTFB metric bug for reasoning models. Stock pipecat stops the TTFB clock on the first chunk with `choices` — but for thinking-enabled models, that's a `reasoning_content` delta, not a user-visible token. The subclass arms TTFB only on the first `content` or `tool_calls` delta. If you swap models or pipecat versions, run `test_nemotron_llm.py` to confirm the subclass still composes correctly with the base class.

### System prompt conventions

The system instruction in both bots enforces "real shop clerk" speech: 1–2 sentences per turn, one question at a time, no filler openers, prices spoken in words, lead with the bouquet name when listing. The `end_call` tool must be called **in the same turn as the spoken goodbye** — never alone — and the tool sets `run_llm=False` to prevent a stray follow-up turn after the hangup.

## Environment

Copy `server/.env.example` → `server/.env`. Required keys depend on the bot:
- `bot-gpt.py`: `OPENAI_API_KEY`, `GRADIUM_API_KEY` (+ `GRADIUM_VOICE_ID`, `OPENAI_MODEL` optional)
- `bot-nemotron.py`: `GRADIUM_API_KEY`, `NVIDIA_ASR_URL`, `NEMOTRON_LLM_URL`, `NEMOTRON_LLM_MODEL`, `NEMOTRON_ENABLE_THINKING`
- Telephony (either bot): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`

Per the user's global instructions: do not create a separate `.env` in subfolders — the project-root `.env` is the source of truth. `load_dotenv(override=True)` is called at the top of each bot.

## Testing the agent with Cekura

The README recommends Cekura (`/cekura-report`) for end-to-end agent evaluation via the Cekura Claude Code plugin. When connecting an agent in Cekura, select **Pipecat** as the provider.
