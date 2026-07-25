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

import os
import time
from dataclasses import dataclass, field

from applypilot.apply.v2 import V2_FLIGHT_ENV, V2_TIER_LABEL, V2_TIER_LABELS
from applypilot.apply.v2 import ir, preflight, resolver
from applypilot.apply.v2.flight_recorder import FlightRecorder
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
    """Route the observation to the right ATS front-end via the registry (Task 7).
    An unrecognized ATS falls back to the Greenhouse parser — the dispatch gate
    (_v2_supported_ats) has already scoped WHICH ATSes reach here, so this default
    only ever fires for a URL whose front-end exists; the fallback is belt-and-
    suspenders so a detection miss degrades to the historical behavior, never a
    KeyError."""
    from applypilot.apply.browser_stream import collect_browser_observation
    from applypilot.apply.prefill import _detect_ats
    from applypilot.apply.v2 import frontend_greenhouse
    from applypilot.apply.v2.frontends import parser_for
    obs = collect_browser_observation(page)
    parse = parser_for(_detect_ats(url)) or frontend_greenhouse.parse_observation
    return parse(obs, company=company, url=url)


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


# --- flight-recorder shim (off-hot-path; invariant 3/15) ------------------

def _flight_enabled() -> bool:
    """Fresh-read the env gate every call (invariant 15) — OFF => zero cost."""
    return (os.environ.get(V2_FLIGHT_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


class _Flight:
    """OFF-hot-path recorder shim (invariant 15). Constructed only when the gate
    is on; every method is exception-guarded so a recorder fault can never change
    an apply outcome. Captures PRE-FILL DOM only (privacy, invariant 3)."""

    def __init__(self, *, ats, company, job_url):
        self._rec = None
        if not _flight_enabled():
            return
        try:
            from applypilot import config
            self._rec = FlightRecorder(run_dir=config.APP_DIR / "flight",
                                       job_url=job_url, ats=ats, company=company)
        except Exception:                                # noqa: BLE001 — never sink the apply
            self._rec = None

    @property
    def active(self) -> bool:
        return self._rec is not None

    def capture_prefill_dom(self, page) -> None:
        """Capture the raw DOM BEFORE any field is filled — the only place raw
        answers could leak, so it MUST run before the fill stage."""
        if self._rec is None:
            return
        try:
            self._rec.set_dom(page.content())            # pre-fill: no PII in inputs yet
        except Exception:                                # noqa: BLE001 — bare object() in tests raises
            pass

    def set_schema(self, schema) -> None:
        if self._rec is None:
            return
        try:
            self._rec.set_schema(schema)
        except Exception:                                # noqa: BLE001
            pass

    def phase(self, name: str, ms: int) -> None:
        if self._rec is None:
            return
        try:
            self._rec.record_phase(name, ms)
        except Exception:                                # noqa: BLE001
            pass

    def fields_from(self, plan, report) -> None:
        """Map field records from plan (binding/driver) + report (committed keys).
        provenance = the binding, or 'parked'/'oracle' when the resolver had none;
        committed = the field's key is in report.committed_keys (invariant 3:
        BINDING strings only, never the answer value)."""
        if self._rec is None or plan is None:
            return
        committed = set(getattr(report, "committed_keys", []) or [])
        for pf in getattr(plan, "planned", []) or []:
            try:
                key = pf.field.semantic_key or pf.field.field_id
                prov = pf.binding or ("parked" if pf.park else "oracle")
                self._rec.record_field(
                    field_fp=ir.field_fp(pf.field), semantic_key=pf.field.semantic_key,
                    provenance=prov, driver=pf.driver, committed=(key in committed),
                    locator_tier=None)               # per-field tier lives in mapping_cache
            except Exception:                            # noqa: BLE001
                continue

    def commit(self, status: str) -> None:
        """Commit a bundle ONLY for non-'applied' outcomes (hot path stays clean).
        Fully guarded: a write fault demotes to a no-op, the apply result stands."""
        if self._rec is None or status == "applied":
            return
        try:
            self._rec.commit(status=status)
        except Exception:                                # noqa: BLE001 — invariant 15
            pass


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
    _last = started

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def _phase_ms() -> int:
        """Delta since the previous phase boundary (per-phase timing, distinct
        from _ms()'s total elapsed)."""
        nonlocal _last
        now = time.monotonic()
        d = int((now - _last) * 1000)
        _last = now
        return d

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

    # PRE-FLIGHT PROBE (spec §6.2) — classify before spending parse budget. A
    # terminal probe state short-circuits to a NAMED bucket-B/irreducible status
    # (never the sentinel: these are true terminals, not fail-open-to-legacy —
    # legacy would just re-hit the same wall). Guarded: a probe crash proceeds
    # to parse (which fails open on its own).
    try:
        pr = preflight.probe(page, intended_url=url)
    except Exception:                                # noqa: BLE001
        pr = None
    if pr is not None and pr.terminal:
        ms = _ms()
        if pr.terminal.startswith("failed:") or pr.terminal in ("captcha", "login_issue"):
            return pr.terminal, ms, None             # worker_loop promotes these to permanent buckets

    # PARSE — pre-submit, safe to fail open (no submit fired).
    try:
        schema = st.parse(page, company, url)
    except Exception:                                # noqa: BLE001 — FAIL OPEN (invariant 2)
        return FALLBACK_SENTINEL, _ms(), None

    # Stamp the REAL parsed ATS onto the telemetry (Task 7): ats + the per-ATS
    # tier label (v2_greenhouse / v2_ashby / v2_lever) so the A/B metric buckets
    # each front-end independently. Placed after parse — the pre-parse default is
    # only ever seen on a None-prefill fail-open return.
    prefill["ats"] = schema.ats
    prefill["tier_used"] = V2_TIER_LABELS.get(schema.ats, V2_TIER_LABEL)

    # Flight recorder built AFTER a successful parse so its ats is the REAL parsed
    # ATS (Greenhouse/Ashby/Lever), not a hardcoded guess. No-op unless the env
    # gate is on. DOM is captured pre-fill below (privacy, invariant 3/15).
    flight = _Flight(ats=schema.ats, company=company, job_url=url)
    flight.phase("parse", _phase_ms())
    flight.set_schema(schema)
    flight.capture_prefill_dom(page)                 # BEFORE any fill (invariant 3/15)

    # RESOLVE -> ORACLE -> FILL — all still PRE-submit: a crash here is safe to
    # fail open (nothing has been submitted) -> hand back to legacy (invariant 2).
    try:
        plan = resolve_stage(schema, profile, conn)
        plan = st.run_oracle(plan, schema, operator)

        report = st.execute(page, schema, plan, conn)
        flight.phase("fill", _phase_ms())
        flight.fields_from(plan, report)
        prefill["fields_filled"] = list(getattr(report, "committed_keys", []))
        if not getattr(report, "ready_to_submit", False):
            # required-completeness interlock failed -> park, never submit an
            # incomplete form (invariant 9).
            ms = _ms()
            prefill["duration_ms"] = ms                # write_review_log reads this
            flight.commit("needs_review:v2_incomplete_required")
            return "needs_review:v2_incomplete_required", ms, prefill
        if dry_run:
            # Submit is structurally impossible in dry-run; park BEFORE any click.
            ms = _ms()
            prefill["duration_ms"] = ms
            flight.commit("needs_review:v2_dry_run")
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
        flight.phase("verify", _phase_ms())
    except Exception:                                # noqa: BLE001 — post-submit crash: DO NOT re-apply
        ms = _ms()
        prefill["duration_ms"] = ms
        flight.commit("needs_review:v2_crashed_post_submit")
        return "needs_review:v2_crashed_post_submit", ms, prefill

    ms = _ms()
    prefill["duration_ms"] = ms                       # stamped on the dict return paths
    if getattr(v, "verified", False):
        flight.commit("applied")                      # no-op by policy (non-applied-only)
        return "applied", ms, prefill
    if getattr(v, "needs_review", False) or not clicked:
        flight.commit("needs_review:unverified_submission")
        return "needs_review:unverified_submission", ms, prefill
    flight.commit("v2_unclear")
    return FALLBACK_SENTINEL, ms, None               # totally unclear -> prefill dropped, let legacy try
