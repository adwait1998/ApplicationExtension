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

_CLAUDE_FALLBACK_MODEL = "sonnet"


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

    Read-only: never raises, never spawns a subprocess or opens a network
    connection (``find_claude_binary()`` only does a PATH lookup plus a
    filesystem glob). Mirrors ``applypilot.llm._detect_provider()``'s own
    precedence order, then adds the same Claude-CLI fallback
    ``get_llm_client()`` applies, so /health never claims a draft will
    work when it would actually fail soft to an empty box.
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
        return True, "local"

    if config.find_claude_binary() is not None:
        return True, "claude-cli"

    return False, ""
