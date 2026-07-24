"""run_form_compiler (spec §6 pipeline): Parse -> Resolve -> oracle-batch ->
Fill -> Submit -> Verify -> Record. Returns run_job's exact tuple
(status, duration_ms, prefill_status) with prefill_status["tier_used"]=
'v2_greenhouse'.

FAILS OPEN (invariant 2), on a submit-safety boundary:
  * parse / resolve / oracle / PRE-submit fill exceptions -> FALLBACK_SENTINEL,
    which the dispatch seam (Task 10) reads to run legacy run_job instead. Safe
    because no submit has fired — legacy re-applying is a normal counted apply.
  * once a submit MAY have fired (the submit->verify block) a crash returns
    needs_review:v2_crashed_post_submit — NEVER the sentinel (legacy re-applying
    would risk a double submission; the dangling ledger INTENT is the designed
    safe state the duplicate guard reconciles on the next pass).

Reuses the SAME safety kernel (broker / ledger / browser_stream / identity,
invariant 1): those are threaded in by the caller (Task 10) via the shared
launcher._safety_prologue — the orchestrator NEVER constructs them here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from applypilot.apply.v2 import V2_TIER_LABEL
from applypilot.apply.v2 import ir, resolver
from applypilot.apply.v2.operator import FieldResolutionRequest, FieldSpec

# The dispatch seam (Task 10) reads this sentinel status to run legacy run_job.
FALLBACK_SENTINEL = "v2_fallback_to_legacy"

# Post-submit page-text confirmation markers (Tier-2 DOM signal, live only).
_CONFIRM_MARKERS = (
    "thank you for applying", "thanks for applying", "application submitted",
    "your application has been", "successfully submitted", "application received",
)


@dataclass
class Stages:
    """Injection seam for tests. Each stage is a plain callable; when
    `stages=None` is passed to run_form_compiler the real (live) stages are
    built by _default_stages(). Partial Stages are fine — the control flow
    short-circuits before an unset (None) stage is ever reached."""
    parse: object = None            # (page, company, url) -> FormSchema
    resolve: object = None          # (schema, profile, conn) -> FillPlan
    run_oracle: object = None       # (plan, schema, operator) -> FillPlan
    execute: object = None          # (page, schema, plan, conn) -> ExecReport
    submit: object = None           # (page, schema) -> clicked: bool
    verify: object = None           # (evidence, conn, dom_signals) -> VerifyResult


# --- lightweight stubs used by the injected tests -------------------------

@dataclass
class ExecStub:
    ready_to_submit: bool = False
    committed_keys: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    missing_required: list = field(default_factory=list)


@dataclass
class VerifyStub:
    verified: bool = False
    tier: int | None = None
    confidence: float = 0.0
    needs_review: bool = False


# --- oracle fold ----------------------------------------------------------

def _run_oracle(plan, schema, operator):
    """Batch the resolver's needs_oracle fields to the Operator, fold answers
    back into PlannedFields. Keeps it honest (invariant 4):
      * free-text answer -> PlannedField with the concrete value (filled).
      * cannot_answer / no answer -> park (never guess).
      * enumerated CUSTOM field: options are LAZY at parse, so the Operator is
        given options=[] and cannot index blindly -> park (a Phase-4 refinement
        would enumerate the react_select options lazily and re-ask). Free-text
        custom questions are the common Greenhouse case and ARE fully handled.
    No-op when nothing needs the oracle or no Operator was threaded in."""
    if not plan.needs_oracle or operator is None:
        return plan
    req = FieldResolutionRequest(
        job_context=f"{schema.company} — {schema.url}",
        fields=[
            FieldSpec(
                field_fp=ir.field_fp(f),
                question_text=f.question_text,
                widget_kind=f.widget.kind,
                # text/textarea -> free-text (None); everything else is
                # enumerated whose options are LAZY at parse -> [].
                options=None if f.widget.kind in ("text", "textarea") else [],
                char_limit=f.char_limit,
            )
            for f in plan.needs_oracle
        ],
    )
    answers = operator.resolve_fields(req)
    for f in plan.needs_oracle:
        fp = ir.field_fp(f)
        ans = answers.by_fp.get(fp) if answers is not None else None
        driver = resolver._driver_for(f.widget.kind)
        if ans is not None and ans.text is not None and not ans.cannot_answer:
            plan.planned.append(resolver.PlannedField(
                f, binding="oracle", value=ans.text, driver=driver))
        else:
            # cannot_answer, missing, or an option_index into LAZY options we
            # cannot resolve -> park-don't-guess.
            plan.planned.append(resolver.PlannedField(f, park=True, driver=driver))
    plan.needs_oracle = []
    return plan


def _passthrough_plan(schema, profile, conn=None, resume_path=None):
    """Resolve with no oracle escalation — the resolver ladder only (used as the
    injected `resolve` stage in tests, and the natural resolve default)."""
    return resolver.resolve(schema, profile, conn=conn, resume_path=resume_path)


# --- Tier-2 DOM signals (live only; injected tests stub verify) -----------

def _dom_signals(page):
    """Best-effort post-submit DOM signals for Tier-2 verification, gathered
    only when Tier-1 network evidence is absent. FULLY defensive: any failure
    returns None so it can never turn a clean submit into a post-submit crash.
    In the injected tests `page` is a bare object() and observation collection
    raises -> None -> the verify stub is called with dom_signals=None."""
    try:
        from applypilot.apply.browser_stream import collect_browser_observation
        from applypilot.apply.v2.verify import DomSignals
        obs = collect_browser_observation(page)
        text = (obs.page_text_sample or "").lower()
        has_submit_buttons = bool(obs.submit_buttons)
        return DomSignals(
            has_confirmation=any(m in text for m in _CONFIRM_MARKERS),
            url_changed=False,                       # no pre-submit baseline here
            submit_gone=not has_submit_buttons,
            submit_disabled=has_submit_buttons and not obs.submit_enabled,
            no_validation_errors=not obs.validation_errors,
            required_ok=not obs.required_missing,
        )
    except Exception:                                # noqa: BLE001 — best-effort
        return None


# --- real (live) stage wiring ---------------------------------------------

def _default_parse(page, company, url):
    from applypilot.apply.browser_stream import collect_browser_observation
    from applypilot.apply.v2 import frontend_greenhouse
    obs = collect_browser_observation(page)
    return frontend_greenhouse.parse_observation(obs, company=company, url=url)


def _default_execute(page, schema, plan, conn):
    from applypilot.apply.v2 import executor
    return executor.execute(page, schema, plan, conn=conn)


def _default_submit(page, schema):
    """Click the terminal submit through the reused submit-broker path (the
    browser_stream._guard route consumes the one-shot ticket). submit_greenhouse
    does NOT self-scope — Greenhouse forms are often embedded in a child iframe
    (vanity careers domains), so hand it _form_scope(page), not the raw page, or
    the submit button won't be found on iframe-embed forms."""
    from applypilot.apply.adapters.greenhouse import submit_greenhouse, _form_scope
    ok, _ = submit_greenhouse(_form_scope(page))
    return ok


