# Phase 3 — Form Compiler Apply Engine v2 on Greenhouse (Shadow / A-B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace v1's monolithic imperative Greenhouse fill (`prefill.prefill_application` + `adapters/greenhouse.fill_greenhouse` + the Claude-CLI agent) with the spec §6 **Form Compiler**: a four-stage pipeline **Parse → Resolve → Fill → Verify** over one shared **FormSchema IR**, one provider-flexible **Operator** (the only AI in the loop), a **mapping cache** (demote-never-archive, bindings-not-values), a **WidgetDriver registry** (each returning a read-back-verified `CommitResult`), and **Tier-1 network-evidence verification** (passive submit-POST listener). Greenhouse ONLY (Ashby/Lever are Phase 4). Ships behind a fresh-read `APPLYPILOT_V2_ENGINE` flag as a shadow/A-B branch beside `skill_runner.dispatch_apply`; the v1 Claude-CLI agent STAYS as a counted fallback and v2 **fails open** (any parse/resolve failure → sentinel → legacy path runs). Cutover only when v2 ≥ v1 on 100+ live Greenhouse rows (spec §13 Phase 3 exit).

**Architecture:** Additive and quarantined. A new `apply/v2/` package holds the pipeline stages, IR, Operator, drivers, and orchestrator; the SAFETY KERNEL objects (`submit_broker.SubmitBroker`, `submission_ledger.SubmissionLedger`, `browser_stream.BrowserStateStream` + its `_guard` network route) are **reused, not rebuilt** — the v2 orchestrator threads the SAME objects `worker_loop` already constructs. `healing.heal`/`ElementSpec` (the 10-tier ladder) and `answer_cache.AnswerCache` (§10.3-hardened) are imported as-is. The Greenhouse front-end promotes the label-synonym taxonomy out of `adapters/greenhouse._standard_plan`; the WidgetDriver registry promotes the react-select/location/phone/file drivers out of `prefill.py` and `adapters/greenhouse.py`. A new `mapping_cache` table joins `boards`/`source_runs`/`submission_ledger`/`engine_control` as a standalone table in `init_db`. `llm._chat_compat` gains optional structured-output support. The integration seam is a `v2` sibling of `dispatch_apply`'s call site in `worker_loop`, gated by `APPLYPILOT_V2_ENGINE` (mirrors the `APPLYPILOT_USE_SKILLS` / `APPLYPILOT_ATLAS_ENABLED` opt-in pattern). A/B is free: `review.jsonl` rows already carry `tier_used`/`duration_ms`/`status` and `reporting.summarize_review` already computes `by_tier` pass_rate + n; v2 sets `prefill_status["tier_used"]="v2_greenhouse"` so the exit gate is a pure query over existing telemetry.

**Tech Stack:** Python 3.11, SQLite (WAL, thread-local via `database.get_connection`), Playwright sync API over CDP, Typer, pytest. Tests use `page.set_content(...)` synthetic DOM via the `page` fixture pattern from `tests/test_greenhouse_adapter.py`, a fake/injected Operator (call-site injection like `test_answer_cache.py`'s `_LLM`) for LLM calls, and a temp DB for `mapping_cache`. NO live network, NO real Chrome subprocess in unit tests. Interpreter `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe` (has pytest + editable applypilot; the `.venv` python does NOT).

**Spec:** `docs/superpowers/specs/2026-07-02-applypilot-v2-design.md` §6 (Form Compiler, all subsections 6.1–6.9), §9 (Operator interface), §10 (safety kernel — reuse only), §11 (testing/flight-recorder/CI), §12 (acceptance gate), §13 Phase 3, §14 YAGNI, §15 risks.

**Prerequisite:** Phase 1 complete — `identity.identity_id`, `gate/engine.gate_job`, `submit_broker.SubmitBroker`, `submission_ledger.SubmissionLedger`, `browser_stream.BrowserStateStream` with the `_guard` route + broker/identity threading, and `answer_cache.AnswerCache` (§10.3-hardened) are all landed. Phase 2 (Atlas) is independent and need not be complete. (Verified present in the repo at plan-authoring time.)

**Conventions (same as Phase 0/1/2):**
- Repo root: `e:\auto-apply-pipeline`. Run all commands from there.
- `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe`.
- Commit with one-shot identity, never push: `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "..."`.
- **The index may contain pre-staged files.** Before EVERY commit run `git diff --cached --stat` and verify only the intended files are staged. If unexpected files are staged, `git reset` first, then re-add only what the task lists.
- TDD: write the failing test first, run it to confirm the failure mode, implement, run to green, commit.
- **Reuse-verify discipline:** every task that touches an existing module opens with a READ step against the exact file + line range. Signatures below were fact-swept at authoring; where a signature has drifted, adapt the step and note it in the task summary. NEVER rebuild a safety-kernel object — thread the SAME instance.

**File structure created by this plan:**
- `src/applypilot/apply/v2/__init__.py` — package marker + shared constants (Task 1)
- `src/applypilot/apply/v2/ir.py` — FormSchema/Step/Field dataclasses, semantic-key taxonomy, widget kinds, fingerprints (Task 1)
- `src/applypilot/apply/v2/operator.py` — `Operator` protocol + `FieldResolutionRequest`/`FieldAnswers` + `LLMOperator._json_call` + `ClaudeCLIOperator` (Task 2)
- `src/applypilot/apply/v2/mapping_cache.py` — `mapping_cache` table repo (demote-never-archive) (Task 3)
- `src/applypilot/apply/v2/frontend_greenhouse.py` — `_OBSERVE_JS` observation → FormSchema; promoted semantic-key taxonomy (Task 4)
- `src/applypilot/apply/v2/resolver.py` — IR + profile + answer_cache + canary + mapping_cache → FillPlan (Task 5)
- `src/applypilot/apply/v2/drivers.py` — WidgetDriver registry (promoted drivers, `CommitResult`, lazy option enum) (Task 6)
- `src/applypilot/apply/v2/executor.py` — walk FillPlan (settle gates, resume-first, required interlock) (Task 7)
- `src/applypilot/apply/v2/verify.py` — Tier-1 network-evidence listener + Tier-2 DOM verdict reuse (Task 8)
- `src/applypilot/apply/v2/orchestrator.py` — `run_form_compiler` + shared safety-prologue helper (Task 9)
- `src/applypilot/apply/v2/flight_recorder.py` — per-attempt bundle writer (Task 11)
- Modified: `src/applypilot/llm.py` (`_chat_compat` gains optional `response_format`; `chat` forwards it) (Task 2), `src/applypilot/database.py` (`mapping_cache` CREATE TABLE) (Task 3), `src/applypilot/apply/launcher.py` (extract `_safety_prologue` shared helper + v2 dispatch branch) (Task 9/10), `src/applypilot/cli.py` (`fixtures promote` command) (Task 11)
- Tests: `tests/test_v2_ir.py`, `tests/test_v2_operator.py`, `tests/test_v2_mapping_cache.py`, `tests/test_v2_frontend_greenhouse.py`, `tests/test_v2_resolver.py`, `tests/test_v2_drivers.py`, `tests/test_v2_executor.py`, `tests/test_v2_verify.py`, `tests/test_v2_orchestrator.py`, `tests/test_v2_dispatch_seam.py`, `tests/test_v2_flight_recorder.py`, `tests/test_v2_ab.py`

**Key invariants (load-bearing):**
1. **The safety kernel is below the parser and is REUSED, never rebuilt (spec §10).** v2 threads the SAME `SubmitBroker`, `SubmissionLedger`, and `BrowserStateStream` (with its `_guard` context route) that `worker_loop` constructs (launcher ~3299-3322). A v2 misparse can NEVER become a submission: the network route already fails closed on dry-run and gates real submits on an open broker ticket. v2 does NOT install its own routes, does NOT call `route.fetch()`, and does NOT open its own broker.
2. **v2 fails OPEN (spec §6.8).** Any parse/resolve/driver failure returns a sentinel that makes the orchestrator hand back to legacy `run_job` — mirroring `_greenhouse_adapter_pass`'s existing `except → return None` fail-open posture (launcher ~1246-1248). The legacy Claude-CLI agent stays as the counted fallback through burn-in; v2 never hard-fails a job.
3. **Bindings, never literal values (spec §6.4).** The mapping cache stores `profile.<path>` / `answer:<question_fp>` + the winning locator tier + widget-driver name — NEVER a literal answer. Privacy-safe, survives profile edits. Demote-never-archive: 2 verified failures demote a mapping for re-resolution; rows are kept and versioned, never deleted (v1's record→drift→archive cycle is the anti-pattern this fixes).
4. **Options are enumerated LAZILY, never at parse (spec §6.3/§6.5).** The IR carries `options: LAZY`. ONLY the `react_select` driver opens→reads→Escapes the dropdown, at commit/oracle-request time, with per-ATS Escape suppression. Parse MUST NOT open dropdowns (blows the parse budget + desyncs react-state). The resolver requests options lazily only when it must map an enumerated answer.
5. **Every driver returns a read-back-verified `CommitResult`; click success is never trusted (spec §6.6).** react-select drivers fold BOTH the portal-click path and the keyboard fallback and confirm the *committed* state (the Chime/Robinhood desync). `committed=False` → field stays unresolved → escalates or parks.
6. **Enumerated answers are an INDEX into provided options (spec §6.5).** The Operator returns an option index; free-typed option values are rejected at the schema layer. At commit the index resolves to freshly-read option text (index is validation, not the commit key). The react-select-desync bug class becomes a type error.
7. **Canary fields never reach the Operator, the answer bank, or fuzzy matching (spec §10.3).** work_auth / sponsorship / citizenship / salary / address / DOB resolve exclusively from exact profile paths or typed policy defaults. The resolver enforces this BEFORE any oracle batching.
8. **Tier-1 network evidence is PASSIVE (spec §6.7).** Verification listens (`context.on("response")` / CDP `Network.responseReceived`, reading status+url only) for the application-submit POST — the language-independent success signal. It NEVER uses `route.fetch()` (that would double-issue the POST). Endpoint signatures auto-harvest into `mapping_cache` on confirmed success. Tier-2 reuses the existing DOM verdict core (`adapters/greenhouse._post_submit_verdict` + `launcher._compute_verification_verdict`) unchanged.
9. **Zero fixed sleeps in the executor (spec §6.6).** Waits are MutationObserver quiet-window settles, locator-state expectations, and network-idle on step transitions. Resume uploads FIRST and the fill blocks on a real filename-chip + a quiet-window settle before reading/filling the rest (Greenhouse re-renders after server-side resume parse).
10. **Fingerprints are stable across id/class churn (spec §6.3/§15).** `field_fp` is built from label + widget kind + options-shape, dropping unstable tokens via `healing._looks_autogenerated`. Cache hit-rate is MEASURED in shadow before any speed or cutover claim.

---

## Task 1: FormSchema IR + fingerprints (pure, zero I/O)

The shared data structure the whole engine passes around. Pure dataclasses + pure fingerprint functions — fully unit-testable at $0, no browser, no DB. This is the contract every later stage codes against.

**Files:**
- Create: `src/applypilot/apply/v2/__init__.py`, `src/applypilot/apply/v2/ir.py`
- Test: `tests/test_v2_ir.py`

- [ ] **Step 1: READ the healing helper the fingerprint reuses**

READ `src/applypilot/apply/healing.py:39-48` (`_AUTOGEN_RE` + `_looks_autogenerated(token) -> bool`) — the field fingerprint drops unstable id/class tokens through this exact function so `field_fp` survives ATS id/class churn (invariant 10). Confirm the signature `_looks_autogenerated(token: str | None) -> bool` before importing it.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_ir.py
from applypilot.apply.v2 import ir


def test_widget_kinds_and_semantic_keys_are_enumerated():
    # widget kinds cover the spec §6.3 set; LAZY sentinel exists.
    assert {"text", "textarea", "native_select", "react_select", "radio_group",
            "checkbox", "file", "date", "phone_intl", "typeahead_location"} <= set(ir.WidgetKind.ALL)
    assert ir.LAZY is not None                            # sentinel exists
    # canary semantic keys are flagged
    assert ir.is_canary_key("work_auth") and ir.is_canary_key("sponsorship")
    assert ir.is_canary_key("salary") and not ir.is_canary_key("first_name")


def test_field_fp_stable_across_id_class_churn():
    # Same label + widget + options-shape, different auto-generated id -> same fp.
    a = ir.Field(field_id="input_r3f9a2ab12cd", frame_path=(), label_text="First name",
                 question_text="First name", semantic_key="first_name",
                 widget=ir.Widget(kind="text"), options=ir.LAZY, required=True)
    b = ir.Field(field_id="input_9xk21zff8801", frame_path=(), label_text="First name",
                 question_text="First name", semantic_key="first_name",
                 widget=ir.Widget(kind="text"), options=ir.LAZY, required=True)
    assert ir.field_fp(a) == ir.field_fp(b)              # id churn does not move the fp


def test_field_fp_distinguishes_widget_and_options_shape():
    base = dict(field_id="x", frame_path=(), label_text="Gender", question_text="Gender",
                semantic_key="eeo.gender", required=False)
    txt = ir.Field(widget=ir.Widget(kind="text"), options=ir.LAZY, **base)
    sel = ir.Field(widget=ir.Widget(kind="react_select"), options=ir.LAZY, **base)
    assert ir.field_fp(txt) != ir.field_fp(sel)          # widget kind is part of the key


def test_template_and_questions_fp_split():
    # template_fp = step structure + standard (semantic-keyed) fields;
    # questions_fp = the custom.* delta. Two forms sharing the GH template but
    # differing only in custom questions share template_fp, differ in questions_fp.
    std = [ir.Field(field_id="f", frame_path=(), label_text="Email", question_text="Email",
                    semantic_key="email", widget=ir.Widget(kind="text"), options=ir.LAZY, required=True)]
    cust_a = ir.Field(field_id="c", frame_path=(), label_text="Why us?", question_text="Why us?",
                      semantic_key="custom.why_us", widget=ir.Widget(kind="textarea"),
                      options=ir.LAZY, required=True)
    cust_b = ir.Field(field_id="c", frame_path=(), label_text="Salary?", question_text="Salary?",
                      semantic_key="custom.salary_expectation", widget=ir.Widget(kind="text"),
                      options=ir.LAZY, required=True)
    fa = ir.FormSchema(ats="greenhouse", company="acme", url="u",
                       steps=[ir.Step(index=0, fields=std + [cust_a], terminal=True)])
    fb = ir.FormSchema(ats="greenhouse", company="acme", url="u",
                       steps=[ir.Step(index=0, fields=std + [cust_b], terminal=True)])
    assert fa.template_fp() == fb.template_fp()           # same standard skeleton
    assert fa.questions_fp() != fb.questions_fp()         # different custom delta


def test_question_fp_stable_and_options_lazy_by_default():
    f = ir.Field(field_id="q", frame_path=("main", "iframe0"), label_text="Are you 18+?",
                 question_text="Are you 18 or older?", semantic_key=None,
                 widget=ir.Widget(kind="native_select"), options=ir.LAZY, required=True)
    assert f.options is ir.LAZY                            # never eagerly enumerated
    assert ir.question_fp(f) == ir.question_fp(f)          # deterministic
    assert f.frame_path == ("main", "iframe0")            # frame path is part of identity
```

- [ ] **Step 3: Run — expect failure** (`ModuleNotFoundError: No module named 'applypilot.apply.v2'`)

Run: `& $PY -m pytest tests/test_v2_ir.py -v`

- [ ] **Step 4: Implement the package + IR**

`src/applypilot/apply/v2/__init__.py`:

```python
"""Form Compiler apply engine v2 (spec §6). Greenhouse-only in Phase 3.

Four stages over one shared FormSchema IR: Parse (frontend_greenhouse) ->
Resolve (resolver) -> Fill (executor + drivers) -> Verify (verify). The
Operator (operator.py) is the only AI in the loop. The safety kernel
(submit_broker / submission_ledger / browser_stream._guard) is REUSED, never
rebuilt — the orchestrator threads the SAME objects worker_loop constructs.
v2 fails OPEN: any failure -> sentinel -> legacy run_job. Behind
APPLYPILOT_V2_ENGINE; cutover only at v2 >= v1 on 100+ live rows."""
from __future__ import annotations

V2_ENGINE_ENV = "APPLYPILOT_V2_ENGINE"
V2_TIER_LABEL = "v2_greenhouse"          # written to prefill_status["tier_used"]
```

`src/applypilot/apply/v2/ir.py`:

```python
"""FormSchema intermediate representation + fingerprints (pure, zero I/O).

The IR is the single data structure every stage shares (spec §6.3). Options are
enumerated LAZILY (the LAZY sentinel) — never at parse (invariant 4). field_fp
is the primary cache key and is built to survive id/class churn (invariant 10)
by dropping auto-generated tokens via healing._looks_autogenerated."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from applypilot.apply.healing import _looks_autogenerated


class _Lazy:
    """Singleton sentinel: 'options not yet enumerated' (distinct from [] =
    'enumerated, none'). Identity-checked with `is ir.LAZY`."""
    __slots__ = ()
    def __repr__(self) -> str:  # noqa: D401
        return "LAZY"


LAZY = _Lazy()


class WidgetKind:
    ALL = (
        "text", "textarea", "native_select", "react_select", "radio_group",
        "checkbox", "file", "date", "phone_intl", "typeahead_location",
        "segmented_button", "unknown",
    )


# Canary semantic keys: resolve ONLY from exact profile paths / typed policy
# defaults — never Operator, never answer bank, never fuzzy (spec §10.3).
_CANARY_KEYS = {"work_auth", "sponsorship", "citizenship", "salary", "address", "dob"}


def is_canary_key(semantic_key: str | None) -> bool:
    if not semantic_key:
        return False
    root = semantic_key.split(".", 1)[0]
    return root in _CANARY_KEYS or semantic_key in _CANARY_KEYS


@dataclass(frozen=True)
class Widget:
    kind: str = "unknown"
    framework_hints: tuple[str, ...] = ()


@dataclass
class Field:
    field_id: str                       # DOM id/name AT PARSE (may be autogen)
    frame_path: tuple[str, ...]         # frame chain, () = top document
    label_text: str
    question_text: str
    semantic_key: str | None            # taxonomy below, or custom.* / None
    widget: Widget
    options: Any = LAZY                 # LAZY until a driver enumerates them
    required: bool = False
    char_limit: int | None = None
    depends_on: str | None = None       # field_id this field is conditional on
    locator_spec: dict[str, Any] = field(default_factory=dict)  # -> ElementSpec (Task 5)


@dataclass
class Step:
    index: int
    fields: list[Field] = field(default_factory=list)
    advance_control: dict[str, Any] | None = None   # locator_spec of Next/Submit
    terminal: bool = False


def _stable_token(tok: str | None) -> str:
    """Drop the token from the fingerprint if it looks auto-generated."""
    return "" if _looks_autogenerated(tok) else (tok or "")


def _options_shape(f: "Field") -> str:
    """Shape, not values: LAZY -> 'lazy'; concrete -> count only (no values leak
    into a fingerprint input)."""
    if f.options is LAZY:
        return "lazy"
    opts = [str(o).strip() for o in (f.options or []) if str(o).strip()]
    return f"n={len(opts)}"


def field_fp(f: "Field") -> str:
    """Primary cache key: label + widget kind + options-shape + semantic_key,
    with id/class churn stripped. Deliberately EXCLUDES the raw field_id when it
    looks auto-generated (invariant 10)."""
    payload = "|".join([
        (f.label_text or "").strip().lower(),
        (f.question_text or "").strip().lower(),
        f.widget.kind,
        _options_shape(f),
        f.semantic_key or "",
        _stable_token(f.field_id),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def question_fp(f: "Field") -> str:
    """Custom-question identity (frame + label + question text + widget)."""
    payload = "|".join([
        "/".join(f.frame_path),
        (f.label_text or "").strip().lower(),
        (f.question_text or "").strip().lower(),
        f.widget.kind,
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _is_standard(f: "Field") -> bool:
    return bool(f.semantic_key) and not f.semantic_key.startswith("custom.")


@dataclass
class FormSchema:
    ats: str
    company: str
    url: str
    steps: list[Step] = field(default_factory=list)

    def _all_fields(self) -> list[Field]:
        return [fld for s in self.steps for fld in s.fields]

    def template_fp(self) -> str:
        """Step structure + standard (semantic-keyed) fields — shared across
        companies on the same ATS template (spec §6.3)."""
        parts = []
        for s in self.steps:
            std = sorted(field_fp(f) for f in s.fields if _is_standard(f))
            parts.append(f"step{s.index}:{'+'.join(std)}:term={int(s.terminal)}")
        payload = json.dumps({"ats": self.ats, "steps": parts}, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def questions_fp(self) -> str:
        """The custom-question delta — differs job-to-job on the same template."""
        cust = sorted(question_fp(f) for f in self._all_fields()
                      if f.semantic_key and f.semantic_key.startswith("custom."))
        return hashlib.sha1("+".join(cust).encode("utf-8")).hexdigest()
```

Note on the taxonomy: semantic keys are `first_name / last_name / email / phone / location / resume / work_auth / sponsorship / eeo.* / salary / years_exp / linkedin / portfolio / custom.*`. `eeo.*` (gender/race/veteran/disability/etc.) are NOT canary (they have safe decline defaults resolved deterministically — see Task 5); the canary set is exactly `_CANARY_KEYS`. Confirm this split matches the spec §10.3 canary list before finalizing.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_ir.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/__init__.py src/applypilot/apply/v2/ir.py tests/test_v2_ir.py
git diff --cached --stat   # exactly these 3 files
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: FormSchema IR (Field/Step/FormSchema) + churn-stable fingerprints + canary-key taxonomy (pure)"
```

---

## Task 2: Operator protocol + `_json_call` over `llm.py` (+ structured output) + `ClaudeCLIOperator`

The single AI seam (spec §9). JSON-in → schema-validated JSON-out, enumerated answers by index, no browser access. Built over the existing `llm.py` router so provider swap (Anthropic ↔ OpenAI ↔ Google ↔ local) needs zero engine changes (acceptance #4). Spend is metered because it rides `MeteredClient`. In Phase 3 the ONLY method the apply path strictly needs is `resolve_fields` (see Open Decisions); `score` (§7.3 matching) and `label_controls` (§6.8 degraded tier) are declared in the protocol but implemented minimally/deferred.

**Files:**
- Modify: `src/applypilot/llm.py` (`_chat_compat` gains optional `response_format`; `chat` forwards it)
- Modify: `src/applypilot/spend_ledger.py` (`MeteredClient.chat` + `.ask` forward optional `response_format` — MANDATORY: `get_client()` returns a `MeteredClient`-wrapped client, and its `chat` has no `**kwargs`, so the Operator's `response_format` kwarg raises `TypeError` on the real path)
- Create: `src/applypilot/apply/v2/operator.py`
- Test: `tests/test_v2_operator.py`

- [ ] **Step 1: READ the llm router seam + the metering wrapper + the CLI client**

READ `src/applypilot/llm.py:165-200` (`_chat_compat(self, messages, temperature, max_tokens) -> str` — builds `payload = {"model","messages","temperature","max_tokens"}` then POSTs to `{base_url}/chat/completions`, returns `data["choices"][0]["message"]["content"]`) and `:204-224` (`chat(self, messages, temperature=0.0, max_tokens=4096) -> str`). READ `:301-392` (`ClaudeCodeClient` + its `chat`) and `:393` (`get_client() -> LLMClient | ClaudeCodeClient`). READ `src/applypilot/spend_ledger.py:71-89` (`MeteredClient` — wraps the client, delegates `chat`/`ask`/`close`, records spend per call). CONFIRM the key hazard: `MeteredClient.chat(self, messages, temperature=0.0, max_tokens=4096)` (~line 80) has NO `**kwargs`, and `.ask` filters kwargs to only `temperature`/`max_tokens` — so the Operator's `response_format` kwarg would raise `TypeError` (or be dropped) on the REAL path (`get_client()` returns a `MeteredClient`-wrapped client). This is why Step 4b widens `MeteredClient` — it is MANDATORY, not conditional. Also confirm `get_client()` may return either an `LLMClient` (has `_chat_compat`) OR a `ClaudeCodeClient` (does NOT) before writing the `response_format` plumb — the plumb must degrade gracefully when the underlying client can't take it.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_operator.py
import json

import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import operator as op


class _FakeClient:
    """Call-site-injected fake Operator LLM (mirrors test_answer_cache._LLM):
    captures messages, returns a scripted JSON string per call."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, temperature=0.0, max_tokens=4096, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self._replies.pop(0)


def _req():
    return op.FieldResolutionRequest(
        job_context="Senior Product Designer at Acme",
        fields=[
            op.FieldSpec(field_fp="fp1", question_text="Why do you want to work here?",
                         widget_kind="textarea", options=None, char_limit=500),
            op.FieldSpec(field_fp="fp2", question_text="Preferred work style?",
                         widget_kind="react_select",
                         options=["Remote", "Hybrid", "Onsite"], char_limit=None),
        ],
    )


def test_resolve_fields_text_and_index_answer():
    reply = json.dumps({"answers": [
        {"field_fp": "fp1", "text": "Your design culture resonates with me."},
        {"field_fp": "fp2", "option_index": 0},
    ]})
    client = _FakeClient([reply])
    operator = op.LLMOperator(client=client)
    ans = operator.resolve_fields(_req())
    assert ans.by_fp["fp1"].text == "Your design culture resonates with me."
    assert ans.by_fp["fp2"].option_index == 0        # index, not free text
    assert ans.by_fp["fp2"].text is None


def test_free_typed_option_value_is_rejected_at_schema_layer():
    # A free-typed option value for an enumerated field must be a type error,
    # not silently accepted (invariant 6 — kills the react-select-desync class).
    bad = json.dumps({"answers": [
        {"field_fp": "fp1", "text": "ok"},
        {"field_fp": "fp2", "text": "Fully Remote Forever"},   # not an index!
    ]})
    good = json.dumps({"answers": [
        {"field_fp": "fp1", "text": "ok"},
        {"field_fp": "fp2", "option_index": 1},
    ]})
    client = _FakeClient([bad, good])     # first invalid -> ONE retry -> valid
    operator = op.LLMOperator(client=client)
    ans = operator.resolve_fields(_req())
    assert len(client.calls) == 2                     # exactly one retry
    assert ans.by_fp["fp2"].option_index == 1


def test_out_of_range_index_rejected_then_retry():
    bad = json.dumps({"answers": [{"field_fp": "fp1", "text": "ok"},
                                  {"field_fp": "fp2", "option_index": 9}]})  # OOR
    good = json.dumps({"answers": [{"field_fp": "fp1", "text": "ok"},
                                   {"field_fp": "fp2", "option_index": 2}]})
    operator = op.LLMOperator(client=_FakeClient([bad, good]))
    ans = operator.resolve_fields(_req())
    assert ans.by_fp["fp2"].option_index == 2


def test_two_invalid_replies_parks_field_not_crash():
    junk = "not json at all"
    operator = op.LLMOperator(client=_FakeClient([junk, junk]))
    ans = operator.resolve_fields(_req())
    # After ONE retry still invalid -> cannot_answer, never an exception.
    assert ans.by_fp["fp1"].cannot_answer is True
    assert ans.by_fp["fp2"].cannot_answer is True


def test_char_limit_clamped():
    long = "x" * 999
    reply = json.dumps({"answers": [{"field_fp": "fp1", "text": long},
                                    {"field_fp": "fp2", "option_index": 0}]})
    operator = op.LLMOperator(client=_FakeClient([reply]))
    ans = operator.resolve_fields(_req())
    assert len(ans.by_fp["fp1"].text) <= 500          # clamped to char_limit


def test_response_format_requested_when_supported(monkeypatch):
    # The operator asks the client for JSON object mode when the client accepts it.
    client = _FakeClient([json.dumps({"answers": [
        {"field_fp": "fp1", "text": "ok"}, {"field_fp": "fp2", "option_index": 0}]})])
    op.LLMOperator(client=client).resolve_fields(_req())
    assert client.calls[0]["kwargs"].get("response_format") == {"type": "json_object"}
```

And a focused test that the llm.py plumb is present + backward compatible:

```python
# tests/test_v2_operator.py (continued)
def test_llm_chat_compat_accepts_response_format(monkeypatch):
    from applypilot import llm
    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "{}"}}]}

    c = llm.LLMClient.__new__(llm.LLMClient)   # bypass __init__ network
    c.model = "gpt-x"; c.api_key = "k"; c.base_url = "http://x"; c._is_gemini = False
    c._client = type("C", (), {"post": lambda self, url, json, headers: (captured.update(payload=json) or _Resp())})()
    out = c._chat_compat([{"role": "user", "content": "hi"}], 0.0, 100,
                         response_format={"type": "json_object"})
    assert out == "{}"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    # backward compat: omitting it must NOT add the key
    c._chat_compat([{"role": "user", "content": "hi"}], 0.0, 100)
    assert "response_format" not in captured["payload"]


