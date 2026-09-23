"""LLM client resolution for the extension's own LLM-touching call sites:
tier 6 drafts (``answers._real_llm_fn``) and the résumé-import LLM pass
(``resume_import._default_llm_fn``).

``applypilot.llm.get_client()`` is the autonomous pipeline's own provider
detection (``GEMINI_API_KEY`` / ``OPENAI_API_KEY`` / ``LLM_URL`` /
``LLM_PROVIDER=claude``) and this module never modifies it -- both
``_detect_provider()`` and the module-level client singleton in
``applypilot.llm`` are untouched. This module exists because an operator
who has the Claude Code CLI installed (for Claude Code itself) but has
never set any of those env vars still has a perfectly usable LLM sitting
right there -- requiring them to export an env var to unlock a feature
they already explicitly turned on (drafts, or a résumé import) is exactly
the friction this module removes. Scoped to these two extension call
sites only; ``applypilot.apply.launcher`` and everything else that calls
``applypilot.llm.get_client()`` directly is unaffected.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request

_CLAUDE_FALLBACK_MODEL = "sonnet"

# A local endpoint (Ollama etc.) being CONFIGURED is not the same as it being
# UP. Reporting "available" from config alone made /health tell the Settings
# page drafts would work while every draft silently failed soft to an empty
# box — the one thing this function exists to prevent. The probe is localhost
# only, short, and cached so /health polling stays cheap.
_LOCAL_PROBE_TIMEOUT_S = 1.5
_LOCAL_PROBE_TTL_S = 30.0
_probe_lock = threading.Lock()
_probe_cache: dict[str, tuple[float, bool]] = {}


def _local_endpoint_up(base_url: str) -> bool:
    now = time.monotonic()
    with _probe_lock:
        hit = _probe_cache.get(base_url)
        if hit and now - hit[0] < _LOCAL_PROBE_TTL_S:
            return hit[1]
    ok = False
    try:
        # OpenAI-compatible servers (Ollama, llama.cpp, LM Studio) expose
        # /models; any HTTP answer at all means something is listening.
        with urllib.request.urlopen(base_url.rstrip("/") + "/models",
                                    timeout=_LOCAL_PROBE_TIMEOUT_S):
            ok = True
    except urllib.error.HTTPError:
        ok = True
    except Exception:            # noqa: BLE001 — refused / timeout / DNS
        ok = False
    with _probe_lock:
        _probe_cache[base_url] = (now, ok)
    return ok


def get_llm_client():
    """Return an LLM client for the extension's draft/résumé-import paths.

    1. Whatever ``applypilot.llm.get_client()`` already resolves to -- an
       operator's explicit ``GEMINI_API_KEY``/``OPENAI_API_KEY``/
       ``LLM_URL``/``LLM_PROVIDER=claude`` always wins, unchanged.
    2. Otherwise, if the Claude Code CLI is installed
       (``config.find_claude_binary()``), a ``ClaudeCodeClient`` built
       directly -- no env var required.
    3. Otherwise, re-raise the original "no provider configured" error, so
       callers' existing fail-soft handling (try/except -> "") is
       unaffected.
    """
    from applypilot import llm as llm_mod

    try:
        return llm_mod.get_client()
    except RuntimeError:
        from applypilot.config import find_claude_binary

        claude_bin = find_claude_binary()
        if not claude_bin:
            raise
        return llm_mod.ClaudeCodeClient(model=_CLAUDE_FALLBACK_MODEL)


def llm_available() -> tuple[bool, str]:
    """``(available, provider_label)`` for GET /health.

    Never raises and never spawns a subprocess. For a LOCAL endpoint
    (``LLM_URL``) it makes one short, cached HTTP probe to localhost, because
    a configured-but-stopped local model would otherwise be reported as
    available while every draft failed soft to an empty box. Remote API-key
    providers are judged on configuration alone (probing them would cost
    latency and quota on every /health poll). Mirrors
    ``applypilot.llm._detect_provider()``'s precedence, then adds the same
    Claude-CLI fallback ``get_llm_client()`` applies.
    """
    from applypilot import config

    provider_override = (
        os.environ.get("APPLYPILOT_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER") or ""
    ).strip().lower()
    if provider_override in {"claude", "claude-code", "claude_code"}:
        return (config.find_claude_binary() is not None), "claude-cli"

    if os.environ.get("GEMINI_API_KEY") and not os.environ.get("LLM_URL"):
        return True, "gemini"
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("LLM_URL"):
        return True, "openai"
    if os.environ.get("LLM_URL"):
        # Configured is not the same as running — see _local_endpoint_up.
        # Deliberately NOT falling back to the Claude CLI here: silently
        # switching a user from their free local model to a metered one is
        # a surprise they didn't opt into. Report it honestly instead.
        return _local_endpoint_up(os.environ["LLM_URL"]), "local"

    if config.find_claude_binary() is not None:
        return True, "claude-cli"

    return False, ""
