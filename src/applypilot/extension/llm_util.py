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
    2. Otherwise, if Chrome's on-device model is live (an extension page
       reported it ready within the last 60 seconds via the bridge), a
       ``BridgeClient``.
    3. Otherwise, if the Claude Code CLI is installed
       (``config.find_claude_binary()``), a ``ClaudeCodeClient`` built
       directly -- no env var required.
    4. Otherwise, re-raise the original "no provider configured" error, so
       callers' existing fail-soft handling (try/except -> "") is
       unaffected.
    """
    from applypilot import llm as llm_mod

    try:
        return llm_mod.get_client()
    except RuntimeError:
        # Chrome's on-device model, answered by an open extension page. Local
        # and free, so it goes before the metered Claude CLI.
        from applypilot.extension import llm_bridge

        if llm_bridge.BRIDGE.live():
            return llm_bridge.BridgeClient()

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
    on-device-bridge and Claude-CLI fallbacks ``get_llm_client()`` applies,
    in the same order (bridge before Claude CLI).
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

    from applypilot.extension import llm_bridge

    if llm_bridge.BRIDGE.live():
        return True, "chrome-on-device"

    if config.find_claude_binary() is not None:
        return True, "claude-cli"

    return False, ""


# ---------------------------------------------------------------------------
# Where the text goes. The extension used to say "nothing leaves your
# machine" while its AI features could use a cloud model (Gemini/OpenAI
# keys, the Claude CLI, or a non-localhost LLM_URL). A local model is used
# freely; anything else needs the applicant's explicit opt-in.
# ---------------------------------------------------------------------------

_PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "openai": "OpenAI",
    "claude-cli": "Anthropic Claude (via the Claude CLI)",
    "remote-endpoint": "a remote model endpoint",
    "local": "a model on this computer",
    "chrome-on-device": "Chrome's built-in AI on this computer",
}


def _is_localhost(url: str) -> bool:
    import urllib.parse

    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def provider_info() -> dict:
    """{"available", "provider", "label", "local"} — never raises, never spawns."""
    ok, provider = llm_available()
    if provider == "local" and not _is_localhost(os.environ.get("LLM_URL", "")):
        provider = "remote-endpoint"
    return {"available": ok, "provider": provider, "label": _PROVIDER_LABELS.get(provider, provider),
            "local": provider in ("local", "chrome-on-device")}


class CloudBlocked(RuntimeError):
    """An AI feature would send text to a non-local model without opt-in."""


# The directory whose extension_settings.json holds the opt-in. The service
# sets it at startup (server.create_app); None reads defaults + env only.
SETTINGS_DIR = None


def cloud_block_reason(app_dir=None) -> str | None:
    """Why an AI feature must not run right now, or None. Only a model that
    is available, NOT on this computer, and not explicitly allowed blocks."""
    info = provider_info()
    if not info["available"] or info["local"]:
        return None
    from applypilot.extension import settings as ext_settings

    if ext_settings.effective_settings(app_dir if app_dir is not None else SETTINGS_DIR).get("cloud_llm_allowed"):
        return None
    return (f"AI features would send text off this computer to {info['label']} — "
            "allow that in Settings → Smart fill, or use a local model")