def test_metered_client_forwards_response_format_to_inner():
    # The REAL path is get_client() -> MeteredClient(inner). Assert MeteredClient
    # does NOT drop response_format (regression guard: its chat() had no **kwargs).
    from applypilot.spend_ledger import MeteredClient, SpendLedger

    captured = {}

    class _Inner:
        def chat(self, messages, temperature=0.0, max_tokens=4096, response_format=None):
            captured["response_format"] = response_format
            return "{}"

    mc_client = MeteredClient(_Inner(), SpendLedger.__new__(SpendLedger), model="m")
    # metering may need a no-op record; if SpendLedger needs init, build it via the
    # normal ctor + a temp path as other spend_ledger tests do.
    op.LLMOperator(client=mc_client).resolve_fields(_req())
    assert captured["response_format"] == {"type": "json_object"}   # reached the inner client
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_operator.py -v`

- [ ] **Step 4: Plumb optional `response_format` through `llm.py`**

In `src/applypilot/llm.py`, edit `_chat_compat` and `chat` to accept an optional `response_format` and include it in the payload only when provided (backward-compatible — existing callers unaffected). ADAPT to the exact current signatures you READ in Step 1.

```python
    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        response_format: dict | None = None,      # <-- new, optional
    ) -> str:
        ...
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:            # <-- only when asked
            payload["response_format"] = response_format
        ...
```

```python
    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict | None = None,       # <-- new, optional
    ) -> str:
        ...
        # wherever chat calls self._chat_compat(messages, temperature, max_tokens):
        return self._chat_compat(messages, temperature, max_tokens,
                                 response_format=response_format)
```

Notes:
- `ClaudeCodeClient.chat` does NOT support `response_format`; add the kwarg to its signature too and simply ignore it (accept-and-drop) so the Operator can pass it uniformly.
- The gemini native-API fallback path (`_GeminiCompatForbidden`) is untouched; response_format is a compat-layer nicety, and the Operator degrades gracefully (it json.loads + validates + retries regardless of whether the provider honored the hint).

- [ ] **Step 4b: Widen `MeteredClient.chat` / `.ask` in `spend_ledger.py` (MANDATORY — this is the real path)**

`get_client()` returns a `MeteredClient`-wrapped client. `MeteredClient.chat` (spend_ledger.py ~line 80) is currently `def chat(self, messages, temperature=0.0, max_tokens=4096) -> str:` with NO `**kwargs`, and `.ask` filters kwargs to only `temperature`/`max_tokens` — so the Operator's `response_format` kwarg would raise `TypeError` (or be silently dropped) on the real path. This is hidden today only because the Operator tests inject a fake client. Widen BOTH to forward `response_format` to the wrapped client:

```python
    def chat(self, messages, temperature: float = 0.0, max_tokens: int = 4096,
             response_format=None) -> str:                    # <-- add response_format
        out = self._inner.chat(messages, temperature=temperature, max_tokens=max_tokens,
                               response_format=response_format)  # <-- forward it
        ...

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **{
            k: v for k, v in kwargs.items()
            if k in ("temperature", "max_tokens", "response_format")})  # <-- allow response_format
