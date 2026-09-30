#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Reference Medicine concierge — biobank voice agent (v0).

Researchers call in to scope a procurement against the live Reference Medicine
inventory. The agent searches cases/specimens, quotes from real fees, captures
buyer info, and produces an order artifact at the end of the call.

Pipeline: NVIDIA STT → Nemotron-3-Super-120B LLM → Gradium TTS, with direct
function tools registered on the LLM context. Inventory backend is loaded once
at boot from server/biobanks/<slug>/inventory/*.xlsx.

Run::

    BIOBANK_ID=reference_medicine uv run bot-biobank.py
"""

import json
import os
import random
import time
from datetime import date
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import (
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from biobank_backend import load_base_prompt, load_biobank, load_site, load_soul
from browser_client import BrowserView
from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService

# Pipecat Cloud delivers calls as Daily.co sessions; importing these lazily-
# guarded so local SmallWebRTC dev does not require the pipecatcloud package
# at import-time when running outside the cloud.
try:
    from pipecat.transports.services.daily import DailyParams, DailyTransport
    from pipecatcloud.agent import DailySessionArguments
    _PCC_AVAILABLE = True
except ImportError:  # pragma: no cover
    DailyParams = DailyTransport = DailySessionArguments = None  # type: ignore
    _PCC_AVAILABLE = False

load_dotenv(override=True)

BIOBANK_ID = os.getenv("BIOBANK_ID", "reference_medicine")
PROMPT_VERSION = os.getenv("PROMPT_VERSION")  # None → highest v{N} on disk
BIOBANK = load_biobank(BIOBANK_ID)
SOUL = load_soul(BIOBANK_ID)
SITE = load_site(BIOBANK_ID)
PROMPT_VERSION_LABEL, BASE_PROMPT = load_base_prompt(PROMPT_VERSION)
logger.info(f"Using base prompt: {PROMPT_VERSION_LABEL}")
logger.info(f"Site base_url: {SITE['base_url']} pages: {list(SITE['pages'])}")
ORDERS_DIR = Path(__file__).parent / "biobanks" / BIOBANK_ID / "orders"
ORDERS_DIR.mkdir(parents=True, exist_ok=True)

# Browser-view sidecar client. Fire-and-forget; if the sidecar isn't running
# the bot keeps working silently. Launch the sidecar separately with:
#   uv run server/browser_view.py
BROWSER = BrowserView(base_url=SITE["base_url"])


async def get_call_info(call_sid: str) -> dict:
    """Fetch call information from Twilio REST API using aiohttp."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    try:
        auth = aiohttp.BasicAuth(account_sid, auth_token)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    logger.error(f"Twilio API error ({response.status}): {await response.text()}")
                    return {}
                data = await response.json()
                return {"from_number": data.get("from"), "to_number": data.get("to")}
    except Exception as e:
        logger.error(f"Error fetching call info from Twilio: {e}")
        return {}


def _build_system_instruction() -> str:
    """Assemble the system prompt in three layers.

    Layers (precedence: later overrides earlier on conflict):
      1. BASE_PROMPT — biobank-agnostic operating instructions + voice rules.
         Auto-improvement loop edits THIS file (server/prompts/v{N}.md).
      2. SOUL — per-biobank persona + biobank-specific hard rules. LOCKED.
      3. Live inventory headline + date — data-derived at boot, not editable.
    """
    s = BIOBANK.summary()
    headline = (
        f"Live inventory: {s['case_count']} cases, {s['specimen_count']} specimens. "
        f"Specimen types available: {', '.join(s['specimen_types'])}. "
        f"Pricing tiers in use: {s['tiers']} (fees range ${min(s['fees_usd'])} to "
        f"${max(s['fees_usd'])} per specimen)."
    )
    pages_listing = ", ".join(sorted(SITE["pages"]))
    site_block = (
        f"Public website pages you can show on screen via the `show_page` tool: "
        f"{pages_listing}."
    )
    return (
        f"{BASE_PROMPT}\n\n"
        f"---\n\n"
        f"# Biobank persona (locked)\n\n{SOUL}\n\n"
        f"---\n\n"
        f"# Live state (data-derived)\n\n"
        f"Today is {date.today().strftime('%A, %B %d, %Y')}.\n\n"
        f"{headline}\n\n"
        f"{site_block}\n"
    )


async def run_bot(
    transport: BaseTransport,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    logger.info(f"Starting biobank bot ({BIOBANK_ID})")

    # Per-call order state, captured by tool closures so each call is isolated.
    order: dict = {"specimen_ids": [], "buyer": None}

    async def search_cases(
        params: FunctionCallParams,
        diagnosis_query: str | None = None,
        tumor_type_query: str | None = None,
        primary_site_query: str | None = None,
        stage: str | None = None,
        treatment_status: str | None = None,
        biomarker_query: str | None = None,
        limit: int = 5,
    ) -> None:
        """Search the case-level inventory by clinical attributes.

        All args are case-insensitive substring matches; pass only what you've
        confirmed with the caller. Use `biomarker_query` for KRAS, EGFR, HER2,
        VHL, etc. — it searches genomic variants and pathology fields.

        Args:
            diagnosis_query: Diagnosis bucket — e.g. "cancer", "benign tumor",
                "healthy", "other diagnosis". Optional.
            tumor_type_query: Histology — e.g. "renal cell carcinoma",
                "adenocarcinoma", "squamous cell". Optional.
            primary_site_query: Primary tumor site — e.g. "kidney", "lung",
                "pancreas", "ampulla of vater". Optional.
            stage: AJCC stage — e.g. "I", "II", "III", "IV". Optional.
            treatment_status: e.g. "treatment naive", "post-treatment",
                "unknown". Optional.
            biomarker_query: Mutation/variant token — e.g. "KRAS", "EGFR",
                "VHL", "PD-L1". Optional.
            limit: Max cases to return (default 5). Don't go above 5 unless
                the caller explicitly asks for more.
        """
        result = BIOBANK.search_cases(
            diagnosis_query=diagnosis_query,
            tumor_type_query=tumor_type_query,
            primary_site_query=primary_site_query,
            stage=stage,
            treatment_status=treatment_status,
            biomarker_query=biomarker_query,
            limit=limit,
        )
        # Mirror to the browser: jump to the inventory page and highlight the
        # most specific filter the LLM provided. Fire-and-forget — never
        # blocks the voice turn.
        if "inventory" in SITE["pages"]:
            highlight = (
                primary_site_query
                or tumor_type_query
                or biomarker_query
                or diagnosis_query
            )
            BROWSER.show(SITE["pages"]["inventory"], highlight=highlight)
        await params.result_callback(result)

    async def get_case_details(params: FunctionCallParams, case_id: str) -> None:
        """Full record for a single RM case ID (e.g. "RM24-01609").

        Use when the caller wants more detail on a case you've already
        narrowed to. Don't read every field aloud — pick what's relevant.
        """
        await params.result_callback(BIOBANK.get_case_details(case_id))

    async def get_specimens_for_case(
        params: FunctionCallParams,
        case_id: str,
        specimen_type: str | None = None,
        tissue_type: str | None = None,
    ) -> None:
        """Specimens available for a case, with tier and fee.

        Args:
            case_id: RM case ID (e.g. "RM22-00014"). Required.
            specimen_type: Optional filter — "Paraffin block", "Plasma",
                "Blood", "Frozen -80C (snap frozen)", "Liquid biopsy set",
                "Matched plasma & buffy set", "Buffy coat", "Serum".
            tissue_type: Optional filter — "Tumor, malignant",
                "Tumor, non-malignant", "Normal", etc.
        """
        result = BIOBANK.get_specimens_for_case(case_id, specimen_type, tissue_type)
        # Mirror to the browser: same inventory page, highlight the case ID.
        if "inventory" in SITE["pages"]:
            BROWSER.show(SITE["pages"]["inventory"], highlight=case_id)
        await params.result_callback(result)

    async def add_to_order(params: FunctionCallParams, specimen_id: str) -> None:
        """Add a specimen to the caller's order. Only call after the caller
        has explicitly confirmed they want this specimen.

        Args:
            specimen_id: The RM specimen ID (e.g. "RM22-00014-D15").
        """
        spec = BIOBANK.specimens_by_id.get(specimen_id)
        if not spec:
            await params.result_callback(
                {"ok": False, "reason": f"No specimen with ID {specimen_id} in inventory."}
            )
            return
        if specimen_id in order["specimen_ids"]:
            await params.result_callback(
                {"ok": False, "reason": f"{specimen_id} is already in the order."}
            )
            return
        order["specimen_ids"].append(specimen_id)
        await params.result_callback(
            {
                "ok": True,
                "added": specimen_id,
                "fee_usd": spec.get("Fee"),
                "items_in_order": len(order["specimen_ids"]),
            }
        )

    async def get_order_summary(params: FunctionCallParams) -> None:
        """Read back the current order with running total. Always call this
        before `submit_order` so the caller can confirm."""
        quote = BIOBANK.quote_total(order["specimen_ids"])
        await params.result_callback(
            {"buyer": order["buyer"], "items": quote["line_items"], "subtotal_usd": quote["subtotal_usd"]}
        )

    async def set_buyer_info(
        params: FunctionCallParams,
        name: str,
        email: str,
        company: str | None = None,
        project_name: str | None = None,
    ) -> None:
        """Capture buyer contact info before order submission.

        Args:
            name: Buyer's full name.
            email: Buyer's email (where the order XLSX will be sent).
            company: Optional company/institution name.
            project_name: Optional project or study name.
        """
        order["buyer"] = {
            "name": name,
            "email": email,
            "company": company,
            "project_name": project_name,
        }
        await params.result_callback({"ok": True, "buyer": order["buyer"]})

    async def submit_order(params: FunctionCallParams) -> None:
        """Finalize the order. Only call after `get_order_summary` and
        the caller's confirmation. Saves an order JSON artifact and returns
        a confirmation number. (v0: artifact is JSON; the XLSX export will
        come in v1.)"""
        if not order["specimen_ids"]:
            await params.result_callback({"ok": False, "reason": "No specimens in the order."})
            return
        if not order["buyer"]:
            await params.result_callback(
                {"ok": False, "reason": "Missing buyer info — call set_buyer_info first."}
            )
            return
        quote = BIOBANK.quote_total(order["specimen_ids"])
        confirmation = f"RMO-{random.randint(100000, 999999)}"
        artifact = {
            "confirmation_number": confirmation,
            "biobank": BIOBANK_ID,
            "timestamp": int(time.time()),
            "buyer": order["buyer"],
            "items": quote["line_items"],
            "subtotal_usd": quote["subtotal_usd"],
            "submit_to": "hello@referencemedicine.com",
        }
        path = ORDERS_DIR / f"{confirmation}.json"
        path.write_text(json.dumps(artifact, indent=2))
        logger.info(f"Order placed: {confirmation} subtotal=${quote['subtotal_usd']} → {path}")
        await params.result_callback(
            {
                "ok": True,
                "confirmation_number": confirmation,
                "subtotal_usd": quote["subtotal_usd"],
                "artifact_path": str(path),
                "next_step": (
                    "Sourcing team will email a formal order acknowledgment within one "
                    "business day. Shipping is arranged separately after acknowledgment."
                ),
            }
        )

    async def show_page(
        params: FunctionCallParams,
        page_id: str,
        highlight: str | None = None,
    ) -> None:
        """Navigate the visible demo browser to one of the biobank's website
        pages so the caller can be shown a reference page while you talk.

        This is a UI affordance for the in-room demo — it does NOT replace
        spoken explanations. Use it when the caller asks about a topic that
        maps to a named page (e.g. "how does your collection process work?"
        → page_id="process"). The browser navigation is parallel to your
        spoken reply.

        Args:
            page_id: One of the known page IDs from the system prompt
                (e.g. "process", "case_studies", "inventory", "about").
                Unknown IDs return ok=False without navigating.
            highlight: Optional substring to scroll to + highlight on the
                page. Useful for narrative pages where you want to call
                attention to a specific phrase.
        """
        path = SITE["pages"].get(page_id)
        if not path:
            await params.result_callback(
                {
                    "ok": False,
                    "reason": f"Unknown page_id '{page_id}'. Known: "
                    f"{sorted(SITE['pages'])}",
                }
            )
            return
        BROWSER.show(path, highlight=highlight)
        await params.result_callback(
            {"ok": True, "showing": page_id, "highlight": highlight}
        )

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call AFTER saying goodbye in the same turn."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        search_cases,
        get_case_details,
        get_specimens_for_case,
        add_to_order,
        get_order_summary,
        set_buyer_info,
        submit_order,
        show_page,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    system_instruction = _build_system_instruction()

    # STT — NVIDIA Parakeet streaming
    stt = NVidiaWebSocketSTTService(
        url=os.getenv("NVIDIA_ASR_URL", "ws://192.168.7.228:8081"),
        strip_interim_prefix=True,
    )

    # LLM — Nemotron-3-Super-120B via vLLM. See bot-nemotron.py and
    # server/nemotron_llm.py for context on the TTFB-fix subclass and the
    # enable_thinking knob. Default OFF for low-latency voice.
    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("NEMOTRON_LLM_URL", "http://192.168.7.228:8000/v1"),
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
        ),
    )

    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        context.add_message(
            {
                "role": "user",
                "content": (
                    "A researcher just called Reference Medicine's concierge line. "
                    "Greet them: 'Reference Medicine concierge — what are you looking "
                    "to source today?' Keep it tight."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    from_number: str | None = None
    transport_overrides: dict = {}

    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter
        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case WebSocketRunnerArguments():
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000
            _, call_data = await parse_telephony_websocket(runner_args.websocket)
            call_info = await get_call_info(call_data["call_id"])
            if call_info:
                from_number = call_info.get("from_number")
                logger.info(f"Call from: {from_number} to: {call_info.get('to_number')}")
            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_data["call_id"],
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
                auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )
            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
        case _ if _PCC_AVAILABLE and isinstance(runner_args, DailySessionArguments):
            # Pipecat Cloud delivers calls via Daily rooms. The pcc runner
            # injects room_url + token; we just plug them into DailyTransport.
            transport = DailyTransport(
                runner_args.room_url,
                runner_args.token,
                "Biobank Concierge",
                DailyParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport, from_number=from_number, **transport_overrides)


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
