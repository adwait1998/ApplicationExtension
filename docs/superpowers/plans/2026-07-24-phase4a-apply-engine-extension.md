# Phase 4A — Apply-Engine Extension (Pre-flight Probe, Ashby/Lever Front-ends, Degraded Tier, Canary Parse, Flight-Recorder Wiring) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is self-contained: a fact-swept READ step, verbatim failing tests, a reference implementation adapted to the REAL landed APIs, and a commit block with one-shot identity.

**Goal:** EXTEND the landed v2 Form Compiler (Phase 3, shipped behind `APPLYPILOT_V2_ENGINE`, Greenhouse-only) to (a) close the two Phase-3 leftovers that block scale — wire the flight recorder into `run_form_compiler` so real runs promote to fixtures, and auto-compute the `report --v2-cutover` §12.3 safety-audit leg so it stops printing "unknown"; (b) add a ~2s **pre-flight probe** (spec §6.2) that classifies `{ats_kind, login_wall, captcha_present, sso_gate, job_expired, form_frame_path}` before any parse, folding tonight's landed expired-redirect guard into one named terminal-state family; (c) add **Ashby** and **Lever** front-ends (spec §6.3) as pure observation→IR parsers, each grounded in a live-DOM **probe** taken first (no widget census exists — see Fact-sweep); (d) an **ATS-routing registry** so `greenhouse|ashby|lever` each dispatch to their parser and each shadow **independently** behind a per-ATS flag; (e) the **degraded tier** (spec §6.8) — `GenericFrontend` + the real `Operator.label_controls` (currently a `NotImplementedError` stub) — counted and capped; (f) a manual/cron **`applypilot canary-parse`** command (~20 live forms per ATS → parse-rate report). Phase 4A REUSES the IR / resolver / executor / verify / drivers / orchestrator conductor UNCHANGED wherever possible — it adds parsers, a probe, routing, a degraded fallback, and telemetry wiring, NOT new pipeline stages.

**Architecture:** Additive and quarantined, exactly as Phase 3. The `apply/v2/` package gains `preflight.py`, `frontend_ashby.py`, `frontend_lever.py`, `frontend_generic.py`, and a `frontends.py` registry; `operator.py` gains the real `label_controls`; `orchestrator.py` gains a probe gate + a registry-driven `_default_parse` + flight-recorder wiring; `launcher.py`'s dispatch gate widens `_is_greenhouse` → `_v2_supported_ats` (a fresh-read per-ATS allowlist); `prefill._detect_ats` gains the missing Lever branch; `reporting.py` gains an audit-computing helper the `report --v2-cutover` leg calls; `cli.py` gains `canary-parse`. The **SAFETY KERNEL** objects (`submit_broker.SubmitBroker`, `submission_ledger.SubmissionLedger`, `browser_stream.BrowserStateStream` + its `_guard` route) are **reused, not rebuilt** — the orchestrator still threads the SAME objects `_safety_prologue` constructs. The resolver's canary-first ladder, the WidgetDriver read-back registry, and the Tier-1 passive network-evidence listener are imported as-is; new front-ends emit the same `ir.FormSchema`, so the resolver/executor/verify code path is untouched. A/B stays free: v2 rows already carry `tier_used`/`duration_ms`/`status`; Ashby/Lever rows will carry `tier_used="v2_ashby"`/`"v2_lever"` so per-ATS pass rate is a pure query over existing telemetry.

**Tech Stack:** Python 3.11, SQLite (WAL, thread-local via `database.get_connection`), Playwright sync API over CDP, Typer, pytest. Unit tests use `page.set_content(...)` synthetic DOM via the `page` fixture pattern from `tests/test_v2_fixture_replay.py`, a fake/injected Operator (call-site injection like `tests/test_v2_operator.py`'s `_FakeClient`), and a temp DB. NO live network, NO real Chrome subprocess in unit tests — EXCEPT the probe harness (Task 4) and `canary-parse` (Task 10), which are explicit dev/ops tools that DO hit live forms and are excluded from the offline suite by a marker. Interpreter `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe` (has pytest + editable applypilot; the `.venv` python does NOT).

**Spec:** `docs/superpowers/specs/2026-07-02-applypilot-v2-design.md` — §6.2 (pre-flight probe), §6.3 (front-ends → FormSchema IR, incl. Ashby/Lever/Generic), §6.8 (degraded tier + legacy fallback + nightly canary parse), §11 (flight recorder / fixtures / canary / blocking metrics), §12 (acceptance gate, esp. §12.3 safety audit), §13 Phase 4 (driver long-tail from the shadow census; Ashby/Lever measured shadow burn-in with a per-ATS go/no-go). §14 YAGNI and §15 risks (correlated ATS DOM churn → degraded tier + canary parse) bound scope.

**Prerequisite:** Phase 3 complete and landed (verified at plan-authoring, `main` @ `df93e81` or later, full suite 811 passed + 1 skip). Present in the tree: `apply/v2/{ir,operator,mapping_cache,frontend_greenhouse,resolver,drivers,executor,verify,orchestrator,flight_recorder}.py`, the `_safety_prologue` / `_make_v2_production_fn` / `_dispatch_apply_v2_aware` dispatch seam in `launcher.py`, `reporting.v2_cutover_gate` / `format_v2_cutover`, `cli.fixtures promote`, the `mapping_cache` + `submit_endpoints` tables, and tonight's expired-redirect guard (`freshness.is_greenhouse_expired_redirect` + the launcher short-circuit). **Phase 4 workstreams B/C/D — compile-then-score matching, trust layer / modes / receipts, and the queue-depth controller — are SEPARATE later plans and are OUT OF SCOPE here.** Phase 4A is the apply-engine extension only.

**Conventions (same as Phase 0/1/2/3):**
- Repo root: `e:\auto-apply-pipeline`. Run all commands from there.
- `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe`.
- Commit with one-shot identity, never push: `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "..."`.
- **The index may contain pre-staged files, and another workflow is actively committing to this repo.** Before EVERY commit run `git reset` then `git add <only this task's files>`, then `git diff --cached --stat` and verify only the intended files are staged. If `git reset`/`add` races with the other workflow, retry once after a few seconds — your add touches only your own new/edited files.
- TDD: write the failing test first, run it to confirm the failure mode, implement, run to green, commit.
- **Reuse-verify discipline:** every task that touches an existing module opens with a READ step against the exact file + line range. Signatures below were fact-swept at authoring; where a signature has drifted, adapt the step and note it in the task summary. NEVER rebuild a safety-kernel object — thread the SAME instance.

**File structure created / modified by this plan:**
- Modified: `src/applypilot/apply/v2/orchestrator.py` — flight-recorder wiring + registry-driven parse + probe gate (Tasks 1, 3, 7)
- Modified: `src/applypilot/reporting.py` — `v2_audit_clean(...)` helper + wire it into `format_v2_cutover` (Task 2)
- Modified: `src/applypilot/cli.py` — pass the computed audit into `report --v2-cutover`; add `canary-parse` (Tasks 2, 10)
- Create: `src/applypilot/apply/v2/preflight.py` — `probe(page, url) -> ProbeResult` (Task 3)
- Create: `src/applypilot/apply/probe_dump.py` + `apply probe-form` CLI — live-DOM observation dump (Task 4)
- Create: `src/applypilot/apply/v2/frontend_ashby.py` (Task 5), `frontend_lever.py` (Task 6), `frontend_generic.py` (Task 9)
- Create: `src/applypilot/apply/v2/frontends.py` — `parser_for(ats)` registry (Task 7)
- Modified: `src/applypilot/apply/prefill.py` — add `LEVER_HOSTS` + a Lever branch to `_detect_ats` (Task 7)
- Modified: `src/applypilot/apply/launcher.py` — `_v2_supported_ats` gate widening + per-ATS `tier_used` (Task 7)
- Modified: `src/applypilot/apply/v2/operator.py` — real `label_controls` (Task 9)
- Modified: `src/applypilot/apply/v2/__init__.py` — `V2_ATS_ENV`, `V2_FLIGHT_ENV`, tier-label constants (Tasks 1, 7)
- Modified: `src/applypilot/apply/v2/drivers.py` — driver long-tail from the probes (Task 8, ONLY if probes show a gap)
- Tests: `tests/test_v2_flight_wiring.py`, `tests/test_v2_cutover_audit.py`, `tests/test_v2_preflight.py`, `tests/test_probe_dump.py`, `tests/test_v2_frontend_ashby.py`, `tests/test_v2_frontend_lever.py`, `tests/test_v2_frontends_registry.py`, `tests/test_v2_ats_gate.py`, `tests/test_v2_drivers_longtail.py` (conditional), `tests/test_v2_degraded_tier.py`, `tests/test_canary_parse.py`

**Key invariants (load-bearing — carried from Phase 3, extended for 4A):**
1. **The safety kernel is below the parser and is REUSED, never rebuilt (spec §10).** Every new front-end and the probe run on the SAME `SubmitBroker`/`SubmissionLedger`/`BrowserStateStream` (with its `_guard` route) threaded by `_safety_prologue` (launcher L2360-2379). A misparse of Ashby/Lever/Generic can NEVER become a submission: the network route fails closed on dry-run and gates real submits on an open broker ticket. New code does NOT install routes, does NOT `route.fetch()`, does NOT open its own broker.
2. **v2 fails OPEN, per-ATS (spec §6.8).** Any parse/probe/resolve/PRE-submit-fill failure returns the `FALLBACK_SENTINEL` and the dispatch seam runs legacy `run_job` for THAT job. Adding Ashby/Lever/Generic never hard-fails a job and never regresses the live queue; the legacy Claude-CLI agent stays the counted fallback per-ATS through burn-in.
3. **Bindings, never literal values (spec §6.4).** Unchanged — the mapping cache and flight-recorder `provenance` store `profile.<path>` / `answer:<qfp>` / `policy.<k>` / `oracle`, never an answer. The flight-recorder DOM capture is the ONE place raw answers could leak, so it captures **pre-fill DOM only** (invariant 15).
4. **Options are enumerated LAZILY, never at parse (spec §6.3/§6.5).** Every new front-end emits `options=ir.LAZY`; only a driver opens a dropdown, at commit time. Ashby/Lever parsers MUST NOT open dropdowns during parse (blows the budget, desyncs framework state).
5. **Every driver returns a read-back-verified `CommitResult` (spec §6.6).** Unchanged — reused as-is. Any driver added in Task 8 ends in a genuine DOM read-back; click success is never trusted.
6. **Enumerated answers are an INDEX into provided options (spec §6.5).** Unchanged — the Operator contract is reused. `label_controls` (Task 9) returns control LABELS only, never option indices, so the degraded tier feeds the resolver, not the submit.
7. **Canary fields never reach the Operator, the answer bank, or fuzzy matching (spec §10.3).** Unchanged and extended: the degraded tier's `label_controls` is given NO canary field, and its best-effort IR is resolved through the SAME canary-first resolver ladder that short-circuits canary keys before any oracle batching.
8. **Tier-1 network evidence is PASSIVE (spec §6.7).** Unchanged — reused. The per-ATS `NetworkEvidence` (Task 7) reads status+url only via the SAME `_ATS_HOST_RE`/`_MUTATION_METHODS` host+method definitions; it never `route.fetch()`.
9. **Zero fixed sleeps in the executor (spec §6.6).** Unchanged — reused. New front-ends and the probe are pure/observational and add no `time.sleep`. The probe's ≤2s budget is a deadline poll, not a blind sleep.
10. **Fingerprints are stable across id/class churn (spec §6.3/§15).** Unchanged — `field_fp` is reused for Ashby/Lever; new parsers assign `semantic_key`/widget/options-shape into the SAME `ir.field_fp` inputs so cache hit-rate stays measurable.
11. **Per-ATS flags default OFF; a new ATS shadows independently (spec §13 Phase 4).** Routing is gated by a fresh-read `APPLYPILOT_V2_ATS` comma-list allowlist (Task 7). Unset ⇒ `greenhouse` only (byte-for-byte the Phase-3 shadow behavior). An unsupported ATS routes to legacy unchanged. Ashby cannot route until `ashby` is in the list; Lever until `lever` is; each burns in on its own go/no-go without touching the others.
12. **Probe-first empiricism (spec §13 Phase 4, "driver long-tail from the shadow census").** **No Ashby/Lever widget census exists** — the landed Phase-2 telemetry (`discovery/atlas/telemetry.py`) is funnel-only and its own docstring defers Ashby/Lever parse-gap/fingerprint rates to Phase 3. Front-end markup assumptions MUST be grounded in a live-DOM observation dump (Task 4) taken BEFORE the parser is written (Tasks 5/6), and the driver long-tail (Task 8) is added ONLY for widget kinds the probes actually surface — never speculatively.
13. **Front-ends stay PURE observation→IR; Phase 4A adds parsers, not pipeline stages.** New front-ends consume a `BrowserObservation` and emit an `ir.FormSchema`; the resolver/executor/verify/drivers are reused UNCHANGED. `parse_observation`'s ATS-neutral signature (`(obs, *, company, url)`) is the seam.
14. **The degraded tier is counted + capped + canary-safe (spec §6.8).** `GenericFrontend` + one batched `label_controls` call is used ONLY when a concrete front-end fails; it is explicitly counted (a distinct status/tier), capped per run, and its IR flows through the canary-first resolver so a mislabeled control can never resolve a canary field.
15. **Flight recording is OFF the hot path (spec §6.9/§11).** Wiring is behind a fresh-read `APPLYPILOT_V2_FLIGHT` env gate (default OFF ⇒ zero cost). When on: capture **pre-fill DOM only** (privacy, invariant 3), per-phase delta timings, field records mapped from `plan`+`report`; commit a bundle only for **non-`applied`** outcomes (plus an optional sample) so the success hot path is never taxed; the whole block is exception-guarded so a recorder fault can NEVER change an apply outcome.

---

## Task 1: Wire the flight recorder into `run_form_compiler` (unlock fixtures-promote on real runs)

The flight-recorder module (`flight_recorder.FlightRecorder`) and the `fixtures promote` CLI both landed in Phase 3, but `run_form_compiler` never instantiates a recorder — so ZERO real bundles are produced and every fixture is still synthetic. This task wires the recorder in behind an off-by-default env gate, implementing the deferred 5-point design: (1) per-phase delta timings; (2) field records mapped from `plan`+`report`; (3) parse-time DOM capture ONLY (privacy, invariant 3/15); (4) commit for non-`applied` outcomes only + exception-guarded; (5) fresh-read env gate `APPLYPILOT_V2_FLIGHT`, default OFF. This is what lets the FIRST live Ashby/Lever run promote same-day (spec §6.8).

**Files:**
- Modify: `src/applypilot/apply/v2/__init__.py` (add `V2_FLIGHT_ENV`)
- Modify: `src/applypilot/apply/v2/orchestrator.py` (build + drive a recorder behind the gate)
- Test: `tests/test_v2_flight_wiring.py`

- [ ] **Step 1: READ the orchestrator conductor + the recorder surface + the plan/report shapes**

READ `src/applypilot/apply/v2/orchestrator.py:195-271` — `run_form_compiler(*, job, page, profile, conn, company, operator, network_evidence=None, stages=None, dry_run=False, verify_threshold=0.75, resume_path=None)`. Note: `started = time.monotonic()` (L206) and `_ms()` (L208-209) give ONE elapsed clock — there are no per-phase deltas yet; the PARSE try-block is L224-227, the RESOLVE→ORACLE→FILL try is L231-249 (produces `plan` and `report`), the SUBMIT→VERIFY try is L254-271. READ `src/applypilot/apply/v2/flight_recorder.py:66-127` — `FlightRecorder(*, run_dir, job_url, ats, company)`, `set_schema(schema)`, `record_field(*, field_fp, semantic_key, provenance, driver, committed, locator_tier=None)`, `record_network(method, url, status)`, `record_phase(phase, ms)`, `set_dom(html)` (the docstring at L92-104 PINS the privacy guardrail: capture DOM **before** any field is filled), `commit(*, status) -> Path`. READ `src/applypilot/apply/v2/resolver.py:43-56` — `PlannedField(field, binding, value, option_intent, driver, park)` and `FillPlan(planned, needs_oracle)`. READ `src/applypilot/apply/v2/executor.py:53-61` — `ExecReport(ready_to_submit, committed_keys, unresolved, missing_required, parked, error)`; `committed_keys`/`unresolved` hold `field.semantic_key or field.field_id`. READ `src/applypilot/config.py:9` (`APP_DIR`) — the bundle dir is `APP_DIR / "flight"` (matches `cli.fixtures_promote`'s fallback path, cli.py L200-201). CONFIRM the orchestrator currently imports NO `flight_recorder` symbol.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_flight_wiring.py
import json

import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import orchestrator as orch


class _Page:
    """Minimal fake page: content() returns a fixed pre-fill DOM string; a fresh
    object() (as the injected tests use) would raise on .content(), which the
    wiring must swallow (exception-guarded)."""
    def __init__(self, html="<html><body><form>PRE-FILL DOM</form></body></html>"):
        self._html = html
    def content(self):
        return self._html


def _schema():
    f = ir.Field(field_id="e", frame_path=(), label_text="Email", question_text="Email",
                 semantic_key="email", widget=ir.Widget(kind="text"), options=ir.LAZY,
                 required=True)
    return ir.FormSchema(ats="greenhouse", company="acme", url="u",
                         steps=[ir.Step(index=0, fields=[f], terminal=True)])


def _stages(status_report_ready=True):
    from applypilot.apply.v2.resolver import FillPlan, PlannedField
    schema = _schema()
    fld = schema.steps[0].fields[0]

    def parse(page, company, url): return schema
    def resolve(schema, profile, conn):
        return FillPlan(planned=[PlannedField(fld, binding="profile.personal.email",
                                              value="a@b.co", driver="text")])
    def run_oracle(plan, schema, operator): return plan
    def execute(page, schema, plan, conn):
        return orch.ExecStub(ready_to_submit=status_report_ready,
                             committed_keys=["email"])
    def submit(page, schema): return True
    def verify(evidence, conn, dom): return orch.VerifyStub(verified=True, tier=1)
    return orch.Stages(parse=parse, resolve=resolve, run_oracle=run_oracle,
                       execute=execute, submit=submit, verify=verify)


def test_flight_gate_off_writes_no_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_FLIGHT", raising=False)
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages())
    assert status == "applied"
    assert not (tmp_path / "flight").exists() or not list((tmp_path / "flight").glob("*.json"))


def test_flight_gate_on_records_nonapplied_bundle(tmp_path, monkeypatch):
    # A non-applied outcome (incomplete required) MUST leave a promotable bundle
    # with pre-fill DOM, per-phase timings, and a field record mapped from plan+report.
    monkeypatch.setenv("APPLYPILOT_V2_FLIGHT", "1")
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages(status_report_ready=False))
    assert status == "needs_review:v2_incomplete_required"
    bundles = list((tmp_path / "flight").glob("*.json"))
    assert len(bundles) == 1
    data = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert data["status"] == "needs_review:v2_incomplete_required"
    assert data["dom_html"].startswith("<html")                 # pre-fill DOM captured
    assert data["phases"].get("parse") is not None              # per-phase delta timing
    provs = {f["provenance"] for f in data["fields"]}
    assert "profile.personal.email" in provs                    # mapped from plan.binding
    assert any(f["committed"] for f in data["fields"])          # mapped from report.committed_keys