```

Because `_json_call` (Step 5) wraps the call in `try/except TypeError` and retries plain, this stays safe even if a wrapped client still can't accept the kwarg — but the metering wrapper MUST NOT be the layer that drops it.

- [ ] **Step 5: Implement `operator.py`**

```python
"""The Operator interface (spec §9) — the only AI in the apply loop.

JSON-in -> schema-validated JSON-out; enumerated answers by INDEX (free-typed
option values are a type error, invariant 6); one retry; no browser access.
Built over llm.get_client() so provider swap needs zero engine changes
(acceptance #4). Spend is metered because get_client()/MeteredClient wraps it.

Phase 3 implements resolve_fields fully; score/label_controls are declared for
transport-agnosticism and stubbed (see plan Open Decisions)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class FieldSpec:
    field_fp: str
    question_text: str
    widget_kind: str
    options: list[str] | None            # None for free-text; list for enumerated
    char_limit: int | None = None


@dataclass
class FieldResolutionRequest:
    job_context: str
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass
class FieldAnswer:
    field_fp: str
    text: str | None = None
    option_index: int | None = None
    cannot_answer: bool = False


@dataclass
class FieldAnswers:
    by_fp: dict[str, FieldAnswer] = field(default_factory=dict)


class Operator(Protocol):
    def score(self, job, rubric, anchors) -> Any: ...                 # §7.3 (Phase 4)
    def resolve_fields(self, request: FieldResolutionRequest) -> FieldAnswers: ...  # §6.5
    def label_controls(self, snapshot) -> Any: ...                    # §6.8 degraded


_SYSTEM = (
    "You fill job-application fields. Return ONLY JSON: "
    '{"answers":[{"field_fp": str, "text": str} | {"field_fp": str, "option_index": int}]}. '
    "For enumerated fields you MUST return option_index (0-based into the given options); "
    "NEVER free-type an option value. For free-text fields return text. "
    "If you cannot answer a field, return {\"field_fp\": str, \"cannot_answer\": true}."
)


class LLMOperator:
    """Default Operator over the llm.py router."""

    def __init__(self, client=None):
        if client is None:
            from applypilot.llm import get_client
            client = get_client()
        self._client = client

    def resolve_fields(self, request: FieldResolutionRequest) -> FieldAnswers:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": self._render(request)},
        ]
        raw = self._json_call(messages)
        parsed = self._validate(raw, request)
        if parsed is None:
            raw = self._json_call(messages)          # ONE retry (spec §6.5)
            parsed = self._validate(raw, request)
        if parsed is None:
            # park-don't-guess: every requested field -> cannot_answer
            return FieldAnswers(by_fp={f.field_fp: FieldAnswer(f.field_fp, cannot_answer=True)
                                       for f in request.fields})
        return parsed

    def _json_call(self, messages: list[dict]) -> str:
        try:
            return self._client.chat(messages, temperature=0.0, max_tokens=1024,
                                     response_format={"type": "json_object"})
        except TypeError:
            # Client doesn't accept response_format (older signature) -> retry plain.
            return self._client.chat(messages, temperature=0.0, max_tokens=1024)

    @staticmethod
    def _render(request: FieldResolutionRequest) -> str:
        payload = {"job": request.job_context, "fields": [
            {"field_fp": f.field_fp, "question": f.question_text,
             "widget": f.widget_kind, "char_limit": f.char_limit,
             "options": f.options} for f in request.fields]}
        return json.dumps(payload)

    @staticmethod
    def _validate(raw: str, request: FieldResolutionRequest) -> FieldAnswers | None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("answers"), list):
            return None
        spec_by_fp = {f.field_fp: f for f in request.fields}
        out = FieldAnswers()
        for a in data["answers"]:
            if not isinstance(a, dict):
                return None
            fp = a.get("field_fp")
            spec = spec_by_fp.get(fp)
            if spec is None:
                return None
            if a.get("cannot_answer") is True:
                out.by_fp[fp] = FieldAnswer(fp, cannot_answer=True)
                continue
            if spec.options is not None:                 # enumerated -> index only
                idx = a.get("option_index")
                if not isinstance(idx, int) or not (0 <= idx < len(spec.options)):
                    return None                          # free text / OOR -> invalid
                out.by_fp[fp] = FieldAnswer(fp, option_index=idx)
            else:                                        # free-text -> text only
                txt = a.get("text")
                if not isinstance(txt, str):
                    return None
                if spec.char_limit:
                    txt = txt[:spec.char_limit]
                out.by_fp[fp] = FieldAnswer(fp, text=txt)
        # every requested field must be answered (else re-ask)
        if set(out.by_fp) != set(spec_by_fp):
            return None
        return out

    # Declared for transport-agnosticism; Phase 4 fills these in.
    def score(self, job, rubric, anchors):
        raise NotImplementedError("score is Phase 4 (compile-then-score matching)")

    def label_controls(self, snapshot):
        raise NotImplementedError("label_controls is the Phase-4 degraded tier")
```

`ClaudeCLIOperator` — the quarantined subscription/no-API-key provider (spec §9). Same file, thin subclass swapping the client:

```python
class ClaudeCLIOperator(LLMOperator):
    """Quarantined provider preserving the Claude Code CLI (subscription, no API
    key) path — proves the Operator is transport-agnostic. Wraps ClaudeCodeClient
    (which ignores response_format; _json_call's TypeError fallback handles it)."""

    def __init__(self):
        from applypilot.llm import ClaudeCodeClient
        super().__init__(client=ClaudeCodeClient())
```

- [ ] **Step 6: Run — expect pass, then confirm no llm regressions**

Run: `& $PY -m pytest tests/test_v2_operator.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -k "llm or score or scorer" -q`
Expected: no regressions from the `response_format` plumb.

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/llm.py src/applypilot/spend_ledger.py src/applypilot/apply/v2/operator.py tests/test_v2_operator.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: Operator protocol + LLMOperator._json_call (index-only enum, 1 retry, park-dont-guess) + response_format plumb + ClaudeCLIOperator"
```

---

## Task 3: `mapping_cache` table + repo (demote-never-archive, bindings-not-values)

The resolver's per-`field_fp` cache (spec §6.4). Stores a BINDING (`profile.<path>` or `answer:<question_fp>`) + the winning locator tier + widget-driver name — NEVER a literal answer (invariant 3, privacy-safe, survives profile edits). Demote-never-archive: 2 verified failures demote a mapping for re-resolution; rows are versioned and kept, never deleted. Also stores the auto-harvested submit-endpoint signature per (ats, company) that Tier-1 verify writes on confirmed success (Task 8).

**Files:**
- Modify: `src/applypilot/database.py` (`init_db`: add `mapping_cache` CREATE TABLE)
- Create: `src/applypilot/apply/v2/mapping_cache.py`
- Test: `tests/test_v2_mapping_cache.py`

- [ ] **Step 1: READ where standalone tables are created**

READ `src/applypilot/database.py:238-239` — the `source_runs` CREATE TABLE (Phase 2) sits directly before the single `conn.commit()`. If Phase 2 has landed, add `mapping_cache` immediately after `source_runs` (still before the commit). If Phase 2 has NOT landed on this branch, add it after the `engine_control` block instead — either way it is a standalone table (NOT in `_ALL_COLUMNS`), like `submission_ledger`/`engine_control`/`boards`/`source_runs`. Confirm the exact insertion point + that `conn.commit()` follows before writing.

- [ ] **Step 2: Add the `mapping_cache` table to `init_db`**

```python
    # v2 Form Compiler resolver cache (spec §6.4). One row per (ats, field_fp).
    # Stores a BINDING, never a literal value (privacy-safe, survives profile
    # edits): binding is 'profile.<path>' or 'answer:<question_fp>' or
    # 'policy.<key>'. Demote-never-archive: fail_streak >= 2 demotes (active=0)
    # for re-resolution; rows are versioned + kept, never deleted. Also holds
    # the auto-harvested submit-endpoint signature per (ats, company) that
    # Tier-1 network-evidence verify writes on confirmed success.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mapping_cache (
            ats            TEXT NOT NULL,       -- 'greenhouse'
            field_fp       TEXT NOT NULL,       -- ir.field_fp (primary key part)
            binding        TEXT NOT NULL,       -- 'profile.<path>' | 'answer:<qfp>' | 'policy.<k>'
            widget_driver  TEXT,                -- registry key ('text','react_select',...)
            locator_tier   TEXT,                -- winning healing tier ('role_name','label',...)
            version        INTEGER NOT NULL DEFAULT 1,
            active         INTEGER NOT NULL DEFAULT 1,  -- 0 = demoted (re-resolve)
            fail_streak    INTEGER NOT NULL DEFAULT 0,
            hits           INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            PRIMARY KEY (ats, field_fp)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submit_endpoints (
            ats            TEXT NOT NULL,       -- 'greenhouse'
            company        TEXT NOT NULL,       -- board token / company slug
            method         TEXT NOT NULL,       -- 'POST'
            url_pattern    TEXT NOT NULL,       -- harvested submit endpoint (host+path)
            seen_count     INTEGER NOT NULL DEFAULT 1,
            first_seen     TEXT NOT NULL,
            last_seen      TEXT NOT NULL,
            PRIMARY KEY (ats, company, url_pattern)
        )
    """)
```

(Keep both `CREATE TABLE`s inside the same `init_db` block before `conn.commit()`.)

- [ ] **Step 3: Write failing tests**

```python
# tests/test_v2_mapping_cache.py
from applypilot import database as db
from applypilot.apply.v2 import mapping_cache as mc


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


def test_put_then_get_active_binding(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.personal.email",
           widget_driver="text", locator_tier="label")
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["binding"] == "profile.personal.email"
    assert row["widget_driver"] == "text" and row["locator_tier"] == "label"
    assert row["active"] == 1 and row["fail_streak"] == 0 and row["version"] == 1


def test_put_never_stores_literal_value(tmp_path):
    # Contract: only bindings. A literal-looking binding is still just a string,
    # but the repo API has no 'value' param at all — enforced structurally.
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp2", binding="answer:qfpXYZ", widget_driver="textarea")
    assert "value" not in mc.get(conn, "greenhouse", "fp2")   # column does not exist


def test_hit_increments_and_stays_active(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.personal.phone", widget_driver="phone_intl")
    mc.record_success(conn, "greenhouse", "fp1")
    mc.record_success(conn, "greenhouse", "fp1")
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["hits"] == 2 and row["fail_streak"] == 0 and row["active"] == 1


def test_demote_after_two_verified_failures_never_deletes(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.personal.city", widget_driver="typeahead_location")
    mc.record_failure(conn, "greenhouse", "fp1")
    assert mc.get(conn, "greenhouse", "fp1")["active"] == 1     # one failure: still active
    mc.record_failure(conn, "greenhouse", "fp1")
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["active"] == 0 and row["fail_streak"] == 2       # demoted, NOT deleted
    # get_active returns None for a demoted mapping (resolver re-resolves)
    assert mc.get_active(conn, "greenhouse", "fp1") is None
    assert mc.get(conn, "greenhouse", "fp1") is not None        # row survives (audit)


def test_reput_bumps_version_and_reactivates(tmp_path):
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.a", widget_driver="text")
    mc.record_failure(conn, "greenhouse", "fp1"); mc.record_failure(conn, "greenhouse", "fp1")
    assert mc.get(conn, "greenhouse", "fp1")["active"] == 0
    mc.put(conn, "greenhouse", "fp1", binding="profile.b", widget_driver="text")  # re-resolved
    row = mc.get(conn, "greenhouse", "fp1")
    assert row["binding"] == "profile.b" and row["version"] == 2
    assert row["active"] == 1 and row["fail_streak"] == 0       # revived on re-resolution


def test_submit_endpoint_harvest_upsert(tmp_path):
    conn = _conn(tmp_path)
    mc.record_submit_endpoint(conn, "greenhouse", "acme", "POST",
                              "boards.greenhouse.io/acme/applications")
    mc.record_submit_endpoint(conn, "greenhouse", "acme", "POST",
                              "boards.greenhouse.io/acme/applications")
    eps = mc.get_submit_endpoints(conn, "greenhouse", "acme")
    assert len(eps) == 1 and eps[0]["seen_count"] == 2         # upsert, count bumps


def test_hit_rate_measurable(tmp_path):
    # invariant 10 / risk "fingerprint hit-rate overstated": hit rate is queryable.
    conn = _conn(tmp_path)
    mc.put(conn, "greenhouse", "fp1", binding="profile.a", widget_driver="text")
    mc.record_success(conn, "greenhouse", "fp1")
    stats = mc.stats(conn, "greenhouse")
    assert stats["active_mappings"] == 1 and stats["total_hits"] == 1
```

- [ ] **Step 4: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_mapping_cache.py -v`

- [ ] **Step 5: Implement `mapping_cache.py`**

```python
"""Resolver mapping cache (spec §6.4). Demote-never-archive; bindings, never
values (invariant 3). Every fn takes an explicit sqlite3.Connection so it
composes with get_connection() and in-memory test DBs."""
from __future__ import annotations

from datetime import datetime, timezone

_DEMOTE_AT = 2      # 2 verified failures demote a mapping (spec §6.4)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(conn, ats: str, field_fp: str) -> dict | None:
    row = conn.execute("SELECT * FROM mapping_cache WHERE ats=? AND field_fp=?",
                       (ats, field_fp)).fetchone()
    return dict(row) if row else None


def get_active(conn, ats: str, field_fp: str) -> dict | None:
    """The binding the resolver should use, or None if absent/demoted."""
    row = conn.execute(
        "SELECT * FROM mapping_cache WHERE ats=? AND field_fp=? AND active=1",
        (ats, field_fp)).fetchone()
    return dict(row) if row else None


def put(conn, ats: str, field_fp: str, *, binding: str,
        widget_driver: str | None = None, locator_tier: str | None = None) -> None:
    """Insert a new active mapping, or (on re-resolution of a demoted/changed
    row) bump version and reactivate. NEVER accepts a literal value."""
    now = _now()
    existing = get(conn, ats, field_fp)
    if existing is None:
        conn.execute(
            "INSERT INTO mapping_cache (ats, field_fp, binding, widget_driver, "
            "locator_tier, version, active, fail_streak, hits, created_at, updated_at) "
            "VALUES (?,?,?,?,?,1,1,0,0,?,?)",
            (ats, field_fp, binding, widget_driver, locator_tier, now, now))
    else:
        conn.execute(
            "UPDATE mapping_cache SET binding=?, widget_driver=?, locator_tier=?, "
            "version=version+1, active=1, fail_streak=0, updated_at=? "
            "WHERE ats=? AND field_fp=?",
            (binding, widget_driver, locator_tier, now, ats, field_fp))
    conn.commit()


def record_success(conn, ats: str, field_fp: str) -> None:
    conn.execute(
        "UPDATE mapping_cache SET hits=hits+1, fail_streak=0, updated_at=? "
        "WHERE ats=? AND field_fp=?", (_now(), ats, field_fp))
    conn.commit()


def record_failure(conn, ats: str, field_fp: str) -> None:
    """A VERIFIED failure (commit read-back said not-committed). Bumps the
    streak; at _DEMOTE_AT it demotes (active=0) but NEVER deletes the row."""
    conn.execute(
        "UPDATE mapping_cache SET fail_streak=fail_streak+1, updated_at=? "
        "WHERE ats=? AND field_fp=?", (_now(), ats, field_fp))
    conn.execute(
        "UPDATE mapping_cache SET active=0 WHERE ats=? AND field_fp=? AND fail_streak>=?",
        (ats, field_fp, _DEMOTE_AT))
    conn.commit()


def record_submit_endpoint(conn, ats: str, company: str, method: str, url_pattern: str) -> None:
    now = _now()
    existing = conn.execute(
        "SELECT seen_count FROM submit_endpoints WHERE ats=? AND company=? AND url_pattern=?",
        (ats, company, url_pattern)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO submit_endpoints (ats, company, method, url_pattern, seen_count, "
            "first_seen, last_seen) VALUES (?,?,?,?,1,?,?)",
            (ats, company, method, url_pattern, now, now))
    else:
        conn.execute(
            "UPDATE submit_endpoints SET seen_count=seen_count+1, last_seen=? "
            "WHERE ats=? AND company=? AND url_pattern=?", (now, ats, company, url_pattern))
    conn.commit()


def get_submit_endpoints(conn, ats: str, company: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM submit_endpoints WHERE ats=? AND company=? ORDER BY seen_count DESC",
        (ats, company)).fetchall()
    return [dict(r) for r in rows]


def stats(conn, ats: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE active=1), COALESCE(SUM(hits),0), COUNT(*) "
        "FROM mapping_cache WHERE ats=?", (ats,)).fetchone()
    return {"active_mappings": row[0], "total_hits": row[1], "total_mappings": row[2]}
```

Note: `COUNT(*) FILTER (WHERE ...)` needs SQLite ≥ 3.30 (bundled with Python 3.12). If the test env's SQLite is older, rewrite `stats` with `SUM(CASE WHEN active=1 THEN 1 ELSE 0 END)`. READ the failure if `test_hit_rate_measurable` errors and adapt.

- [ ] **Step 6: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_mapping_cache.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/database.py src/applypilot/apply/v2/mapping_cache.py tests/test_v2_mapping_cache.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: mapping_cache + submit_endpoints tables + repo (demote-never-archive, bindings-not-values, endpoint harvest)"
```

---

## Task 4: Greenhouse front-end — `_OBSERVE_JS` observation → FormSchema IR (promote `_standard_plan` taxonomy)

The Parse stage (spec §6.3). A PURE DOM→IR translator: it consumes a `BrowserObservation` (from the reused `browser_stream.collect_browser_observation`) and emits a `FormSchema`. It assigns `semantic_key` deterministically from the promoted label-synonym tables, classifies each control into a widget kind, seeds each `Field.locator_spec` (a dict that becomes an `ElementSpec` at fill time), and marks `options=LAZY` on every enumerated control. It MUST NOT open dropdowns (invariant 4) — react-select controls carry `options=LAZY`, never a read list.

**Files:**
- Create: `src/applypilot/apply/v2/frontend_greenhouse.py`
- Test: `tests/test_v2_frontend_greenhouse.py`

- [ ] **Step 1: READ the observation shape + the taxonomy source**

READ `src/applypilot/apply/browser_stream.py:58-104` (`ControlObservation` fields: `control_id, label, role, control_type, selector, value, required, disabled, invalid, visible, frame_index, frame_url, bbox`; `BrowserObservation.controls / .submit_buttons / .required_missing / .validation_errors`) and `:429-496` (`collect_browser_observation(page) -> BrowserObservation`, per-frame merge). READ `src/applypilot/apply/adapters/greenhouse.py:74-139` (`_standard_plan` — the `key`/`labels`/`kind` synonym table this task PROMOTES verbatim: first_name, last_name, email, phone, location, linkedin, portfolio, resume, work_authorization, sponsorship, sexual_orientation, gender_identity, first_generation, gender, race, veteran, disability). Confirm the `control_type` strings `_OBSERVE_JS` emits for `<select>`, react-select `<input>`, `<textarea>`, `type=file`, radios/checkboxes before writing the widget classifier (READ `_OBSERVE_JS` lines 106-425 if the control_type vocabulary is unclear — adapt the classifier to the ACTUAL strings emitted).

- [ ] **Step 2: Write failing tests** (synthetic DOM via the `page` fixture + `set_content`, real `collect_browser_observation`)

```python
# tests/test_v2_frontend_greenhouse.py
import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import frontend_greenhouse as fe
from applypilot.apply.browser_stream import collect_browser_observation


# Reuse the adapter-test synthetic form shape (label+control pairs, a react-
# select-ish combobox, a required custom textarea, a submit button).
_FORM = """
<!doctype html><html><body>
<form id="application_form">
  <label for="fn">First name</label>
  <input id="fn" name="first_name" type="text" required class="gh-in a1">
  <label for="em">Email</label>
  <input id="em" name="email" type="email" required class="gh-in c3">
  <label for="loc">Current location (City)</label>
  <input id="loc" name="location" type="text" class="gh-in e5">
  <label for="rz">Resume/CV</label>
  <input id="rz" name="resume" type="file" required class="gh-file g7">
  <label for="wa-i">Are you legally authorized to work in the US?</label>
  <div class="select__control" id="wa-c" tabindex="0">
    <span class="select__single-value">Select...</span>
    <input class="select__input" id="wa-i" role="combobox" autocomplete="off" aria-required="true">
  </div>
  <label for="cust">Describe a product you shipped</label>
  <textarea id="cust" name="why_8801" required maxlength="500" class="gh-ta z9"></textarea>
  <button id="sub" type="button">Submit application</button>
</form></body></html>
"""


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


def _schema(page):
    page.set_content(_FORM)
    obs = collect_browser_observation(page)
    return fe.parse_observation(obs, company="acme", url="https://boards.greenhouse.io/acme/jobs/1")


def test_assigns_semantic_keys_from_promoted_taxonomy(page):
    schema = _schema(page)
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert "first_name" in by_key and "email" in by_key
    assert "location" in by_key and "resume" in by_key
    assert "work_auth" in by_key                      # promoted work_authorization synonyms


def test_custom_question_gets_custom_key_not_none(page):
    schema = _schema(page)
    cust = [f for s in schema.steps for f in s.fields
            if f.semantic_key and f.semantic_key.startswith("custom.")]
    assert len(cust) == 1
    assert "product" in cust[0].question_text.lower()
    # front-end can't read maxlength from the observation; the oracle length-clamps
    # text answers (§6.5) and the textarea driver clamps only when char_limit is
    # explicitly set.
    assert cust[0].char_limit is None


def test_widget_kinds_classified(page):
    schema = _schema(page)
    kinds = {f.semantic_key: f.widget.kind for s in schema.steps for f in s.fields}
    assert kinds["first_name"] == "text"
    assert kinds["email"] == "text"
    assert kinds["resume"] == "file"
    assert kinds["work_auth"] == "react_select"       # select__control -> react_select
    cust_kind = [f.widget.kind for s in schema.steps for f in s.fields
                 if f.semantic_key.startswith("custom.")][0]
    assert cust_kind == "textarea"


def test_options_are_lazy_never_enumerated_at_parse(page):
    schema = _schema(page)
    for s in schema.steps:
        for f in s.fields:
            if f.widget.kind in ("react_select", "native_select"):
                assert f.options is ir.LAZY          # invariant 4: never opened at parse


def test_required_flags_and_locator_spec_seeded(page):
    schema = _schema(page)
    by_key = {f.semantic_key: f for s in schema.steps for f in s.fields}
    assert by_key["first_name"].required is True
    assert by_key["location"].required is False
    # locator_spec is a dict usable to build a healing.ElementSpec at fill time
    ls = by_key["first_name"].locator_spec
    assert ls.get("label") and (ls.get("elem_id") or ls.get("name_attr"))


def test_terminal_step_and_advance_control(page):
    schema = _schema(page)
    assert len(schema.steps) == 1
    assert schema.steps[0].terminal is True           # single-step GH form
    assert schema.steps[0].advance_control is not None  # the Submit button locator_spec


def test_canary_keys_flagged_on_schema(page):
    schema = _schema(page)
    wa = [f for s in schema.steps for f in s.fields if f.semantic_key == "work_auth"][0]
    assert ir.is_canary_key(wa.semantic_key) is True  # resolver will not oracle it
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_frontend_greenhouse.py -v`

- [ ] **Step 4: Implement `frontend_greenhouse.py`**

Promote the `_standard_plan` label-synonym table into a `_SEMANTIC_SYNONYMS` mapping (`semantic_key -> tuple[label-substring, ...]`), remapping the adapter's keys to the IR taxonomy (`work_authorization -> work_auth`, `gender/race/veteran/disability/... -> eeo.*`). Match on normalized label substring, most-specific first (mirror the adapter's ordering comment: specific EEO labels before the generic "gender").

```python
"""Greenhouse front-end: BrowserObservation -> FormSchema IR (spec §6.3).

PURE translation (no browser I/O of its own — it consumes an observation the
reused browser_stream.collect_browser_observation produced). Assigns semantic
keys from the promoted _standard_plan synonym table, classifies widget kinds,
seeds locator_spec, and marks every enumerated control options=LAZY (invariant
4 — dropdowns are NEVER opened at parse)."""
from __future__ import annotations

import re

from applypilot.apply.v2 import ir

# Promoted verbatim from adapters/greenhouse._standard_plan, remapped to the IR
# taxonomy. Order matters: most-specific labels first so "...transgender..."
# isn't hijacked by the generic "gender".
_SEMANTIC_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("first_name", ("legal first name", "first name")),
    ("last_name", ("legal last name", "last name", "surname")),
    ("email", ("email",)),
    ("phone", ("phone", "mobile")),
    ("location", ("current location", "location", "city", "where are you")),
    ("linkedin", ("linkedin",)),
    ("portfolio", ("portfolio", "website", "personal site")),
    ("resume", ("resume", "cv", "resume/cv")),
    ("work_auth", ("authorized to work", "legally authorized", "work authorization",
                   "eligible to work", "currently authorized")),
    ("sponsorship", ("sponsorship", "require sponsorship", "need sponsorship", "visa")),
    ("eeo.sexual_orientation", ("sexual orientation",)),
    ("eeo.gender_identity", ("gender identity", "identify as transgender", "transgender")),
    ("eeo.first_generation", ("first-generation", "first generation")),
    ("eeo.gender", ("gender",)),
    ("eeo.race", ("race", "ethnicity", "race/ethnicity")),
    ("eeo.veteran", ("veteran",)),
    ("eeo.disability", ("disability",)),
]

_CUSTOM_PREFIX = "custom."


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _semantic_key(label: str, question: str) -> str | None:
    hay = _norm(f"{label} {question}")
    for key, needles in _SEMANTIC_SYNONYMS:
        if any(n in hay for n in needles):
            return key
    return None


def _custom_key(question: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", _norm(question)).strip("_")[:40] or "unnamed"
    return _CUSTOM_PREFIX + slug


def _widget_kind(ctrl) -> str:
    t = (ctrl.control_type or "").lower()
    role = (ctrl.role or "").lower()
    if t == "file":
        return "file"
    if t == "textarea":
        return "textarea"
    if t in ("select", "select-one", "native_select"):
        return "native_select"
    if role == "combobox" or "select__" in (ctrl.selector or "").lower() or t == "combobox":
        return "react_select"
    if t in ("radio",):
        return "radio_group"
    if t in ("checkbox",):
        return "checkbox"
    if t in ("date",):
        return "date"
    return "text"


def _locator_spec(ctrl) -> dict:
    """Dict that build_element_spec() (Task 5) turns into a healing.ElementSpec.
    Seeds the label + role + stable ids/names; healing decides the winning tier
    at fill time and writes it back to the mapping cache."""
    return {
        "label": ctrl.label or None,
        "name": ctrl.label or None,
        "role": ctrl.role or None,
        "elem_id": ctrl.control_id or None,
        "selector": ctrl.selector or None,
        "frame_url": ctrl.frame_url or None,
    }


def _to_field(ctrl) -> ir.Field:
    label = ctrl.label or ""
    question = ctrl.label or ""
    sem = _semantic_key(label, question) or _custom_key(question)
    kind = _widget_kind(ctrl)
    # char_limit is ALWAYS None at parse: _OBSERVE_JS / ControlObservation do not
    # capture maxlength, so there is nothing to read here (a `maxlength=(\d+)`
    # regex over ctrl.selector would always miss — it was dead code). The oracle
    # length-clamps free-text answers (§6.5) and the textarea DRIVER clamps only
    # when char_limit is explicitly set (Task 6). See note below.
    return ir.Field(
        field_id=ctrl.control_id or ctrl.selector or "",
        frame_path=(ctrl.frame_url,) if ctrl.frame_url else (),
        label_text=label,
        question_text=question,
        semantic_key=sem,
        widget=ir.Widget(kind=kind),
        options=ir.LAZY,
        required=bool(ctrl.required),
        char_limit=None,
        locator_spec=_locator_spec(ctrl),
    )


def parse_observation(obs, *, company: str, url: str) -> ir.FormSchema:
    fields = [_to_field(c) for c in obs.controls if c.visible and (c.label or c.control_id)]
    advance = None
    if obs.submit_buttons:
        b = obs.submit_buttons[0]
        advance = {"role": "button", "name": b.label or "Submit application",
                   "label": b.label or "Submit application", "text": b.label,
                   "selector": b.selector or None}
    step = ir.Step(index=0, fields=fields, advance_control=advance, terminal=True)
    return ir.FormSchema(ats="greenhouse", company=company, url=url, steps=[step])
```

Notes / adapt-as-needed:
- **char_limit source (DECIDED).** `_OBSERVE_JS` / `ControlObservation` do NOT surface `maxlength` (verified: it is not on any `ControlObservation` field, so a front-end regex over `ctrl.selector` always misses — that regex was dead code and has been removed). The front-end therefore sets `char_limit=None` unconditionally; length control lives elsewhere: the oracle length-clamps free-text answers (§6.5) and the textarea DRIVER (Task 6) clamps only when `char_limit` is explicitly set on a field. `test_custom_question_gets_custom_key_not_none` asserts `char_limit is None` accordingly. Do NOT edit the reused `browser_stream` module to surface maxlength in Phase 3.
- **Multi-step is out of scope for the Greenhouse single-step form** (Greenhouse is one page). The `Step` list has exactly one terminal step. The executor (Task 7) still LOOPS over steps so Phase-4 multi-step ATSes drop in without an executor rewrite — but the front-end only ever emits one step for Greenhouse.
- **iframe/frame_path.** `collect_browser_observation` already merges child frames and stamps `frame_url` per control (vanity-domain embeds). `frame_path` uses `frame_url`; the executor resolves the right frame from it. Confirm `frame_url` is populated for embedded forms (it is, per the merge loop).

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_frontend_greenhouse.py -v`
Expected: ALL PASS. If `_widget_kind` misclassifies the react-select (the synthetic uses `role="combobox"` + `select__` selector), READ the actual `control_type`/`role`/`selector` your observation produced (print the obs in a scratch script) and tighten the classifier.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/frontend_greenhouse.py tests/test_v2_frontend_greenhouse.py
# NOTE: browser_stream.py is NOT touched — char_limit stays None at parse (maxlength
# is not in the observation); the oracle + textarea driver own length clamping.
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: Greenhouse front-end (BrowserObservation -> FormSchema; promote _standard_plan taxonomy; options=LAZY, never open dropdowns at parse)"
```

---

## Task 5: Resolver — IR + profile + answer_cache + canary + mapping_cache → FillPlan (deterministic, zero I/O)

The Resolve stage (spec §6.4). PURE and zero-I/O except the DB reads (mapping_cache lookup, answer bank). Walks the resolver ladder per field: (a) `semantic_key` → profile path; (b) canary guard (canary keys resolve ONLY from exact profile paths / typed policy defaults — never oracle, never bank, invariant 7); (c) answer-bank hit via the reused `AnswerCache`; (d) mapping-cache hit per `field_fp`; (e) unresolved → batched to the Operator. Produces a `FillPlan`: an ordered list of `PlannedField` (field + resolved binding + concrete value/index-intent + driver name) plus the list of fields that still need the oracle. Promotes canary-first ordering and the safe-answer policy table (EEO decline chains, hard refusals).

**Files:**
- Create: `src/applypilot/apply/v2/resolver.py`
- Test: `tests/test_v2_resolver.py`

- [ ] **Step 1: READ the answer bank + profile shape + healing spec builder inputs**

READ `src/applypilot/apply/answer_cache.py:120-124` (`AnswerCache(profile, bank_path=None, *, threshold=0.70)`) and `:180` (`answer(self, question, *, context="", llm_fn=None) -> AnswerResult`) — note the §10.3 hardening already scrubs canary entries, so the bank is safe to consult for NON-canary custom questions only. READ the profile shape from `tests/test_greenhouse_adapter.py:99-110` (`personal.{full_name,email,phone,city,linkedin_url,portfolio_url}`, `work_authorization.{legally_authorized_to_work,require_sponsorship}`, `eeo_voluntary.{gender,race_ethnicity,veteran_status,disability_status}`). READ `src/applypilot/apply/healing.py:52-72` (`ElementSpec` fields) so `build_element_spec(locator_spec_dict) -> ElementSpec` maps the front-end's dict to the real dataclass. Confirm `AnswerResult` has a `.answer`/`.source` shape (READ the `AnswerResult` dataclass near the top of answer_cache.py) before consuming it.

- [ ] **Step 2: Write failing tests** (temp DB for mapping_cache; injected profile; fake Operator)

