"""Tier 2 patch prompt builder.

A skill's Tier 1 replay can fill the deterministic 80% of a form. When the
skill marks fields as `unresolved` (free-text custom questions the recorder
couldn't trace to a profile value), Tier 2 spawns a SHORT-SCOPED Claude Code
session whose ONLY job is to fill those specific fields and return — NOT
submit, NOT navigate, NOT re-fill anything Tier 1 already handled.

Compared to the full `prompt.py`, this prompt is:
- ~10× smaller (no STEP-BY-STEP, no salary section, no SSO maze, no CAPTCHA)
- Single-purpose: "here are N selectors, fill them, exit"
- Doesn't emit RESULT:APPLIED — emits RESULT:PATCHED so the launcher knows
  to fall back to Tier 1's submit_only() to finish the apply

Why "scoped" matters for reliability: the model can't get confused about
where it is in a multi-step apply, can't accidentally double-submit, can't
hallucinate that an unresolved-but-OPTIONAL field is required. It only sees
what it needs to.
"""
from __future__ import annotations

from typing import Iterable

from applypilot.apply.skill_schema import UnresolvedField


def build_patch_prompt(
    *,
    job: dict,
    apply_url: str,
    unresolved: Iterable[UnresolvedField],
    profile: dict,
    tailored_resume: str = "",
) -> str:
    """Construct the Tier 2 patch prompt.

    Args:
        job: the job dict (used for {title}, {site}, {description} context only).
        apply_url: the URL of the (already-loaded) application page.
        unresolved: the UnresolvedField list from the skill, in the order they
            should be filled. ORDER MATTERS — if there's a multi-step form,
            the launcher should have run Tier 1 up to the current step first.
        profile: the applicant profile dict — included as context so the model
            can ANSWER free-text questions in the applicant's voice.
        tailored_resume: optional plain-text resume content for context.

    Returns: a single string ready to feed as the Claude Code prompt body.
    """
    unresolved_list = list(unresolved)
    if not unresolved_list:
        # Caller should never call this with zero unresolved — but be defensive.
        raise ValueError("build_patch_prompt requires at least one unresolved field")

    fields_block = _format_fields(unresolved_list)
    profile_summary = _format_profile_summary(profile)
    resume_block = _format_resume(tailored_resume)

    return _TEMPLATE.format(
        title=job.get("title", "the role"),
        site=job.get("site", ""),
        apply_url=apply_url,
        n=len(unresolved_list),
        fields_block=fields_block,
        profile_summary=profile_summary,
        resume_block=resume_block,
    )


_TEMPLATE = """You are a SCOPED form-patch agent. The application form for "{title}" @ {site} is already loaded at {apply_url}. A deterministic helper has filled MOST fields. Your ONE job is to fill exactly {n} remaining field(s) and stop — do NOT submit, do NOT navigate, do NOT touch any other field on the page.

== HARD RULES ==
1. Do NOT call browser_navigate. The page is already loaded. Navigating wipes the deterministic fills.
2. Do NOT click Submit / Apply / Continue. The launcher will submit after you exit.
3. Do NOT re-fill any field other than the ones listed below. They are already correct.
4. If a listed field is already filled with the SAME content you would have written, leave it. Idempotency wins.
5. Your FIRST action must be browser_snapshot to confirm the form is in the expected state. If you don't see the listed selectors, output RESULT:FAILED:patch_form_drift and stop.

== APPLICANT PROFILE (use for answers, do not modify) ==
{profile_summary}

{resume_block}

== FIELDS TO PATCH ==
There are {n} unresolved field(s). Fill each one with a brief, honest, applicant-voiced answer derived from the profile and resume above:

{fields_block}

== STEP-BY-STEP ==
1. browser_snapshot — confirm the form is loaded and the selectors above are present. If any selector is missing → RESULT:FAILED:patch_form_drift, stop.
2. For each field in order: browser_type / browser_fill_form / browser_select_option as appropriate. For long_text fields, write 2-4 sentences max. Do not exceed any visible character limit.
3. After all fields are filled, browser_snapshot one more time to verify your fills stuck.
4. Output exactly one line: RESULT:PATCHED — then stop. The launcher will handle the submit.

== RESULT CODES ==
RESULT:PATCHED -- all listed fields filled, no submit attempted (launcher will submit)
RESULT:FAILED:patch_form_drift -- one or more listed selectors are missing from the page
RESULT:FAILED:patch_uninterpretable -- a listed field has a constraint (dropdown / file / etc.) you cannot fulfill from the profile

Be concise. The shorter and more direct your turns, the better. You are PATCH ONLY.
"""


def _format_fields(unresolved: list[UnresolvedField]) -> str:
    """Render the unresolved field list in a model-friendly numbered block."""
    lines = []
    for i, u in enumerate(unresolved, 1):
        label = (u.label or "").strip() or "(no label)"
        type_hint = u.type or "text"
        hint = (u.hint or "").strip()
        line = f"  {i}. selector=`{u.selector}`  type={type_hint}  label=\"{label}\""
        if hint:
            # Hint is the original LLM-written value from the recording — gives
            # the patcher a clear template of expected length/tone
            line += f"\n     prior-answer-hint: \"{hint[:200]}\""
        lines.append(line)
    return "\n".join(lines)


def _format_profile_summary(profile: dict) -> str:
    """Compact applicant summary — just what's useful for answering open-ended Q's.

    Intentionally smaller than the full prompt.py profile dump — Tier 2 doesn't
    need legal name, salary, EEO answers, etc. Just enough to answer "tell us
    about a project you're proud of" or "why this role".
    """
    p = profile or {}
    personal = p.get("personal") or {}
    exp = p.get("experience") or {}
    skills_b = p.get("skills_boundary") or {}
    facts = p.get("resume_facts") or {}

    lines = []
    if personal.get("full_name"):
        lines.append(f"Name: {personal['full_name']}")
    if exp.get("current_title"):
        lines.append(f"Role: {exp['current_title']}")
    if exp.get("years_of_experience_total"):
        lines.append(f"Experience: {exp['years_of_experience_total']} years")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")
    if personal.get("city"):
        lines.append(f"Based in: {personal['city']}")

    companies = facts.get("preserved_companies") or []
    if companies:
        lines.append("Past companies: " + ", ".join(companies))

    tools = skills_b.get("tools") or []
    if tools:
        lines.append("Tools: " + ", ".join(tools[:12]))

    return "\n".join(lines) or "(profile unavailable)"


def _format_resume(resume_text: str) -> str:
    if not resume_text:
        return ""
    # Cap resume snippet for token-efficiency; Tier 2 is meant to be lean.
    snippet = resume_text.strip()[:1500]
    return f"== TAILORED RESUME (excerpt) ==\n{snippet}\n"
