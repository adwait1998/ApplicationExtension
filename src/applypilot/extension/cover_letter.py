"""Cover-letter drafts for the page being filled.

A DRAFT, like every generated text in this extension: returned for the
applicant to review, never attached or typed without them seeing it, and
refused outright if it claims experience somewhere the applicant never
worked (the same grounding check tier 6 drafts get). The prompt and the
validator are the autonomous pipeline's own (scoring/cover_letter.py,
scoring/validator.py), so both produce letters to the same standard.
"""
from __future__ import annotations

from typing import Callable

from applypilot.extension import grounding
from applypilot.scoring.cover_letter import _build_cover_letter_prompt, _strip_preamble
from applypilot.scoring.validator import sanitize_text, validate_cover_letter

MAX_ATTEMPTS = 3


class CoverLetterError(Exception):
    """User-facing, safe to display."""


def _resume_text(profile: dict, resume_text: str) -> str:
    if resume_text and resume_text.strip():
        return resume_text
    # No stored résumé text: build a minimal one from the profile's own facts.
    lines = []
    for w in (profile or {}).get("work_history") or []:
        lines.append(f"{w.get('title', '')} at {w.get('company', '')} ({w.get('start', '')} - "
                     f"{w.get('end', '') or 'present'})\n{w.get('description', '')}")
    for e in (profile or {}).get("education") or []:
        lines.append(f"{e.get('degree', '')} {e.get('field', '')}, {e.get('school', '')}")
    return "\n\n".join(lines)


def draft_cover_letter(profile: dict, job: dict, resume_text: str,
                       chat: Callable[[list[dict]], str]) -> dict:
    """{"text", "warnings"} or raises CoverLetterError. ``chat`` takes the
    messages list and returns the model's reply (injected so tests never
    touch a real model)."""
    if not job or not (job.get("description") or "").strip():
        raise CoverLetterError("couldn't find this job's description to write a letter against")
    resume = _resume_text(profile, resume_text)
    if not resume.strip():
        raise CoverLetterError("no résumé or work history in your profile to write from")

    job_text = (f"TITLE: {job.get('title', '')}\nCOMPANY: {job.get('company', '')}\n\n"
                f"DESCRIPTION:\n{job['description'][:6000]}")
    base_prompt = _build_cover_letter_prompt(profile)
    avoid: list[str] = []
    letter = ""
    warnings: list[str] = []
    for _ in range(MAX_ATTEMPTS):
        prompt = base_prompt + ("\n\n## AVOID THESE ISSUES:\n" + "\n".join(f"- {n}" for n in avoid[-5:])
                                if avoid else "")
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": f"RESUME:\n{resume}\n\n---\n\nTARGET JOB:\n{job_text}\n\n"
                                                "Write the cover letter:"}]
        letter = _strip_preamble(sanitize_text(chat(messages) or "")).strip()
        if not letter:
            avoid.append("Empty response.")
            continue
        verdict = validate_cover_letter(letter, mode="normal")
        warnings = list(verdict.get("warnings") or [])
        if verdict.get("passed"):
            break
        avoid.extend(verdict.get("errors") or [])
    if not letter:
        raise CoverLetterError("the model returned nothing — try again")

    unsupported = grounding.find_unsupported_claims(letter, profile, target_company=job.get("company", ""))
    if unsupported:
        raise CoverLetterError("draft refused — it claimed experience at " + ", ".join(unsupported)
                               + ", which is not in your history")
    return {"text": letter, "warnings": warnings}