```python
# tests/test_v2_resolver.py
from applypilot import database as db
from applypilot.apply.v2 import ir
from applypilot.apply.v2 import resolver as rz
from applypilot.apply.v2 import mapping_cache as mc


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


PROFILE = {
    "personal": {"full_name": "Nida Shah", "email": "nida@example.com",
                 "phone": "4081234567", "city": "San Jose",
                 "linkedin_url": "https://linkedin.com/in/nidashah"},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True},
    "eeo_voluntary": {"gender": "Decline to self-identify",
                      "race_ethnicity": "Decline to self-identify"},
}


def _field(sem, kind="text", label="x", options=ir.LAZY, required=True, fid="id1"):
    return ir.Field(field_id=fid, frame_path=(), label_text=label, question_text=label,
                    semantic_key=sem, widget=ir.Widget(kind=kind), options=options, required=required)


def _schema(fields):
    return ir.FormSchema(ats="greenhouse", company="acme", url="u",
                         steps=[ir.Step(index=0, fields=fields, terminal=True)])


def test_semantic_key_resolves_from_profile_path(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("first_name", label="First name"),
                      _field("email", label="Email")])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    assert byk["first_name"].value == "Nida"        # first token of full_name
    assert byk["email"].value == "nida@example.com"
    assert byk["first_name"].binding == "profile.personal.full_name"
    assert plan.needs_oracle == []                  # both resolved deterministically


def test_canary_never_oracled_and_only_from_profile(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("work_auth", kind="react_select",
                             label="Are you authorized to work?", options=ir.LAZY),
                      _field("sponsorship", kind="react_select",
                             label="Do you require sponsorship?", options=ir.LAZY)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    # canary -> resolved to an INTENT string from profile; never queued to oracle
    assert byk["work_auth"].option_intent == "yes"          # legally_authorized_to_work True
    assert byk["sponsorship"].option_intent == "yes"        # require_sponsorship True
    assert all(f.semantic_key not in ("work_auth", "sponsorship") for f in plan.needs_oracle)


def test_eeo_resolves_to_decline_default_not_oracle(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("eeo.gender", kind="react_select", label="Gender", options=ir.LAZY),
                      _field("eeo.veteran", kind="react_select", label="Veteran status", options=ir.LAZY)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    assert byk["eeo.gender"].option_intent == "decline to self-identify"
    assert byk["eeo.veteran"].option_intent  # a safe decline default even absent in profile
    assert plan.needs_oracle == []                  # EEO has safe canonical answers


def test_custom_question_unresolved_goes_to_oracle(tmp_path):
    conn = _conn(tmp_path)
    schema = _schema([_field("custom.why_us", kind="textarea",
                             label="Why do you want to work here?", options=None)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    assert len(plan.needs_oracle) == 1
    assert plan.needs_oracle[0].semantic_key == "custom.why_us"


def test_mapping_cache_hit_skips_reresolution(tmp_path):
    conn = _conn(tmp_path)
    f = _field("custom.why_us", kind="textarea",
               label="Why do you want to work here?", options=None)
    fp = ir.field_fp(f)
    # a previously-learned binding to a cached answer
    mc.put(conn, "greenhouse", fp, binding="answer:qfp123", widget_driver="textarea")
    schema = _schema([f])
    # seed the answer bank with the qfp the binding points at (via context)
    plan = rz.resolve(schema, PROFILE, conn=conn,
                      answer_lookup=lambda binding: "Cached why-us answer"
                      if binding == "answer:qfp123" else None)
    byk = {pf.field.semantic_key: pf for pf in plan.planned}
    assert byk["custom.why_us"].value == "Cached why-us answer"
    assert plan.needs_oracle == []                  # cache hit -> no oracle


def test_hard_refusal_parks_uncovered_legal_attestation(tmp_path):
    conn = _conn(tmp_path)
    # a legal attestation the profile does not cover -> park, never guess (spec §6.4)
    schema = _schema([_field("custom.felony_attestation", kind="react_select",
                             label="I attest under penalty of perjury...", options=ir.LAZY)])
    plan = rz.resolve(schema, PROFILE, conn=conn)
    parked = [pf for pf in plan.planned if pf.park]
    # either parked directly, or routed to oracle which will cannot_answer -> park.
    assert parked or plan.needs_oracle    # not silently filled with a guess


def test_build_element_spec_maps_locator_dict():
    from applypilot.apply.healing import ElementSpec
    spec = rz.build_element_spec({"label": "Email", "role": "textbox",
                                  "elem_id": "em", "name": "Email"})
    assert isinstance(spec, ElementSpec)
    assert spec.label == "Email" and spec.elem_id == "em"
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_resolver.py -v`

- [ ] **Step 4: Implement `resolver.py`**

```python
"""Resolver (spec §6.4): FormSchema + profile + answer_cache + mapping_cache ->
FillPlan. Deterministic ladder; canary-first (invariant 7); demote-never-archive
cache; safe-answer policy table (EEO decline, hard refusals). Zero I/O beyond
the DB reads."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from applypilot.apply.healing import ElementSpec
from applypilot.apply.v2 import ir
from applypilot.apply.v2 import mapping_cache as mc

# semantic_key -> profile path (dotted). Canary keys map here EXCLUSIVELY.
_PROFILE_PATHS = {
    "first_name": "personal.full_name",     # split at fill
    "last_name": "personal.full_name",
    "email": "personal.email",
    "phone": "personal.phone",
    "location": "personal.city",
    "linkedin": "personal.linkedin_url",
    "portfolio": "personal.portfolio_url",
    "work_auth": "work_authorization.legally_authorized_to_work",
    "sponsorship": "work_authorization.require_sponsorship",
}

# EEO safe canonical answers (spec §6.4 policy table). Absent-in-profile -> decline.
_EEO_DEFAULTS = {
    "eeo.gender": "eeo_voluntary.gender",
    "eeo.race": "eeo_voluntary.race_ethnicity",
    "eeo.veteran": "eeo_voluntary.veteran_status",
    "eeo.disability": "eeo_voluntary.disability_status",
    "eeo.gender_identity": "eeo_voluntary.gender_identity",
    "eeo.sexual_orientation": "eeo_voluntary.sexual_orientation",
    "eeo.first_generation": "eeo_voluntary.first_generation",
}
_DECLINE = "decline to self-identify"

# Labels that are legal attestations the profile cannot cover -> park, never guess.
_HARD_REFUSAL_MARKERS = ("penalty of perjury", "i attest", "i certify under", "felony")


@dataclass
class PlannedField:
    field: ir.Field
    binding: str | None = None            # profile.<path> | answer:<qfp> | policy.<k>
    value: str | None = None              # concrete text for text/file drivers
    option_intent: str | None = None      # intent string for enumerated drivers
    driver: str = "text"                  # widget-driver registry key
    park: bool = False                    # park-don't-guess (required + no safe answer)


@dataclass
class FillPlan:
    planned: list[PlannedField] = field(default_factory=list)
    needs_oracle: list[ir.Field] = field(default_factory=list)


def _dig(profile: dict, dotted: str) -> Any:
    cur: Any = profile
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _yn(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v or "").strip().lower()


def build_element_spec(locator_spec: dict) -> ElementSpec:
    """Front-end locator_spec dict -> healing.ElementSpec (fill-time input)."""
    return ElementSpec(
        role=locator_spec.get("role"),
        name=locator_spec.get("name"),
        label=locator_spec.get("label"),
        elem_id=locator_spec.get("elem_id"),
        name_attr=locator_spec.get("name"),
        css_fallbacks=[locator_spec["selector"]] if locator_spec.get("selector") else [],
        fingerprint={"label_text": locator_spec.get("label"), "tag": "input"},
    )


def _driver_for(kind: str) -> str:
    return {"text": "text", "textarea": "textarea", "file": "file",
            "react_select": "react_select", "native_select": "native_select",
            "phone_intl": "phone_intl", "typeahead_location": "typeahead_location",
            "radio_group": "radio_group", "checkbox": "checkbox",
            "date": "date"}.get(kind, "text")


def resolve(schema: ir.FormSchema, profile: dict, *, conn=None,
            answer_lookup: Callable[[str], str | None] | None = None,
            answer_cache=None) -> FillPlan:
    plan = FillPlan()
    ats = schema.ats
    for step in schema.steps:
        for f in step.fields:
            pf = _resolve_field(f, profile, ats, conn, answer_lookup, answer_cache)
            if pf is None:
                plan.needs_oracle.append(f)
            else:
                plan.planned.append(pf)
    return plan


def _resolve_field(f, profile, ats, conn, answer_lookup, answer_cache):
    key = f.semantic_key
    driver = _driver_for(f.widget.kind)

    # (b) canary: EXACT profile path only, never oracle/bank/fuzzy (invariant 7).
    if ir.is_canary_key(key):
        path = _PROFILE_PATHS.get(key)
        raw = _dig(profile, path) if path else None
        if raw is None:
            return PlannedField(f, park=True, driver=driver)   # canary + no data -> park
        if key in ("work_auth", "sponsorship"):
            return PlannedField(f, binding=f"profile.{path}", option_intent=_yn(raw), driver=driver)
        return PlannedField(f, binding=f"profile.{path}",
                            value=str(raw), option_intent=str(raw).lower(), driver=driver)

    # (a) non-canary semantic_key -> profile path.
    if key in _PROFILE_PATHS:
        path = _PROFILE_PATHS[key]
        raw = _dig(profile, path)
        if raw is None:
            return PlannedField(f, park=True, driver=driver) if f.required else PlannedField(f, driver=driver)
        # first_name/last_name both map to personal.full_name; SPLIT so the
        # PlannedField carries the exact token (test asserts first_name == "Nida",
        # NOT the full "Nida Shah"). _split_name(full) -> (first, rest).
        if key in ("first_name", "last_name"):
            from applypilot.apply.prefill import _split_name
            first, last = _split_name(str(raw))
            val = first if key == "first_name" else last
            return PlannedField(f, binding=f"profile.{path}", value=val,
                                option_intent=val.lower(), driver=driver)
        val = str(raw)
        return PlannedField(f, binding=f"profile.{path}", value=val,
                            option_intent=val.lower(), driver=driver)

    # EEO: safe canonical decline default (never oracle).
    if key in _EEO_DEFAULTS:
        raw = _dig(profile, _EEO_DEFAULTS[key]) or _DECLINE
        return PlannedField(f, binding=f"policy.{key}", option_intent=str(raw).lower(), driver=driver)

    # hard refusal: an uncovered legal attestation -> park (spec §6.4).
    hay = f"{f.label_text} {f.question_text}".lower()
    if any(m in hay for m in _HARD_REFUSAL_MARKERS):
        return PlannedField(f, park=True, driver=driver)

    # (d) mapping-cache hit per field_fp.
    if conn is not None:
        fp = ir.field_fp(f)
        cached = mc.get_active(conn, ats, fp)
        if cached is not None:
            binding = cached["binding"]
            if binding.startswith("profile."):
                raw = _dig(profile, binding[len("profile."):])
                if raw is not None:
                    return PlannedField(f, binding=binding, value=str(raw),
                                        option_intent=str(raw).lower(),
                                        driver=cached.get("widget_driver") or driver)
            elif binding.startswith("answer:") and answer_lookup is not None:
                ans = answer_lookup(binding)
                if ans:
                    return PlannedField(f, binding=binding, value=ans,
                                        driver=cached.get("widget_driver") or driver)

    # (c) answer-bank hit (NON-canary custom free-text only; bank is §10.3-scrubbed).
    if answer_cache is not None and f.widget.kind in ("text", "textarea"):
        res = answer_cache.answer(f.question_text or f.label_text)
        # AnswerResult shape verified in Step 1; use .answer / .source accordingly.
        ans = getattr(res, "answer", None)
        if ans:
            return PlannedField(f, binding="answer:bank", value=ans, driver=driver)

    # (e) unresolved -> oracle.
    return None
```

Notes / adapt:
- **first_name/last_name split.** `_PROFILE_PATHS` maps both to `personal.full_name`. The split is done IN THE RESOLVER (see the branch in `_resolve_field` above) so the PlannedField carries the exact token — `test_semantic_key_resolves_from_profile_path` asserts `first_name` value == "Nida", NOT the full "Nida Shah", so a naive `value=str(raw)` would fail it. Reuse `from applypilot.apply.prefill import _split_name` (real signature `_split_name(full) -> tuple[str, str]`, defined in `prefill.py:131`; greenhouse re-imports it). Do NOT rewrite the split — import the existing helper.
- **AnswerResult shape.** VERIFY the `AnswerResult` dataclass fields in answer_cache.py (Step 1). If it exposes `.answer`/`.source` differently (e.g., `.text`), adapt the `getattr`. The bank is consulted ONLY for non-canary text/textarea (canary is short-circuited above).
- **option_intent vs value.** Enumerated drivers (react_select/native_select) consume `option_intent` (mapped to a real option at commit, invariant 6). Text/file consume `value`. Keeping both on `PlannedField` lets the driver registry pick the right one by kind.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_resolver.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/resolver.py tests/test_v2_resolver.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: resolver (canary-first ladder -> profile/EEO-decline/mapping-cache/answer-bank -> FillPlan; hard-refusal park; oracle batch)"
```

---

## Task 6: WidgetDriver registry — promoted drivers, read-back-verified `CommitResult`, lazy option enum

The Fill primitives (spec §6.6). Each driver takes a resolved `PlannedField` + a Playwright scope (Page or Frame) and returns a `CommitResult(committed: bool, ...)` from a **read-back**, never click-success (invariant 5). Drivers are PROMOTED from `prefill.py`/`adapters/greenhouse.py` (do not re-invent). The `react_select` driver is the ONLY one that opens→reads→Escapes a dropdown (lazy option enumeration, invariant 4); it folds BOTH the portal-click and keyboard-fallback paths (the Chime/Robinhood desync, invariant 5 + 6). Locator resolution seeds from `Field.locator_spec` through `healing.heal` (10-tier ladder); the winning tier is returned so the executor can write it to the mapping cache.

**Files:**
- Create: `src/applypilot/apply/v2/drivers.py`
- Test: `tests/test_v2_drivers.py`

- [ ] **Step 1: READ the drivers being promoted (do NOT re-invent)**

READ, in order, and promote (extract, do not rewrite the JS/logic):
- `src/applypilot/apply/prefill.py:687-703` (`_select_combobox_robust(root, label_needles, preferred) -> bool` — the WINNING version: portal-click first, verify via `_combobox_committed`, else keyboard fallback).
- `src/applypilot/apply/prefill.py:435-478` (`_combobox_committed(root, label_needles, preferred) -> bool` — the read-back that checks the REAL committed state: native `<select>` selectedIndex, react-select `.single-value`, hidden input non-invalid — this IS the CommitResult read-back for react_select).
- `src/applypilot/apply/prefill.py:530-550` (`_visible_combobox_options(root)` / `_click_option_by_text`) — the LAZY option reader (open→read visible options). `_match_real_option` at `:484-528` maps an intent to a real option (index-safe).
- `src/applypilot/apply/prefill.py:576-702` (`_commit_combobox_keyboard`) — the keyboard commit path.
- `src/applypilot/apply/prefill.py:823-916` (`_set_greenhouse_location(root, value) -> bool` — typeahead_location driver).
- `src/applypilot/apply/prefill.py:705-822` (`_set_greenhouse_phone_country(root) -> bool` — phone_intl driver).
- `from applypilot.apply.prefill import _split_name` (the split-name helper — real home is `prefill.py:131`, `_split_name(full) -> tuple[str, str]`; greenhouse re-imports it) + the file-upload pattern `loc.set_input_files(value)` at `greenhouse.py:497`.
- `src/applypilot/apply/healing.py:197` (`heal(page, spec, *, timeout_ms=1000) -> (Locator|None, tier)`).

Confirm each signature before promoting; ADAPT the driver wrappers to the exact ones you read.

- [ ] **Step 2: Write failing tests** (synthetic DOM incl. the react-select-ish widget from `test_greenhouse_adapter.py`)

```python
# tests/test_v2_drivers.py
import pytest

from applypilot.apply.v2 import drivers
from applypilot.apply.v2 import ir
from applypilot.apply.v2.resolver import PlannedField, build_element_spec


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


_TEXT = """<!doctype html><body><form>
  <label for="fn">First name</label>
  <input id="fn" name="first_name" type="text" required>
  <label for="rz">Resume/CV</label>
  <input id="rz" name="resume" type="file">
</form></body>"""

# react-select-ish widget (same wiring as test_greenhouse_adapter._FORM)
_RS = """<!doctype html><body><form>
  <label for="wa-i">Are you legally authorized to work?</label>
  <div class="select__control" id="wa-c" tabindex="0">
    <span class="select__single-value" id="wa-v">Select...</span>
    <input class="select__input" id="wa-i" role="combobox" autocomplete="off">
  </div>
  <div class="select__menu" id="wa-m" style="display:none">
    <div class="select__option">Yes, I am authorized to work in the US</div>
    <div class="select__option">No, I require sponsorship</div>
  </div>
<script>
  const c=wa_c=document.getElementById('wa-c'),i=document.getElementById('wa-i'),
        m=document.getElementById('wa-m'),v=document.getElementById('wa-v');
  let hi=null; const open=()=>m.style.display='block';
  c.addEventListener('mousedown',open); c.addEventListener('click',open); i.addEventListener('focus',open);
  i.addEventListener('input',()=>{const q=i.value.toLowerCase();
    hi=[...m.querySelectorAll('.select__option')].find(o=>q&&o.textContent.toLowerCase().includes(q))||null; open();});
  i.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){e.preventDefault(); if(!hi) hi=m.querySelector('.select__option');}
    else if(e.key==='Enter'){e.preventDefault(); if(hi){v.textContent=hi.textContent; i.value=hi.textContent; m.style.display='none';}}});
  m.querySelectorAll('.select__option').forEach(o=>o.addEventListener('click',()=>{v.textContent=o.textContent; m.style.display='none';}));
</script></form></body>"""


def _field(sem, kind, label, options=ir.LAZY, fid="fn"):
    return ir.Field(field_id=fid, frame_path=(), label_text=label, question_text=label,
                    semantic_key=sem, widget=ir.Widget(kind=kind), options=options, required=True)


def test_text_driver_reads_back_committed(page):
    page.set_content(_TEXT)
    f = _field("first_name", "text", "First name", fid="fn")
    pf = PlannedField(f, value="Nida", driver="text")
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#fn").input_value() == "Nida"
    assert res.locator_tier                                # a healing tier is reported


def test_text_driver_reports_not_committed_on_readback_mismatch(page, monkeypatch):
    page.set_content(_TEXT)
    f = _field("first_name", "text", "First name", fid="fn")
    pf = PlannedField(f, value="Nida", driver="text")
    # Force the fill to silently no-op so read-back != intended -> committed False.
    monkeypatch.setattr(drivers, "_do_fill", lambda loc, val, **k: None)
    res = drivers.commit(page, pf)
    assert res.committed is False                          # never trusts click/fill success


def test_file_driver_uploads_and_reads_back(page, tmp_path):
    page.set_content(_TEXT)
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
    f = _field("resume", "file", "Resume/CV", fid="rz")
    pf = PlannedField(f, value=str(pdf), driver="file")
    res = drivers.commit(page, pf)
    assert res.committed is True                           # filename-chip / input value present


def test_react_select_lazy_enumerates_only_at_commit(page):
    page.set_content(_RS)
    f = _field("work_auth", "react_select", "Are you legally authorized to work?", options=ir.LAZY)
    pf = PlannedField(f, option_intent="yes", driver="react_select")
    assert f.options is ir.LAZY                            # still lazy going in
    res = drivers.commit(page, pf)
    assert res.committed is True
    assert page.locator("#wa-v").inner_text().lower().startswith("yes")


def test_react_select_readback_catches_desync(page):
    # Simulate the Chime/Robinhood desync: option click does NOT update the
    # single-value; the driver must fall back to keyboard AND read-back-verify.
    desync = _RS.replace(
        "o.addEventListener('click',()=>{v.textContent=o.textContent; m.style.display='none';})",
        "o.addEventListener('click',()=>{ m.style.display='none'; })")  # click no-ops the commit
    page.set_content(desync)
    f = _field("work_auth", "react_select", "Are you legally authorized to work?", options=ir.LAZY)
    pf = PlannedField(f, option_intent="yes", driver="react_select")
    res = drivers.commit(page, pf)
    assert res.committed is True                           # keyboard fallback committed it
    assert page.locator("#wa-v").inner_text().lower().startswith("yes")


def test_react_select_no_matching_option_not_committed(page):
    page.set_content(_RS)
    f = _field("work_auth", "react_select", "Are you legally authorized to work?", options=ir.LAZY)
    pf = PlannedField(f, option_intent="maybe someday", driver="react_select")  # no real match
    res = drivers.commit(page, pf)
    assert res.committed is False                          # never blind-types a non-option
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_drivers.py -v`

- [ ] **Step 4: Implement `drivers.py`**

Structure: a `CommitResult` dataclass; a `commit(scope, planned_field) -> CommitResult` dispatcher keyed on `planned_field.driver`; per-driver functions that reuse the promoted logic. Every driver ends with a read-back. The react_select driver wraps the promoted `_select_combobox_robust` + `_visible_combobox_options` + `_match_real_option` (lazy enum + keyboard fallback), and the read-back IS `_combobox_committed`.

```python
"""WidgetDriver registry (spec §6.6). Each driver returns a read-back-verified
CommitResult; click success is never trusted (invariant 5). Drivers are PROMOTED
from prefill.py / adapters/greenhouse.py (see Task 6 Step 1 for exact sources) —
this module wraps them behind a uniform commit() dispatcher. Only react_select
opens a dropdown, and only at commit time (invariant 4)."""
from __future__ import annotations

from dataclasses import dataclass

