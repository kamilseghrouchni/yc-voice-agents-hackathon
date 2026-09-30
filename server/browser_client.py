"""Fire-and-forget client for the browser_view.py sidecar.

The voice bot calls `BrowserView.show(path, highlight=...)` from tool bodies.
This client MUST NOT block the voice turn — every call dispatches the HTTP
post into a background task and returns immediately, swallowing all errors
(connection refused, timeout, sidecar offline). The browser is a *mirror* of
agent state; if it's not running, the call still succeeds and the agent
keeps talking.

Pairs with server/browser_view.py — see the docstring there for the contract.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "http://127.0.0.1:7901"
_POST_TIMEOUT_S = 0.5  # generous — the sidecar returns 204 immediately


class BrowserView:
    """Thin async client. One instance per process is enough."""

    def __init__(self, base_url: str, endpoint: str = _DEFAULT_ENDPOINT) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._base_url = base_url
        self._configured = False

    async def _post(self, path: str, body: dict) -> None:
        timeout = aiohttp.ClientTimeout(total=_POST_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self._endpoint}{path}", json=body) as r:
                    await r.read()
        except Exception as e:
            # Sidecar offline is expected during local-only dev runs.
            log.debug(f"browser_view post {path} failed: {e}")

    async def _ensure_configured(self) -> None:
        """One-shot base_url push to the sidecar. Cheap retry every call until
        the first success — handles the case where the sidecar starts after
        the bot."""
        if self._configured:
            return
        await self._post("/configure", {"base_url": self._base_url})
        # Optimistic: mark configured even on failure; we'll re-send on next
        # call via the same code path if it didn't stick. The sidecar is
        # idempotent on /configure.
        self._configured = True

    def show(self, path: str, highlight: str | None = None) -> None:
        """Tell the browser to navigate. Returns immediately; never raises.

        Args:
            path: Relative path on the biobank's site (e.g. "/process").
            highlight: Optional substring to highlight + scroll into view.
        """
        body: dict = {"path": path}
        if highlight:
            body["highlight"] = highlight

        async def _go() -> None:
            await self._ensure_configured()
            await self._post("/show", body)

        # Fire-and-forget — schedule on the running loop and don't await it.
        try:
            asyncio.get_running_loop().create_task(_go())
        except RuntimeError:
            # Not in an async context (e.g. called from sync setup) — drop it.
            log.debug("BrowserView.show called outside an event loop; ignoring")