def test_flight_gate_on_applied_is_not_recorded(tmp_path, monkeypatch):
    # Success is the hot path: non-applied-only policy => no bundle for 'applied'.
    monkeypatch.setenv("APPLYPILOT_V2_FLIGHT", "1")
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages())
    assert status == "applied"
    assert not list((tmp_path / "flight").glob("*.json"))


def test_flight_recorder_fault_never_changes_outcome(tmp_path, monkeypatch):
    # A recorder that raises on commit must NOT change the apply status.
    monkeypatch.setenv("APPLYPILOT_V2_FLIGHT", "1")
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path)

    class _BoomRecorder:
        def __init__(self, **kw): pass
        def set_schema(self, s): pass
        def record_field(self, **kw): pass
        def record_phase(self, *a): pass
        def set_dom(self, h): pass
        def commit(self, *, status): raise RuntimeError("disk full")
    monkeypatch.setattr(orch, "FlightRecorder", _BoomRecorder)
    status, ms, prefill = orch.run_form_compiler(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=_Page(), profile={}, conn=None, company="acme", operator=None,
        stages=_stages(status_report_ready=False))
    assert status == "needs_review:v2_incomplete_required"      # unchanged despite fault
```

- [ ] **Step 3: Run — expect failure** (`AttributeError`/no bundle written — the recorder is not wired)

Run: `& $PY -m pytest tests/test_v2_flight_wiring.py -v`

- [ ] **Step 4: Wire the recorder into the orchestrator**

Add to `src/applypilot/apply/v2/__init__.py`:

```python
V2_FLIGHT_ENV = "APPLYPILOT_V2_FLIGHT"   # fresh-read gate; OFF => zero flight-recorder cost
```

In `src/applypilot/apply/v2/orchestrator.py`, import the recorder at module top so tests can monkeypatch `orch.FlightRecorder`:

```python
from applypilot.apply.v2.flight_recorder import FlightRecorder
```

Add a small gate + capture helper, and thread per-phase deltas + a terminal record through `run_form_compiler`. The design is a thin, fully-guarded shim: `_Flight` is a no-op unless the env gate is on and DOM capture succeeds pre-fill.

```python
import os
from applypilot.apply.v2 import V2_FLIGHT_ENV


def _flight_enabled() -> bool:
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
        provenance = the binding, or 'oracle'/'parked' when the resolver had none;
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
```

Now thread it through `run_form_compiler`, capturing per-phase deltas between the existing try-blocks (adapt to the EXACT control flow you READ in Step 1 — this shows the shape, not new stages):

```python
    started = time.monotonic()
    _last = started

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def _phase_ms() -> int:
        nonlocal _last
        now = time.monotonic()
        d = int((now - _last) * 1000)
        _last = now
        return d

    flight = _Flight(ats="greenhouse", company=company, job_url=url)   # ats set post-parse below
    # PARSE
    try:
        schema = st.parse(page, company, url)
    except Exception:
        return FALLBACK_SENTINEL, _ms(), None
    flight.phase("parse", _phase_ms())
    flight.set_schema(schema)
    flight.capture_prefill_dom(page)            # BEFORE any fill (privacy invariant 3/15)

    # RESOLVE -> ORACLE -> FILL
    try:
        plan = resolve_stage(schema, profile, conn)
        plan = st.run_oracle(plan, schema, operator)
        report = st.execute(page, schema, plan, conn)
        flight.phase("fill", _phase_ms())
        flight.fields_from(plan, report)
        prefill["fields_filled"] = list(getattr(report, "committed_keys", []))
        if not getattr(report, "ready_to_submit", False):
            ms = _ms(); prefill["duration_ms"] = ms
            flight.commit("needs_review:v2_incomplete_required")
            return "needs_review:v2_incomplete_required", ms, prefill
        if dry_run:
            ms = _ms(); prefill["duration_ms"] = ms
            flight.commit("needs_review:v2_dry_run")
            return "needs_review:v2_dry_run", ms, prefill
    except Exception:
        return FALLBACK_SENTINEL, _ms(), None

    # SUBMIT + VERIFY
    try:
        clicked = st.submit(page, schema)
        dom = None
        if network_evidence is None or not getattr(network_evidence, "submitted", False):
            dom = _dom_signals(page)
        v = st.verify(network_evidence, conn, dom)
        flight.phase("verify", _phase_ms())
    except Exception:
        ms = _ms(); prefill["duration_ms"] = ms
        flight.commit("needs_review:v2_crashed_post_submit")
        return "needs_review:v2_crashed_post_submit", ms, prefill

    ms = _ms(); prefill["duration_ms"] = ms
    if getattr(v, "verified", False):
        flight.commit("applied")                # no-op by policy (non-applied-only)
        return "applied", ms, prefill
    if getattr(v, "needs_review", False) or not clicked:
        flight.commit("needs_review:unverified_submission")
        return "needs_review:unverified_submission", ms, prefill
    flight.commit("v2_unclear")
    return FALLBACK_SENTINEL, ms, None
```

Notes / adapt warnings:
- `ats` on the recorder should be `schema.ats` (Greenhouse/Ashby/Lever) once parse succeeds; set `_Flight`'s `self._rec.ats` after `set_schema`, or pass `ats=schema.ats` by constructing `_Flight` lazily AFTER parse. Simplest: construct `_Flight` after a successful parse with `ats=schema.ats`; the pre-parse failure path (`FALLBACK_SENTINEL`) records nothing anyway. Adapt so the recorder's `ats` is the real parsed ATS, not a hardcoded "greenhouse".
- Keep `_dom_signals`/`_run_oracle`/`_default_stages` unchanged. This task ONLY adds the `_Flight` shim + phase deltas + terminal `commit` calls; it changes no status semantics (the four tests assert status is unchanged).
- DOM capture uses `page.content()` (a single HTML string), NOT MHTML — §6.9 forbids MHTML on the critical path; `page.content()` is cheap and ONLY runs when the env gate is on (OFF in production by default). Note this in the summary.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_flight_wiring.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/test_v2_orchestrator.py tests/test_v2_flight_recorder.py -q`
Expected: no regressions (the wiring is additive + gated OFF by default).

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/__init__.py src/applypilot/apply/v2/orchestrator.py tests/test_v2_flight_wiring.py
git diff --cached --stat   # exactly these 3 files
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: wire flight recorder into run_form_compiler (per-phase timings, plan+report field records, pre-fill DOM only, non-applied-only, env-gated APPLYPILOT_V2_FLIGHT)"
```

---

## Task 2: Auto-compute the `report --v2-cutover` §12.3 safety-audit leg (stop printing "unknown")

`v2_cutover_gate` already computes §12.1 pass-rate and §12.2 speed from `review.jsonl`, but §12.3 safety is injected via `audit_clean` and the CLI calls `format_v2_cutover(rows)` with NO audit — so the leg always prints `[????]` and a manual query trio. This task adds a pure `v2_audit_clean(...)` helper that reads the EXISTING durable records — `submission_ledger.dangling_count()`, per-token `confirmed_count_for_token` for duplicate detection, and a flight-recorder canary-violation scan — and wires it into the CLI so the leg renders GO/HOLD from real data.

**Files:**
- Modify: `src/applypilot/reporting.py` (add `v2_audit_clean`; `format_v2_cutover` gains an optional injected result)
- Modify: `src/applypilot/cli.py` (`report --v2-cutover` computes and passes the audit)
- Test: `tests/test_v2_cutover_audit.py`

- [ ] **Step 1: READ the audit contract + the ledger query surface + the recorder provenance**

READ `src/applypilot/reporting.py:239-306` — `v2_cutover_gate(rows, *, min_rows=100, max_p50_ms=45000, audit_clean=None, ...)`; the §12.3 query trio is documented at L257-264 and the audit branch at L287-297 (`audit_clean is None` ⇒ `{"go": None, ...}` HOLD). READ `:372-406` — `format_v2_cutover(rows, *, audit_clean=None)` prints the manual trio when `au["go"] is None` (L392-403). READ `src/applypilot/submission_ledger.py:39-63` — `has_open_intent`, `has_confirmed`, `confirmed_count_for_token(board_token, since_iso) -> int` (L49), `dangling_count() -> int` (L57). READ `src/applypilot/apply/v2/flight_recorder.py:43-51` — the `_FieldRecord.provenance` field (a BINDING string; a canary VIOLATION would be a canary `semantic_key` whose provenance is `oracle` or `answer:*` — canary must be `profile.*`/`policy.*` only, invariant 7). READ `src/applypilot/apply/v2/ir.py:36-45` — `is_canary_key(semantic_key)` (the exact canary set). CONFIRM `cli.py:817-841` calls `format_v2_cutover(rows)` with no audit arg today.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_cutover_audit.py
import json

from applypilot import reporting as rp


class _Ledger:
    def __init__(self, dangling=0, per_token=None):
        self._d = dangling
        self._t = per_token or {}
    def dangling_count(self):
        return self._d
    def confirmed_count_for_token(self, token, since_iso):
        return self._t.get(token, 0)


def test_audit_clean_true_when_no_dangling_no_dupes_no_canary(tmp_path):
    ledger = _Ledger(dangling=0, per_token={"acme": 1})
    # a flight dir with one clean bundle (email via profile binding)
    fdir = tmp_path / "flight"; fdir.mkdir()
    (fdir / "acme_x.json").write_text(json.dumps({"fields": [
        {"semantic_key": "email", "provenance": "profile.personal.email"},
        {"semantic_key": "work_auth", "provenance": "profile.work_authorization.legally_authorized_to_work"},
    ]}), encoding="utf-8")
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=fdir, tokens=["acme"])
    assert res["go"] is True
    assert res["dangling"] == 0 and res["duplicates"] == 0 and res["canary_violations"] == 0


def test_audit_hold_on_dangling_intent(tmp_path):
    ledger = _Ledger(dangling=2, per_token={})
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=tmp_path / "nope", tokens=[])
    assert res["go"] is False and res["dangling"] == 2


def test_audit_hold_on_identity_double_submit(tmp_path):
    ledger = _Ledger(dangling=0, per_token={"acme": 2})     # >1 confirmed for one token
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=tmp_path / "nope", tokens=["acme"])
    assert res["go"] is False and res["duplicates"] == 1


def test_audit_hold_on_canary_violation(tmp_path):
    ledger = _Ledger(dangling=0, per_token={})
    fdir = tmp_path / "flight"; fdir.mkdir()
    # a canary key resolved from the ORACLE is a hard violation (invariant 7).
    (fdir / "bad.json").write_text(json.dumps({"fields": [
        {"semantic_key": "salary", "provenance": "oracle"},
    ]}), encoding="utf-8")
    res = rp.v2_audit_clean(ledger=ledger, flight_dir=fdir, tokens=[])
    assert res["go"] is False and res["canary_violations"] == 1


def test_cutover_gate_consumes_computed_audit():
    # v2_cutover_gate already takes audit_clean; assert the bool flows to the verdict.
    rows = []
    gate = rp.v2_cutover_gate(rows, audit_clean=True)
    assert gate["audit"]["go"] is True
    gate2 = rp.v2_cutover_gate(rows, audit_clean=False)
    assert gate2["audit"]["go"] is False


def test_format_v2_cutover_renders_go_when_audit_true():
    out = rp.format_v2_cutover([], audit_clean=True)
    assert "????" not in out.split("§12.3")[1][:10]     # §12.3 line is not the unknown mark
```