from applypilot.apply.healing import heal
from applypilot.apply.v2.resolver import build_element_spec, PlannedField

# Promote (import) the winning implementations rather than re-authoring:
from applypilot.apply.prefill import (
    _select_combobox_robust, _combobox_committed, _visible_combobox_options,
    _match_real_option, _set_greenhouse_location, _set_greenhouse_phone_country,
)


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    locator_tier: str | None = None
    error: str | None = None


def _locate(scope, planned: PlannedField, *, timeout_ms: int = 1500):
    spec = build_element_spec(planned.field.locator_spec)
    loc, tier = heal(scope, spec, timeout_ms=timeout_ms)
    return loc, tier


def _do_fill(loc, value, *, timeout_ms: int = 1500):
    """Extracted so tests can monkeypatch a no-op fill to exercise read-back."""
    loc.fill(str(value), timeout=timeout_ms)


def _text(scope, planned: PlannedField) -> CommitResult:
    loc, tier = _locate(scope, planned)
    if loc is None:
        return CommitResult(False, error="not_located")
    try:
        _do_fill(loc, planned.value)
    except Exception as e:                       # noqa: BLE001
        return CommitResult(False, tier, str(e))
    # READ-BACK (never trust fill success).
    try:
        got = loc.input_value(timeout=1000)
    except Exception:
        got = ""
    return CommitResult(str(got).strip() == str(planned.value).strip(), tier)


def _file(scope, planned: PlannedField) -> CommitResult:
    loc, tier = _locate(scope, planned)
    if loc is None:
        return CommitResult(False, error="not_located")
    try:
        loc.set_input_files(planned.value, timeout=2000)
    except Exception as e:                       # noqa: BLE001
        return CommitResult(False, tier, str(e))
    # READ-BACK: the input carries a filename (chip settle handled by executor).
    try:
        got = loc.evaluate("el => (el.files && el.files.length) ? el.files[0].name : ''")
    except Exception:
        got = ""
    return CommitResult(bool(got), tier)


def _react_select(scope, planned: PlannedField) -> CommitResult:
    """Lazy-enumerate at commit, map intent -> real option, portal-click then
    keyboard fallback, read-back via _combobox_committed (invariants 4/5/6)."""
    needles = tuple(n for n in (planned.field.label_text, planned.field.question_text) if n)
    intent = (planned.option_intent or "").strip()
    if not intent:
        return CommitResult(False, error="no_intent")
    ok = _select_combobox_robust(scope, needles, (intent,))
    if not ok:
        return CommitResult(False, error="no_matching_option")
    committed = _combobox_committed(scope, needles, (intent,))
    _, tier = _locate(scope, planned)            # best-effort tier for telemetry
    return CommitResult(bool(committed), tier)


def _native_select(scope, planned: PlannedField) -> CommitResult:
    loc, tier = _locate(scope, planned)
    if loc is None:
        return CommitResult(False, error="not_located")
    try:
        loc.select_option(label=planned.option_intent, timeout=1500)
    except Exception:
        # fall back to value/index matching against real options
        try:
            loc.select_option(planned.option_intent, timeout=1500)
        except Exception as e:                   # noqa: BLE001
            return CommitResult(False, tier, str(e))
    try:
        got = loc.evaluate("el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''")
    except Exception:
        got = ""
    return CommitResult(planned.option_intent.lower() in str(got).lower(), tier)


def _typeahead_location(scope, planned: PlannedField) -> CommitResult:
    ok = _set_greenhouse_location(scope, planned.value)
    return CommitResult(bool(ok), "location_typeahead")


def _phone_intl(scope, planned: PlannedField) -> CommitResult:
    ok = _set_greenhouse_phone_country(scope)
    # phone number itself is a text field handled separately; this sets country.
    return CommitResult(bool(ok), "phone_country")


_REGISTRY = {
    "text": _text, "textarea": _text, "file": _file,
    "react_select": _react_select, "native_select": _native_select,
    "typeahead_location": _typeahead_location, "phone_intl": _phone_intl,
}


def commit(scope, planned: PlannedField) -> CommitResult:
    driver = _REGISTRY.get(planned.driver)
    if driver is None:
        return CommitResult(False, error=f"no_driver:{planned.driver}")
    try:
        return driver(scope, planned)
    except Exception as e:                       # noqa: BLE001 — one field never kills the fill
        return CommitResult(False, error=str(e))
```

Notes / adapt:
- **`_select_combobox_robust` signature.** VERIFIED at authoring as `_select_combobox_robust(root, label_needles, preferred)` and `_combobox_committed(root, label_needles, preferred)`. If the imports fail (name changed) READ prefill.py and adjust the import + call. The intent string ("yes"/"no"/"decline to self-identify") is the `preferred[0]`; the promoted code maps it to a REAL option (index-safe) so invariant 6 holds without a separate index dance here.
- **Escape suppression (invariant 4).** The promoted keyboard/portal code already closes the menu (Enter commits + menu hides). If a residual open menu desyncs the next field, add a per-commit `scope.keyboard.press("Escape")` guarded by an ATS flag (Greenhouse tolerates Escape) — note it. Do NOT open dropdowns anywhere except inside `_react_select`.
- **textarea** reuses `_text` (fill + read-back). The primary char_limit clamp is in the operator (`_validate`, `txt[:spec.char_limit]`). The front-end never sets `char_limit` (Task 4 — maxlength is not in the observation), so the driver-side clamp only fires when a field carries an explicit `char_limit`; keep a `_textarea` that truncates `planned.value[:char_limit]` when `char_limit` is set (and its driver test that constructs a field with `char_limit=500` explicitly) — that path is correct and stays.
- **phone split.** phone_intl sets the COUNTRY; the phone number digits are a separate `text` field. The executor sequences country-then-number if both are present.
- **SHOULD-NOTE (speed-gate tax).** The promoted react-select path is NOT sleep-free: `prefill._commit_combobox_keyboard` / `_select_combobox_by_label` contain ~5 `time.sleep()` calls (~1.4s/field). The executor itself is sleep-free (invariant 9), but the promoted fill path is not — a 6-EEO-react-select form burns ~8s of fixed sleeps, taxing the §12.2 ≤45s warm budget directly. Track this against the speed gate (Task 12). Do NOT gold-plate now (don't rip the sleeps out in Phase 3); just measure the react-select fill cost and note it if p50 approaches 45s.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_drivers.py -v`
Expected: ALL PASS. The desync test proves the keyboard fallback + read-back; if it flakes, confirm the promoted `_commit_combobox_keyboard` fires when `_combobox_committed` returns False after the portal click (that is `_select_combobox_robust`'s exact control flow — verified).

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/drivers.py tests/test_v2_drivers.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: WidgetDriver registry (promote react-select-robust/location/phone/file/text; read-back CommitResult; lazy option enum only in react_select)"
```

---

## Task 7: Executor — walk FillPlan, resume-first + settle gate, MutationObserver waits, required-completeness interlock

The Fill stage orchestration (spec §6.6). Walks a resolved `FillPlan` step-by-step: **resume uploads FIRST**, then blocks on a real filename-chip + MutationObserver quiet-window before reading/filling the rest (Greenhouse re-renders after server-side resume parse, invariant 9). Zero fixed sleeps — all waits are mutation-settle or locator-state expectations. Each field is committed through the driver registry (Task 6); a `committed=False` on a required field flips the field to unresolved (escalate to oracle or park). Before advancing/submitting, a **required-completeness sweep** must pass. Multi-step LOOPS (Greenhouse is single-step, but the loop shape lets Phase 4 drop in). Returns an `ExecReport` the orchestrator uses to decide submit vs park.

**Files:**
- Create: `src/applypilot/apply/v2/executor.py`
- Test: `tests/test_v2_executor.py`

- [ ] **Step 1: READ the settle/mutation primitives available**

READ `src/applypilot/apply/browser_stream.py` for any existing MutationObserver/quiet-window helper (grep the module for `MutationObserver` / `quiet` / `settle`). If NONE exists, this task adds a small `_wait_settle(scope, quiet_ms, timeout_ms)` that installs a MutationObserver via `scope.evaluate` and polls a JS-side "last mutation timestamp" until `now - last >= quiet_ms` or `timeout`. READ `src/applypilot/apply/adapters/greenhouse.py:265-353` (`_required_labels_on_page` / `_missing_required_labels_on_page`) — PROMOTE these for the required-completeness interlock (they already compute required-but-empty labels on the live scope). Confirm their signatures before promoting.

- [ ] **Step 2: Write failing tests** (synthetic DOM; a resume input that re-renders the form on upload)

```python
# tests/test_v2_executor.py
import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import executor as ex
from applypilot.apply.v2.resolver import PlannedField, FillPlan


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


# Form where uploading the resume injects the REST of the fields (server-side
# parse re-render). If the executor fills before the chip/settle, #fn won't exist.
_RERENDER = """<!doctype html><body><form id="f">
  <label for="rz">Resume/CV</label>
  <input id="rz" name="resume" type="file" required>
  <div id="rest"></div>
<script>
  document.getElementById('rz').addEventListener('change',()=>{
    setTimeout(()=>{ document.getElementById('rest').innerHTML =
      '<label for="fn">First name</label><input id="fn" type="text" required>' +
      '<span id="chip">resume.pdf</span>'; }, 30);   // async re-render
  });
</script></form></body>"""


def _f(sem, kind, label, fid, options=ir.LAZY, required=True):
    return ir.Field(field_id=fid, frame_path=(), label_text=label, question_text=label,
                    semantic_key=sem, widget=ir.Widget(kind=kind), options=options, required=required)


def test_resume_fills_first_then_settles_before_rest(page, tmp_path):
    page.set_content(_RERENDER)
    pdf = tmp_path / "resume.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
    resume = PlannedField(_f("resume", "file", "Resume/CV", "rz"), value=str(pdf), driver="file")
    first = PlannedField(_f("first_name", "text", "First name", "fn"), value="Nida", driver="text")
    plan = FillPlan(planned=[first, resume])          # note: NOT resume-first in the list
    report = ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                            [ir.Step(0, [resume.field, first.field], terminal=True)]),
                        plan)
    # executor MUST reorder resume-first + wait for settle so #fn exists when filled
    assert page.locator("#fn").input_value() == "Nida"
    assert report.committed_keys and "resume" in report.committed_keys


def test_no_fixed_sleeps_uses_settle_gate(page, tmp_path, monkeypatch):
    import time as _t
    calls = {"sleep": 0}
    monkeypatch.setattr(ex, "time", type("T", (), {"sleep": lambda s: calls.__setitem__("sleep", calls["sleep"] + 1),
                                                    "monotonic": _t.monotonic})())
    page.set_content(_RERENDER)
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
    plan = FillPlan(planned=[PlannedField(_f("resume", "file", "Resume/CV", "rz"), value=str(pdf), driver="file")])
    ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                   [ir.Step(0, [plan.planned[0].field], terminal=True)]), plan)
    assert calls["sleep"] == 0                         # zero fixed sleeps (invariant 9)


def test_required_completeness_interlock_blocks_submit_when_missing(page):
    page.set_content('<form id="f"><label for="fn">First name</label>'
                     '<input id="fn" type="text" required></form>')
    f = _f("first_name", "text", "First name", "fn")
    # a plan that DID NOT fill it (park) -> required sweep must report incomplete
    plan = FillPlan(planned=[PlannedField(f, park=True, driver="text")])
    report = ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                            [ir.Step(0, [f], terminal=True)]), plan)
    assert report.ready_to_submit is False
    assert report.missing_required                      # names the empty required field


def test_committed_false_on_required_marks_unresolved(page):
    page.set_content('<form><label for="fn">First name</label>'
                     '<input id="fn" type="text" required></form>')
    f = _f("first_name", "text", "First name", "fn")
    plan = FillPlan(planned=[PlannedField(f, value="Nida", driver="text")])
    # driver reports committed True here (value read-back matches) -> resolved
    report = ex.execute(page, ir.FormSchema("greenhouse", "acme", "u",
                                            [ir.Step(0, [f], terminal=True)]), plan)
    assert report.ready_to_submit is True
    assert "first_name" in report.committed_keys
    assert report.unresolved == []
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_executor.py -v`

- [ ] **Step 4: Implement `executor.py`**

```python
"""Executor (spec §6.6): walk a FillPlan step-by-step. Resume-first + filename-
chip / MutationObserver settle gate; zero fixed sleeps (invariant 9); each field
committed through the driver registry with read-back; required-completeness
interlock before submit. Multi-step LOOPS (Greenhouse = 1 step; loop shape lets
Phase 4 drop in)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from applypilot.apply.v2 import drivers
from applypilot.apply.v2 import mapping_cache as mc
from applypilot.apply.v2.resolver import PlannedField

_SETTLE_JS = r"""
() => {
  if (!window.__ap_mo) {
    window.__ap_last = Date.now();
    window.__ap_mo = new MutationObserver(() => { window.__ap_last = Date.now(); });
    window.__ap_mo.observe(document.documentElement,
        {subtree:true, childList:true, attributes:true, characterData:true});
  }
  return Date.now() - window.__ap_last;
}"""


@dataclass
class ExecReport:
    ready_to_submit: bool = False
    committed_keys: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    parked: bool = False
    error: str | None = None


def _wait_settle(scope, *, quiet_ms: int = 400, timeout_ms: int = 6000) -> None:
    """Block until the DOM has been quiet for quiet_ms, or timeout. No fixed
    sleeps — this polls the MutationObserver's last-mutation delta via the event
    loop's own timing (Playwright's wait_for_timeout is mutation-gated here via
    a JS predicate, not a blind sleep)."""
    try:
        scope.evaluate(_SETTLE_JS)                      # install observer
    except Exception:
        return
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            idle = scope.evaluate(_SETTLE_JS)
        except Exception:
            return
        if isinstance(idle, (int, float)) and idle >= quiet_ms:
            return
        try:
            scope.wait_for_timeout(50)                  # mutation-gated micro-yield, not a sleep
        except Exception:
            return


def _resolve_scope(page, schema):
    """Greenhouse form may be in a child frame (vanity domains). Reuse the
    adapter's frame detection idea; default to the page."""
    return page


def _required_missing(scope) -> list[str]:
    """PROMOTED from adapters/greenhouse._missing_required_labels_on_page —
    required-completeness interlock (spec §6.6). Import + call; adapt signature."""
    try:
        from applypilot.apply.adapters.greenhouse import _missing_required_labels_on_page
        return list(_missing_required_labels_on_page(scope))
    except Exception:
        return []


def execute(page, schema, plan: PlannedField, *, conn=None) -> ExecReport:
    report = ExecReport()
    scope = _resolve_scope(page, schema)
    planned = list(plan.planned)
    # RESUME FIRST (invariant 9): pull any file/resume field to the front.
    planned.sort(key=lambda pf: 0 if pf.driver == "file" or pf.field.semantic_key == "resume" else 1)

    for pf in planned:
        if pf.park:
            report.unresolved.append(pf.field.semantic_key or pf.field.field_id)
            continue
        res = drivers.commit(scope, pf)
        key = pf.field.semantic_key or pf.field.field_id
        if res.committed:
            report.committed_keys.append(key)
            if conn is not None:
                from applypilot.apply.v2 import ir as _ir
                fp = _ir.field_fp(pf.field)
                if pf.binding:
                    mc.put(conn, schema.ats, fp, binding=pf.binding,
                           widget_driver=pf.driver, locator_tier=res.locator_tier)
                mc.record_success(conn, schema.ats, fp)
        else:
            report.unresolved.append(key)
            if conn is not None:
                from applypilot.apply.v2 import ir as _ir
                mc.record_failure(conn, schema.ats, _ir.field_fp(pf.field))
        # After a resume/file commit, WAIT for the server-side re-render to settle
        # before touching the rest (invariant 9).
        if res.committed and (pf.driver == "file" or pf.field.semantic_key == "resume"):
            _wait_settle(scope, quiet_ms=400, timeout_ms=8000)

    # Required-completeness interlock (spec §6.6): never advance/submit with an
    # empty required field.
    report.missing_required = _required_missing(scope)
    report.ready_to_submit = (not report.missing_required and not report.unresolved)
    report.parked = bool(report.unresolved) and not report.ready_to_submit
    return report
```

Notes / adapt:
- **`time` monkeypatch seam.** The test patches `ex.time` to assert zero `sleep()` calls. `_wait_settle` uses `time.monotonic` (not `sleep`) and `scope.wait_for_timeout` (mutation-gated). The patched fake provides `monotonic`; ensure no code path calls `time.sleep`. If any promoted driver internally calls `time.sleep`, that is inside `drivers`/`prefill` (out of `ex`'s namespace) and does not violate THIS module's zero-sleep contract — but note it: the spec's "delete the 36 fixed sleeps" applies to the executor's own waits, which are settle-gated here. The residual `time.sleep(0.3)` inside promoted react-select code is a known carry-over; flag it in the summary and, if cheap, replace with a `wait_for` in a follow-up (do NOT gold-plate in this task).
- **frame scope.** `_resolve_scope` returns the page for the synthetic tests. For real vanity-domain embeds, promote `adapters/greenhouse._form_scope` (greenhouse.py:369-389) to return the child frame; wire it and add an iframe test if time permits (otherwise note as a Phase-4 hardening — Greenhouse boards.greenhouse.io is top-frame).
- **oracle round-trip.** This task's `execute` fills the DETERMINISTIC plan. The orchestrator (Task 9) runs the resolver's `needs_oracle` batch through the Operator BEFORE calling `execute` (turning oracle answers into additional `PlannedField`s), so `execute` only ever sees resolved fields. Keep `execute` oracle-free.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_executor.py -v`
Expected: ALL PASS. If `test_resume_fills_first_then_settles_before_rest` flakes (30ms async re-render), raise `_wait_settle` timeout or lower `quiet_ms` — the point is it must NOT be a fixed sleep.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/executor.py tests/test_v2_executor.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: executor (resume-first + MutationObserver settle gate; driver read-back; required-completeness interlock; zero fixed sleeps; mapping-cache writeback)"
```

---

## Task 8: Verification — Tier-1 passive network-evidence listener + Tier-2 DOM verdict reuse

The Verify stage (spec §6.7). **Tier 1 — network evidence:** a PASSIVE response listener (`context.on("response")` reading status+url only — NOT `route.fetch()`, invariant 8) captures the application-submit POST as the language-independent success signal, distinguishing it from analytics / resume-parse / validation XHR. On confirmed success it auto-harvests the endpoint signature into `mapping_cache.submit_endpoints`. **Tier 2 — DOM signals:** reuse the existing pure verdict cores UNCHANGED (`adapters/greenhouse._post_submit_verdict` + `launcher._compute_verification_verdict`). Tier 1 is preferred; Tier 2 is the fallback; ambiguity → `needs_review`.

**Files:**
- Create: `src/applypilot/apply/v2/verify.py`
- Test: `tests/test_v2_verify.py`

- [ ] **Step 1: READ the reused verdict cores + the passive-listener seam**

READ `src/applypilot/apply/adapters/greenhouse.py:392-413` (`_post_submit_verdict(button_gone, errors_visible, button_enabled, deadline_hit) -> (verdict, error)`) and `src/applypilot/apply/launcher.py:1064-1092+` (`_compute_verification_verdict(*, has_confirmation, url_changed, submit_gone, submit_disabled, no_validation_errors, required_ok, verify_threshold) -> (confidence, verified)`). Both are PURE — import and reuse, do not reimplement. READ `src/applypilot/apply/browser_stream.py:33-56` (`_request_host`, `should_block_request`) + the `_ATS_HOST_RE`/`_MUTATION_METHODS` constants near `_guard` (browser_stream.py ~660-680) — the submit-POST classifier should reuse the SAME host regex + mutation-method set the safety kernel already uses, so "what counts as a submit POST" is defined once. Confirm those constant names before importing.

- [ ] **Step 2: Write failing tests** (pure classifier + a fake response-event feed; no live network)

```python
# tests/test_v2_verify.py
from applypilot import database as db
from applypilot.apply.v2 import verify
from applypilot.apply.v2 import mapping_cache as mc


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


class _Resp:
    def __init__(self, method, url, status):
        self.request = type("R", (), {"method": method})()
        self.url = url
        self.status = status


def test_submit_post_classified_over_analytics_and_resume_parse():
    # The application-submit POST is the success signal; analytics / resume-parse
    # / validation XHR are NOT (invariant 8).
    assert verify.is_submit_post("POST", "https://boards.greenhouse.io/acme/applications", 200)
    assert not verify.is_submit_post("POST", "https://www.google-analytics.com/collect", 200)
    assert not verify.is_submit_post("POST", "https://boards.greenhouse.io/acme/resume/parse", 200)
    assert not verify.is_submit_post("GET", "https://boards.greenhouse.io/acme/applications", 200)
    assert not verify.is_submit_post("POST", "https://boards.greenhouse.io/acme/applications", 422)  # rejected


def test_network_recorder_captures_submit_and_ignores_noise():
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")
    rec.on_response(_Resp("POST", "https://www.google-analytics.com/collect", 200))
    rec.on_response(_Resp("POST", "https://boards.greenhouse.io/acme/resume/parse", 200))
    assert rec.submitted is False
    rec.on_response(_Resp("POST", "https://boards.greenhouse.io/acme/applications", 200))
    assert rec.submitted is True
    assert rec.submit_url.endswith("/acme/applications")


def test_tier1_success_harvests_endpoint(tmp_path):
    conn = _conn(tmp_path)
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")
    rec.on_response(_Resp("POST", "https://boards.greenhouse.io/acme/applications", 201))
    v = verify.verify(rec, conn=conn, dom_signals=None)
    assert v.verified is True and v.tier == 1
    eps = mc.get_submit_endpoints(conn, "greenhouse", "acme")
    assert eps and "applications" in eps[0]["url_pattern"]   # auto-harvested


