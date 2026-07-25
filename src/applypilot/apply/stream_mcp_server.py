"""MCP server exposing ApplyPilot's structured browser stream to Claude.

Run by the launcher as a per-worker stdio MCP server:

    python -m applypilot.apply.stream_mcp_server --cdp-port 9222
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from applypilot.apply.browser_stream import (
    BrowserObservation,
    BrowserStateStream,
    _observation_signature,
    collect_browser_observation,
    observe_cdp,
)
from applypilot.apply.stream_executor import (
    execute_stream_actions_on_page,
    observation_for_claude,
)


def build_server(cdp_port: int, dry_run: bool = False,
                 broker_file: str | None = None, job_identity: str | None = None):
    """Build the FastMCP server. Import mcp lazily for testability.

    When `dry_run` is True, final submits are refused server-side no matter
    what `allow_submit` value the model passes (see
    stream_executor.effective_allow_submit — the prompt-only guard failed
    live on 2026-06-12 and submitted a real application during a dry run).

    `broker_file`/`job_identity` are a SECONDARY gate for this separate-process
    MCP path: the executor refuses final submits unless a broker ticket is open
    for `job_identity`. The PRIMARY always-on containment is the CDP network
    route installed by BrowserStateStream in the launcher process (which owns
    the live broker); this gate defends the raw @playwright/mcp escape path.
    """
    broker = None
    if broker_file:
        try:
            from applypilot.apply.submit_broker import SubmitBroker
            broker = SubmitBroker(broker_file, dry_run=dry_run)
        except Exception:
            broker = None
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "The applypilot_stream MCP server requires the Python 'mcp' package. "
            "Install project dependencies after pyproject.toml is updated."
        ) from e

    server = FastMCP("applypilot_stream")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"applypilot-stream-mcp-{cdp_port}")
    stream = BrowserStateStream(cdp_port, poll_interval_s=0.35, debounce_s=0.1).start()
    session = _PersistentCDPSession(cdp_port, broker=broker, identity_id=job_identity)
    atexit.register(session.close)
    atexit.register(stream.close)

    async def run_blocking(fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, lambda: fn(*args))

    @server.tool()
    async def stream_latest(timeout_ms: int = 3000) -> dict[str, Any]:
        """Return the latest structured browser state from the active tab.

        Use this before browser_snapshot. It includes control_id handles that
        can be passed to stream_execute.
        """
        obs = await run_blocking(_latest_or_observe, stream, session, cdp_port, timeout_ms)
        return observation_for_claude(obs)

    @server.tool()
    async def stream_wait_for_change(
        previous_signature: str | None = None,
        timeout_ms: int = 3000,
        poll_ms: int = 200,
    ) -> dict[str, Any]:
        """Wait briefly for URL/form/validation state to change."""
        return await run_blocking(
            _wait_for_change_from_stream,
            stream,
            previous_signature,
            timeout_ms,
            poll_ms,
        )

    @server.tool()
    async def stream_execute(
        actions: list[dict[str, Any]],
        allow_submit: bool = False,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        """Execute batched browser actions using stream control selectors.

        Actions look like:
        {"action":"fill","control_id":"f0:control:0:...","value":"Ada"}
        {"action":"select","label":"Country *","value":"United States"}
        {"action":"submit","control_id":"...","allow_submit":true}
        {"action":"reattach_resume","value":"/path/to/Resume.pdf"}

        reattach_resume re-uploads the resume PDF to the form's file input and
        confirms the attachment registered (returns ok, or reattach_no_file_input
        / reattach_readback_failed). Use it when stream_latest shows
        resume_present=false, or after a submit is refused with
        submit_refused_resume_missing.
        """
        from applypilot.apply.stream_executor import effective_allow_submit

        effective = effective_allow_submit(allow_submit, dry_run)
        result = await run_blocking(
            session.execute,
            actions,
            effective,
            timeout_ms,
        )
        if dry_run and allow_submit and isinstance(result, dict):
            result["dry_run_submit_blocked"] = (
                "DRY RUN: submit was refused server-side. The form is ready; "
                "report RESULT:APPLIED for the dry run WITHOUT submitting."
            )
        return result

    return server


def _latest_or_observe(
    stream: BrowserStateStream,
    session: "_PersistentCDPSession",
    cdp_port: int,
    timeout_ms: int,
) -> BrowserObservation:
    """Prefer the persistent stream cache; reconnect only when it is stale."""
    deadline = time.monotonic() + max(0.1, timeout_ms / 1000.0)
    while time.monotonic() <= deadline:
        obs = stream.latest()
        if obs is not None and not obs.error and (time.time() - obs.observed_at) < 2.0:
            return obs
        time.sleep(0.05)
    try:
        return session.observe(timeout_ms=timeout_ms)
    except Exception:
        return observe_cdp(cdp_port, timeout_ms=timeout_ms)


class _PersistentCDPSession:
    """Single-thread-owned Playwright/CDP connection for MCP tool calls."""

    def __init__(self, cdp_port: int, *, broker=None, identity_id: str | None = None) -> None:
        self.cdp_port = cdp_port
        self.broker = broker
        self.identity_id = identity_id
        self._pw = None
        self._browser = None

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        finally:
            self._browser = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._pw = None

    def _ensure(self, timeout_ms: int = 4000):
        if self._browser is not None:
            try:
                self._browser.contexts
                return self._browser
            except Exception:
                self.close()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}",
            timeout=timeout_ms,
        )
        return self._browser

    def active_page_and_tabs(self, timeout_ms: int = 4000):
        browser = self._ensure(timeout_ms=timeout_ms)
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        tabs = []
        for i, page in enumerate(pages):
            try:
                tabs.append({"index": i, "url": page.url, "title": page.title()})
            except Exception:
                tabs.append({"index": i, "url": "", "title": ""})
        return (pages[-1] if pages else None), tabs

    def observe(self, timeout_ms: int = 3000) -> BrowserObservation:
        page, tabs = self.active_page_and_tabs(timeout_ms=timeout_ms)
        if page is None:
            return BrowserObservation(observed_at=time.time(), error="no_page")
        return collect_browser_observation(page, tabs=tabs)

    def execute(
        self,
        actions: list[dict[str, Any]],
        allow_submit: bool = False,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            page, tabs = self.active_page_and_tabs(timeout_ms=timeout_ms)
            if page is None:
                return {
                    "ok": False,
                    "error": "no_page",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "results": [],
                    "observation": None,
                }
            return execute_stream_actions_on_page(
                page,
                actions,
                allow_submit=allow_submit,
                timeout_ms=timeout_ms,
                tabs=tabs,
                started=started,
                broker=self.broker,
                identity_id=self.identity_id,
            )
        except Exception as e:
            self.close()
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "results": [],
                "observation": None,
            }


def _wait_for_change_from_stream(
    stream: BrowserStateStream,
    previous_signature: str | None,
    timeout_ms: int,
    poll_ms: int,
) -> dict[str, Any]:
    """Wait on the cached stream instead of opening a new CDP connection."""
    started = time.monotonic()
    deadline = started + max(0.1, timeout_ms / 1000.0)
    baseline = previous_signature
    last_obs: BrowserObservation | None = None
    if baseline is None:
        initial = stream.latest()
        if initial is not None:
            baseline = _observation_signature(initial)
            last_obs = initial

    while time.monotonic() <= deadline:
        obs = stream.latest()
        if obs is not None:
            last_obs = obs
            sig = _observation_signature(obs)
            if baseline is None or sig != baseline:
                return {
                    "changed": True,
                    "signature": sig,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "observation": observation_for_claude(obs),
                }
        time.sleep(max(0.05, poll_ms / 1000.0))

    if last_obs is None:
        return {
            "changed": False,
            "error": "no_observation",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "changed": False,
        "signature": _observation_signature(last_obs),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "observation": observation_for_claude(last_obs),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Refuse final submits server-side regardless of allow_submit.")
    parser.add_argument("--broker-file", default=None,
                        help="Path to the submit-ticket file; gates final submits on an open broker ticket.")
    parser.add_argument("--job-identity", default=None,
                        help="identity_id this job's broker ticket is keyed by.")
    args = parser.parse_args(argv)
    build_server(args.cdp_port, dry_run=args.dry_run,
                 broker_file=args.broker_file, job_identity=args.job_identity).run()


if __name__ == "__main__":
    main()