- [ ] **Step 3: Run — expect failure** (`AttributeError: module 'applypilot.reporting' has no attribute 'v2_audit_clean'`)

Run: `& $PY -m pytest tests/test_v2_cutover_audit.py -v`

- [ ] **Step 4: Implement `v2_audit_clean` + wire the CLI**

In `src/applypilot/reporting.py` (pure over injected records — no import of `apply.v2` beyond the tiny canary predicate, keep it lazy):

```python
def v2_audit_clean(*, ledger, flight_dir=None, tokens=None) -> dict:
    """Compute the §12.3 safety-audit leg from EXISTING durable records (no new
    telemetry). Three conditions, ALL required for go=True:
      1. zero dangling INTENTs (ledger.dangling_count() == 0) — a
         needs_review:v2_crashed_post_submit leaves one by design; it must be
         reconciled, not counted as clean.
      2. no identity double-submitted: confirmed_count_for_token(token) <= 1 for
         every board token in the v2 burn-in corpus.
      3. zero canary-field violations in the flight-recorder bundles: a field
         whose semantic_key is canary (ir.is_canary_key) MUST have a
         profile.*/policy.* provenance — an 'oracle'/'answer:*' provenance on a
         canary key is a hard violation (invariant 7).
    Returns {"go", "dangling", "duplicates", "canary_violations", "reason"}."""
    from pathlib import Path
    import json as _json
    from applypilot.apply.v2.ir import is_canary_key

    dangling = int(ledger.dangling_count()) if ledger is not None else 0
    duplicates = 0
    for tok in (tokens or []):
        try:
            if ledger.confirmed_count_for_token(tok, "") > 1:
                duplicates += 1
        except Exception:
            continue
    canary_violations = 0
    fdir = Path(flight_dir) if flight_dir else None
    if fdir and fdir.exists():
        for p in fdir.glob("*.json"):
            try:
                bundle = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for f in bundle.get("fields", []):
                sk = f.get("semantic_key")
                prov = (f.get("provenance") or "")
                if is_canary_key(sk) and not (prov.startswith("profile.") or prov.startswith("policy.")):
                    canary_violations += 1
    go = dangling == 0 and duplicates == 0 and canary_violations == 0
    reason = ("clean: 0 dangling, 0 duplicate, 0 canary" if go
              else f"HOLD: dangling={dangling} duplicates={duplicates} canary={canary_violations}")
    return {"go": go, "dangling": dangling, "duplicates": duplicates,
            "canary_violations": canary_violations, "reason": reason}
```

In `src/applypilot/cli.py`, `report` computes the audit and passes it (adapt to the exact `report` body at L829-841):

```python
    audit_clean = None
    if v2_cutover:
        try:
            from applypilot.submission_ledger import SubmissionLedger
            from applypilot.database import get_connection
            from applypilot.reporting import v2_audit_clean
            ledger = SubmissionLedger(get_connection())
            tokens = sorted({t for r in rows if (t := r.get("board_token"))})   # if present in rows
            audit = v2_audit_clean(ledger=ledger, flight_dir=config.APP_DIR / "flight", tokens=tokens)
            audit_clean = audit["go"]
        except Exception:
            audit_clean = None      # fall back to the manual trio print (never crash report)
        typer.echo(format_v2_cutover(rows, audit_clean=audit_clean))
```

Notes / adapt warnings:
- `confirmed_count_for_token(token, since_iso)` takes a `since_iso` string; passing `""` counts all-time confirmed (the burn-in corpus). Confirm the ledger's query treats `""` as "no lower bound" — if it filters `>= since_iso`, `""` sorts before any ISO timestamp, so all rows count. Verify against `submission_ledger.py:49-56` and adapt (use a fixed early sentinel like `"1970-01-01"` if the empty string breaks the SQL).
- `board_token` may not be a field on `review.jsonl` rows. If it is absent, `tokens` is empty and the duplicate leg is a no-op (0 duplicates) — HONEST: the audit still catches dangling INTENTs and canary violations, and the duplicate check is best-effort over whatever token telemetry exists. Note this limitation in the summary; a follow-up can add `board_token` to the review row if the operator wants the duplicate leg to bite.
- `format_v2_cutover` already accepts `audit_clean` (reporting.py L372) — no signature change needed there; only the CLI call site changes.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_cutover_audit.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -k "report or cutover or reporting" -q`
Expected: no regressions.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/reporting.py src/applypilot/cli.py tests/test_v2_cutover_audit.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "report: auto-compute v2-cutover §12.3 safety audit (ledger dangling/duplicate + flight-recorder canary-violation scan) so the leg stops printing unknown"
```

---

## Task 3: Pre-flight probe (`preflight.py`, spec §6.2) — classify before parse; fold the expired-redirect guard into it

A ~2s classifier run on the navigated page BEFORE any parse work: `{ats_kind, login_wall, captcha_present, sso_gate, job_expired, form_frame_path}`. It converts v1's 216–432s discoveries into <5s named terminal states. Tonight's landed expired-redirect guard (`freshness.is_greenhouse_expired_redirect`, wired at launcher L3449-3457) becomes ONE member of a named terminal-state family the probe returns, so the orchestrator has a single place that decides "don't even parse this" and returns a bucket-B terminal instead of a mislabeled parse failure.

**Files:**
- Create: `src/applypilot/apply/v2/preflight.py`
- Modify: `src/applypilot/apply/v2/orchestrator.py` (call `probe` before parse; map terminal states)
- Test: `tests/test_v2_preflight.py`

- [ ] **Step 1: READ the observation contract + the expired helper + the existing terminal statuses**

READ `src/applypilot/apply/browser_stream.py:57-99` — `ControlObservation` (control_type, `value`, `frame_index`, `frame_url`) and `BrowserObservation` (`url`, `controls`, `submit_buttons`, `page_text_sample`, `validation_errors`), plus `collect_browser_observation(page, ...)` (L429). READ `src/applypilot/freshness.py:56-` — `is_greenhouse_expired_redirect(intended_url, landed_url) -> bool` (conservative; False unless a clear soft-302-to-board-index signature). READ `src/applypilot/apply/prefill.py:103-129` — `_detect_ats(url) -> "greenhouse"|"ashby"|"workday"|"unsupported"` (NOTE: no lever branch yet — Task 7 adds it; the probe calls `_detect_ats` so it inherits the fix for free). READ `src/applypilot/apply/launcher.py:3432-3457` — the existing expired short-circuit the probe SUBSUMES (the launcher path stays as the pre-CDP guard; the probe is the in-orchestrator classifier that also catches login/captcha/sso). Confirm the terminal statuses already in use (`failed:expired`, `captcha`, `login_issue` — launcher L3202) so the probe maps to the SAME vocabulary.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_preflight.py
from applypilot.apply.v2 import preflight as pf
from applypilot.apply.browser_stream import BrowserObservation, ControlObservation


def _obs(**kw):
    return BrowserObservation(**kw)


def test_classifies_ats_kind_from_url():
    r = pf.classify(intended_url="https://boards.greenhouse.io/acme/jobs/1",
                    landed_url="https://boards.greenhouse.io/acme/jobs/1",
                    obs=_obs(page_text_sample="Apply for this job", submit_buttons=[ControlObservation()]))
    assert r.ats_kind == "greenhouse"
    assert not r.job_expired and not r.login_wall
    assert r.terminal is None                     # clean -> proceed to parse


def test_expired_redirect_is_a_named_terminal():
    r = pf.classify(intended_url="https://boards.greenhouse.io/acme/jobs/1",
                    landed_url="https://job-boards.greenhouse.io/acme?error=true",
                    obs=_obs(page_text_sample="Open roles at Acme"))
    assert r.job_expired is True
    assert r.terminal == "failed:expired"         # bucket B, one named family


def test_login_wall_detected():
    obs = _obs(page_text_sample="Sign in to continue to your application",
               controls=[ControlObservation(control_type="password")])
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.login_wall is True and r.terminal == "login_issue"


def test_captcha_detected():
    obs = _obs(page_text_sample="Please verify you are human",
               controls=[ControlObservation(selector='iframe[src*="recaptcha"]')])
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.captcha_present is True and r.terminal == "captcha"


def test_sso_gate_detected():
    obs = _obs(page_text_sample="Continue with Google  Continue with Okta single sign-on")
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.sso_gate is True                       # sso is a login-family terminal
    assert r.terminal == "login_issue"


def test_form_frame_path_from_iframe_embed():
    obs = _obs(submit_buttons=[ControlObservation(frame_index=1, frame_url="https://boards.greenhouse.io/embed/job_app?token=abc")],
               controls=[ControlObservation(frame_index=1, frame_url="https://boards.greenhouse.io/embed/job_app?token=abc")])
    r = pf.classify(intended_url="u", landed_url="u", obs=obs)
    assert r.form_frame_path == ("https://boards.greenhouse.io/embed/job_app",)  # query stripped