def test_tier2_dom_fallback_when_no_network_evidence(tmp_path):
    conn = _conn(tmp_path)
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")   # never saw a submit POST
    # DOM says: confirmation present, url changed, submit gone, no errors, required ok.
    dom = verify.DomSignals(has_confirmation=True, url_changed=True, submit_gone=True,
                            submit_disabled=False, no_validation_errors=True, required_ok=True)
    v = verify.verify(rec, conn=conn, dom_signals=dom, verify_threshold=0.75)
    assert v.verified is True and v.tier == 2
    assert v.confidence >= 0.75                              # from _compute_verification_verdict
    assert mc.get_submit_endpoints(conn, "greenhouse", "acme") == []  # no harvest without net evidence


def test_ambiguous_is_needs_review(tmp_path):
    conn = _conn(tmp_path)
    rec = verify.NetworkEvidence(ats="greenhouse", company="acme")
    dom = verify.DomSignals(has_confirmation=False, url_changed=False, submit_gone=False,
                            submit_disabled=False, no_validation_errors=False, required_ok=True)
    v = verify.verify(rec, conn=conn, dom_signals=dom, verify_threshold=0.75)
    assert v.verified is False and v.needs_review is True    # neither tier confirmed
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_verify.py -v`

- [ ] **Step 4: Implement `verify.py`**

```python
"""Verification (spec §6.7). Tier 1 = PASSIVE network evidence (the application
submit POST, read status+url only — never route.fetch, invariant 8); harvests
the endpoint signature on confirmed success. Tier 2 = DOM verdict cores REUSED
unchanged from the v1 verifier. Tier 1 preferred; ambiguity -> needs_review."""
from __future__ import annotations

import re
from dataclasses import dataclass

from applypilot.apply.v2 import mapping_cache as mc

# Reuse the safety kernel's host + mutation-method definitions so "submit POST"
# is defined ONCE across the codebase. Adapt import names to what Step 1 found.
try:
    from applypilot.apply.browser_stream import _ATS_HOST_RE, _request_host
    _MUTATION = {"POST", "PUT", "PATCH"}
except Exception:                                # pragma: no cover - import guard
    _ATS_HOST_RE = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com", re.I)
    def _request_host(url):
        m = re.match(r"https?://([^/]+)", url or "")
        return m.group(1) if m else ""
    _MUTATION = {"POST", "PUT", "PATCH"}

# Endpoints that are NOT the application submit even though they POST to an ATS
# host (resume parse, analytics, validation).
_NOT_SUBMIT = re.compile(r"/(resume|cv)/?parse|/validate|analytics|/collect|/track|/log", re.I)
# The Greenhouse application submit path.
_SUBMIT_HINT = re.compile(r"/applications?\b|/apply\b|/submit\b", re.I)


def is_submit_post(method: str, url: str, status: int) -> bool:
    if (method or "").upper() not in _MUTATION:
        return False
    if not (200 <= int(status) < 400):               # rejected/validation != success
        return False
    host = _request_host(url)
    if not _ATS_HOST_RE.search(host or ""):
        return False
    if _NOT_SUBMIT.search(url or ""):
        return False
    return bool(_SUBMIT_HINT.search(url or ""))


class NetworkEvidence:
    """Attach on_response as a PASSIVE context listener (context.on('response',
    ev.on_response)). Reads status+url only; never blocks or refetches."""

    def __init__(self, *, ats: str, company: str):
        self.ats = ats
        self.company = company
        self.submitted = False
        self.submit_url: str | None = None
        self.submit_method: str | None = None

    def on_response(self, response) -> None:
        try:
            method = getattr(response.request, "method", "")
            url = getattr(response, "url", "")
            status = getattr(response, "status", 0)
        except Exception:
            return
        if is_submit_post(method, url, status):
            self.submitted = True
            self.submit_url = url
            self.submit_method = (method or "POST").upper()


@dataclass
class DomSignals:
    has_confirmation: bool = False
    url_changed: bool = False
    submit_gone: bool = False
    submit_disabled: bool = False
    no_validation_errors: bool = False
    required_ok: bool = False


@dataclass
class VerifyResult:
    verified: bool
    tier: int | None = None
    confidence: float = 0.0
    needs_review: bool = False
    submit_url: str | None = None


def _url_pattern(url: str) -> str:
    m = re.match(r"https?://([^?#]+)", url or "")
    return (m.group(1) if m else url or "").rstrip("/")


def verify(evidence: NetworkEvidence, *, conn=None, dom_signals: DomSignals | None,
           verify_threshold: float = 0.75) -> VerifyResult:
    # TIER 1: network evidence (language-independent, strongest).
    if evidence.submitted and evidence.submit_url:
        if conn is not None:
            mc.record_submit_endpoint(conn, evidence.ats, evidence.company,
                                      evidence.submit_method or "POST",
                                      _url_pattern(evidence.submit_url))
        return VerifyResult(True, tier=1, confidence=1.0, submit_url=evidence.submit_url)

    # TIER 2: DOM verdict core, REUSED unchanged.
    if dom_signals is not None:
        from applypilot.apply.launcher import _compute_verification_verdict
        confidence, verified = _compute_verification_verdict(
            has_confirmation=dom_signals.has_confirmation,
            url_changed=dom_signals.url_changed,
            submit_gone=dom_signals.submit_gone,
            submit_disabled=dom_signals.submit_disabled,
            no_validation_errors=dom_signals.no_validation_errors,
            required_ok=dom_signals.required_ok,
            verify_threshold=verify_threshold,
        )
        if verified:
            return VerifyResult(True, tier=2, confidence=confidence)
        return VerifyResult(False, tier=2, confidence=confidence, needs_review=True)

    return VerifyResult(False, needs_review=True)
```

Notes / adapt:
- **Wiring the passive listener (orchestrator, Task 9).** The orchestrator registers `context.on("response", evidence.on_response)` on the SAME browser context `BrowserStateStream` uses — it does NOT install a route (the `_guard` route is the safety kernel's; `context.on("response")` is additive and passive). If Playwright sync `context.on` isn't ergonomic on the CDP-attached context, fall back to `page.on("response", ...)`. Never use `route.fetch()` (would double-issue the POST, invariant 8). Confirm the context object the stream owns is reachable; if not, attach on the page the executor drives.
- **`_ATS_HOST_RE` reuse.** VERIFY the constant name in browser_stream.py (the `_guard` uses `_ATS_HOST_RE.search(_request_host(req.url))`). If the name differs, adapt the import; the import-guard fallback keeps tests green regardless, but prefer the real shared constant so the definition stays single-sourced.
- **Endpoint auto-harvest is Tier-1-only** (spec §6.7: "from the HAR of every confirmed success"). Tier-2 (DOM) success does NOT harvest — you didn't observe the POST, so you have no endpoint to trust.

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_verify.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/v2/verify.py tests/test_v2_verify.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: verify (Tier-1 passive submit-POST listener + endpoint auto-harvest; Tier-2 reuses _post_submit_verdict/_compute_verification_verdict; ambiguity -> needs_review)"
```

---

## Task 9: `run_form_compiler` orchestrator + extract the shared safety-prologue helper

The stage conductor (spec §6 pipeline) AND the refactor that lets v2 reuse the SAME safety kernel objects as legacy. First EXTRACT `run_job`'s safety prologue (pre-apply gates + `ledger.record_intent` + `broker.issue`, launcher ~2293-2404) into a shared `_safety_prologue(...)` helper that BOTH `run_job` and `run_form_compiler` call — so v2 threads the SAME `broker`/`identity_id`/`browser_stream`/`ledger` (invariant 1). Then implement `run_form_compiler` = Parse → Resolve → (oracle batch) → Fill → Verify, returning the SAME `(status, duration_ms, prefill_status)` tuple with `prefill_status["tier_used"]="v2_greenhouse"`. v2 FAILS OPEN: any parse/resolve failure returns a `_SENTINEL_FALLBACK` the dispatch seam (Task 10) reads to run legacy instead (invariant 2).

**Files:**
- Modify: `src/applypilot/apply/launcher.py` (extract `_safety_prologue`; call it from `run_job`)
- Create: `src/applypilot/apply/v2/orchestrator.py`
- Test: `tests/test_v2_orchestrator.py`, extend `tests/test_launcher_safety_prologue.py` (or the existing launcher test file)

- [ ] **Step 1: READ the exact prologue region + the return-tuple contract + the fail-open exemplar**

READ `src/applypilot/apply/launcher.py:2293-2412` — this is the block to extract: `load_profile` + `_effective_apply_url`, `_preapply_location_reject` (→ `failed:` return), `_compute_idempotency_key` + checkpoint guards (→ `needs_review:possible_duplicate_guard`), the `if identity_id:` ledger gates (`has_confirmed`/`has_open_intent`/`confirmed_count_for_token` → `needs_review:` returns), resume path resolution, `reset_worker_dir`, `ledger.record_intent(identity_id, worker_id=...)` (2398), `broker.issue(identity_id)` (2401), and the MCP config write (2407-2412, which is LEGACY-ONLY — do NOT move it into the shared helper). READ `run_job`'s signature (launcher.py:2230) + its documented return tuple (`(status, duration_ms, prefill_status)`). READ `launcher.py:1208,1246-1248` (`_greenhouse_adapter_pass` fail-open: `except Exception: ... return None`) — v2's fail-open mirrors this exactly.

- [ ] **Step 2: Write failing tests**

First, a launcher test proving the extraction is behavior-preserving (the prologue still short-circuits the same way):

```python
# tests/test_launcher_safety_prologue.py  (new file, or append to existing launcher test)
from applypilot.apply import launcher


def test_safety_prologue_returns_gate_decision_shape():
    # The extracted helper returns a structured decision, not raw returns, so
    # both run_job and run_form_compiler branch on it identically.
    # Contract: dec.blocked (bool), dec.status (str|None), dec.duration_ms,
    #           dec.profile, dec.apply_url, dec.resume_path, dec.ledger.
    assert hasattr(launcher, "_safety_prologue")
    # A location-reject job -> blocked with a 'failed:...' status.
    job = {"url": "https://boards.greenhouse.io/x/jobs/1",
           "application_url": "https://boards.greenhouse.io/x/jobs/1",
           "location": "Germany (no US work auth)"}
    # profile monkeypatching / a real profile fixture as the existing launcher
    # tests do — READ how they build `job`/profile and mirror it. The assertion:
    # a blocked decision carries a status and never raises.


def test_safety_prologue_carries_broker_file_ticket_path():
    # Regression guard: broker_file must be derived from the broker's `path`
    # attribute, NOT from broker.issue()'s return (which is None). A non-None
    # broker_file is what keeps fail-closed submit containment armed (invariant 1).
    # Build a clean (non-blocked) job + a broker with a real ticket path exactly as
    # the existing launcher tests construct worker_loop's broker; run the prologue.
    dec = launcher._safety_prologue(
        job={"url": "https://boards.greenhouse.io/x/jobs/1",
             "application_url": "https://boards.greenhouse.io/x/jobs/1"},
        worker_id=0, run_started=0.0, job_meta={}, identity_id="id-1",
        broker=_broker_with_path,      # a SubmitBroker whose .path is set on issue()
        dry_run=False)
    assert dec.blocked is False
    assert dec.broker_file is not None          # points at the issued ticket path
```

(Adapt the launcher-side test to however the existing launcher tests construct jobs/profiles + monkeypatch `config.load_profile`. The POINT of the test is: extracting the prologue did not change the short-circuit statuses. If the existing suite already covers these short-circuits end-to-end through `run_job`, keep those tests green as the regression proof and make this new test light.)

Then the orchestrator test (fully injected — no Chrome, no network):

```python
# tests/test_v2_orchestrator.py
from applypilot import database as db
from applypilot.apply.v2 import orchestrator as orch
from applypilot.apply.v2 import ir


def _conn(tmp_path):
    p = tmp_path / "t.db"
    db.close_connection(p)
    db.init_db(p)
    return db.get_connection(p)


PROFILE = {"personal": {"full_name": "Nida Shah", "email": "nida@example.com",
                        "phone": "4081234567", "city": "San Jose"},
           "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True}}


def _schema():
    fields = [
        ir.Field("fn", (), "First name", "First name", "first_name", ir.Widget("text"), ir.LAZY, True),
        ir.Field("em", (), "Email", "Email", "email", ir.Widget("text"), ir.LAZY, True),
    ]
    return ir.FormSchema("greenhouse", "acme", "https://boards.greenhouse.io/acme/jobs/1",
                         [ir.Step(0, fields, advance_control={"role": "button", "name": "Submit"}, terminal=True)])


def test_orchestrator_happy_path_returns_v2_tier(tmp_path):
    conn = _conn(tmp_path)
    # Inject fakes for every stage so the orchestrator's WIRING is under test,
    # not the browser: parse returns a schema; executor reports ready+committed;
    # verify returns Tier-1 verified.
    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,      # nothing needs oracle
        execute=lambda page, schema, plan, conn: orch.ExecStub(ready_to_submit=True,
                                                               committed_keys=["first_name", "email"]),
        submit=lambda page, schema: True,
        verify=lambda evidence, conn, dom_signals: orch.VerifyStub(verified=True, tier=1),
    )
    status, duration_ms, prefill = orch.run_form_compiler(
        job={"url": "https://boards.greenhouse.io/acme/jobs/1",
             "application_url": "https://boards.greenhouse.io/acme/jobs/1"},
        page=object(), profile=PROFILE, conn=conn, company="acme",
        operator=None, stages=fakes)
    assert status == "applied"
    assert prefill["tier_used"] == "v2_greenhouse"
    assert isinstance(duration_ms, int)


def test_orchestrator_parse_failure_fails_open(tmp_path):
    conn = _conn(tmp_path)
    def _boom(*a, **k):
        raise RuntimeError("DOM churn: cannot parse")
    fakes = orch.Stages(parse=_boom)
    status, duration_ms, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes)
    assert status == orch.FALLBACK_SENTINEL          # -> dispatch runs legacy (invariant 2)
    assert prefill is None or prefill.get("tier_used") == "v2_greenhouse_fallback"


def test_orchestrator_parks_when_not_ready(tmp_path):
    conn = _conn(tmp_path)
    fakes = orch.Stages(
        parse=lambda page, company, url: _schema(),
        resolve=lambda schema, profile, conn: orch._passthrough_plan(schema, profile),
        run_oracle=lambda plan, schema, operator: plan,
        execute=lambda page, schema, plan, conn: orch.ExecStub(ready_to_submit=False,
                                                               missing_required=["First name"]),
    )
    status, _, prefill = orch.run_form_compiler(
        job={"url": "u", "application_url": "u"}, page=object(), profile=PROFILE,
        conn=conn, company="acme", operator=None, stages=fakes)
    assert status.startswith("needs_review")         # never submits an incomplete form
    assert prefill["tier_used"] == "v2_greenhouse"
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_orchestrator.py tests/test_launcher_safety_prologue.py -v`

- [ ] **Step 4: Extract `_safety_prologue` in `launcher.py`**

Refactor the 2293-2404 block into a helper returning a small decision object. The helper does NOT write the MCP config (legacy-only) and does NOT run prefill. It DOES run all gates + `record_intent` + `broker.issue`. Both callers branch on `decision.blocked`.

```python
# launcher.py  (near run_job)
from dataclasses import dataclass

@dataclass
class _PrologueDecision:
    blocked: bool
    status: str | None = None          # the short-circuit status when blocked
    duration_ms: int = 0
    profile: dict | None = None
    apply_url: str = ""
    resume_path: str | None = None
    resume_text: str = ""
    ledger: "SubmissionLedger | None" = None
    broker_file: str | None = None


def _safety_prologue(job, *, worker_id, run_started, job_meta, identity_id,
                     broker, dry_run) -> _PrologueDecision:
    """Shared pre-apply safety gate (spec §10). Extracted from run_job so v2's
    run_form_compiler reuses the SAME broker/ledger/identity path (invariant 1).
    Returns a decision; when blocked, `status` is the exact short-circuit
    (failed:* / needs_review:*) both engines return verbatim."""
    profile = config.load_profile()
    apply_url = _effective_apply_url(job) or ""
    job["application_url"] = apply_url
    location_reject = _preapply_location_reject(job, profile)
    if location_reject:
        job_meta["failure_class"] = _classify_failure_class(f"failed:{location_reject}", location_reject)
        _write_job_runtime_metadata(job["url"], last_failure_class=job_meta["failure_class"],
                                    checkpoint=_checkpoint(CHECKPOINT_PAGE_REACHED,
                                                           apply_url=_canonicalize_url(apply_url),
                                                           preapply_location_gate=True))
        return _PrologueDecision(True, f"failed:{location_reject}",
                                 int((time.time() - run_started) * 1000))
    # ... (idempotency key + checkpoint guards + ledger gates + resume paths +
    #      reset_worker_dir + record_intent + broker.issue — MOVE lines 2307-2404
    #      here verbatim, replacing each `return X, ms, None` with
    #      `return _PrologueDecision(True, X, ms)`.)
    #
    # CRITICAL — the broker block moves as TWO lines together, VERBATIM:
    #     broker.issue(identity_id)                                  # returns None!
    #     broker_file = str(getattr(broker, "path", "") or "") or None
    # `issue()` returns None — NEVER assign its result to broker_file
    # (`broker_file = broker.issue(...)` would null the ticket path and disable
    # fail-closed submit containment, invariant 1). broker_file is derived on the
    # NEXT line from the broker's own `path` attribute; the decision carries it.
    return _PrologueDecision(False, None, 0, profile=profile, apply_url=apply_url,
                             resume_path=resume_path, resume_text=resume_text,
                             ledger=ledger, broker_file=broker_file)
```

Then in `run_job`, REPLACE the extracted block with:

```python
    dec = _safety_prologue(job, worker_id=worker_id, run_started=run_started,
                           job_meta=job_meta, identity_id=identity_id,
                           broker=broker, dry_run=dry_run)
    if dec.blocked:
        return dec.status, dec.duration_ms, None
    profile = dec.profile
    apply_url = dec.apply_url
    resume_path = dec.resume_path
    resume_text = dec.resume_text
    ledger = dec.ledger
    broker_file = dec.broker_file
    # (MCP config write + prefill continue UNCHANGED below — legacy-only.)
```

CRITICAL: run the FULL existing launcher test suite after this refactor (`& $PY -m pytest tests/ -k launcher -v`) BEFORE writing v2 — the extraction must be behavior-preserving. The two-phase ledger + broker semantics are safety-critical (invariant 1); a regression here is a wrong/duplicate submission.

- [ ] **Step 5: Implement `orchestrator.py`**