def _default_stages(verify_threshold: float = 0.75,
                    resume_path: str | None = None) -> Stages:
    def _verify(evidence, conn, dom_signals):
        from applypilot.apply.v2 import verify as verify_mod
        return verify_mod.verify(evidence, conn=conn, dom_signals=dom_signals,
                                 verify_threshold=verify_threshold)

    return Stages(
        parse=_default_parse,
        # the default resolve CLOSES OVER resume_path so the Resume/CV file field
        # binds to the prologue-resolved path; injected fakes keep their 3-arg
        # (schema, profile, conn) shape and are unaffected.
        resolve=lambda schema, profile, conn: resolver.resolve(
            schema, profile, conn=conn, resume_path=resume_path),
        run_oracle=_run_oracle,
        execute=_default_execute,
        submit=_default_submit,
        verify=_verify,
    )


# --- the stage conductor --------------------------------------------------

def run_form_compiler(*, job, page, profile, conn, company, operator,
                      network_evidence=None, stages: Stages | None = None,
                      dry_run: bool = False, verify_threshold: float = 0.75,
                      resume_path: str | None = None):
    """Run the v2 Form Compiler for one job. Returns run_job's exact tuple
    (status, duration_ms, prefill_status). See module docstring for the
    fail-open / fail-closed boundary.

    `resume_path` (the prologue-resolved authoritative resume path) is threaded
    into the resolve stage so the Resume/CV file field binds deterministically —
    a file widget the Operator cannot answer never lands in needs_oracle."""
    started = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    prefill = {"ats": "greenhouse", "tier_used": V2_TIER_LABEL,
               "fields_filled": [], "error": None}
    st = stages if stages is not None else _default_stages(verify_threshold, resume_path)
    # An injected partial Stages may omit resolve (to exercise the real resolver
    # while faking the browser stages); fall back to the resume_path-aware default
    # so resume_path is threaded on that path too. Injected 3-arg resolve fakes are
    # used verbatim and are unaffected.
    resolve_stage = st.resolve if st.resolve is not None else (
        lambda schema, profile, conn: resolver.resolve(
            schema, profile, conn=conn, resume_path=resume_path))
    url = job.get("application_url") or job.get("url") or ""

    # PARSE — pre-submit, safe to fail open (no submit fired).
    try:
        schema = st.parse(page, company, url)
    except Exception:                                # noqa: BLE001 — FAIL OPEN (invariant 2)
        return FALLBACK_SENTINEL, _ms(), None

    # RESOLVE -> ORACLE -> FILL — all still PRE-submit: a crash here is safe to
    # fail open (nothing has been submitted) -> hand back to legacy (invariant 2).
    try:
        plan = resolve_stage(schema, profile, conn)
        plan = st.run_oracle(plan, schema, operator)

        report = st.execute(page, schema, plan, conn)
        prefill["fields_filled"] = list(getattr(report, "committed_keys", []))
        if not getattr(report, "ready_to_submit", False):
            # required-completeness interlock failed -> park, never submit an
            # incomplete form (invariant 9).
            ms = _ms()
            prefill["duration_ms"] = ms                # write_review_log reads this
            return "needs_review:v2_incomplete_required", ms, prefill
        if dry_run:
            # Submit is structurally impossible in dry-run; park BEFORE any click.
            ms = _ms()
            prefill["duration_ms"] = ms
            return "needs_review:v2_dry_run", ms, prefill
    except Exception:                                # noqa: BLE001 — FAIL OPEN (pre-submit only)
        return FALLBACK_SENTINEL, _ms(), None

    # SUBMIT + VERIFY — once a submit MAY have fired, a crash must NOT fall back
    # to legacy (that would risk a double-submit re-apply). The ledger INTENT is
    # already recorded, so leave it dangling for reconciliation and park.
    try:
        clicked = st.submit(page, schema)
        dom = None
        if network_evidence is None or not getattr(network_evidence, "submitted", False):
            dom = _dom_signals(page)                 # Tier-2 fallback (live only)
        v = st.verify(network_evidence, conn, dom)
    except Exception:                                # noqa: BLE001 — post-submit crash: DO NOT re-apply
        ms = _ms()
        prefill["duration_ms"] = ms
        return "needs_review:v2_crashed_post_submit", ms, prefill

    ms = _ms()
    prefill["duration_ms"] = ms                       # stamped on the dict return paths
    if getattr(v, "verified", False):
        return "applied", ms, prefill
    if getattr(v, "needs_review", False) or not clicked:
        return "needs_review:unverified_submission", ms, prefill
    return FALLBACK_SENTINEL, ms, None               # totally unclear -> prefill dropped, let legacy try
