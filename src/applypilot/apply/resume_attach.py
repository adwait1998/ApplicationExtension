"""Shared Greenhouse resume-attachment upload routine.

The ONE implementation of "put the resume PDF onto the form's file input" used
by both the deterministic prefill (`apply/prefill.py`) and the LLM stream
executor's `reattach_resume` recovery action (`apply/stream_executor.py`).

Why this module exists: on a long LLM-driven session Greenhouse can silently
drop the resume the prefill uploaded (checkbox JS-eval batches, react-select
re-renders, form re-mounts). The server then rejects the submit for a missing
attachment (live attempt #5, 2026-07-24). The recovery path must re-run the
SAME upload the prefill did, so both share this helper rather than duplicating.

Leaf module: it imports nothing from prefill/stream_executor/browser_stream so
those callers can all depend on it without an import cycle. It operates on a
Playwright page/frame `root`; all failures degrade to a safe return value.

Invariant (inherited from prefill): `set_input_files` fires input+change on the
element and Greenhouse re-renders the form afterwards, which can DETACH the
original input node. Callers MUST NOT touch the returned locator afterwards —
re-observe the page for read-back instead.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Greenhouse's resume file input (stable id), plus a name-scoped fallback. A
# bare input[type=file] is intentionally NOT included: forms with cover-letter
# or portfolio uploads have multiple file inputs, and an unguarded fallback
# drops the resume into the wrong field and silently reports success.
GH_RESUME_PRIMARY = "input[type='file']#resume"
GH_RESUME_FALLBACK = "input[type='file'][name*='resume' i]"
RESUME_FILE_SELECTORS = (GH_RESUME_PRIMARY, GH_RESUME_FALLBACK)


def find_resume_file_input(root):
    """Return ``(locator, selector)`` for the form's resume file input.

    Matches hidden inputs too (Greenhouse's real ``input[type=file]#resume`` is
    often visually replaced by an "Attach" widget) — ``locator.count()`` does
    not require visibility. Returns ``(None, None)`` when no resume input is
    present.
    """
    for sel in RESUME_FILE_SELECTORS:
        try:
            loc = root.locator(sel).first
            if loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    return None, None


def upload_resume_to_form(root, resume_pdf_path: str, *, timeout_ms: int = 2500) -> str | None:
    """Set the resume PDF on the form's resume file input.

    Returns the selector that accepted the file on success, or ``None`` when no
    resume input matched or the upload raised. Does not read back — the input
    may be detached by a React re-render immediately after; the caller must
    re-observe the page to confirm the attachment registered.
    """
    if not resume_pdf_path:
        return None
    loc, sel = find_resume_file_input(root)
    if loc is None:
        return None
    try:
        loc.set_input_files(resume_pdf_path, timeout=timeout_ms)
        return sel
    except Exception as e:
        logger.debug("resume_attach: upload via %s failed: %s", sel, e)
        return None