```python
"""run_form_compiler (spec §6 pipeline): Parse -> Resolve -> oracle-batch ->
Fill -> Submit -> Verify -> Record. Returns run_job's exact tuple
(status, duration_ms, prefill_status) with tier_used='v2_greenhouse'. FAILS OPEN
(invariant 2): any stage exception -> FALLBACK_SENTINEL so the dispatch seam runs
legacy run_job. Reuses the SAME broker/ledger/browser_stream (invariant 1) —
those are threaded in by the caller (Task 10), never constructed here."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from applypilot.apply.v2 import V2_TIER_LABEL
from applypilot.apply.v2 import frontend_greenhouse, resolver, executor, verify as verify_mod
from applypilot.apply.v2.operator import FieldResolutionRequest, FieldSpec

FALLBACK_SENTINEL = "v2_fallback_to_legacy"


@dataclass
class Stages:
    """Injection seam for tests (defaults = real stage fns)."""
    parse: object = None
    resolve: object = None
    run_oracle: object = None
    execute: object = None
    submit: object = None
    verify: object = None


# lightweight stubs used in tests
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


def _passthrough_plan(schema, profile, conn=None):
    return resolver.resolve(schema, profile, conn=conn)


def _default_stages() -> Stages:
    return Stages(
        parse=lambda page, company, url: frontend_greenhouse.parse_observation(
            __import__("applypilot.apply.browser_stream", fromlist=["collect_browser_observation"])
            .collect_browser_observation(page), company=company, url=url),
        resolve=lambda schema, profile, conn: resolver.resolve(schema, profile, conn=conn),
        run_oracle=_run_oracle,
        execute=lambda page, schema, plan, conn: executor.execute(page, schema, plan, conn=conn),
        submit=_submit,
        verify=lambda evidence, conn, dom_signals: verify_mod.verify(
            evidence, conn=conn, dom_signals=dom_signals),
    )


def _run_oracle(plan, schema, operator):
    """Batch the resolver's needs_oracle fields to the Operator, fold answers
    back into PlannedFields (index -> real option resolved at commit)."""
    if not plan.needs_oracle or operator is None:
        return plan
    req = FieldResolutionRequest(
        job_context=f"{schema.company} — {schema.url}",
        fields=[FieldSpec(field_fp=__import__("applypilot.apply.v2.ir", fromlist=["field_fp"]).field_fp(f),
                          question_text=f.question_text, widget_kind=f.widget.kind,
                          options=None if f.widget.kind in ("text", "textarea") else [],
                          char_limit=f.char_limit) for f in plan.needs_oracle])
    answers = operator.resolve_fields(req)
    # fold: cannot_answer -> park; text -> value; option_index -> option_intent
    # (real option resolution happens in the react_select driver at commit).
    # ... build PlannedFields and append to plan.planned; drop from needs_oracle.
    return plan


def _submit(page, schema):
    """Click the terminal advance_control THROUGH the reused submit broker path
    (the browser_stream _guard route consumes the ticket). Reuse
    adapters/greenhouse.submit_greenhouse on the resolved scope, or heal+click
    schema.steps[-1].advance_control. Returns clicked:bool.

    NOTE: submit_greenhouse(scope, ...) does NOT self-scope — it takes whatever
    scope you hand it. Greenhouse forms are often embedded in a child iframe
    (vanity careers domains), so pass _form_scope(page) (greenhouse.py:369), NOT
    the raw page, or the submit button won't be found on iframe-embed forms."""
    from applypilot.apply.adapters.greenhouse import submit_greenhouse, _form_scope
    ok, _ = submit_greenhouse(_form_scope(page))
    return ok


def run_form_compiler(*, job, page, profile, conn, company, operator,
                      network_evidence=None, stages: Stages | None = None,
                      dry_run: bool = False, verify_threshold: float = 0.75):
    started = time.monotonic()
    prefill = {"ats": "greenhouse", "tier_used": V2_TIER_LABEL, "fields_filled": [], "error": None}
    st = stages or _default_stages()
    url = job.get("application_url") or job.get("url") or ""
    try:
        schema = st.parse(page, company, url)
    except Exception as e:                       # noqa: BLE001 — FAIL OPEN (invariant 2)
        return FALLBACK_SENTINEL, int((time.monotonic() - started) * 1000), None
    # execute + pre-submit fill are still PRE-SUBMIT: a crash here is safe to
    # fail open (no submit has fired) -> hand back to legacy (invariant 2).
    try:
        plan = st.resolve(schema, profile, conn)
        plan = st.run_oracle(plan, schema, operator)

        report = st.execute(page, schema, plan, conn)
        prefill["fields_filled"] = list(getattr(report, "committed_keys", []))
        if not getattr(report, "ready_to_submit", False):
            # required-completeness interlock failed -> park, never submit (invariant 9).
            return ("needs_review:v2_incomplete_required",
                    int((time.monotonic() - started) * 1000), prefill)

        if dry_run:
            # network containment already blocks the POST; report success-shaped park.
            return "needs_review:v2_dry_run", int((time.monotonic() - started) * 1000), prefill
    except Exception:                            # noqa: BLE001 — FAIL OPEN (pre-submit only)
        return FALLBACK_SENTINEL, int((time.monotonic() - started) * 1000), None

    # SUBMIT + VERIFY: once a submit MAY have fired, a crash must NOT fall back to
    # legacy (that would risk a double-submit re-apply). If the submission ledger
    # INTENT is already recorded and a submit may have gone out, return
    # needs_review:v2_crashed_post_submit (dangling INTENT, safe) — NEVER sentinel.
    try:
        clicked = st.submit(page, schema)
        dom = None                               # orchestrator gathers DOM signals here
        v = st.verify(network_evidence, conn, dom)
    except Exception:                            # noqa: BLE001 — post-submit crash: DO NOT re-apply
        return ("needs_review:v2_crashed_post_submit",
                int((time.monotonic() - started) * 1000), prefill)
    ms = int((time.monotonic() - started) * 1000)
    if v.verified:
        return "applied", ms, prefill
    if getattr(v, "needs_review", False) or not clicked:
        return "needs_review:unverified_submission", ms, prefill
    return FALLBACK_SENTINEL, ms, None           # unclear -> let legacy try
```