def test_probe_live_wraps_collect(monkeypatch):
    # probe(page, ...) collects an observation then classifies; a collect failure
    # returns a benign result (terminal=None) so the caller falls through to parse
    # (which owns its own fail-open) rather than the probe hard-failing.
    class _P:
        url = "https://boards.greenhouse.io/acme/jobs/1"
    monkeypatch.setattr(pf, "collect_browser_observation",
                        lambda page, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = pf.probe(_P(), intended_url=_P.url)
    assert r.terminal is None and r.ats_kind == "greenhouse"
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: applypilot.apply.v2.preflight`)

Run: `& $PY -m pytest tests/test_v2_preflight.py -v`

- [ ] **Step 4: Implement `preflight.py`**

```python
"""Pre-flight probe (spec §6.2): classify a navigated page BEFORE any parse work
into {ats_kind, login_wall, captcha_present, sso_gate, job_expired,
form_frame_path}. Pure over a BrowserObservation + the intended/landed URLs; the
live wrapper collect-then-classifies. Terminal states map to the SAME status
vocabulary the launcher already promotes (failed:expired / captcha /
login_issue). The expired-redirect guard (freshness.is_greenhouse_expired_redirect)
is folded in as ONE member of the named terminal family — the probe is the single
'don't even parse this' authority."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from applypilot.apply.browser_stream import collect_browser_observation
from applypilot.apply.prefill import _detect_ats
from applypilot.freshness import is_greenhouse_expired_redirect

_CAPTCHA_RE = re.compile(r"recaptcha|hcaptcha|are you human|verify you are|cf-challenge|turnstile", re.I)
_LOGIN_RE = re.compile(r"\bsign in\b|\blog in\b|\blogin\b|create an account|password", re.I)
_SSO_RE = re.compile(r"single sign-on|continue with (google|okta|microsoft|azure|sso)|saml", re.I)


@dataclass
class ProbeResult:
    ats_kind: str = "unsupported"
    login_wall: bool = False
    captcha_present: bool = False
    sso_gate: bool = False
    job_expired: bool = False
    form_frame_path: tuple[str, ...] = ()
    terminal: str | None = None          # failed:expired | captcha | login_issue | None


def _stable_frame(url: str) -> str:
    return (url or "").split("?", 1)[0].split("#", 1)[0]


def classify(*, intended_url: str, landed_url: str, obs) -> ProbeResult:
    r = ProbeResult(ats_kind=_detect_ats(landed_url or intended_url or ""))
    text = (getattr(obs, "page_text_sample", "") or "")
    controls = list(getattr(obs, "controls", []) or [])
    submits = list(getattr(obs, "submit_buttons", []) or [])

    # (1) expired: the conservative soft-302 signature (bucket B, irreducible).
    if is_greenhouse_expired_redirect(intended_url, landed_url):
        r.job_expired = True
        r.terminal = "failed:expired"
        return r                              # nothing else matters — the req is dead

    # (2) captcha: text markers OR a captcha iframe among the controls.
    if _CAPTCHA_RE.search(text) or any(_CAPTCHA_RE.search(c.selector or "") for c in controls):
        r.captcha_present = True
        r.terminal = "captcha"
        return r

    # (3) sso gate: an explicit SSO prompt (a login-family terminal).
    if _SSO_RE.search(text):
        r.sso_gate = True
        r.terminal = "login_issue"
        return r

    # (4) login wall: a password field OR a sign-in prompt with NO application form.
    has_password = any((c.control_type or "").lower() == "password" for c in controls)
    if has_password or (_LOGIN_RE.search(text) and not submits):
        r.login_wall = True
        r.terminal = "login_issue"
        return r

    # (5) form frame path: the (stable) frame the submit/controls live in, if embedded.
    for c in submits + controls:
        if getattr(c, "frame_index", 0) and getattr(c, "frame_url", ""):
            r.form_frame_path = (_stable_frame(c.frame_url),)
            break
    return r                                  # terminal is None -> proceed to parse


def probe(page, *, intended_url: str | None = None) -> ProbeResult:
    """Live wrapper: collect an observation, then classify. A collect failure is
    benign — return a non-terminal result so the caller falls through to parse
    (which owns its own fail-open, invariant 2). ~2s budget: this is one
    observation collect, no dropdown opens, no fill."""
    landed = getattr(page, "url", "") or (intended_url or "")
    try:
        obs = collect_browser_observation(page)
    except Exception:                          # noqa: BLE001 — never hard-fail the probe
        return ProbeResult(ats_kind=_detect_ats(landed))
    return classify(intended_url=intended_url or landed, landed_url=landed, obs=obs)
```

Then in `run_form_compiler` (orchestrator), call the probe right after `url` is resolved and BEFORE `st.parse`, mapping a terminal to the appropriate return (adapt to the control flow from Task 1):

```python
    # PRE-FLIGHT PROBE (spec §6.2) — classify before spending parse budget. A
    # terminal probe state short-circuits to a NAMED bucket-B/irreducible status
    # (never the sentinel: these are true terminals, not fail-open-to-legacy —
    # legacy would just re-hit the same wall). Guarded: a probe crash proceeds
    # to parse (which fails open on its own).
    try:
        pr = preflight.probe(page, intended_url=url)
    except Exception:                          # noqa: BLE001
        pr = None
    if pr is not None and pr.terminal:
        ms = _ms()
        if pr.terminal.startswith("failed:") or pr.terminal in ("captcha", "login_issue"):
            return pr.terminal, ms, None       # worker_loop promotes these to permanent buckets
```

Notes / adapt warnings:
- The probe RETURNS a terminal status string that the worker loop already knows how to promote (`captcha`/`login_issue`/`failed:expired` are in the launcher's promote set, L3121/L3202). Returning `None` prefill is correct — no submit fired, nothing to reconcile; the launcher's `_make_v2_production_fn` already releases the pre-submit INTENT for the expired case (L3455) — for the probe's login/captcha terminals, add the SAME `_release_presubmit_intent(dec.ledger, ...)` call in the production closure when `run_form_compiler` returns a `captcha`/`login_issue`/`failed:expired` status (these are provably pre-submit). Wire that release in Task 7 alongside the routing changes, and note it here as the cross-task dependency.
- Keep the launcher's existing pre-CDP expired guard (L3449-3457) — it fires BEFORE the CDP page even loads the form, which is cheaper. The probe is the in-orchestrator second line that ALSO catches login/captcha/sso once the page is up. Both use the same `is_greenhouse_expired_redirect`, so there is one definition of "expired".
- `_detect_ats` is Greenhouse/Ashby/Workday-only until Task 7 adds Lever — the probe's `ats_kind` will read "unsupported" for Lever until then. That is fine: Task 7 lands the Lever branch and the probe inherits it.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_preflight.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/test_v2_orchestrator.py -q`
Expected: no regressions (the injected-stage tests pass a bare `object()` page → `probe` swallows the collect failure → `terminal=None` → parse proceeds).

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/preflight.py src/applypilot/apply/v2/orchestrator.py tests/test_v2_preflight.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: pre-flight probe (§6.2) — classify {ats_kind,login_wall,captcha,sso,expired,form_frame_path} before parse; fold expired-redirect guard into one named terminal-state family"
```

---

## Task 4: Live-DOM probe harness (`apply probe-form <url>`) — empirical grounding for Ashby/Lever

**No Ashby/Lever widget census exists** (fact-swept: `discovery/atlas/telemetry.py` is funnel-only; its docstring explicitly defers Ashby/Lever parse-gap/fingerprint rates to Phase 3; the only Ashby artifacts on disk are v1 Claude-agent transcripts, not a structured census). So before writing a single markup assumption into `frontend_ashby.py`/`frontend_lever.py`, the implementer MUST dump a real observation from live forms — exactly the successful live-probe pattern used tonight. This task ships a small dev/ops command that navigates to a URL, runs `collect_browser_observation`, and writes the raw observation (controls + widget kinds + selectors + frame paths + submit buttons) to a JSON the parser tasks read as ground truth. The live DB holds **5,955 Ashby** and **637 Lever** candidate rows to sample from.

**Files:**
- Create: `src/applypilot/apply/probe_dump.py`
- Modify: `src/applypilot/cli.py` (add `apply probe-form` under a small `apply` Typer sub-app, or a top-level `probe-form` command)
- Test: `tests/test_probe_dump.py`

- [ ] **Step 1: READ the observation collector + a URL-sampling query + the CLI nesting**

READ `src/applypilot/apply/browser_stream.py:429-` (`collect_browser_observation(page, *, tabs=None) -> BrowserObservation`) and the `ControlObservation` fields (L57-72) the dump serializes. READ `src/applypilot/apply/launcher.py:3269-3283` (`_v2_connect_page(port, apply_url)`) — the CDP-connect pattern the harness reuses to drive a live page (or a standalone `sync_playwright().chromium.launch()` for an out-of-band probe that doesn't need the worker's Chrome). READ `src/applypilot/cli.py:27` (`console`), `:86-87`/`:181-182` (the `add_typer` nesting pattern), `:66-73` (`_bootstrap`). Confirm there is NO existing `probe-form`/`probe_dump` symbol.

- [ ] **Step 2: Write failing tests** (the SERIALIZER is pure + unit-testable; the live drive is excluded from the offline suite)

```python
# tests/test_probe_dump.py
import json

from applypilot.apply import probe_dump as pd
from applypilot.apply.browser_stream import BrowserObservation, ControlObservation


def test_serialize_observation_shape(tmp_path):
    obs = BrowserObservation(
        url="https://jobs.ashbyhq.com/acme/app",
        controls=[
            ControlObservation(label="Name", control_type="text", selector="#name",
                               required=True, frame_index=0, frame_url="https://jobs.ashbyhq.com/acme/app"),
            ControlObservation(label="Location", role="combobox", control_type="text",
                               selector="div._select_abc", frame_index=0, frame_url="x"),
        ],
        submit_buttons=[ControlObservation(label="Submit Application", control_type="button", selector="button[type=submit]")],
        page_text_sample="Apply to Acme",
    )
    out = pd.serialize(obs, ats="ashby", url="https://jobs.ashbyhq.com/acme/app")
    assert out["ats"] == "ashby"
    assert out["counts"]["controls"] == 2
    labels = [c["label"] for c in out["controls"]]
    assert "Name" in labels and "Location" in labels
    # widget-kind HINT is included so the parser author sees what browser_stream saw
    assert all("control_type" in c and "role" in c and "selector" in c for c in out["controls"])
    assert out["submit_buttons"][0]["label"] == "Submit Application"


def test_dump_writes_json(tmp_path):
    obs = BrowserObservation(url="u", controls=[ControlObservation(label="Email", control_type="text")])
    path = pd.dump(obs, ats="lever", url="u", out_dir=tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ats"] == "lever" and data["counts"]["controls"] == 1
    assert path.parent == tmp_path
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: applypilot.apply.probe_dump`)

Run: `& $PY -m pytest tests/test_probe_dump.py -v`

- [ ] **Step 4: Implement `probe_dump.py` + the CLI command**

```python
"""Live-DOM probe harness (spec §13 Phase 4 empiricism, invariant 12). Navigate a
live application URL, collect ONE BrowserObservation, and dump the raw controls +
widget hints + selectors + frame paths to JSON. This is the ground truth the
Ashby/Lever front-end tasks read BEFORE writing a parser — no widget census
exists, so markup assumptions are empirical, never guessed. Dev/ops only; NOT on
any apply hot path."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from applypilot.apply.browser_stream import collect_browser_observation
from applypilot.apply.prefill import _detect_ats


def serialize(obs, *, ats: str, url: str) -> dict:
    def _ctrl(c) -> dict:
        return {"label": c.label, "role": c.role, "control_type": c.control_type,
                "selector": c.selector, "required": c.required, "visible": c.visible,
                "frame_index": c.frame_index, "frame_url": c.frame_url}
    controls = [_ctrl(c) for c in (obs.controls or [])]
    return {
        "ats": ats, "url": url, "captured_at": datetime.now(timezone.utc).isoformat(),
        "page_text_sample": (obs.page_text_sample or "")[:2000],
        "counts": {"controls": len(controls), "submit_buttons": len(obs.submit_buttons or [])},
        "controls": controls,
        "submit_buttons": [_ctrl(b) for b in (obs.submit_buttons or [])],
        "validation_errors": list(obs.validation_errors or []),
    }


def dump(obs, *, ats: str, url: str, out_dir) -> Path:
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    stem = f"{ats}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    path = out / f"{stem}.json"
    path.write_text(json.dumps(serialize(obs, ats=ats, url=url), indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def probe_url(url: str, *, out_dir, headless: bool = True) -> Path:
    """Standalone live probe: launch a throwaway headless Chromium, navigate,
    collect, dump. Out-of-band from the worker's persistent Chrome so it never
    touches a live apply session."""
    from playwright.sync_api import sync_playwright
    ats = _detect_ats(url)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless)
        page = b.new_context().new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        obs = collect_browser_observation(page)
        b.close()
    return dump(obs, ats=ats, url=url, out_dir=out_dir)
```

CLI (add near the `atlas`/`fixtures` sub-apps in `cli.py`):

```python
@app.command("probe-form")
def probe_form(
    url: str = typer.Argument(..., help="A live application URL (Ashby/Lever/Greenhouse)."),
    out_dir: str = typer.Option("docs/superpowers/probes", "--out", help="Where to write the observation JSON."),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Dump a live form's raw observation (controls, widget kinds, selectors,
    frame paths) to JSON — the empirical ground truth for a new front-end parser.
    Dev/ops only; no apply, no submission."""
    _bootstrap()
    from applypilot.apply.probe_dump import probe_url
    path = probe_url(url, out_dir=out_dir, headless=headless)
    console.print(f"Probe written to [bold]{path}[/bold]")
```

Notes / adapt warnings:
- `probe_url` (the live drive) is NOT unit-tested — it needs Chromium + network. The tests cover `serialize`/`dump` (pure). Do NOT add a live test to the offline suite.
- The implementer RUNS this against a handful of live Ashby URLs (sample from the DB: `SELECT application_url FROM jobs WHERE application_url LIKE '%jobs.ashbyhq.com%' AND apply_status IS NULL LIMIT 10`) and live Lever URLs before Tasks 5/6. The probe JSONs land in `docs/superpowers/probes/` and are the fact-source the parser READ steps cite. They are dev artifacts — commit them alongside the parser OR keep them local; either way the parser's synonym tables and widget mappings must trace to a real probe.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_probe_dump.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/probe_dump.py src/applypilot/cli.py tests/test_probe_dump.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "apply: live-DOM probe harness ('applypilot probe-form <url>') — empirical observation dump grounding the Ashby/Lever front-ends (no widget census exists)"
```

---

## Task 5: Ashby front-end (`frontend_ashby.py`) — probe-first observation→IR parser

A pure `parse_observation(obs, *, company, url) -> ir.FormSchema` for `jobs.ashbyhq.com` forms, structurally identical in shape to `frontend_greenhouse.parse_observation` (invariant 13) but with Ashby-specific widget classification + a taxonomy grounded in the Task-4 probe. Ashby renders a React SPA (`_select_*` combobox classes, a custom file dropzone, native selects for some EEO), so the widget classifier and the react-select inner-input suppression are re-tuned from what the probe actually shows — NOT copied blind from Greenhouse. Reuses `ir`, the semantic-key taxonomy shape, and `_locator_spec`/`_frame_path` helpers.

**Files:**
- Create: `src/applypilot/apply/v2/frontend_ashby.py`
- Test: `tests/test_v2_frontend_ashby.py`

- [ ] **Step 1: READ the Greenhouse front-end (the template) + the IR + the LIVE Ashby probe**

READ `src/applypilot/apply/v2/frontend_greenhouse.py` in full (272 lines) — the structure to mirror: `_SEMANTIC_SYNONYMS` (L18-37), `_semantic_key` (L61-68), `_widget_kind` (L85-105), `_is_react_select_inner` + `_bbox_contains` (L164-211), `_to_field` (L214-239), `parse_observation` (L242-272). READ `src/applypilot/apply/v2/ir.py:48-108` — `Widget`, `Field`, `field_fp`, `WidgetKind.ALL`. **READ the Task-4 Ashby probe JSON(s) under `docs/superpowers/probes/ashby_*.json`** — this is the empirical ground truth (invariant 12). From the probe, record the ACTUAL: (a) Ashby's combobox marker (its `role`/`selector` token — Ashby uses `_select_`/`_input_` CSS module hashes, not Greenhouse's `select__`); (b) whether Ashby file upload surfaces as `control_type="file"` or a custom dropzone `<button>`; (c) Ashby's required-marker; (d) whether custom questions carry a stable id or an autogen hash. If a probe has NOT been run yet, STOP and run Task 4 against ≥5 live Ashby forms first — do not write the classifier from memory.

- [ ] **Step 2: Write failing tests** (assertions keyed to the probe; the synthetic DOM below MIRRORS a real Ashby probe — adjust the markers to your actual probe)

```python
# tests/test_v2_frontend_ashby.py
import pytest

from applypilot.apply.v2 import frontend_ashby as fe
from applypilot.apply.v2 import ir
from applypilot.apply.browser_stream import collect_browser_observation


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


# Synthetic Ashby form mirroring the Task-4 probe markup (jobs.ashbyhq.com).
_ASHBY_HTML = """
<form>
  <label for="_systemfield_name">Name</label>
  <input id="_systemfield_name" type="text" required />
  <label for="_systemfield_email">Email</label>
  <input id="_systemfield_email" type="email" required />
  <label for="_systemfield_resume">Resume</label>
  <input id="_systemfield_resume" type="file" />
  <label for="loc">Location</label>
  <div class="_container_x"><input id="loc" role="combobox" class="_input_9ab2" /></div>
  <label for="q_custom_1">Why Acme?</label>
  <textarea id="q_custom_1" required></textarea>
  <button type="submit">Submit Application</button>
</form>
"""


def test_ashby_parses_standard_semantic_keys(page):
    page.set_content(_ASHBY_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="https://jobs.ashbyhq.com/acme/app")
    assert schema.ats == "ashby"
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert {"first_name", "email", "resume"} <= keys or {"email", "resume"} <= keys


def test_ashby_location_is_react_select_options_lazy(page):
    page.set_content(_ASHBY_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    loc = [f for s in schema.steps for f in s.fields if f.semantic_key == "location"]
    assert loc and loc[0].widget.kind == "react_select"
    assert loc[0].options is ir.LAZY               # never enumerated at parse (invariant 4)


def test_ashby_custom_question_keyed_custom(page):
    page.set_content(_ASHBY_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    cust = [f for s in schema.steps for f in s.fields if (f.semantic_key or "").startswith("custom.")]
    assert cust and cust[0].widget.kind == "textarea"


def test_ashby_single_terminal_step_with_submit(page):
    page.set_content(_ASHBY_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    assert len(schema.steps) == 1 and schema.steps[0].terminal
    assert schema.steps[0].advance_control is not None
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: applypilot.apply.v2.frontend_ashby`)

Run: `& $PY -m pytest tests/test_v2_frontend_ashby.py -v`

- [ ] **Step 4: Implement `frontend_ashby.py`** (adapt the widget markers to your probe)

```python
"""Ashby front-end: BrowserObservation -> FormSchema IR (spec §6.3). PURE
translation (no browser I/O). Mirrors frontend_greenhouse structurally
(invariant 13) but classifies Ashby's React widgets from the Task-4 probe:
Ashby combobox = role='combobox' OR a CSS-module '_select_/_input_' token;
file = <input type=file> OR the dropzone button; system fields carry a stable
'_systemfield_' id. Options are LAZY at parse (invariant 4)."""
from __future__ import annotations

import re

from applypilot.apply.v2 import ir
# Reuse the Greenhouse helpers that are genuinely ATS-neutral (locator/frame).
from applypilot.apply.v2.frontend_greenhouse import (
    _locator_spec, _frame_path, _parse_selector, _norm, _custom_key, _IDENTITY_KEYS,
    _is_checkbox_like,
)

# Grounded in the Ashby probe (docs/superpowers/probes/ashby_*.json). Order:
# most-specific first (mirrors the Greenhouse ordering note).
_SEMANTIC_SYNONYMS = [
    ("first_name", ("first name", "legal first name")),
    ("last_name", ("last name", "surname")),
    ("email", ("email",)),
    ("phone", ("phone", "mobile")),
    ("location", ("location", "city", "where are you based")),
    ("linkedin", ("linkedin",)),
    ("portfolio", ("portfolio", "website", "personal site")),
    ("resume", ("resume", "cv", "resume/cv")),
    ("work_auth", ("authorized to work", "legally authorized", "work authorization",
                   "eligible to work")),
    ("sponsorship", ("require sponsorship", "need sponsorship", "sponsorship", "visa")),
    ("eeo.gender", ("gender",)),
    ("eeo.race", ("race", "ethnicity")),
    ("eeo.veteran", ("veteran",)),
    ("eeo.disability", ("disability",)),
]

# Ashby's CSS-module combobox/select token (from the probe — a hashed '_select_'
# / '_input_' class on a role=combobox wrapper). Re-confirm against your probe.
_ASHBY_SELECT_RE = re.compile(r"_select_|_input_", re.I)


def _semantic_key(label: str, question: str) -> str | None:
    hay = _norm(f"{label} {question}")
    if not hay:
        return None
    for key, needles in _SEMANTIC_SYNONYMS:
        if any(n in hay for n in needles):
            return key
    return None


def _widget_kind(ctrl) -> str:
    t = (ctrl.control_type or "").lower()
    role = (ctrl.role or "").lower()
    selector = (ctrl.selector or "").lower()
    if t == "file":
        return "file"
    if t == "textarea":
        return "textarea"
    if t in ("select", "select-one", "select-multiple", "native_select"):
        return "native_select"
    if role == "combobox" or _ASHBY_SELECT_RE.search(selector) or t == "combobox":
        return "react_select"
    if t == "radio":
        return "radio_group"
    if t == "checkbox":
        return "checkbox"
    if t == "date":
        return "date"
    return "text"


def _to_field(ctrl) -> ir.Field:
    label = ctrl.label or ""
    kind = _widget_kind(ctrl)
    sem = _semantic_key(label, label)
    if sem in _IDENTITY_KEYS and _is_checkbox_like(ctrl, kind):
        sem = None                                    # checkbox label-bleed guard (reused)
    sem = sem or _custom_key(label)
    return ir.Field(
        field_id=ctrl.control_id or ctrl.selector or "",
        frame_path=_frame_path(ctrl),
        label_text=label, question_text=label, semantic_key=sem,
        widget=ir.Widget(kind=kind), options=ir.LAZY,
        required=bool(ctrl.required), char_limit=None,
        locator_spec=_locator_spec(ctrl),
    )


def parse_observation(obs, *, company: str, url: str) -> ir.FormSchema:
    """Ashby is a single-page React form -> one terminal Step (mirrors Greenhouse;
    the executor loop lets a future multi-step Ashby drop in without a rewrite)."""
    candidates = [c for c in obs.controls if c.visible and (c.label or c.control_id)]
    fields = [_to_field(c) for c in candidates]
    advance = None
    if obs.submit_buttons:
        b = obs.submit_buttons[0]
        b_id, b_name = _parse_selector(b.selector)
        advance = {"role": "button", "name": b.label or "Submit Application",
                   "label": b.label or "Submit Application", "text": b.label or None,
                   "elem_id": b_id, "name_attr": b_name, "selector": b.selector or None,
                   "frame_url": b.frame_url or None}
    step = ir.Step(index=0, fields=fields, advance_control=advance, terminal=True)
    return ir.FormSchema(ats="ashby", company=company, url=url, steps=[step])
```

Notes / adapt warnings:
- The imports of `_norm`/`_custom_key`/`_IDENTITY_KEYS`/`_is_checkbox_like`/`_locator_spec`/`_frame_path`/`_parse_selector` from `frontend_greenhouse` are the genuinely ATS-neutral helpers — confirm they are module-level (they are, per Task-5 Step-1 READ). If any is not importable, promote it to a shared `frontend_common.py` in this task and note it. Do NOT copy the Greenhouse react-select-inner suppression blind — run the probe to see whether Ashby even renders a phantom inner input; add the suppression ONLY if the probe shows one.
- `_ASHBY_SELECT_RE` MUST match your actual probe's combobox class token. The `_select_`/`_input_` guess is a placeholder — replace with the real hashed prefix from `docs/superpowers/probes/ashby_*.json`.
- The taxonomy is grounded in the probe. If the probe shows Ashby uses different label phrasing (e.g. "Full name" as one field, not first/last), adjust `_SEMANTIC_SYNONYMS` and the test's expected keys accordingly.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_frontend_ashby.py -v`
Expected: ALL PASS (against synthetic DOM mirroring the probe).

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/frontend_ashby.py tests/test_v2_frontend_ashby.py docs/superpowers/probes/ashby_*.json
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: Ashby front-end (frontend_ashby.py) — probe-grounded observation->IR parser (jobs.ashbyhq.com), options LAZY, taxonomy reuse"
```

---

## Task 6: Lever front-end (`frontend_lever.py`) — same probe-first pattern

The Lever twin of Task 5 for `jobs.lever.co`. Lever's application form is a simpler server-rendered form (native `<select>` for many dropdowns, `<input type=file>` resume, cards for custom questions) — but that is a HYPOTHESIS until the Task-4 probe confirms it. Same structure, same reuse; Lever-specific widget classification grounded in the probe. The live DB has 637 Lever candidates to sample.

**Files:**
- Create: `src/applypilot/apply/v2/frontend_lever.py`
- Test: `tests/test_v2_frontend_lever.py`

- [ ] **Step 1: READ the Ashby front-end (now the second template) + the LIVE Lever probe**

READ `src/applypilot/apply/v2/frontend_ashby.py` (from Task 5) — the two-front-end shape to generalize. **READ the Task-4 Lever probe JSON(s) under `docs/superpowers/probes/lever_*.json`.** Record from the probe: (a) whether Lever dropdowns are native `<select>` (→ `native_select`, the easy path) or a JS widget; (b) resume upload control type; (c) Lever's field naming (`name="cards[...]"` / `name="urls[LinkedIn]"` custom-question pattern); (d) required marker. If no Lever probe exists yet, run Task 4 against ≥5 live `jobs.lever.co` forms first (invariant 12).

- [ ] **Step 2: Write failing tests** (mirror Task 5; markers from the Lever probe)

```python
# tests/test_v2_frontend_lever.py
import pytest

from applypilot.apply.v2 import frontend_lever as fe
from applypilot.apply.v2 import ir
from applypilot.apply.browser_stream import collect_browser_observation


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


# Synthetic Lever form mirroring the Task-4 probe (jobs.lever.co).
_LEVER_HTML = """
<form>
  <label for="name">Full name</label>
  <input id="name" name="name" type="text" required />
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required />
  <label for="resume">Resume/CV</label>
  <input id="resume" name="resume" type="file" />
  <label for="loc">Location</label>
  <select id="loc" name="cards[location]"><option>Select</option><option>Remote</option></select>
  <label for="lk">LinkedIn URL</label>
  <input id="lk" name="urls[LinkedIn]" type="text" />
  <label for="q1">Why Lever?</label>
  <textarea id="q1" name="cards[abc][field0]" required></textarea>
  <button type="submit">Submit application</button>
</form>
"""


def test_lever_parses_standard_and_native_select(page):
    page.set_content(_LEVER_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="https://jobs.lever.co/acme/1")
    assert schema.ats == "lever"
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert {"email", "resume"} <= keys
    loc = [f for s in schema.steps for f in s.fields if f.semantic_key == "location"]
    assert loc and loc[0].widget.kind == "native_select"     # Lever native <select>
    assert loc[0].options is ir.LAZY                          # still LAZY at parse (invariant 4)


def test_lever_linkedin_keyed_and_custom_textarea(page):
    page.set_content(_LEVER_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert "linkedin" in keys
    cust = [f for s in schema.steps for f in s.fields if (f.semantic_key or "").startswith("custom.")]
    assert cust and cust[0].widget.kind == "textarea"


def test_lever_single_terminal_step(page):
    page.set_content(_LEVER_HTML)
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company="acme", url="u")
    assert len(schema.steps) == 1 and schema.steps[0].terminal
    assert schema.steps[0].advance_control is not None
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_frontend_lever.py -v`

- [ ] **Step 4: Implement `frontend_lever.py`**

Structurally identical to `frontend_ashby.py` — reuse the same neutral helpers, a Lever-tuned `_SEMANTIC_SYNONYMS` (grounded in the probe), and a `_widget_kind` that maps Lever's native `<select>` → `native_select` (browser_stream reports it as `control_type in ("select","select-one")` AND `role="combobox"`, so — as in Greenhouse — the native-select check MUST precede the combobox check). Emit `ats="lever"`. Do NOT invent a react-select suppression unless the probe shows a JS widget. Keep `parse_observation`'s signature identical (invariant 13). (Full listing mirrors Task 5's `frontend_ashby.py` — swap the ATS string, the synonym table, and the widget markers to the Lever probe.)

Notes / adapt warnings:
- If the probe shows Lever custom questions carry NO useful `<label>` (only a `name="cards[...]"`), derive `question_text` from the nearest text node the observation captured; `_custom_key` will slug whatever `label` is present. Note any gap.
- If Lever uses native selects throughout, the `react_select` driver is never invoked for Lever and the sleep-tax risk (drivers.py L13-18) does not apply — a speed win worth noting for the §12.2 p50.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_frontend_lever.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/frontend_lever.py tests/test_v2_frontend_lever.py docs/superpowers/probes/lever_*.json
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: Lever front-end (frontend_lever.py) — probe-grounded observation->IR parser (jobs.lever.co), native-select mapping, options LAZY"
```

---

## Task 7: ATS routing registry + `_v2_supported_ats` per-ATS shadow flags (+ Lever `_detect_ats` branch)

Wire the three front-ends behind a registry so `run_form_compiler` parses with the right dialect, and widen the dispatch gate so Ashby/Lever can each shadow INDEPENDENTLY behind a fresh-read per-ATS allowlist (invariant 11). The gate `_is_greenhouse` becomes `_v2_supported_ats(job)` reading `APPLYPILOT_V2_ATS` (comma-list; unset ⇒ `greenhouse` only, byte-for-byte the Phase-3 behavior). Also add the missing Lever branch to `prefill._detect_ats` and thread per-ATS `tier_used` + `NetworkEvidence(ats=...)`.

**Flag decision (justified):** ONE fresh-read `APPLYPILOT_V2_ATS` comma-separated allowlist, NOT per-ATS envs. Rationale: (a) it mirrors the single-env `APPLYPILOT_V2_ENGINE` gate the operator already flips; (b) one place to read, no env-var sprawl as ATSes grow; (c) each ATS still burns in independently — add `ashby` to the list to shadow Ashby without touching Lever; (d) default-empty maps to `greenhouse` so the current shadow is preserved with zero config. `APPLYPILOT_V2_ENGINE` remains the master on/off; `APPLYPILOT_V2_ATS` scopes WHICH ATSes v2 handles when the master is on.

**Files:**
- Modify: `src/applypilot/apply/v2/__init__.py` (`V2_ATS_ENV`, `V2_TIER_LABELS`)
- Create: `src/applypilot/apply/v2/frontends.py` (`parser_for(ats)` registry)
- Modify: `src/applypilot/apply/v2/orchestrator.py` (`_default_parse` uses the registry)
- Modify: `src/applypilot/apply/prefill.py` (`LEVER_HOSTS` + Lever branch)
- Modify: `src/applypilot/apply/launcher.py` (`_v2_supported_ats`, per-ATS `tier_used`/`NetworkEvidence`, pre-submit release for probe terminals)
- Test: `tests/test_v2_frontends_registry.py`, `tests/test_v2_ats_gate.py`

- [ ] **Step 1: READ the current gate + parse wiring + detect_ats**

READ `src/applypilot/apply/launcher.py:3254-3266` (`_v2_enabled`, `_is_greenhouse`), `:3459-3478` (`NetworkEvidence(ats="greenhouse", company=company)` + the `run_form_compiler` call), `:3523-3537` (`_v2_enabled() and _is_greenhouse(job)` + `tier_used="v2_greenhouse"`), `:3730` (the worker-loop `_v2_gate`). READ `src/applypilot/apply/v2/orchestrator.py:149-153` (`_default_parse` hardcodes `frontend_greenhouse.parse_observation`). READ `src/applypilot/apply/prefill.py:25-27` (`GREENHOUSE_HOSTS`/`ASHBY_HOSTS`/`WORKDAY_HOSTS` — NO `LEVER_HOSTS`) and `:103-129` (`_detect_ats` — no lever branch). READ `src/applypilot/apply/v2/__init__.py` (current constants: `V2_ENGINE_ENV`, `V2_TIER_LABEL="v2_greenhouse"`).

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_frontends_registry.py
from applypilot.apply.v2 import frontends
from applypilot.apply.v2 import frontend_greenhouse, frontend_ashby, frontend_lever


def test_registry_routes_each_ats():
    assert frontends.parser_for("greenhouse") is frontend_greenhouse.parse_observation
    assert frontends.parser_for("ashby") is frontend_ashby.parse_observation
    assert frontends.parser_for("lever") is frontend_lever.parse_observation


def test_registry_unknown_ats_returns_none():
    assert frontends.parser_for("workday") is None
    assert frontends.parser_for("unsupported") is None
```

```python
# tests/test_v2_ats_gate.py
from applypilot.apply import launcher
from applypilot.apply.prefill import _detect_ats


def test_detect_ats_now_recognizes_lever():
    assert _detect_ats("https://jobs.lever.co/acme/1234") == "lever"
    assert _detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert _detect_ats("https://jobs.ashbyhq.com/acme/app") == "ashby"


def test_v2_supported_ats_default_is_greenhouse_only(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    monkeypatch.delenv("APPLYPILOT_V2_ATS", raising=False)
    assert launcher._v2_supported_ats({"application_url": "https://boards.greenhouse.io/a/jobs/1"})
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.ashbyhq.com/a/app"})
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.lever.co/a/1"})


def test_v2_supported_ats_allowlist_scopes_independently(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    monkeypatch.setenv("APPLYPILOT_V2_ATS", "greenhouse,ashby")
    assert launcher._v2_supported_ats({"application_url": "https://jobs.ashbyhq.com/a/app"})
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.lever.co/a/1"})   # lever not in list


def test_v2_supported_ats_off_when_engine_disabled(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_ENGINE", raising=False)
    monkeypatch.setenv("APPLYPILOT_V2_ATS", "greenhouse,ashby,lever")
    assert not launcher._v2_supported_ats({"application_url": "https://jobs.ashbyhq.com/a/app"})
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: frontends`; `_detect_ats` returns "unsupported" for lever; `_v2_supported_ats` missing)

Run: `& $PY -m pytest tests/test_v2_frontends_registry.py tests/test_v2_ats_gate.py -v`

- [ ] **Step 4: Implement the registry + the gate widening + the Lever detection**

`src/applypilot/apply/v2/frontends.py`:

```python
"""Front-end registry (spec §6.3): ats -> pure parse_observation fn. The single
place that maps an ATS dialect to its observation->IR parser. Adding an ATS is a
one-line registry entry + a frontend module (invariant 13)."""
from __future__ import annotations

from applypilot.apply.v2 import frontend_greenhouse, frontend_ashby, frontend_lever

_REGISTRY = {
    "greenhouse": frontend_greenhouse.parse_observation,
    "ashby": frontend_ashby.parse_observation,
    "lever": frontend_lever.parse_observation,
}


def parser_for(ats: str):
    """The parse_observation fn for `ats`, or None if v2 has no front-end for it
    (caller falls open to legacy — invariant 2)."""
    return _REGISTRY.get((ats or "").lower())
```

`src/applypilot/apply/v2/__init__.py` add:

```python
V2_ATS_ENV = "APPLYPILOT_V2_ATS"           # fresh-read per-ATS allowlist (comma list)
V2_TIER_LABELS = {"greenhouse": "v2_greenhouse", "ashby": "v2_ashby", "lever": "v2_lever"}
```

`src/applypilot/apply/v2/orchestrator.py` — route `_default_parse` via the registry (adapt L149-153):

```python
def _default_parse(page, company, url):
    from applypilot.apply.browser_stream import collect_browser_observation
    from applypilot.apply.prefill import _detect_ats
    from applypilot.apply.v2.frontends import parser_for
    obs = collect_browser_observation(page)
    parse = parser_for(_detect_ats(url)) or __import__(
        "applypilot.apply.v2.frontend_greenhouse", fromlist=["parse_observation"]).parse_observation
    return parse(obs, company=company, url=url)
```

`src/applypilot/apply/prefill.py` — add Lever detection (adapt L25-27 + L103-129):

```python
LEVER_HOSTS = ("jobs.lever.co", "lever.co")
# ... inside _detect_ats, after the ASHBY_HOSTS loop:
    for host in LEVER_HOSTS:
        if host in lowered:
            return "lever"
```

`src/applypilot/apply/launcher.py` — the gate widening (replace `_is_greenhouse` usage; keep `_is_greenhouse` as a thin alias for back-compat if referenced elsewhere):

```python
def _v2_supported_ats(job: dict) -> bool:
    """Per-ATS v2 gate (invariant 11). The master flag APPLYPILOT_V2_ENGINE must
    be on; then APPLYPILOT_V2_ATS (fresh-read comma allowlist) scopes WHICH ATSes
    v2 handles. Unset allowlist => 'greenhouse' only (the Phase-3 shadow, byte-
    for-byte). An ATS with no front-end (parser_for is None) is never supported."""
    if not _v2_enabled():
        return False
    from applypilot.apply.prefill import _detect_ats
    from applypilot.apply.v2 import V2_ATS_ENV
    from applypilot.apply.v2.frontends import parser_for
    ats = _detect_ats(job.get("application_url") or job.get("url") or "")
    if parser_for(ats) is None:
        return False
    raw = (os.environ.get(V2_ATS_ENV) or "").strip()
    allow = {a.strip().lower() for a in raw.split(",") if a.strip()} or {"greenhouse"}
    return ats in allow
```

Then:
- At the dispatch gate (L3523) replace `_v2_enabled() and _is_greenhouse(job)` with `_v2_supported_ats(job)`; at the worker-loop `_v2_gate` (L3730) do the same.
- In `_make_v2_production_fn` (L3459) set `ats = _detect_ats(dec.apply_url)` and pass `NetworkEvidence(ats=ats, company=company)`; the per-ATS `tier_used` is set by `run_form_compiler` (it already stamps `V2_TIER_LABEL` — change the orchestrator's `prefill["tier_used"]` to `V2_TIER_LABELS.get(schema.ats, "v2_greenhouse")` after parse). Adapt the `prefill.setdefault("tier_used", ...)` at L3536 to the ATS-specific label too.
- Extend the pre-submit INTENT release (Task 3 dependency): when `run_form_compiler` returns a probe terminal (`captcha`/`login_issue`/`failed:expired`), the production closure must `_release_presubmit_intent(dec.ledger, identity_id, dry_run, reason="v2_probe_terminal")` (provably pre-submit) before returning — mirror the existing expired release at L3455.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_frontends_registry.py tests/test_v2_ats_gate.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -k "dispatch or launcher or detect_ats or prefill" -q`
Expected: no regressions — especially confirm the flag-OFF path is still a byte-for-byte legacy passthrough (invariant 2/11) and every legacy prologue short-circuit is preserved (the safety-critical assertion).

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/frontends.py src/applypilot/apply/v2/__init__.py src/applypilot/apply/v2/orchestrator.py src/applypilot/apply/prefill.py src/applypilot/apply/launcher.py tests/test_v2_frontends_registry.py tests/test_v2_ats_gate.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: ATS routing registry + _v2_supported_ats per-ATS shadow flags (APPLYPILOT_V2_ATS allowlist, default greenhouse-only) + Lever _detect_ats branch + per-ATS tier_used/NetworkEvidence"
```

---

## Task 8: Driver long-tail from the Ashby/Lever probes (ONLY what the data shows is needed)

Spec §13 Phase 4 says the driver long-tail comes from "the shadow census" — which does NOT exist (invariant 12). So this task is DATA-DRIVEN by the Task-4/5/6 probes and the FIRST shadow runs, NOT speculative: add a WidgetDriver ONLY for a widget kind the Ashby/Lever probes actually surface that the current registry (`drivers._REGISTRY`, L391-402: text/textarea/file/react_select/native_select/typeahead_location/phone_intl/radio_group/checkbox/date) cannot handle with a read-back. If the probes show the existing ten drivers cover Ashby/Lever (likely for Lever's native selects; probable for Ashby's combobox via the existing `react_select` driver), this task is a NO-OP documented as such — do not gold-plate.

**Files:**
- Modify: `src/applypilot/apply/v2/drivers.py` (ONLY if a gap is found)
- Test: `tests/test_v2_drivers_longtail.py` (ONLY if a driver is added)

- [ ] **Step 1: READ the existing registry + diff against the probes**

READ `src/applypilot/apply/v2/drivers.py:391-416` (`_REGISTRY` + `commit`) and the per-driver read-back conventions (L89-388). READ every `docs/superpowers/probes/{ashby,lever}_*.json` widget kind produced by the Task-4 probe. Build the gap list: for each distinct `(control_type, role, selector-token)` the probes show, confirm `frontend_{ashby,lever}._widget_kind` maps it to a `WidgetKind.ALL` member that `_REGISTRY` handles. If EVERY observed widget maps to an existing driver, write NO code — record "no long-tail driver needed; the ten existing drivers cover the Ashby/Lever widget set observed in N probes" and skip to Step 6 with an empty commit-or-skip. If a gap exists (e.g. Ashby's custom file dropzone is a `<button>` not `<input type=file>`, so the `file` driver's `set_input_files` can't target it), proceed.

- [ ] **Step 2: Write failing tests** (EXAMPLE — only if the probe shows an Ashby dropzone-button file upload)

```python
# tests/test_v2_drivers_longtail.py  (create ONLY if a gap is found)
import pytest

from applypilot.apply.v2 import drivers
from applypilot.apply.v2.resolver import PlannedField
from applypilot.apply.v2 import ir


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


def test_ashby_dropzone_file_commits_with_readback(page, tmp_path):
    # Ashby renders resume upload as a dropzone <button> wrapping a hidden
    # <input type=file>; the driver must reach the hidden input and read back the
    # filename (invariant 5). Mirrors the real probe markup.
    f = tmp_path / "resume.pdf"; f.write_text("%PDF-1.4")
    page.set_content('<div class="_dropzone"><button>Upload</button>'
                     '<input type="file" style="display:none" data-testid="file-upload"></div>')
    fld = ir.Field(field_id="file-upload", frame_path=(), label_text="Resume",
                   question_text="Resume", semantic_key="resume",
                   widget=ir.Widget(kind="file"), options=ir.LAZY,
                   locator_spec={"selector": 'input[type=file]'})
    res = drivers.commit(page, PlannedField(fld, value=str(f), driver="file"))
    assert res.committed is True         # read-back saw the filename on the hidden input
```

- [ ] **Step 3: Run — expect failure** (only if a driver is added)

Run: `& $PY -m pytest tests/test_v2_drivers_longtail.py -v`

- [ ] **Step 4: Implement the specific driver(s) the probe demands**

Follow THIS module's conventions exactly: `heal` → act → genuine DOM read-back → `CommitResult(committed, tier)`; NO fixed sleeps in new code (invariant 9); options LAZY (invariant 4). Register the new driver key in `_REGISTRY` and route it from `resolver._driver_for` if it needs a new widget kind. Keep the change minimal — one driver per proven gap. (No reference impl is prescribed because the exact gap is probe-dependent; the existing `_file`/`_react_select` drivers at drivers.py L126-167 are the pattern to extend.)

- [ ] **Step 5: Run — expect pass** (only if a driver is added)

Run: `& $PY -m pytest tests/test_v2_drivers_longtail.py -v`
Run: `& $PY -m pytest tests/test_v2_drivers.py -q` (no regressions)

- [ ] **Step 6: Commit (or record the no-op)**

If a driver was added:
```powershell
git reset
git add src/applypilot/apply/v2/drivers.py tests/test_v2_drivers_longtail.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: driver long-tail from Ashby/Lever probes — <specific widget> read-back driver (data-driven, not speculative)"
```
If NO gap was found: make NO commit; record in the task summary "Task 8 no-op: the ten existing drivers cover the Ashby/Lever widget set observed across N probes (invariant 12 — data-driven, no speculative drivers added)."

---

## Task 9: Degraded tier — `GenericFrontend` + real `Operator.label_controls` (spec §6.8)

When a concrete front-end can't parse a form (DOM churn zeroing out a dialect — risk §15), a `GenericFrontend` builds a best-effort IR by asking the Operator to LABEL the unlabeled controls in one batched call, then flows that IR through the SAME canary-first resolver. `Operator.label_controls` is currently a `NotImplementedError` stub (operator.py L142-143) — this task gives it its real implementation. The degraded tier is explicitly COUNTED (a distinct `tier_used`) and CAPPED per run (invariant 14), and canary-safe: `label_controls` never sees a canary field and the resolver still short-circuits canary keys.

**Files:**
- Modify: `src/applypilot/apply/v2/operator.py` (implement `label_controls`)
- Create: `src/applypilot/apply/v2/frontend_generic.py`
- Modify: `src/applypilot/apply/v2/orchestrator.py` (fall to generic on concrete-parse failure; count + cap)
- Test: `tests/test_v2_degraded_tier.py`

- [ ] **Step 1: READ the Operator contract + a concrete front-end + the orchestrator parse gate**

READ `src/applypilot/apply/v2/operator.py:45-57` (the `Operator` Protocol + `_SYSTEM`), `:69-136` (`resolve_fields` + `_json_call` + `_validate` — the JSON-in/validated-JSON-out pattern to mirror), `:142-143` (the `label_controls` stub to replace). READ `src/applypilot/apply/v2/frontend_greenhouse.py:214-272` (`_to_field`/`parse_observation` — the IR shape the generic front-end must also emit). READ `src/applypilot/apply/v2/ir.py:41-45` (`is_canary_key` — the generic front-end must NOT hand canary-looking controls to the oracle) and `:54-66` (`Field`). READ `src/applypilot/apply/v2/orchestrator.py:149-153`/`223-227` (`_default_parse` + the PARSE try — where the generic fallback slots in).

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_degraded_tier.py
import json

from applypilot.apply.v2 import operator as op
from applypilot.apply.v2 import frontend_generic as fg
from applypilot.apply.v2 import ir
from applypilot.apply.browser_stream import BrowserObservation, ControlObservation


class _FakeClient:
    def __init__(self, reply): self._reply = reply; self.calls = []
    def chat(self, messages, temperature=0.0, max_tokens=4096, **kw):
        self.calls.append(messages); return self._reply


def test_label_controls_returns_labels_by_control_id():
    reply = json.dumps({"labels": [
        {"control_id": "c1", "label": "First name", "widget": "text"},
        {"control_id": "c2", "label": "Why us?", "widget": "textarea"},
    ]})
    operator = op.LLMOperator(client=_FakeClient(reply))
    snap = [{"control_id": "c1", "selector": "#a"}, {"control_id": "c2", "selector": "#b"}]
    labels = operator.label_controls(snap)
    assert labels["c1"]["label"] == "First name" and labels["c1"]["widget"] == "text"
    assert labels["c2"]["widget"] == "textarea"


def test_label_controls_invalid_json_returns_empty_not_crash():
    operator = op.LLMOperator(client=_FakeClient("not json"))
    assert operator.label_controls([{"control_id": "c1"}]) == {}


def test_generic_frontend_builds_ir_from_labels():
    obs = BrowserObservation(controls=[
        ControlObservation(control_id="c1", control_type="text", selector="#a", visible=True),
        ControlObservation(control_id="c2", control_type="textarea", selector="#b", visible=True),
    ], submit_buttons=[ControlObservation(label="Submit", selector="button")])

    class _Op:
        def label_controls(self, snapshot):
            return {"c1": {"label": "Email", "widget": "text"},
                    "c2": {"label": "Cover letter", "widget": "textarea"}}
    schema = fg.parse_observation(obs, company="acme", url="u", operator=_Op())
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    assert "email" in keys                                   # oracle label -> semantic key
    assert schema.ats == "generic"


def test_generic_frontend_never_labels_canary_via_oracle():
    # A control whose EXISTING label is canary-ish is NOT sent to label_controls;
    # generic only labels controls with no usable label, and canary keys still
    # resolve deterministically downstream (invariant 7/14).
    obs = BrowserObservation(controls=[
        ControlObservation(control_id="c1", label="Desired salary", control_type="text",
                           selector="#s", visible=True),
    ])
    sent = {}

    class _Op:
        def label_controls(self, snapshot):
            sent["ids"] = [s["control_id"] for s in snapshot]
            return {}
    fg.parse_observation(obs, company="acme", url="u", operator=_Op())
    assert "c1" not in sent.get("ids", [])                   # canary-labeled control not sent to oracle
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_degraded_tier.py -v`

- [ ] **Step 4: Implement `label_controls` + `frontend_generic.py`**

In `operator.py`, replace the stub (L142-143):

```python
    def label_controls(self, snapshot) -> dict:
        """Degraded tier (spec §6.8): label a batch of unlabeled controls in ONE
        JSON call. Input: [{control_id, selector, widget_hint?, nearby_text?}, ...].
        Output: {control_id: {label, widget}}. Best-effort — invalid JSON returns
        {} so the caller degrades to 'unknown' fields, never crashes. Sees NO
        canary field (the generic front-end filters those out before calling)."""
        messages = [
            {"role": "system", "content":
                'Label form controls. Return ONLY JSON: {"labels":[{"control_id":str,'
                '"label":str,"widget":"text|textarea|native_select|react_select|'
                'radio_group|checkbox|file|date"}]}. Use the selector and nearby text.'},
            {"role": "user", "content": json.dumps({"controls": list(snapshot)})},
        ]
        raw = self._json_call(messages)
        try:
            data = json.loads(raw)
            out = {}
            for item in data.get("labels", []):
                cid = item.get("control_id")
                if cid:
                    out[cid] = {"label": item.get("label") or "",
                                "widget": item.get("widget") or "text"}
            return out
        except Exception:                                # noqa: BLE001 — best-effort
            return {}
```

`src/applypilot/apply/v2/frontend_generic.py`:

```python
"""GenericFrontend (spec §6.8 degraded tier): best-effort observation->IR when a
concrete front-end fails (DOM churn). Controls with a usable label are keyed
deterministically (reusing the Greenhouse taxonomy); UNLABELED controls are
batched to Operator.label_controls in ONE call. Canary-looking controls are NEVER
sent to the oracle (invariant 7/14) — they keep their observed label and resolve
deterministically downstream. Explicitly counted (ats='generic') and the caller
caps its use per run."""
from __future__ import annotations

from applypilot.apply.v2 import ir
from applypilot.apply.v2.frontend_greenhouse import (
    _semantic_key, _widget_kind, _locator_spec, _frame_path, _parse_selector,
    _custom_key,
)


def _has_label(c) -> bool:
    return bool((c.label or "").strip())


def parse_observation(obs, *, company: str, url: str, operator=None) -> ir.FormSchema:
    candidates = [c for c in obs.controls if c.visible and (c.label or c.control_id)]
    # (1) controls we can key deterministically keep their observed label.
    labeled, unlabeled = [], []
    for c in candidates:
        if _has_label(c):
            labeled.append(c)
        else:
            unlabeled.append(c)
    # (2) batch the truly-unlabeled controls to the oracle for a LABEL only.
    #     A control whose observed label already reads canary is treated as
    #     labeled (never sent) — handled above since it has a label.
    oracle_labels = {}
    if unlabeled and operator is not None:
        snapshot = [{"control_id": c.control_id, "selector": c.selector,
                     "widget_hint": c.control_type} for c in unlabeled]
        try:
            oracle_labels = operator.label_controls(snapshot) or {}
        except Exception:                                # noqa: BLE001 — degraded is best-effort
            oracle_labels = {}

    fields = []
    for c in labeled:
        fields.append(_field(c, c.label or ""))
    for c in unlabeled:
        lab = (oracle_labels.get(c.control_id) or {}).get("label") or ""
        fields.append(_field(c, lab))

    advance = None
    if obs.submit_buttons:
        b = obs.submit_buttons[0]
        b_id, b_name = _parse_selector(b.selector)
        advance = {"role": "button", "name": b.label or "Submit", "label": b.label or "Submit",
                   "text": b.label or None, "elem_id": b_id, "name_attr": b_name,
                   "selector": b.selector or None, "frame_url": b.frame_url or None}
    step = ir.Step(index=0, fields=fields, advance_control=advance, terminal=True)
    return ir.FormSchema(ats="generic", company=company, url=url, steps=[step])


def _field(c, label: str) -> ir.Field:
    kind = _widget_kind(c)
    sem = _semantic_key(label, label) or (_custom_key(label) if label else None)
    return ir.Field(field_id=c.control_id or c.selector or "", frame_path=_frame_path(c),
                    label_text=label, question_text=label, semantic_key=sem,
                    widget=ir.Widget(kind=kind), options=ir.LAZY,
                    required=bool(c.required), char_limit=None, locator_spec=_locator_spec(c))
```

Then wire the fallback + cap into `run_form_compiler` (adapt the PARSE try, L223-227):

```python
    # PARSE — concrete front-end first; on failure, ONE degraded generic attempt
    # (counted + capped). Both are pre-submit, so a total failure still fails open.
    try:
        schema = st.parse(page, company, url)
    except Exception:
        if _degraded_allowed():                          # per-run cap (invariant 14)
            try:
                from applypilot.apply.v2 import frontend_generic
                from applypilot.apply.browser_stream import collect_browser_observation
                schema = frontend_generic.parse_observation(
                    collect_browser_observation(page), company=company, url=url, operator=operator)
                prefill["tier_used"] = "v2_degraded"     # explicitly counted
                _degraded_consume()
            except Exception:
                return FALLBACK_SENTINEL, _ms(), None
        else:
            return FALLBACK_SENTINEL, _ms(), None
```

Notes / adapt warnings:
- The per-run cap (`_degraded_allowed`/`_degraded_consume`) is a small counter — implement it as a module-level counter reset per process, or thread a `degraded_budget` int through `run_form_compiler` (cleaner + testable). Default cap: a few per run (spec §6.8 "capped per run"). Keep it simple; note the chosen mechanism.
- The generic tier's `tier_used="v2_degraded"` makes it a first-class counted metric (spec §11 "escalation/degraded-tier usage" is a blocking weekly metric). `reporting.summarize_review`'s `by_tier` picks it up for free.
- Canary safety holds two ways: (1) `label_controls` only receives UNLABELED controls (a canary field like "Desired salary" has a label, so it is never sent); (2) even a mislabeled control resolves through the canary-first resolver (resolver.py L134-143), which short-circuits canary keys to exact profile paths BEFORE any oracle. Both are asserted by the tests.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_degraded_tier.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/test_v2_operator.py tests/test_v2_orchestrator.py -q`
Expected: no regressions (the `label_controls` NotImplementedError removal doesn't affect `resolve_fields`).

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/operator.py src/applypilot/apply/v2/frontend_generic.py src/applypilot/apply/v2/orchestrator.py tests/test_v2_degraded_tier.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: degraded tier (§6.8) — GenericFrontend + real Operator.label_controls (batched, canary-safe, counted as v2_degraded, capped per run)"
```

---

## Task 10: Nightly canary-parse command (`applypilot canary-parse`) — ~20 live forms/ATS → parse-rate report

A manual/cron-invoked command (NO daemon — spec §14 "long-running daemon deferred; tick model instead") that samples ~20 live forms per supported ATS from the DB, runs each through the front-end registry, and reports a per-ATS parse-rate + the parse gaps. It alarms on DOM churn BEFORE the queue feels it (spec §6.8/§11) and feeds same-day fixture promotion. It never submits — it only navigates + parses (like the probe, but scored against the registry).

**Files:**
- Create: `src/applypilot/apply/canary_parse.py` (pure scorer + a thin live runner)
- Modify: `src/applypilot/cli.py` (add `canary-parse`)
- Test: `tests/test_canary_parse.py`

- [ ] **Step 1: READ the registry + a URL-sampling query + the report render style**

READ `src/applypilot/apply/v2/frontends.py` (`parser_for`, from Task 7). READ `src/applypilot/apply/probe_dump.py` (Task 4 — the live-navigate-then-collect pattern to reuse). READ `src/applypilot/reporting.py:335-369` (`format_report` — the render style to mirror for the parse-rate table). READ `src/applypilot/database.py:get_connection` + how `jobs.application_url` is queried elsewhere (e.g. the atlas telemetry queries). Confirm there is NO existing `canary-parse`/`canary_parse` symbol.

- [ ] **Step 2: Write failing tests** (the SCORER is pure; the live sampler is not unit-tested)

```python
# tests/test_canary_parse.py
from applypilot.apply import canary_parse as cp
from applypilot.apply.v2 import ir


def _schema(ats, keys):
    fields = [ir.Field(field_id=k, frame_path=(), label_text=k, question_text=k,
                       semantic_key=k, widget=ir.Widget(kind="text"), options=ir.LAZY)
              for k in keys]
    return ir.FormSchema(ats=ats, company="c", url="u", steps=[ir.Step(0, fields, terminal=True)])


def test_score_parse_results_counts_pass_and_gap():
    # A parse "passes" when it recovers >=1 standard field (email/first_name/resume).
    results = [
        {"ats": "ashby", "schema": _schema("ashby", ["email", "resume"]), "error": None},
        {"ats": "ashby", "schema": _schema("ashby", ["custom.only"]), "error": None},   # gap
        {"ats": "ashby", "schema": None, "error": "parse crashed"},                     # gap
    ]
    report = cp.score(results)
    assert report["by_ats"]["ashby"]["n"] == 3
    assert report["by_ats"]["ashby"]["parsed"] == 1
    assert report["by_ats"]["ashby"]["parse_rate"] == round(1 / 3, 3)
    assert report["by_ats"]["ashby"]["gaps"] == 2


def test_format_canary_report_renders_per_ats():
    report = {"by_ats": {"ashby": {"n": 20, "parsed": 19, "parse_rate": 0.95, "gaps": 1},
                         "lever": {"n": 20, "parsed": 20, "parse_rate": 1.0, "gaps": 0}}}
    out = cp.format_report(report)
    assert "ashby" in out and "95%" in out and "lever" in out


def test_alarm_threshold_flags_low_parse_rate():
    report = {"by_ats": {"ashby": {"n": 20, "parsed": 14, "parse_rate": 0.70, "gaps": 6}}}
    alarms = cp.alarms(report, min_parse_rate=0.90)
    assert "ashby" in alarms                              # 70% < 90% -> churn alarm
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: applypilot.apply.canary_parse`)

Run: `& $PY -m pytest tests/test_canary_parse.py -v`

- [ ] **Step 4: Implement `canary_parse.py` + the CLI**

```python
"""Nightly canary parse (spec §6.8/§11): sample ~N live forms per supported ATS,
parse each through the front-end registry, report a per-ATS parse-rate + gaps.
Manual/cron-invoked, NO daemon (spec §14). Never submits — navigate + parse only.
Alarms on a parse-rate drop (DOM churn) before the queue feels it; gaps feed
same-day fixture promotion."""
from __future__ import annotations

from applypilot.apply.v2 import frontends

_STANDARD = {"email", "first_name", "last_name", "resume", "phone"}


def _parsed_ok(schema) -> bool:
    if schema is None:
        return False
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    return bool(keys & _STANDARD)


def score(results: list[dict]) -> dict:
    by_ats: dict[str, dict] = {}
    for r in results:
        d = by_ats.setdefault(r["ats"], {"n": 0, "parsed": 0, "gaps": 0})
        d["n"] += 1
        if r.get("error") is None and _parsed_ok(r.get("schema")):
            d["parsed"] += 1
        else:
            d["gaps"] += 1
    for d in by_ats.values():
        d["parse_rate"] = round(d["parsed"] / d["n"], 3) if d["n"] else 0.0
    return {"by_ats": by_ats}


def alarms(report: dict, *, min_parse_rate: float = 0.90) -> list[str]:
    return [ats for ats, d in report["by_ats"].items() if d["parse_rate"] < min_parse_rate]


def format_report(report: dict) -> str:
    lines = ["=" * 48, "  Canary parse — per-ATS parse rate", "=" * 48]
    for ats, d in sorted(report["by_ats"].items()):
        lines.append(f"  {ats:12s} {d['parsed']:>3}/{d['n']:<3} ({d['parse_rate']:.0%})  gaps={d['gaps']}")
    lines.append("=" * 48)
    return "\n".join(lines)


def sample_urls(conn, ats: str, *, limit: int = 20) -> list[str]:
    """Sample recent candidate URLs for `ats` from the jobs table (live runner)."""
    host = {"greenhouse": "greenhouse.io", "ashby": "jobs.ashbyhq.com", "lever": "jobs.lever.co"}[ats]
    rows = conn.execute(
        "SELECT application_url FROM jobs WHERE application_url LIKE ? "
        "AND application_url IS NOT NULL ORDER BY discovered_at DESC LIMIT ?",
        (f"%{host}%", limit)).fetchall()
    return [r[0] for r in rows if r[0]]


def run_live(conn, *, atses=("greenhouse", "ashby", "lever"), limit: int = 20,
             headless: bool = True) -> dict:
    """Live runner: navigate + parse each sampled URL; collect (ats, schema, error).
    Uses a throwaway headless Chromium (out-of-band; never submits)."""
    from playwright.sync_api import sync_playwright
    from applypilot.apply.browser_stream import collect_browser_observation
    results = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless)
        for ats in atses:
            parse = frontends.parser_for(ats)
            if parse is None:
                continue
            for url in sample_urls(conn, ats, limit=limit):
                page = b.new_context().new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    obs = collect_browser_observation(page)
                    results.append({"ats": ats, "schema": parse(obs, company="canary", url=url),
                                    "error": None})
                except Exception as e:                   # noqa: BLE001 — a nav/parse failure IS a gap
                    results.append({"ats": ats, "schema": None, "error": str(e)})
                finally:
                    page.close()
        b.close()
    return score(results)
```

CLI (`cli.py`):

```python
@app.command("canary-parse")
def canary_parse_cmd(
    limit: int = typer.Option(20, "--limit", help="Live forms sampled per ATS."),
    ats: str = typer.Option("greenhouse,ashby,lever", "--ats", help="Comma list of ATSes."),
    min_parse_rate: float = typer.Option(0.90, "--min-parse-rate"),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Nightly canary parse: sample live forms per ATS, parse through the v2
    front-ends, report parse-rate + alarm on churn. Manual/cron; never submits."""
    _bootstrap()
    from applypilot.apply import canary_parse as cp
    from applypilot.database import get_connection
    conn = get_connection()
    report = cp.run_live(conn, atses=tuple(a.strip() for a in ats.split(",") if a.strip()),
                         limit=limit, headless=headless)
    typer.echo(cp.format_report(report))
    fired = cp.alarms(report, min_parse_rate=min_parse_rate)
    if fired:
        typer.echo(f"ALARM: parse-rate below {min_parse_rate:.0%} for: {', '.join(fired)} "
                   f"— promote a fixture from a failing form (applypilot fixtures promote).")
        raise typer.Exit(code=1)          # non-zero so cron/CI notices
```

Notes / adapt warnings:
- `run_live` is NOT unit-tested (needs Chromium + network + the live DB). Tests cover `score`/`alarms`/`format_report`/(and `sample_urls` can be tested against a temp DB if desired). Do NOT put a live test in the offline suite.
- `sample_urls`'s `discovered_at` column: confirm it exists on `jobs` (it does per CLAUDE.md's discovery description); if the exact column name differs, adapt the ORDER BY. A missing column must not crash — wrap the ORDER BY in a try or use `rowid DESC` as a fallback.
- Exit code 1 on alarm makes this cron/CI-friendly (spec §11 "nightly canary parse → parse-rate alert"). The parse-rate is scored against the SAME registry the live engine uses, so a churn that would break real applies is caught here first.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_canary_parse.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/canary_parse.py src/applypilot/cli.py tests/test_canary_parse.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "cli: 'applypilot canary-parse' — sample ~20 live forms/ATS through the v2 front-ends, per-ATS parse-rate report + churn alarm (manual/cron, no daemon, never submits)"
```

---

## Task 11: Phase 4A verification

- [ ] **Step 1: Full suite green**

Run: `& $PY -m pytest tests/ -q`
Expected: ALL PASS, 0 failed (the fixture-replay test SKIPS unless a fixture was promoted — not a failure). Investigate any regression, especially `-k "launcher or ledger or broker or dispatch or detect_ats or prefill"` — the `_v2_supported_ats` gate widening and the Lever `_detect_ats` branch (Task 7) are the highest-risk edits: a regression there is a submission-routing bug, not a test nit.

- [ ] **Step 2: Lint the new/changed modules**

Run: `ruff check src/applypilot/apply/v2 src/applypilot/apply/canary_parse.py src/applypilot/apply/probe_dump.py`
Expected: clean. Keep the deliberate `# noqa: BLE001` markers on the fail-open / best-effort `except Exception` blocks (invariants 2/14/15) with a one-word reason.

- [ ] **Step 3: Prove the per-ATS flags are true no-ops when scoped OFF (shadow safety)**

Confirm by READING (not a live apply):
- With `APPLYPILOT_V2_ENGINE` unset, `_v2_supported_ats` returns False for every ATS ⇒ `_dispatch_apply_v2_aware` calls only `legacy_dispatch_fn` (byte-for-byte the pre-Phase-4A path).
- With `APPLYPILOT_V2_ENGINE=1` and `APPLYPILOT_V2_ATS` unset, ONLY Greenhouse routes to v2 (invariant 11) — Ashby/Lever still legacy, exactly as Phase 3 shipped.
- With `APPLYPILOT_V2_ATS=greenhouse,ashby`, Ashby routes to v2 but Lever does NOT — each ATS shadows independently.
- The flag-off flight recorder (`APPLYPILOT_V2_FLIGHT` unset) writes ZERO bundles (Task 1) — no hot-path cost.

- [ ] **Step 4: End-to-end wiring smoke on synthetic DOM (no network, no real Chrome subprocess, no live apply)**

Write a scratch script (scratchpad dir, NOT the repo) that, using the `page` fixture pattern (headless Playwright + `set_content` + a fake Operator + a temp DB):
1. Synthetic Ashby DOM → `frontend_ashby.parse_observation` → a schema with expected semantic keys; same for a synthetic Lever DOM → `frontend_lever.parse_observation`.
2. `frontends.parser_for("ashby"|"lever"|"greenhouse")` returns the right fn; `parser_for("workday")` is None.
3. `preflight.classify` on an `?error=true` landed URL → `terminal="failed:expired"`; on a password-field obs → `login_issue`.
4. `run_form_compiler` with `APPLYPILOT_V2_FLIGHT=1` + a non-applied injected outcome writes a promotable bundle under a temp `APP_DIR/flight`, with pre-fill DOM + phases + plan-mapped field records.
5. Two synthetic review rows (`v2_ashby`, `legacy_llm`) through `summarize_review` → `by_tier` carries both; `v2_audit_clean` over a fake ledger + a clean flight dir returns go=True.
6. `frontend_generic.parse_observation` with a fake `label_controls` builds a `generic` schema.
Confirm the whole extension composes at $0. Do NOT commit the scratch script.

- [ ] **Step 5: Confirm posture (by reading)**

- Every new front-end is PURE observation→IR; the resolver/executor/verify/drivers are unchanged (invariant 13) — grep confirms no new pipeline stage and no safety-kernel object constructed in `apply/v2/frontend_*`/`preflight.py`/`frontend_generic.py`.
- v2 fails OPEN per-ATS (parse/probe/resolve exceptions → sentinel → legacy for THAT job; probe terminals → named bucket, INTENT released pre-submit) — the live queue never regresses (invariant 2/11).
- Canary keys never reach the Operator: the resolver still short-circuits them (resolver.py L134-143), and the degraded tier never sends a labeled canary control to `label_controls` (invariant 7/14).
- Flight recording is pre-fill DOM only + non-applied-only + env-gated + exception-guarded (invariant 15) — no PII in bundles, no hot-path cost, no fault can change an apply outcome.
- No Ashby/Lever markup assumption is un-grounded: every `_widget_kind`/synonym-table entry traces to a `docs/superpowers/probes/{ashby,lever}_*.json` (invariant 12).

- [ ] **Step 6: Tree clean + report**

Run: `git status --short`
Expected: EMPTY (all Phase-4A work committed; no stray scratch files).

Summarize to the user: commits made, total new tests + pass count (+ the intentional replay skip), the synthetic end-to-end smoke result, whether Task 8 added a driver or was a documented no-op, and the reminder that Ashby/Lever ship **behind `APPLYPILOT_V2_ATS` in independent per-ATS shadow** — legacy stays the counted fallback per-ATS until each clears its own go/no-go (§13 Phase 4: measured shadow burn-in per ATS). Note that the driver long-tail and any parser refinements are DATA-DRIVEN by the probes + first shadow runs, and that `canary-parse` should be scheduled (cron/manual) so churn alarms before the queue feels it.

---

## Open decisions for the controller

- **Per-ATS flag shape: single `APPLYPILOT_V2_ATS` allowlist vs per-ATS envs.** Recommended default (encoded in Task 7): **one fresh-read `APPLYPILOT_V2_ATS` comma-list**, default-empty ⇒ greenhouse-only. Rationale: mirrors the single `APPLYPILOT_V2_ENGINE` master flag, no env-var sprawl, each ATS still burns in independently by adding a token, and the default preserves the Phase-3 shadow byte-for-byte. The alternative (per-ATS `APPLYPILOT_V2_ASHBY`/`_LEVER` booleans) is more envs to manage for the same expressiveness. If the operator prefers per-ATS envs for muscle-memory reasons, the gate is a one-line change — but the allowlist is the recommended shape.

- **Ashby/Lever front-end scope: ship both now, or Ashby-first?** Recommended default: **ship both parsers (Tasks 5+6) but shadow them independently** — build cost is low (they mirror the Greenhouse template) and the registry (Task 7) is the same work either way, but flip `APPLYPILOT_V2_ATS` to add `ashby` FIRST (5,955 live candidates vs 637 for Lever — Ashby has the volume to reach the 100-live-row go/no-go faster), then `lever` once Ashby clears. Do NOT enable both on day one; each needs its own measured burn-in (§13 Phase 4).

- **Degraded-tier cap mechanism + budget.** Recommended default: **thread a `degraded_budget` int through `run_form_compiler`** (testable, per-run) with a small cap (a few per run, spec §6.8 "capped per run"). A module-level process counter is simpler but harder to test and reset. The exact cap is a tuning knob — start conservative (the degraded tier spends an oracle call and produces best-effort IR; it is insurance against correlated churn, not a routine path) and raise only if the canary-parse alarm shows a real dialect gap.

- **Probe artifacts: commit them or keep local?** Recommended default: **commit the `docs/superpowers/probes/{ashby,lever}_*.json` alongside the parser** (Tasks 5/6) — they are the fact-source the parser's markup assumptions trace to (invariant 12), and committing them makes the parser's empirical grounding auditable and lets a reviewer diff a future churn against the recorded baseline. They contain no PII (pre-fill form structure only). If the operator considers them noise, keep them local — but then the parser's synonym-table entries lose their audit trail.

- **Duplicate-detection leg of the §12.3 audit (Task 2).** The duplicate check relies on `board_token` being present on `review.jsonl` rows; it is NOT today, so the leg is a documented best-effort no-op (0 duplicates) while dangling-INTENT and canary-violation legs bite fully. Recommended follow-up: **add `board_token` to the review row** (a one-field write in the launcher's review-log emit) so the duplicate leg becomes real. Deferred here because it is a telemetry-schema change, not apply-engine work — flag it if the operator wants the full §12.3 trio live before cutover.

- **`canary-parse` scheduling.** Recommended default: **manual/cron, NO daemon** (encoded in Task 10, spec §14). The command exits non-zero on a parse-rate alarm so a cron wrapper or CI job can notify. Wiring it into the machine's Task Scheduler (nightly) is an ops step, not code — note it in the summary and leave the schedule to the operator. Same-day fixture promotion from a flagged failing form (`fixtures promote`) is the intended remediation loop.

- **Pre-flight probe scope vs the launcher's pre-CDP expired guard.** Recommended default: **keep BOTH** (Task 3). The launcher's pre-CDP guard (L3449-3457) is the cheapest possible expired short-circuit (fires before the form even loads); the in-orchestrator probe is the second line that ALSO catches login/captcha/sso once the page is up. Both share `is_greenhouse_expired_redirect`, so "expired" has one definition. Collapsing them into one site is possible but would move the expired check later (after CDP setup), losing the cheap early exit — not worth it.
