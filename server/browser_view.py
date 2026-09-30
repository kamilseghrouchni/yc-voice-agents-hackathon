"""Standalone browser-view sidecar — visualizes what the agent is doing.

The voice agent (bot-biobank.py) posts navigation intents to this process via
HTTP. This process owns a long-lived, headed Chromium that the demo audience
watches on a second monitor while the caller talks to the agent.

Architecture (deliberately decoupled from the bot):

    bot-biobank.py  ──HTTP POST /show──►  browser_view.py  ──►  Chromium (headed)
       (tool body or                       (this file)            referencemedicine.com
        FrameProcessor)                                            (or any biobank site)

If this process is down, the bot keeps working (browser_client.py swallows all
errors). The bot must NEVER block waiting for the browser — every nav is
fire-and-forget on the bot side and best-effort on this side.

Run::

    uv run server/browser_view.py
    # then in another terminal:
    BIOBANK_ID=reference_medicine uv run server/bot-biobank.py

The first run downloads Chromium (~300 MB)::

    uv run playwright install chromium

Endpoint::

    POST http://localhost:7901/show
    body: {"path": "/process", "highlight": "fresh-frozen kidney"}
        - path: relative path on the biobank's site (joined to base_url)
        - highlight: optional substring to find + highlight + scroll into view

    POST http://localhost:7901/configure
    body: {"base_url": "https://www.referencemedicine.com"}
        - sets the base URL the browser navigates against; the bot posts this
          once at startup so the sidecar doesn't have to be restarted when you
          swap biobanks.

Design choices:
  - stdlib HTTP server (no FastAPI dep): one endpoint, fire-and-forget calls.
  - One Playwright Browser + Page kept alive for the process lifetime. Nav
    requests serialize through an asyncio.Queue so concurrent posts don't race.
  - Highlight uses a TreeWalker over text nodes + a yellow background span;
    survives until the next navigation. Best-effort.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

logging.basicConfig(level=logging.INFO, format="[browser_view] %(message)s")
log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 7901

# Injected JS — find the first text node matching `needle` (case-insensitive),
# wrap it in a highlighted <span>, scroll it into view smoothly. Best-effort:
# fails silently if the text isn't present.
_HIGHLIGHT_JS = r"""
(needle) => {
  // strip old highlights so we don't accumulate stale state across navs
  document.querySelectorAll('span[data-agent-highlight="1"]').forEach((el) => {
    const parent = el.parentNode;
    while (el.firstChild) parent.insertBefore(el.firstChild, el);
    parent.removeChild(el);
  });
  if (!needle) return false;
  const re = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.parentElement && node.parentElement.tagName === 'SCRIPT') continue;
    if (re.test(node.nodeValue)) {
      const span = document.createElement('span');
      span.setAttribute('data-agent-highlight', '1');
      span.style.cssText = 'background: #fff59d; outline: 2px solid #f57f17; border-radius: 2px; padding: 0 2px;';
      node.parentNode.insertBefore(span, node);
      span.appendChild(node);
      span.scrollIntoView({behavior: 'smooth', block: 'center'});
      return true;
    }
  }
  return false;
}
"""


@dataclass
class _NavCommand:
    path: str
    highlight: str | None = None


class BrowserView:
    """Owns the long-lived Playwright browser + serializes nav commands."""

    def __init__(self) -> None:
        self._base_url: str | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._queue: asyncio.Queue[_NavCommand] = asyncio.Queue()
        self._playwright = None

    def set_base_url(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        log.info(f"base_url set to {self._base_url}")

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=[
                "--window-size=1280,900",
                "--window-position=100,80",
            ],
        )
        await self._ensure_page()
        # Immediately bring the window forward and load a recognizable page so
        # the user knows where to look. about:blank is invisible on macOS and
        # easy to lose under other windows.
        try:
            await self._page.goto(
                "data:text/html,"
                "<html><head><title>Biobank Agent — visual companion</title></head>"
                "<body style='font-family:-apple-system,system-ui,sans-serif;"
                "background:#0f172a;color:#e2e8f0;display:grid;place-items:center;"
                "height:100vh;margin:0;text-align:center'>"
                "<div><h1 style='font-weight:300;font-size:2rem;margin:0 0 .5rem'>"
                "Biobank Agent — visual companion</h1>"
                "<p style='opacity:.7;margin:0'>This window mirrors what the agent "
                "is doing. Start talking to the bot at "
                "<code>localhost:7860</code>.</p></div></body></html>"
            )
            await self._page.bring_to_front()
            log.info("Chromium launched (headed) and brought to front.")
        except Exception as e:
            log.warning(f"initial bring-to-front failed: {e}")

    async def _ensure_page(self) -> None:
        """Make sure we have a usable Page. Recreate if user closed the window."""
        if self._page is not None and not self._page.is_closed():
            return
        assert self._browser is not None
        # Reuse existing context if any context is still open; else make a new one.
        contexts = self._browser.contexts
        ctx = contexts[0] if contexts else await self._browser.new_context(
            viewport={"width": 1280, "height": 900}
        )
        self._page = await ctx.new_page()
        log.info("(re)created Page")

    async def enqueue(self, cmd: _NavCommand) -> None:
        await self._queue.put(cmd)

    async def consume(self) -> None:
        """Drain the queue forever. Errors per-command never crash the loop."""
        while True:
            cmd = await self._queue.get()
            try:
                if self._base_url is None:
                    log.warning("nav requested before base_url set — dropping")
                    continue
                await self._ensure_page()
                assert self._page is not None
                url = f"{self._base_url}{cmd.path}"
                log.info(f"goto {url}  highlight={cmd.highlight!r}")
                await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
                try:
                    await self._page.bring_to_front()
                except Exception:
                    pass
                if cmd.highlight:
                    # Give the page a beat to render before we hunt for text.
                    await asyncio.sleep(0.4)
                    found = await self._page.evaluate(_HIGHLIGHT_JS, cmd.highlight)
                    log.info(f"  highlight {'found' if found else 'NOT FOUND'}")
            except Exception as e:
                log.warning(f"nav failed: {e}")
                # Force a fresh page on next nav.
                self._page = None


# Module-level singleton + asyncio loop reference set in main().
_VIEW: BrowserView | None = None
_LOOP: asyncio.AbstractEventLoop | None = None


class _Handler(BaseHTTPRequestHandler):
    """Tiny stdlib HTTP handler — accepts /show and /configure, returns 204."""

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _ok(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        assert _VIEW is not None and _LOOP is not None
        body = self._read_json()
        if self.path == "/show":
            cmd = _NavCommand(
                path=str(body.get("path") or "/"),
                highlight=body.get("highlight"),
            )
            asyncio.run_coroutine_threadsafe(_VIEW.enqueue(cmd), _LOOP)
            self._ok()
        elif self.path == "/configure":
            base = body.get("base_url")
            if isinstance(base, str) and base:
                _VIEW.set_base_url(base)
                self._ok()
            else:
                self._ok(400)
        else:
            self._ok(404)

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        """Friendly response so visiting the sidecar URL in a browser doesn't
        return a confusing 501."""
        msg = (
            "browser_view sidecar OK.\n"
            "This endpoint accepts POST /show and POST /configure - "
            "see server/browser_view.py for the contract.\n"
            "Look for the headed Chromium window the agent drives.\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        log.debug(f"HTTP {self.client_address[0]} - {fmt % args}")


def _serve_http_in_thread() -> None:
    """Run the HTTP listener on a background thread; it talks to the asyncio
    loop via run_coroutine_threadsafe."""
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    log.info(f"HTTP listening on http://{HOST}:{PORT}  (POST /show, /configure)")
    server.serve_forever()


async def _main() -> None:
    global _VIEW, _LOOP
    _VIEW = BrowserView()
    _LOOP = asyncio.get_running_loop()
    await _VIEW.start()

    # HTTP listener runs in a worker thread so the asyncio loop is free for
    # Playwright. Posts cross threads via run_coroutine_threadsafe.
    import threading

    threading.Thread(target=_serve_http_in_thread, daemon=True).start()
    await _VIEW.consume()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("bye")