Notes / adapt:
- **Fail-open boundary (fix from review).** The try/except boundary reflects submit-safety: parse/resolve/oracle/**pre-submit-fill** exceptions → `FALLBACK_SENTINEL` (legacy re-applies, safe because no submit fired, counted); a crash in the **submit→verify** stage → `needs_review:v2_crashed_post_submit` (the ledger INTENT is already recorded and a submit may have fired, so re-applying via legacy would risk a double-submit — leave the INTENT dangling for reconciliation instead of blindly retrying). The dangling-INTENT case is exactly what the submission-ledger duplicate guard is designed to catch on the next pass.
- **`_run_oracle` fold** is sketched — complete it: build a `PlannedField` per answered field (`FieldAnswer.text` → `value`; `FieldAnswer.option_index` → look up `field.options`? NO — options are LAZY at parse; the oracle got `options=[]` for enumerated custom fields, so for Phase 3 route enumerated CUSTOM questions that need real options through the react_select driver's own lazy enum by passing the intent text the operator would need — simplest correct behavior: for enumerated custom fields the oracle can't index blindly, so mark them `cannot_answer` → park, and note that enumerated CUSTOM questions are a Phase-4 refinement. Free-text custom questions are the common Greenhouse case and are fully handled). Keep it honest: text custom → filled; enumerated custom with unknown options → park.
- **DOM signals for Tier-2.** The orchestrator should gather `DomSignals` from the post-submit page (confirmation text / url change / submit-gone) to feed Tier-2 when Tier-1 network evidence is absent. Reuse the observation: `collect_browser_observation` after submit gives `submit_enabled`/`validation_errors`/`page_text_sample`. Build `DomSignals` from it. In the injected tests `verify` is stubbed, so this is only exercised live.
- **network_evidence wiring** is the caller's job (Task 10): register `context.on("response", network_evidence.on_response)` on the stream's context BEFORE navigation, pass the `NetworkEvidence` in.
- **SHOULD-NOTE (no §6.2 pre-flight probe in Phase 3).** The spec's §6.2 pre-flight probe (~2s classify before parse) is NOT built here — v2 relies on the legacy prologue gates (`_safety_prologue`) + fail-open instead of a dedicated classifier. The ≤45s §12.2 budget should account for its ABSENCE (there is no +2s probe cost, but also no early-classify short-circuit). Building §6.2 is a later refinement, not a Phase-3 deliverable.

- [ ] **Step 6: Run — expect pass, then the launcher regression suite**

Run: `& $PY -m pytest tests/test_v2_orchestrator.py -v`
Run: `& $PY -m pytest tests/ -k "launcher or safety or ledger or broker" -q`   # extraction must not regress
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/apply/launcher.py src/applypilot/apply/v2/orchestrator.py tests/test_v2_orchestrator.py tests/test_launcher_safety_prologue.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: extract shared _safety_prologue (reuses broker/ledger/identity) + run_form_compiler orchestrator (fail-open to legacy; tier_used=v2_greenhouse)"
```

---

## Task 10: Dispatch seam + `APPLYPILOT_V2_ENGINE` flag + `tier_used` labeling

Wire v2 into `worker_loop` as a sibling of the `dispatch_apply` call (launcher ~3340), gated by a fresh-read `APPLYPILOT_V2_ENGINE` flag (mirrors `is_skill_flow_enabled`). When ON **and** the job is Greenhouse, call `run_form_compiler` with the SAME `broker`/`ident`/`browser_stream` already constructed (invariant 1) + register the passive `NetworkEvidence` response listener; if it returns the fallback sentinel, fall through to the existing `dispatch_apply` (legacy) path (invariant 2). v2 sets `prefill_status["tier_used"]="v2_greenhouse"` so A/B is free (Task 12).

**Files:**
- Modify: `src/applypilot/apply/launcher.py` (v2 branch beside `dispatch_apply`; a small `_v2_enabled()` + `_is_greenhouse(job)` helper)
- Test: `tests/test_v2_dispatch_seam.py`

- [ ] **Step 1: READ the exact dispatch call site + the flag idiom + Greenhouse detection**

READ `src/applypilot/apply/launcher.py:3299-3361` — the region where `broker`, `ident`, `browser_stream` are constructed (3306-3322) and `dispatch_apply(...)` is called with `run_job_kwargs` carrying `broker`/`identity_id` (3340-3361). The v2 branch goes AT this call site, using the SAME `broker`/`ident`/`browser_stream`. READ `src/applypilot/apply/skill_runner.py:41,53-56` (`FEATURE_FLAG_ENV` + `is_skill_flow_enabled` — the fresh-read `os.environ.get(...).strip().lower() in {"1","true","yes","on"}` idiom to mirror). READ `src/applypilot/apply/prefill.py:103-129` (`_detect_ats(url) -> str`) — reuse it for `_is_greenhouse` rather than re-parsing.

- [ ] **Step 2: Write failing tests** (inject fakes for both engines; assert routing + fail-open + labeling)

```python
# tests/test_v2_dispatch_seam.py
import os

from applypilot.apply import launcher


def test_v2_flag_fresh_read(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_ENGINE", raising=False)
    assert launcher._v2_enabled() is False
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    assert launcher._v2_enabled() is True             # fresh-read, no import-time cache
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "off")
    assert launcher._v2_enabled() is False


def test_is_greenhouse_gate():
    assert launcher._is_greenhouse({"application_url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert not launcher._is_greenhouse({"application_url": "https://jobs.lever.co/x/y"})


def test_dispatch_v2_route_when_enabled_and_greenhouse(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    calls = {"v2": 0, "legacy": 0}

    def fake_v2(**kwargs):
        calls["v2"] += 1
        return "applied", 1234, {"tier_used": "v2_greenhouse", "ats": "greenhouse"}

    def fake_legacy(**kwargs):
        calls["legacy"] += 1
        return "applied", 10, {"tier_used": "legacy_llm"}

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=object(), conn=object(), company="acme", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=fake_v2, legacy_dispatch_fn=fake_legacy)
    assert status == "applied" and prefill["tier_used"] == "v2_greenhouse"
    assert calls["v2"] == 1 and calls["legacy"] == 0


def test_dispatch_v2_fails_open_to_legacy(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    from applypilot.apply.v2.orchestrator import FALLBACK_SENTINEL
    calls = {"legacy": 0}

    def fake_v2(**kwargs):
        return FALLBACK_SENTINEL, 5, None             # v2 could not parse -> fall open

    def fake_legacy(**kwargs):
        calls["legacy"] += 1
        return "applied", 20, {"tier_used": "legacy_llm"}

    status, ms, prefill = launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=object(), conn=object(), company="acme", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=fake_v2, legacy_dispatch_fn=fake_legacy)
    assert status == "applied" and prefill["tier_used"] == "legacy_llm"
    assert calls["legacy"] == 1                        # legacy ran as counted fallback


def test_dispatch_legacy_when_flag_off(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_V2_ENGINE", raising=False)
    calls = {"v2": 0, "legacy": 0}
    launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://boards.greenhouse.io/acme/jobs/1", "url": "u"},
        page=object(), conn=object(), company="acme", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=lambda **k: (calls.__setitem__("v2", 1), ("applied", 1, {}))[1],
        legacy_dispatch_fn=lambda **k: (calls.__setitem__("legacy", 1), ("applied", 1, {"tier_used": "legacy_llm"}))[1])
    assert calls["v2"] == 0 and calls["legacy"] == 1   # flag off -> legacy only


def test_dispatch_legacy_when_not_greenhouse(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_V2_ENGINE", "1")
    calls = {"v2": 0, "legacy": 0}
    launcher._dispatch_apply_v2_aware(
        job={"application_url": "https://jobs.lever.co/x/y", "url": "u"},
        page=object(), conn=object(), company="x", operator=None,
        broker=object(), identity_id="id1", browser_stream=object(), dry_run=False,
        verify_threshold=0.75,
        run_form_compiler_fn=lambda **k: (calls.__setitem__("v2", 1), ("applied", 1, {}))[1],
        legacy_dispatch_fn=lambda **k: (calls.__setitem__("legacy", 1), ("applied", 1, {"tier_used": "legacy_llm"}))[1])
    assert calls["v2"] == 0 and calls["legacy"] == 1   # Lever is Phase 4 -> legacy
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_dispatch_seam.py -v`

- [ ] **Step 4: Add the flag + gate helpers + the v2-aware dispatch wrapper**

```python
# launcher.py
def _v2_enabled() -> bool:
    """Fresh-read APPLYPILOT_V2_ENGINE each call (mirrors is_skill_flow_enabled)."""
    return (os.environ.get("APPLYPILOT_V2_ENGINE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_greenhouse(job: dict) -> bool:
    from applypilot.apply.prefill import _detect_ats
    return _detect_ats(job.get("application_url") or job.get("url") or "") == "greenhouse"


def _dispatch_apply_v2_aware(*, job, page, conn, company, operator, broker,
                             identity_id, browser_stream, dry_run, verify_threshold,
                             run_form_compiler_fn=None, legacy_dispatch_fn=None):
    """Route Greenhouse jobs to v2 when the flag is on; fall OPEN to legacy on
    the fallback sentinel (invariant 2). SAME broker/identity/stream (invariant 1)."""
    from applypilot.apply.v2.orchestrator import FALLBACK_SENTINEL
    if run_form_compiler_fn is None:
        from applypilot.apply.v2.orchestrator import run_form_compiler as run_form_compiler_fn
    if _v2_enabled() and _is_greenhouse(job):
        # register the PASSIVE network-evidence listener on the reused stream's
        # context (invariant 8 — additive, never a route).
        from applypilot.apply.v2.verify import NetworkEvidence
        evidence = NetworkEvidence(ats="greenhouse", company=company)
        try:
            _attach_response_listener(browser_stream, page, evidence)   # see note
        except Exception:
            pass
        status, ms, prefill = run_form_compiler_fn(
            job=job, page=page, profile=config.load_profile(), conn=conn,
            company=company, operator=operator, network_evidence=evidence,
            dry_run=dry_run, verify_threshold=verify_threshold)
        if status != FALLBACK_SENTINEL:
            if isinstance(prefill, dict):
                prefill.setdefault("tier_used", "v2_greenhouse")
            return status, ms, prefill
        # else: fall through to legacy (counted fallback).
    return legacy_dispatch_fn(job=job)
```

Then at the `worker_loop` call site (launcher ~3340) REPLACE the direct `dispatch_apply(...)` call with `_dispatch_apply_v2_aware(...)`, passing a `legacy_dispatch_fn` closure that calls the existing `dispatch_apply(job=..., port=port, worker_id=..., run_job_fn=run_job, run_job_kwargs={...broker, identity_id...}, browser_stream=browser_stream)` exactly as today. The `operator` is constructed once per worker (or per job) from the configured provider — `from applypilot.apply.v2.operator import LLMOperator; operator = LLMOperator()` (rides MeteredClient via get_client). The `conn` is `get_connection()`; `company` is `company_key_for_job(job)` (reuse skill_runner's helper — READ it).

Note on `_attach_response_listener`: the browser_stream owns its own Playwright context on a daemon thread; the executor drives a SEPARATE short-lived CDP connection. Attach the response listener on the PAGE the orchestrator drives (`page.on("response", evidence.on_response)`), not the stream's private context — simpler and correct. READ how the orchestrator obtains its `page` (it connects over CDP like `prefill_application` does, prefill.py:1255-1260) and attach there. If threading the listener through the stream is cleaner, expose a `browser_stream.add_response_listener(cb)` that registers on its context — but the page-level attach is the minimal correct choice; document which you chose.

- [ ] **Step 5: Run — expect pass, then the launcher suite**

Run: `& $PY -m pytest tests/test_v2_dispatch_seam.py -v`
Run: `& $PY -m pytest tests/ -k "launcher or dispatch or skill_runner" -q`
Expected: ALL PASS. With `APPLYPILOT_V2_ENGINE` unset, behavior is byte-for-byte the legacy path (the wrapper is a passthrough) — confirm no skill/legacy test regressed.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/launcher.py tests/test_v2_dispatch_seam.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: dispatch seam (_dispatch_apply_v2_aware) gated by APPLYPILOT_V2_ENGINE; Greenhouse-only; fail-open to legacy; passive response listener; tier_used=v2_greenhouse"
```

---

## Task 11: Flight recorder + `applypilot fixtures promote <run>` CLI + recorded-real-DOM CI regression test

The testing/telemetry spine (spec §11). Currently ZERO recorded fixtures exist — all DOM tests use synthetic `set_content`. This task adds: (1) a `flight_recorder` that writes a per-attempt bundle (form IR, per-field provenance + CommitResults, network log, the captured DOM HTML) under `$APPLYPILOT_DIR/flight/`; (2) an `applypilot fixtures promote <run>` CLI that turns a bundle's captured real DOM into a replayable pytest fixture; (3) a CI regression test that replays a promoted fixture through the v2 front-end + resolver (real recorded DOM, not synthetic). Per-phase p50/p90 timings land in the bundle from day one.

**Files:**
- Create: `src/applypilot/apply/v2/flight_recorder.py`
- Modify: `src/applypilot/cli.py` (add `fixtures` Typer sub-app with `promote`)
- Create: `tests/fixtures/v2/` (promoted-fixture directory; starts empty with a `.gitkeep`)
- Create: `tests/test_v2_flight_recorder.py`, `tests/test_v2_fixture_replay.py`

- [ ] **Step 1: READ the CLI sub-app pattern + the recorder precedent + artifact paths**

READ `src/applypilot/cli.py:22-26` (`app = typer.Typer(...)`), `:66-73` (`_bootstrap()`), and the `atlas_app`/`app.add_typer` nesting example (added in Phase 2 Task 10 if present, else the doctor/report command style). READ `src/applypilot/apply/recorder.py:76-148` (`SkillRecorder` — the v1 precedent for capturing a run; the v2 recorder is simpler: it dumps IR+provenance+DOM, it does NOT reduce to a replayable Skill). READ `src/applypilot/config.py` for `APP_DIR` / `LOG_DIR` so the bundle path keys off `APPLYPILOT_DIR`. Confirm there is NO existing `fixtures` command (verified absent at authoring).

- [ ] **Step 2: Write failing tests**

```python
# tests/test_v2_flight_recorder.py
import json

from applypilot.apply.v2 import flight_recorder as fr
from applypilot.apply.v2 import ir


def test_bundle_roundtrips_ir_provenance_dom_timings(tmp_path):
    rec = fr.FlightRecorder(run_dir=tmp_path, job_url="https://boards.greenhouse.io/acme/jobs/1",
                            ats="greenhouse", company="acme")
    rec.set_schema(ir.FormSchema("greenhouse", "acme", "u",
                                 [ir.Step(0, [], terminal=True)]))
    rec.record_field(field_fp="fp1", semantic_key="email", provenance="profile.personal.email",
                     driver="text", committed=True, locator_tier="label")
    rec.record_network("POST", "https://boards.greenhouse.io/acme/applications", 201)
    rec.record_phase("parse", 900); rec.record_phase("fill", 18000)
    rec.set_dom("<html><body><form>...captured real DOM...</form></body></html>")
    path = rec.commit(status="applied")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "applied" and data["ats"] == "greenhouse"
    assert data["fields"][0]["provenance"] == "profile.personal.email"
    assert data["network"][0]["status"] == 201
    assert data["phases"]["parse"] == 900
    assert data["dom_html"].startswith("<html")          # real DOM captured
```

```python
# tests/test_v2_fixture_replay.py
import json
from pathlib import Path

import pytest

from applypilot.apply.v2 import frontend_greenhouse as fe
from applypilot.apply.v2 import resolver as rz
from applypilot.apply.browser_stream import collect_browser_observation


FIXDIR = Path(__file__).parent / "fixtures" / "v2"
_fixtures = sorted(FIXDIR.glob("*.html")) if FIXDIR.exists() else []


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_context().new_page()
        yield p
        b.close()


@pytest.mark.skipif(not _fixtures, reason="no promoted fixtures yet (populate via 'applypilot fixtures promote')")
@pytest.mark.parametrize("html_path", _fixtures, ids=lambda p: p.stem)
def test_recorded_dom_parses_and_resolves(page, html_path):
    # Real recorded DOM (NOT synthetic) parses to a schema whose standard fields
    # resolve deterministically. Guards against parse-gap regressions (spec §11).
    page.set_content(html_path.read_text(encoding="utf-8"))
    obs = collect_browser_observation(page)
    schema = fe.parse_observation(obs, company=html_path.stem, url="https://boards.greenhouse.io/x/jobs/1")
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    # every promoted GH fixture must at least surface the resume + a name/email field
    assert "resume" in keys or "email" in keys or "first_name" in keys
    expected = json.loads(html_path.with_suffix(".expected.json").read_text(encoding="utf-8")) \
        if html_path.with_suffix(".expected.json").exists() else None
    if expected:
        assert {f.semantic_key for s in schema.steps for f in s.fields} >= set(expected["semantic_keys"])
```

- [ ] **Step 3: Run — expect failure / skip**

Run: `& $PY -m pytest tests/test_v2_flight_recorder.py tests/test_v2_fixture_replay.py -v`
(The replay test SKIPS until a fixture is promoted — that is expected and correct in Phase 3.)

- [ ] **Step 4: Implement `flight_recorder.py`**

```python
"""Flight recorder (spec §11): per-attempt bundle = form IR + per-field
provenance/CommitResults + network log + captured real DOM + per-phase timings.
The bundle is what 'applypilot fixtures promote' turns into a CI regression, so
recorded REAL DOM finally replaces synthetic-only tests. Storage-capped by the
caller's retention policy (90-day artifacts, rows forever)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class _FieldRecord:
    field_fp: str
    semantic_key: str | None
    provenance: str            # profile.<path> | answer:<qfp> | policy.<k> | oracle
    driver: str
    committed: bool
    locator_tier: str | None = None


class FlightRecorder:
    def __init__(self, *, run_dir, job_url: str, ats: str, company: str):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.job_url = job_url
        self.ats = ats
        self.company = company
        self._schema = None
        self._fields: list[_FieldRecord] = []
        self._network: list[dict] = []
        self._phases: dict[str, int] = {}
        self._dom_html: str = ""

    def set_schema(self, schema) -> None:
        self._schema = schema

    def record_field(self, *, field_fp, semantic_key, provenance, driver,
                     committed, locator_tier=None) -> None:
        self._fields.append(_FieldRecord(field_fp, semantic_key, provenance, driver,
                                         committed, locator_tier))

    def record_network(self, method: str, url: str, status: int) -> None:
        self._network.append({"method": method, "url": url, "status": status})

    def record_phase(self, phase: str, ms: int) -> None:
        self._phases[phase] = ms

    def set_dom(self, html: str) -> None:
        self._dom_html = html or ""

    def commit(self, *, status: str) -> Path:
        stem = f"{self.company}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        bundle = {
            "job_url": self.job_url, "ats": self.ats, "company": self.company,
            "status": status, "created_at": datetime.now(timezone.utc).isoformat(),
            "template_fp": self._schema.template_fp() if self._schema else None,
            "questions_fp": self._schema.questions_fp() if self._schema else None,
            "fields": [asdict(f) for f in self._fields],
            "network": self._network,
            "phases": self._phases,
            "dom_html": self._dom_html,
        }
        path = self.run_dir / f"{stem}.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=0), encoding="utf-8")
        return path
```

- [ ] **Step 5: Add the `fixtures promote` CLI**

```python
# cli.py
fixtures_app = typer.Typer(help="Flight-recorder fixture management (v2 CI regressions).")
app.add_typer(fixtures_app, name="fixtures")


@fixtures_app.command("promote")
def fixtures_promote(
    run: str = typer.Argument(..., help="Path to a flight-recorder bundle .json (or run stem)."),
    out_dir: str = typer.Option("tests/fixtures/v2", "--out", help="Fixture dir."),
) -> None:
    """Turn a flight-recorder bundle into a replayable CI fixture: writes
    <company>.html (the captured real DOM) + <company>.expected.json (semantic
    keys the front-end must recover). Real recorded DOM replaces synthetic tests."""
    _bootstrap()
    import json
    from pathlib import Path
    p = Path(run)
    if not p.exists():
        from applypilot import config
        p = config.APP_DIR / "flight" / f"{run}.json"
    bundle = json.loads(p.read_text(encoding="utf-8"))
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    stem = bundle["company"]
    (out / f"{stem}.html").write_text(bundle.get("dom_html", ""), encoding="utf-8")
    expected = {"semantic_keys": sorted({f["semantic_key"] for f in bundle.get("fields", [])
                                         if f.get("semantic_key")})}
    (out / f"{stem}.expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    console.print(f"Promoted fixture [bold]{stem}[/bold] to {out} "
                  f"({len(expected['semantic_keys'])} semantic keys).")
```

Also create `tests/fixtures/v2/.gitkeep` so the directory exists in the repo before any real fixture is promoted (the replay test skips cleanly on empty).

- [ ] **Step 6: Wire the recorder into the orchestrator (light) + CI-on-push note**

In `run_form_compiler` (Task 9), instantiate a `FlightRecorder` on failure/`needs_review`/sampled success and call `record_field`/`record_network`/`record_phase`/`set_dom`/`commit`. Keep it OFF the critical path for the common success case (sample rate, or record only non-`applied` outcomes in Phase 3) — MHTML/DOM capture on the hot path is explicitly forbidden by §6.9. The orchestrator already tracks per-phase timings; pass them to `record_phase`.

CI-on-push (spec §11/§13 Phase 0): the fixture-replay test runs in the existing pytest suite, so once fixtures are promoted CI covers real-DOM parse regressions automatically. If CI-on-push is not yet configured (Phase 0 item), note that the fixture test is READY but the workflow wiring is a Phase-0/infra task — see Open Decisions on whether `fixtures promote` + CI-on-push lands in Phase 3 or is deferred.

- [ ] **Step 7: Run — expect pass (replay skips), then commit**

Run: `& $PY -m pytest tests/test_v2_flight_recorder.py tests/test_v2_fixture_replay.py -v`
Expected: recorder tests PASS; replay test SKIPS (no fixtures yet).

```powershell
git reset
git add src/applypilot/apply/v2/flight_recorder.py src/applypilot/cli.py tests/test_v2_flight_recorder.py tests/test_v2_fixture_replay.py tests/fixtures/v2/.gitkeep
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: flight recorder (IR+provenance+network+DOM+timings bundle) + 'applypilot fixtures promote' + recorded-real-DOM CI regression test (skips until first fixture)"
```

---

## Task 12: A/B measurement (reporting `by_tier` v2-vs-legacy) + shadow go/no-go for cutover

The Phase-3 exit gate is FREE telemetry (spec §13 Phase 3 + §12): `review.jsonl` rows already carry `tier_used`/`duration_ms`/`status`, and `reporting.summarize_review` already computes `by_tier[<tier>] = {n, applied, pass_rate}`. Because v2 writes `tier_used="v2_greenhouse"` (Task 10) and legacy writes `tier_used="legacy_llm"` (via `dispatch_apply`), the A/B falls out of the existing report. This task adds ONE pure helper — `v2_ab_verdict(summary)` — that reads `by_tier` and returns the cutover go/no-go: **v2 ≥ v1 on 100+ Greenhouse rows**. No new telemetry infra.

**Files:**
- Modify: `src/applypilot/reporting.py` (add pure `v2_ab_verdict(summary)`; optionally a `by_tier` line in the `report` renderer)
- Test: `tests/test_v2_ab.py`

- [ ] **Step 1: READ the existing by_tier structure + the report renderer**

READ `src/applypilot/reporting.py:111-117` (`by_tier` accumulation: `d = by_tier.setdefault(t, {"n": 0, "applied": 0})`) and `:161-162` (the return: `by_tier[k] = {**v, "pass_rate": round(applied/n, 3)}`). CONFIRM the exact key strings: v2 rows → `"v2_greenhouse"`, legacy rows → `"legacy_llm"` (set in `skill_runner.dispatch_apply` line ~391 and the v2 seam in Task 10). READ how `applypilot report` (cli.py) calls `summarize_review` so the new verdict can render alongside.

- [ ] **Step 2: Write failing tests** (pure — synthetic review rows, $0)

```python
# tests/test_v2_ab.py
from applypilot.reporting import summarize_review, v2_ab_verdict


def _rows(v2_applied, v2_n, legacy_applied, legacy_n, *, dry_run=False):
    rows = []
    for i in range(v2_n):
        rows.append({"status": "applied" if i < v2_applied else "failed:x",
                     "tier_used": "v2_greenhouse", "duration_ms": 40000, "dry_run": dry_run,
                     "site": "greenhouse"})
    for i in range(legacy_n):
        rows.append({"status": "applied" if i < legacy_applied else "failed:x",
                     "tier_used": "legacy_llm", "duration_ms": 90000, "dry_run": dry_run,
                     "site": "greenhouse"})
    return rows


def test_verdict_no_go_below_100_rows():
    summary = summarize_review(_rows(v2_applied=40, v2_n=50, legacy_applied=35, legacy_n=50))
    v = v2_ab_verdict(summary)
    assert v["go"] is False
    assert v["reason"] and "100" in v["reason"]        # not enough rows yet
    assert v["v2"]["n"] == 50 and v["legacy"]["n"] == 50


def test_verdict_go_when_v2_ge_v1_and_100plus_rows():
    # v2 pass_rate 0.90 (108/120), legacy 0.80 (80/100) -> GO
    summary = summarize_review(_rows(v2_applied=108, v2_n=120, legacy_applied=80, legacy_n=100))
    v = v2_ab_verdict(summary)
    assert v["v2"]["n"] >= 100
    assert v["v2"]["pass_rate"] >= v["legacy"]["pass_rate"]
    assert v["go"] is True


def test_verdict_no_go_when_v2_worse():
    summary = summarize_review(_rows(v2_applied=70, v2_n=110, legacy_applied=95, legacy_n=110))
    v = v2_ab_verdict(summary)
    assert v["go"] is False                            # v2 pass_rate < legacy
    assert "pass_rate" in v["reason"]


def test_verdict_ignores_dry_run_rows():
    # summarize_review already excludes dry-run from `live`; verdict inherits that.
    summary = summarize_review(_rows(108, 120, 80, 100, dry_run=True))
    v = v2_ab_verdict(summary)
    assert v["v2"]["n"] == 0 and v["go"] is False       # nothing live to judge
```

- [ ] **Step 3: Run — expect failure**

Run: `& $PY -m pytest tests/test_v2_ab.py -v`

- [ ] **Step 4: Implement `v2_ab_verdict`**

```python
# reporting.py (append; PURE — reads the summary dict summarize_review returns)
def v2_ab_verdict(summary: dict, *, min_rows: int = 100,
                  v2_tier: str = "v2_greenhouse", legacy_tier: str = "legacy_llm") -> dict:
    """Phase-3 cutover gate (spec §13): cut over to v2 iff it matches or beats
    legacy on >= min_rows live Greenhouse rows. Pure over summarize_review's
    by_tier. No new telemetry — v2 vs legacy fall out of tier_used."""
    by_tier = summary.get("by_tier", {})
    v2 = by_tier.get(v2_tier, {"n": 0, "applied": 0, "pass_rate": 0.0})
    legacy = by_tier.get(legacy_tier, {"n": 0, "applied": 0, "pass_rate": 0.0})
    if v2["n"] < min_rows:
        reason = f"insufficient v2 sample: {v2['n']} < {min_rows} live rows"
        go = False
    elif v2["pass_rate"] < legacy["pass_rate"]:
        reason = (f"v2 pass_rate {v2['pass_rate']} < legacy {legacy['pass_rate']} "
                  f"(n_v2={v2['n']}, n_legacy={legacy['n']})")
        go = False
    else:
        reason = (f"v2 pass_rate {v2['pass_rate']} >= legacy {legacy['pass_rate']} "
                  f"on {v2['n']} live rows")
        go = True
    return {"go": go, "reason": reason, "v2": v2, "legacy": legacy, "min_rows": min_rows}
```

Optionally, in the `applypilot report` renderer (cli.py), print a `by_tier` table + the verdict line when both tiers are present:

```python
    if "v2_greenhouse" in summary.get("by_tier", {}):
        v = reporting.v2_ab_verdict(summary)
        console.print(f"v2 A/B: {'[green]CUTOVER-READY[/green]' if v['go'] else '[yellow]HOLD[/yellow]'} — {v['reason']}")
```

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_v2_ab.py -v`
Expected: ALL PASS.

- [ ] **Step 5b: Make §12.2 (speed) and §12.3 (audit) EXPLICIT go/no-go sub-steps (not just prose)**

The cutover gate is THREE conditions, all of which must be checked as explicit sub-steps before flipping the flag — pass-rate alone (Step 4) is not sufficient:

1. **§12.1 pass-rate:** `v2_ab_verdict(summary)["go"] is True` (v2 ≥ legacy on ≥100 live Greenhouse rows). Already implemented in Step 4.
2. **§12.2 speed (p50 ≤45s warm):** compute the **p50 of `duration_ms`** over the LIVE `tier_used="v2_greenhouse"` rows in `review.jsonl` and assert `p50_ms <= 45000`. This is a pure query over the same rows `summarize_review` reads — add it as a `v2_p50_duration_ms(rows)` helper (or read `by_tier["v2_greenhouse"]` durations) and gate on it. Account for the react-select sleep tax (Task 6 note) here.
3. **§12.3 safety audit (zero violations):** query the `submission_ledger` for **zero duplicate and zero dangling INTENTs** attributable to v2 (a `needs_review:v2_crashed_post_submit` leaves a dangling INTENT by design — it must be reconciled, not counted as a clean submit), and query the flight-recorder bundles / canary provenance for **zero canary-field violations** and **zero unauthorized submissions** (no submit-POST without an open broker ticket).

Cutover is GO only if ALL THREE pass. Wire these as concrete checks the operator runs (a small `applypilot report --v2-cutover` sub-command or a documented query trio), not as a paragraph — a lucky 100 with p50=60s or a single dangling INTENT is a HOLD.

- [ ] **Step 6: Document the shadow go/no-go (in the verdict docstring + task summary)**

The Phase-3 → cutover go/no-go (spec §13 Phase 3 exit + §12 items 1-3):
- **GO / cut over Greenhouse to v2 when:** `v2_ab_verdict` returns `go=True` (v2 pass_rate ≥ legacy on ≥100 live Greenhouse rows) AND acceptance-gate corroboration holds: zero canary-field violations + zero identity-duplicate submissions across the burn-in corpus (§12 item 3 — query `submission_ledger` for dangling/duplicate INTENTs and the flight recorder for canary provenance), AND median v2 wall-clock ≤45s warm Greenhouse (§12 item 2 — `by_tier["v2_greenhouse"]` duration_ms p50 from review.jsonl). Only then retire the legacy tier for Greenhouse (spec §6.8: "retired per-ATS once v2 ≥ v1 on 100+ live rows").
- **HOLD / iterate when:** v2 sample < 100, OR v2 pass_rate < legacy, OR any canary/duplicate violation, OR p50 > 45s. Keep shadow/A-B running; drive the named-failure-class grind (§13 Phase 5 pattern) on v2's `(A)-removable` failures from `reporting`'s bucketing.
- **Shadow sample aggressiveness** (Open Decisions): 100 rows is the floor; recommend accumulating on a **live but non-cutover** A/B where v2 runs on a fraction of Greenhouse jobs (flag on) while legacy remains the counted fallback, so a v2 regression never zeroes the live queue (invariant 2 guarantees fail-open). Cache-hit rate (`mapping_cache.stats`) and parse-gap rate must be MEASURED (risk "fingerprint/cache hit-rate overstated", §15) before any speed claim — report them alongside the verdict.

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/reporting.py tests/test_v2_ab.py
# add src/applypilot/cli.py IF you added the report render line
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "v2: A/B cutover verdict (pure v2_ab_verdict over existing by_tier: v2>=v1 on 100+ live Greenhouse rows) + report render"
```

---

## Task 13: Phase 3 verification

- [ ] **Step 1: Full suite green**

Run: `& $PY -m pytest tests/ -q`
Expected: ALL PASS, 0 failed (the fixture-replay test SKIPS — that is not a failure). Investigate any regression, especially in `-k "launcher or ledger or broker or skill_runner"` (the safety-prologue extraction in Task 9 is the highest-risk refactor — a regression there is a submission-safety bug, not a test nit).

- [ ] **Step 2: Lint the new package**

Run: `ruff check src/applypilot/apply/v2`
Expected: clean. Fix mechanical findings (unused imports, whitespace); for anything non-trivial add a targeted `# noqa: <code>` with a one-word reason and note it in the summary. The broad `except Exception` blocks are DELIBERATE (fail-open, invariant 2) — keep the `# noqa: BLE001` markers with the "fail-open" reason.

- [ ] **Step 3: Prove the flag is a true no-op when OFF (shadow safety)**

Confirm by reading (not a live apply) that with `APPLYPILOT_V2_ENGINE` unset:
- `_dispatch_apply_v2_aware` calls only `legacy_dispatch_fn` (the existing `dispatch_apply`) — byte-for-byte the pre-Phase-3 path.
- No v2 module is imported on the legacy path except the cheap `FALLBACK_SENTINEL`/flag check.
- The safety-prologue extraction (Task 9) preserved every short-circuit status verbatim (the launcher regression suite in Step 1 is the proof).

- [ ] **Step 4: End-to-end wiring smoke on synthetic DOM (no network, no real Chrome subprocess, no live apply)**

Write a scratch script (in the scratchpad dir, NOT the repo) that, using the `page` fixture pattern (headless Playwright + `set_content` of a synthetic Greenhouse form + a fake Operator + a temp DB):
1. `collect_browser_observation` → `frontend_greenhouse.parse_observation` → a FormSchema with the expected semantic keys.
2. `resolver.resolve` → a FillPlan (standard fields resolved from profile; a custom question in `needs_oracle`).
3. `executor.execute` (resume-first + settle) → an ExecReport with `ready_to_submit` and committed keys; assert `mapping_cache` got binding rows (not values).
4. Feed a fake `NetworkEvidence` a submit-POST response → `verify.verify` returns Tier-1 verified + `submit_endpoints` harvested.
5. `run_form_compiler` with injected fakes returns `("applied", ms, {"tier_used": "v2_greenhouse"})`.
6. Two synthetic review rows (one `v2_greenhouse`, one `legacy_llm`) through `summarize_review` → `v2_ab_verdict` returns a NO-GO (n<100) with a sane reason.
Confirm the whole pipeline composes without a live crawl or a real submission. Do NOT commit the scratch script. This is the "dry-run vs fixtures" leg of the Phase-3 exit (spec §13) exercised at $0.

- [ ] **Step 5: Confirm shadow/A-B posture is non-disruptive + the legacy fallback is intact**

Verify (by reading):
- v2 is Greenhouse-ONLY (`_is_greenhouse` gate) — Ashby/Lever untouched, still legacy (Phase 4).
- v2 fails OPEN everywhere (parse/resolve exceptions → sentinel → legacy; unclear verify → sentinel → legacy) — the live queue never regresses (invariant 2).
- The SAME `broker`/`identity_id`/`browser_stream`/`ledger` flow through v2 (invariant 1) — v2 did NOT construct its own safety-kernel objects, install its own routes, or call `route.fetch()`.
- Canary keys never reach the Operator (resolver short-circuits before oracle batching, invariant 7).

- [ ] **Step 6: Tree clean + report**

Run: `git status --short`
Expected: EMPTY (all v2 work committed; no stray scratch files; `tests/fixtures/v2/.gitkeep` present, no promoted fixtures committed unless one was intentionally recorded).

Summarize to the user: commits made, total new tests + pass count (+ the one intentional skip), the synthetic end-to-end smoke result, the documented cutover go/no-go criteria (v2 ≥ v1 on 100+ live Greenhouse rows + zero canary/duplicate violations + p50 ≤45s), and the reminder that v2 ships **behind `APPLYPILOT_V2_ENGINE` in shadow/A-B** — the legacy Claude-CLI agent stays as the counted fallback (spec §6.8) until the operator flips the flag, accumulates 100+ live rows, and `v2_ab_verdict` clears. Cutover retires the legacy tier for Greenhouse ONLY.

---

## Open decisions for the controller

- **Front-end: pure Greenhouse-only now vs a GenericFrontend seam.** Recommended default: **pure Greenhouse-only** (`frontend_greenhouse.parse_observation`) in Phase 3. The spec's GenericFrontend + `label_controls` degraded tier (§6.8) is a Phase-4 concern (Ashby/Lever long-tail). Building a generic seam now is speculative generality (§14 YAGNI) — the observation→IR translation is already ATS-agnostic in shape (it consumes a `BrowserObservation`), so promoting a `frontend_generic.py` in Phase 4 is a clean add, not a rewrite. Ship one concrete Greenhouse front-end; keep `parse_observation`'s signature ATS-neutral so Phase 4 slots in.

- **`fixtures promote` + CI-on-push: Phase 3 or deferred?** Recommended default: **ship `fixtures promote` + the (skipping) replay test in Phase 3** (Task 11) but treat **CI-on-push wiring as the Phase-0 infra item it already is** (§13 Phase 0). Rationale: the fixture machinery must exist so the FIRST live v2 run's flight-recorder bundle can be promoted same-day (spec §6.8 "same-day fixture promotion by policy"), and having ZERO recorded fixtures today is called out as the gap to close. But the GitHub Actions workflow that runs pytest on push is Phase-0 hygiene; if it's not yet green, note it and let the fixture test ride the local suite meanwhile. Do NOT block Phase 3 on CI infra.

- **Shadow A/B sample aggressiveness before cutover.** Recommended default: **100 live Greenhouse rows minimum (the §12/§13 floor), accumulated in a live-but-non-cutover A/B** where `APPLYPILOT_V2_ENGINE=1` routes a fraction of Greenhouse jobs to v2 while legacy stays the counted fallback. Because v2 fails open (invariant 2), a v2 regression can never zero the live queue — so the A/B can run on real applies safely from day one. Require, alongside the pass-rate gate: measured cache-hit rate (`mapping_cache.stats`) and parse-gap rate (risk §15 "hit-rate overstated" — MEASURE before any speed claim), zero canary/duplicate violations, and p50 ≤45s. Do not cut over on a lucky 100; prefer 100+ with a stable trailing window.

- **Operator: ship all 3 methods now, or just `resolve_fields`?** Recommended default: **`resolve_fields` fully; `score` and `label_controls` declared-but-`NotImplementedError`** (Task 2). `resolve_fields` is the only method the apply path strictly needs. `score` is Phase-4 compile-then-score matching (§7.3); `label_controls` is the Phase-4 degraded parse tier (§6.8). Declaring all three in the `Operator` Protocol now locks the transport-agnostic interface (acceptance #4) without building unused surface — a provider file implements `resolve_fields` and inherits the stubs. Wire `score`/`label_controls` in Phase 4 when their consumers exist.

- **Where the passive network-evidence listener attaches.** Recommended default: **attach `page.on("response", evidence.on_response)` on the orchestrator's own CDP-driven page** (the one the executor fills through), NOT on `BrowserStateStream`'s private daemon-thread context. Simpler, correct, and keeps the safety kernel's `_guard` route untouched (invariant 8: additive passive listener, never a route, never `route.fetch()`). If a future need arises to observe cross-tab submits, expose `browser_stream.add_response_listener(cb)` then — but page-level is the minimal correct choice for the single-tab Greenhouse submit.

- **Legacy-tier retirement scope.** Recommended default: **retire legacy for Greenhouse ONLY at cutover**, keep it live for every other ATS (Ashby/Lever/Workday) through Phase 4 (spec §6.8: "retired per-ATS once v2 ≥ v1"). The `_dispatch_apply_v2_aware` gate is already per-job/per-ATS, so retirement is a policy flip (stop counting legacy on Greenhouse), not a code deletion — keep the legacy path in the tree as documented insurance against correlated Greenhouse DOM churn (risk §15) until Phase 5's ratchet explicitly removes it.
