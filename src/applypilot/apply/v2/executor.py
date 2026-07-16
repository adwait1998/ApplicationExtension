"""Executor (spec §6.6): walk a resolved FillPlan step-by-step.

Resume/file fields are committed FIRST, then the executor blocks on a
MutationObserver quiet-window settle gate before touching the rest — Greenhouse
re-renders the form server-side after the resume parse, so filling before the
DOM settles targets elements that do not exist yet (invariant 9). The executor's
own waits are settle gates / locator-state expectations — never time.sleep
(invariant 9). Each field is committed through the driver registry (Task 6) with
read-back; a committed=False on a REQUIRED field flips it to unresolved (the
orchestrator escalates to the oracle or parks) — never a silent success. Before
reporting ready_to_submit a required-completeness sweep (promoted greenhouse
helper) must find no required-but-empty control. Multi-step LOOPS over
schema.steps (Greenhouse emits one terminal step; the loop shape lets Phase 4
drop advance-control clicking in). execute() is oracle-free: the orchestrator
(Task 9) resolves needs_oracle into PlannedFields BEFORE calling this.

NOTE on the residual react-select sleeps: the promoted prefill react-select path
(reached via drivers.commit) still carries ~5 fixed time.sleep() calls. Those
live in the drivers/prefill namespace, not here; THIS module's own waits are
settle-gated (invariant 9) and the zero-sleep test monkeypatches ex.time to
prove it. Replacing the react-select sleeps is a separate follow-up (not this
task — no gold-plating)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from applypilot.apply.v2 import drivers
from applypilot.apply.v2 import ir
from applypilot.apply.v2 import mapping_cache as mc
from applypilot.apply.v2.resolver import FillPlan, PlannedField

log = logging.getLogger(__name__)

# A self-installing MutationObserver that stamps the last-mutation time; the JS
# returns the current idle delta (ms since the last mutation). Polled from the
# executor to detect a quiet window — no fixed sleep, the wait is gated on real
# DOM activity (invariant 9).
_SETTLE_JS = r"""
() => {
  if (!window.__ap_mo) {
    window.__ap_last = Date.now();
    window.__ap_mo = new MutationObserver(() => { window.__ap_last = Date.now(); });
    window.__ap_mo.observe(document.documentElement,
        {subtree: true, childList: true, attributes: true, characterData: true});
  }
  return Date.now() - window.__ap_last;
}
"""


@dataclass
class ExecReport:
    """What the orchestrator (Task 9) reads to decide submit vs park."""
    ready_to_submit: bool = False
    committed_keys: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    parked: bool = False
    error: str | None = None


def _is_resume(pf: PlannedField) -> bool:
    """A resume/file upload — committed first, then gated on the settle window."""
    return pf.driver == "file" or pf.field.semantic_key == "resume"


def _wait_settle(scope, *, quiet_ms: int = 400, timeout_ms: int = 8000) -> None:
    """Block until the DOM has been quiet for quiet_ms, or timeout — no fixed
    sleep (invariant 9). The loop is gated on the MutationObserver's
    last-mutation delta; scope.wait_for_timeout is a mutation-gated micro-yield
    between polls, not a blind time.sleep. Any evaluate failure (page closed /
    navigation) returns quietly: the settle gate is best-effort and must never
    raise into the fill."""
    try:
        scope.evaluate(_SETTLE_JS)                     # install the observer
    except Exception:                                  # noqa: BLE001
        return
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            idle = scope.evaluate(_SETTLE_JS)
        except Exception:                              # noqa: BLE001
            return
        if isinstance(idle, (int, float)) and idle >= quiet_ms:
            return
        try:
            scope.wait_for_timeout(50)                 # mutation-gated micro-yield
        except Exception:                              # noqa: BLE001
            return


def _resolve_scope(page, schema):
    """Greenhouse forms on vanity careers domains sit in a child iframe; promote
    the adapter's frame detection so locators resolve there. boards.greenhouse.io
    is top-frame, so this returns the page for the common case (and for the
    synthetic tests, whose selectors don't match the greenhouse probe)."""
    try:
        from applypilot.apply.adapters.greenhouse import _form_scope
        return _form_scope(page)
    except Exception:                                  # noqa: BLE001
        return page


def _required_missing(scope) -> list[str]:
    """PROMOTED greenhouse._missing_required_labels_on_page: the live-DOM
    required-completeness sweep (spec §6.6) — labels of required controls that
    are still empty or invalid. Any hit blocks submit."""
    try:
        from applypilot.apply.adapters.greenhouse import _missing_required_labels_on_page
        return list(_missing_required_labels_on_page(scope))
    except Exception:                                  # noqa: BLE001
        return []


def _cache_success(conn, schema, pf: PlannedField, res) -> None:
    """Mapping-cache writeback on a verified commit: persist the binding + driver
    + winning locator tier, then record the hit (invariant 3: bindings, never
    values). No-op without a DB connection.

    Best-effort, exception-isolated: the cache write is a SIDE-EFFECT of the fill
    and MUST NOT sink it. A transient sqlite fault (DB locked / disk full / schema
    drift) mid-fill would otherwise propagate out of execute(), crash an
    otherwise-fillable form, and return no ExecReport. Mirror the sibling
    verify.py harvest contract: log and continue — a cache fault demotes to a
    no-op, the commit stands."""
    if conn is None:
        return
    try:
        fp = ir.field_fp(pf.field)
        if pf.binding:
            mc.put(conn, schema.ats, fp, binding=pf.binding,
                   widget_driver=pf.driver, locator_tier=res.locator_tier)
        mc.record_success(conn, schema.ats, fp)
    except Exception:                                  # noqa: BLE001 - best-effort side-effect
        log.warning("mapping-cache success writeback failed for %s; commit stands",
                    pf.field.semantic_key or pf.field.field_id, exc_info=True)


def _cache_failure(conn, schema, pf: PlannedField) -> None:
    """Record a VERIFIED failure (read-back said not-committed) against the
    mapping — demote-never-archive (spec §6.4). No-op without a DB connection or
    a pre-existing mapping row.

    Best-effort, exception-isolated like _cache_success: a cache fault must
    demote to a no-op, never abort the fill (mirrors verify.py's isolated
    harvest)."""
    if conn is None:
        return
    try:
        mc.record_failure(conn, schema.ats, ir.field_fp(pf.field))
    except Exception:                                  # noqa: BLE001 - best-effort side-effect
        log.warning("mapping-cache failure writeback failed for %s",
                    pf.field.semantic_key or pf.field.field_id, exc_info=True)


def _fill_fields(scope, schema, planned: list[PlannedField], report: ExecReport, conn) -> None:
    """Commit a step's planned fields resume-first, settling after each resume/
    file commit before touching the rest of the form (invariant 9)."""
    ordered = sorted(planned, key=lambda pf: 0 if _is_resume(pf) else 1)
    for pf in ordered:
        key = pf.field.semantic_key or pf.field.field_id
        if pf.park:                                    # resolver parked (no safe answer)
            report.unresolved.append(key)
            continue
        res = drivers.commit(scope, pf)
        if res.committed:
            report.committed_keys.append(key)
            _cache_success(conn, schema, pf, res)
            if _is_resume(pf):
                _wait_settle(scope)                    # settle the re-render before the rest
        elif pf.field.required:
            # committed=False on a REQUIRED field -> unresolved (never a silent
            # success); the orchestrator escalates or parks. Demote the failing
            # mapping. Optional fields with no profile data commit False too but
            # are simply skipped — they are not failures.
            report.unresolved.append(key)
            _cache_failure(conn, schema, pf)


def _planned_for_step(plan: FillPlan, step, seen: set) -> list[PlannedField]:
    """The plan's fields belonging to this step, matched by field fingerprint
    (survives id/class churn, invariant 10). `seen` dedupes so a field is
    committed exactly once even if two steps share a fingerprint."""
    step_fps = {ir.field_fp(f) for f in step.fields}
    out = []
    for pf in plan.planned:
        if id(pf) in seen:
            continue
        if ir.field_fp(pf.field) in step_fps:
            seen.add(id(pf))
            out.append(pf)
    return out


def execute(page, schema, plan: FillPlan, *, conn=None) -> ExecReport:
    """Walk the FillPlan step-by-step and return an ExecReport the orchestrator
    uses to decide submit vs park. Oracle-free — Task 9 resolves needs_oracle
    into PlannedFields before calling this."""
    report = ExecReport()
    scope = _resolve_scope(page, schema)
    seen: set = set()
    # Multi-step LOOP (Greenhouse = 1 terminal step; the loop shape lets Phase 4
    # add advance-control clicking + a per-step interlock between steps).
    for step in schema.steps:
        _fill_fields(scope, schema, _planned_for_step(plan, step, seen), report, conn)
    # Defensive: any planned field not matched to a step is still committed once
    # (nothing silently dropped).
    leftovers = [pf for pf in plan.planned if id(pf) not in seen]
    if leftovers:
        _fill_fields(scope, schema, leftovers, report, conn)

    # Required-completeness interlock (spec §6.6): never report ready_to_submit
    # with an empty required control or an unresolved field.
    report.missing_required = _required_missing(scope)
    report.ready_to_submit = not report.missing_required and not report.unresolved
    report.parked = bool(report.unresolved) and not report.ready_to_submit
    return report
