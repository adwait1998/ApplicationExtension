# Phase 1 — Spine + Safety Kernel (under the current engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the v2 "spine" (job identity, one gate engine at ingest, one queue-policy function, spend ledger) and the safety kernel (canary-field hardening, submit broker + CDP network containment, two-phase submission ledger) **underneath the existing apply engine** — so the current pipeline becomes safer and less wasteful immediately, before any engine rewrite.

**Architecture:** Additive. New pure modules (`identity.py`, `gate.py`, `canary.py`, `submit_broker.py`, `spend_ledger.py`) with deterministic logic and heavy unit coverage; new SQLite columns/tables via the existing `_ALL_COLUMNS`/`ensure_columns()` pattern; a single `queue_policy()` in `database.py` that the ~6 drifting eligibility predicates route through; hooks wired into the 5 discovery INSERT sites (gate-at-ingest), the answer-cache consumer (canary), the submission path (broker + network route), and the `llm.py` singleton (metering). No apply-engine control-flow rewrite — that's Phase 3.

**Tech Stack:** Python 3.11, SQLite (WAL, thread-local), Playwright sync over CDP, pytest, Typer.

**Spec:** `docs/superpowers/specs/2026-07-02-applypilot-v2-design.md` §5 (spine), §10 (safety kernel).

**Prerequisite:** Phase 0 complete (green tree, all committed).

**Conventions (same as Phase 0):** run from `e:\auto-apply-pipeline`; `PY = C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe`; commit with one-shot identity, never push; `git diff --cached --stat` before every commit; `git reset` first if unexpected files are staged.

**File structure created by this plan:**
- `src/applypilot/identity.py` — URL → `(ats, token, job_id)` parser + `identity_id` (Tasks 1-2)
- `src/applypilot/gate/__init__.py`, `gate/engine.py`, `gate/rules.py`, `gate/gazetteer.py`, `gate/profile_map.py` — the one gate engine + the profile→policy mapper (Tasks 3-6)
- `src/applypilot/apply/canary.py` — canary-class classifier + deterministic resolver (Task 7)
- `src/applypilot/apply/submit_broker.py` — file-based one-shot submit tickets (Task 9)
- `src/applypilot/submission_ledger.py` — durable two-phase INTENT→CONFIRMED submission record (Task 10A)
- `src/applypilot/spend_ledger.py` — metered LLM client wrapper + ledger (Task 11)
- Modified: `database.py` (columns, tables, `queue_policy()` incl. already-applied/cooldown), `answer_cache.py` + `tests/test_answer_cache.py` (canary hardening), `stream_executor.py` + `browser_stream.py` + `launcher.py` (broker + network route + submission ledger + spend enforcement), all 5 discovery INSERT sites, `pipeline.py`/`cli.py` (gate stage), `llm.py` (metering singleton).

**One-key rule (load-bearing — a review caught a deadlock here):** the submit broker, the submission ledger, the network route guard, and the already-applied/cooldown checks ALL key off `identity_id` (the `ats:token:job_id` string from `identity.py`) — NEVER the sha256 `idempotency_key`. Compute `ident = identity_id(job["url"], company=job.get("site"), title=job.get("title"), location=job.get("location"))` once in `worker_loop` and thread that exact value into `run_job`, the `BrowserStateStream`, `_greenhouse_adapter_pass`, and the submission ledger. Mixing the two keys means tickets never match and every submission blocks.

---

## Group A — Job Identity

### Task 1: URL → (ats, token, job_id) parser

**Files:**
- Create: `src/applypilot/identity.py`
- Test: `tests/test_identity.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_identity.py
from applypilot.identity import parse_ats_url, AtsRef

def test_greenhouse_canonical():
    assert parse_ats_url("https://boards.greenhouse.io/chime/jobs/8141068002?gh_jid=8141068002") == \
        AtsRef("greenhouse", "chime", "8141068002", confident=True)

def test_greenhouse_job_boards_host():
    assert parse_ats_url("https://job-boards.greenhouse.io/affirm/jobs/7546216003") == \
        AtsRef("greenhouse", "affirm", "7546216003", confident=True)

def test_greenhouse_vanity_gh_jid():
    r = parse_ats_url("https://careers.airbnb.com/positions/6153760?gh_jid=6153760")
    assert r.ats == "greenhouse" and r.token == "airbnb" and r.job_id == "6153760"
    assert r.confident is False  # token guessed from host label

def test_greenhouse_vanity_dotcareers():
    r = parse_ats_url("https://instacart.careers/job/?gh_jid=123456")
    assert r.ats == "greenhouse" and r.token == "instacart" and r.job_id == "123456"

def test_lever():
    assert parse_ats_url("https://jobs.lever.co/palantir/15f01f3a-922d-4cff-b093-888333d88628") == \
        AtsRef("lever", "palantir", "15f01f3a-922d-4cff-b093-888333d88628", confident=True)

def test_lever_apply_suffix():
    r = parse_ats_url("https://jobs.lever.co/palantir/15f01f3a-922d-4cff-b093-888333d88628/apply")
    assert r.job_id == "15f01f3a-922d-4cff-b093-888333d88628"

def test_lever_substring_false_positive_guarded():
    # 'cleverhealth' contains 'lever' but is not jobs.lever.co
    assert parse_ats_url("https://cleverhealth.com/careers/x") is None

def test_ashby():
    assert parse_ats_url("https://jobs.ashbyhq.com/baseten/126d54b4-a7bc-4456-bf4d-5d224e4f5d63") == \
        AtsRef("ashby", "baseten", "126d54b4-a7bc-4456-bf4d-5d224e4f5d63", confident=True)

def test_workday():
    r = parse_ats_url("https://adobe.wd5.myworkdayjobs.com/external_experienced/job/New-York/Digital-Strategist_R168109")
    assert r.ats == "workday" and r.token == "adobe" and r.job_id == "R168109"

def test_unsupported():
    assert parse_ats_url("https://example.com/careers/123") is None
    assert parse_ats_url("") is None
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'applypilot.identity'`.

- [ ] **Step 3: Implement `src/applypilot/identity.py`**

```python
"""URL → canonical ATS reference. Single source of truth for ATS detection,
superseding prefill._detect_ats and skill_runner._infer_ats (kept as thin
delegators). Exact-host matching only — substring 'lever'/'ashby' matching
false-positives (e.g. 'cleverhealth')."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, unquote

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,80}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)", re.I)
_WD_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$")
_WD_REQ_RE = re.compile(r"_([A-Za-z0-9-]+)$")
_VANITY_STOP = {"greenhouse", "lever", "ashbyhq", "myworkdayjobs", "icims"}


@dataclass(frozen=True)
class AtsRef:
    ats: str
    token: str
    job_id: str | None
    confident: bool = True


def _clean_token(value: str | None) -> str | None:
    if not value:
        return None
    tok = unquote(value).strip().strip("/")
    if not tok or tok.lower() in {"jobs", "job", "embed", "departments", "applications"}:
        return None
    if not _TOKEN_RE.match(tok):
        return None
    return tok.lower()


def parse_ats_url(url: str) -> AtsRef | None:
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = parsed.netloc.lower()
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    lowered = url.lower()

    # Greenhouse — canonical hosts
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts:
        token = _clean_token(parts[0])
        job_id = None
        if "jobs" in parts:
            i = parts.index("jobs")
            if i + 1 < len(parts) and parts[i + 1].isdigit():
                job_id = parts[i + 1]
        if job_id is None:
            m = _GH_JID_RE.search(url)
            job_id = m.group(1) if m else None
        return AtsRef("greenhouse", token, job_id, confident=True) if token else None
    if host == "boards-api.greenhouse.io" and len(parts) >= 3 and parts[:2] == ["v1", "boards"]:
        token = _clean_token(parts[2])
        return AtsRef("greenhouse", token, None, confident=True) if token else None

    # Lever — exact host
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        token = _clean_token(parts[0])
        job_id = parts[1] if len(parts) >= 2 and _UUID_RE.match(parts[1]) else None
        return AtsRef("lever", token, job_id, confident=True) if token else None
    if host == "api.lever.co" and len(parts) >= 3 and parts[:2] == ["v0", "postings"]:
        token = _clean_token(parts[2])
        return AtsRef("lever", token, None, confident=True) if token else None

    # Ashby — exact host
    if host == "jobs.ashbyhq.com" and parts:
        token = _clean_token(parts[0])
        job_id = parts[1] if len(parts) >= 2 and _UUID_RE.match(parts[1]) else None
        return AtsRef("ashby", token, job_id, confident=True) if token else None
    if host == "api.ashbyhq.com" and len(parts) >= 3 and parts[:2] == ["posting-api", "job-board"]:
        token = _clean_token(parts[2])
        return AtsRef("ashby", token, None, confident=True) if token else None

    # Workday — tenant.wdN.myworkdayjobs.com
    wd = _WD_HOST_RE.match(host)
    if wd:
        job_id = None
        if parts:
            m = _WD_REQ_RE.search(parts[-1])
            job_id = m.group(1) if m else None
        return AtsRef("workday", wd.group("tenant"), job_id, confident=True)

    # Greenhouse — vanity domain (gh_jid present, token guessed from host)
    m = _GH_JID_RE.search(url)
    if m:
        job_id = m.group(1)
        company = None
        if host.startswith("careers."):
            company = host.split(".")[1]
        elif host.endswith(".careers"):
            company = host.rsplit(".", 1)[0]
        elif host.startswith("jobs."):
            company = host.split(".")[1]
        else:
            path = parsed.path.lower()
            if any(seg in path for seg in ("/careers", "/positions", "/jobs", "/job")):
                labels = [p for p in host.split(".") if p and p != "www"]
                if len(labels) >= 2 and labels[0] not in _VANITY_STOP:
                    company = labels[0]
        token = _clean_token(company)
        if token:
            return AtsRef("greenhouse", token, job_id, confident=False)

    return None
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_identity.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```powershell
git reset
git add src/applypilot/identity.py tests/test_identity.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "identity: URL->(ats,token,job_id) parser (gh/lever/ashby/workday + vanity gh_jid)"
```

---

### Task 2: identity_id + cross-source dedup key

**Files:**
- Modify: `src/applypilot/identity.py`
- Test: `tests/test_identity.py`

- [ ] **Step 1: Add failing tests**

```python
# append to tests/test_identity.py
from applypilot.identity import identity_id

def test_identity_id_from_ats_ref():
    a = identity_id("https://boards.greenhouse.io/chime/jobs/8141068002?gh_jid=8141068002")
    b = identity_id("https://boards.greenhouse.io/chime/jobs/8141068002?utm_source=li")
    assert a == b == "greenhouse:chime:8141068002"   # tracking params collapse

def test_identity_id_cross_source_same_posting():
    # aggregator vanity URL and canonical URL for the same gh job → same identity
    canonical = identity_id("https://boards.greenhouse.io/airbnb/jobs/6153760")
    vanity = identity_id("https://careers.airbnb.com/positions/6153760?gh_jid=6153760")
    assert canonical == vanity == "greenhouse:airbnb:6153760"

def test_identity_id_falls_back_to_normalized_url():
    # unparseable ATS → deterministic fallback on normalized (company,title,location) not raw url
    key = identity_id("https://example.com/careers/x", company="Acme", title="Product Designer", location="Remote US")
    assert key.startswith("norm:")
    assert key == identity_id("https://other.example.com/y", company="acme", title="product designer", location="remote us")
```

- [ ] **Step 2: Run — expect failure** (`ImportError: cannot import name 'identity_id'`)

Run: `& $PY -m pytest tests/test_identity.py::test_identity_id_from_ats_ref -v`

- [ ] **Step 3: Implement**

Append to `src/applypilot/identity.py`:

```python
import hashlib


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def identity_id(url: str, *, company: str | None = None,
                title: str | None = None, location: str | None = None) -> str:
    """Stable cross-source identity for a job posting.

    ATS-parseable → 'ats:token:job_id' (or 'ats:token' when job_id unknown).
    Otherwise → 'norm:<sha1 of normalized company|title|location>'.
    Keys money/reputation records (decisions, receipts, submission ledger,
    already-applied block) so aliases and reposts fold correctly."""
    ref = parse_ats_url(url)
    if ref and ref.token:
        return f"{ref.ats}:{ref.token}:{ref.job_id}" if ref.job_id else f"{ref.ats}:{ref.token}"
    payload = "|".join((_norm(company), _norm(title), _norm(location)))
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"norm:{digest}"
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_identity.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```powershell
git reset
git add src/applypilot/identity.py tests/test_identity.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "identity: identity_id() cross-source dedup key with normalized fallback"
```

---

## Group B — The Gate Engine

### Task 3: Gate verdict schema + gazetteer location rule

**Files:**
- Create: `src/applypilot/gate/__init__.py`, `src/applypilot/gate/gazetteer.py`, `src/applypilot/gate/rules.py`
- Test: `tests/test_gate_location.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_gate_location.py
from applypilot.gate.rules import location_rule
from applypilot.gate import Verdict

# geo policy: remote-US ok; onsite only in CA / Seattle / NYC
POLICY = {"remote_ok": True, "remote_scope": "US",
          "onsite_regions": ["us-ca", "us-wa-seattle", "us-ny-nyc"]}

def test_ca_substring_bug_is_dead():
    # "ca" must NOT match "appliCAtions" — word-boundary only
    v = location_rule("Applications Engineer role", POLICY, is_title_field=False)
    assert v.code != "location_ok"  # not spuriously accepted via substring

def test_remote_us_accepts():
    assert location_rule("Remote - United States", POLICY).result == "PASS"

def test_onsite_allowed_metro():
    assert location_rule("San Francisco, CA", POLICY).result == "PASS"

def test_onsite_rejected_metro():
    v = location_rule("Austin, TX", POLICY)
    assert v.result == "REJECT" and v.code == "location_onsite_out_of_region"

def test_non_us_remote_rejected():
    v = location_rule("Remote - EMEA", POLICY)
    assert v.result == "REJECT" and v.code == "location_remote_scope"

def test_unknown_location_is_unknown_not_pass():
    v = location_rule("", POLICY)
    assert v.result == "UNKNOWN"   # never silently PASS (the 55-parked-jobs bug)

def test_description_carveout_downgrades_to_unknown():
    v = location_rule("Remote (US)", POLICY, description="Not available to residents of CA or NY.")
    assert v.result == "UNKNOWN" and "carveout" in v.code
```

- [ ] **Step 2: Run — expect failure** (module missing)

Run: `& $PY -m pytest tests/test_gate_location.py -v`

- [ ] **Step 3: Implement the verdict type**

`src/applypilot/gate/__init__.py`:

```python
"""The one gate engine — single authority for job eligibility.
Every rule returns PASS / REJECT(code, evidence) / UNKNOWN(code). UNKNOWN
NEVER auto-passes. Jobs are always stored; only eligible+automatable rows
become queue-visible (see database.queue_policy)."""
from __future__ import annotations

from dataclasses import dataclass

GATE_VERSION = 1


@dataclass(frozen=True)
class Verdict:
    result: str            # "PASS" | "REJECT" | "UNKNOWN"
    code: str              # machine-readable reason, e.g. "location_remote_scope"
    evidence: str = ""     # verbatim quote supporting the verdict

    @property
    def is_reject(self) -> bool:
        return self.result == "REJECT"
```

`src/applypilot/gate/gazetteer.py`:

```python
"""Bundled geo lexicon. Word-boundary matching only — the 'ca' in
'appliCAtions' bug is banned by construction (a lint test asserts every
pattern here is \\b-anchored)."""
from __future__ import annotations

import re

US_STATES = {  # full names → USPS code
    "california": "ca", "washington": "wa", "new york": "ny", "texas": "tx",
    "massachusetts": "ma", "illinois": "il", "colorado": "co", "oregon": "or",
    # ... (ship all 50; abbreviated here for the plan — the engineer fills the full set)
}
US_METROS = {
    "san francisco": "us-ca", "san jose": "us-ca", "palo alto": "us-ca",
    "seattle": "us-wa-seattle", "new york": "us-ny-nyc", "new york city": "us-ny-nyc",
    "los angeles": "us-ca", "austin": "us-tx", "boston": "us-ma",
    # ... (ship the ~40 from searches.yaml location_accept)
}
NON_US_MARKERS = {  # substring-safe because these are distinctive tokens
    "united kingdom", "london", "emea", "apac", "latam", "canada", "toronto",
    "india", "bangalore", "bengaluru", "singapore", "germany", "berlin",
    # ... (ship the full set from searches.yaml location_reject_non_remote)
}
REMOTE_RE = re.compile(r"\bremote\b", re.I)
US_COUNTRY_RE = re.compile(r"\b(united states|u\.?s\.?a?|usa)\b", re.I)


def word_match(needle: str, haystack: str) -> bool:
    """True if `needle` appears in `haystack` as a whole word/phrase."""
    return re.search(rf"\b{re.escape(needle)}\b", haystack, re.I) is not None
```

`src/applypilot/gate/rules.py` (location rule; other rules added in later tasks):

```python
"""Deterministic eligibility rules. Each: (value, profile_policy, **ctx) -> Verdict."""
from __future__ import annotations

import re

from . import Verdict
from . import gazetteer as gz

_CARVEOUT_RE = re.compile(
    r"(not (available|eligible)|residents of|excluding|must reside|"
    r"overlap .* (\d+ )?(hours?|time ?zone)|must overlap|not .* residents|"
    r"itar|u\.?s\.? person|us citizens? only|must be a u\.?s\.? citizen)", re.I)


def location_rule(location: str, policy: dict, *, description: str = "",
                  is_title_field: bool = False) -> Verdict:
    loc = (location or "").strip()
    # description-level carve-outs override a clean location field
    if description and _CARVEOUT_RE.search(description):
        m = _CARVEOUT_RE.search(description)
        return Verdict("UNKNOWN", "location_body_carveout", description[max(0, m.start()-20):m.end()+20])
    if not loc:
        return Verdict("UNKNOWN", "location_missing")
    low = loc.lower()
    is_remote = gz.REMOTE_RE.search(low) is not None
    # non-US markers → reject unless a US marker also present
    non_us = any(gz.word_match(m, low) for m in gz.NON_US_MARKERS)
    us = gz.US_COUNTRY_RE.search(low) is not None or \
        any(gz.word_match(s, low) for s in gz.US_STATES) or \
        any(gz.word_match(m, low) for m in gz.US_METROS)
    if is_remote:
        if policy.get("remote_ok") and (us or not non_us) and not (non_us and not us):
            if non_us and not us:
                return Verdict("REJECT", "location_remote_scope", loc)
            return Verdict("PASS", "location_ok", loc)
        if non_us and not us:
            return Verdict("REJECT", "location_remote_scope", loc)
        return Verdict("PASS", "location_ok", loc) if policy.get("remote_ok") else \
            Verdict("REJECT", "location_remote_not_ok", loc)
    # onsite: must be in an allowed metro/region
    for metro, region in gz.US_METROS.items():
        if gz.word_match(metro, low):
            return Verdict("PASS", "location_ok", loc) if region in policy.get("onsite_regions", []) \
                else Verdict("REJECT", "location_onsite_out_of_region", loc)
    if non_us:
        return Verdict("REJECT", "location_onsite_non_us", loc)
    return Verdict("UNKNOWN", "location_unresolved", loc)
```

(The engineer fills the elided US_STATES/US_METROS/NON_US_MARKERS from `searches.yaml` `location_accept`/`location_reject_non_remote` — all 50 states, ~40 metros, ~120 non-US markers. Copy the values verbatim; they are already curated.)

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_gate_location.py -v`
Expected: ALL PASS. If `test_ca_substring_bug_is_dead` or the metro tests fail, the elided gazetteer sets are incomplete — fill them.

- [ ] **Step 5: Add the word-boundary lint test**

```python
# tests/test_gate_location.py — append
import re
from applypilot.gate import gazetteer as gz

def test_no_substring_patterns_leak():
    # every US state code is 2 chars; ensure we never match them as substrings
    assert not gz.word_match("ca", "applications")
    assert not gz.word_match("or", "coordinator")
```

Run: `& $PY -m pytest tests/test_gate_location.py -v` → PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/gate tests/test_gate_location.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "gate: verdict schema + gazetteer + word-boundary location rule (kills the 'ca'/appliCAtions bug)"
```

---

### Task 4: Seniority/track + sponsorship + automatability rules

**Files:**
- Modify: `src/applypilot/gate/rules.py`
- Create: `tests/test_gate_rules.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_gate_rules.py
from applypilot.gate.rules import seniority_rule, sponsorship_rule, automatability_rule

SENIORITY = {"accept_bands": ["mid", "senior"], "ic_only": True}

def test_manager_rejected_for_ic():
    v = seniority_rule("Design Manager", SENIORITY)
    assert v.result == "REJECT" and v.code == "seniority_management_track"

def test_lead_is_ic_accepted():
    assert seniority_rule("Lead Product Designer", SENIORITY).result == "PASS"

def test_intern_rejected():
    v = seniority_rule("Product Design Intern", SENIORITY)
    assert v.result == "REJECT" and v.code == "seniority_early_career"

def test_senior_ic_accepted():
    assert seniority_rule("Senior Product Designer", SENIORITY).result == "PASS"

def test_sponsorship_hard_marker_rejects():
    v = sponsorship_rule("Must be authorized to work in the US without sponsorship.",
                         needs_sponsorship=True)
    assert v.result == "REJECT" and v.code == "sponsorship_blocked"
    assert "without sponsorship" in v.evidence.lower()

def test_sponsorship_silent_is_unknown_for_visa_user():
    v = sponsorship_rule("Great team, competitive pay.", needs_sponsorship=True)
    assert v.result == "UNKNOWN" and v.code == "sponsorship_unknown"

def test_sponsorship_irrelevant_when_not_needed():
    assert sponsorship_rule("US citizenship required.", needs_sponsorship=False).result == "PASS"

def test_workday_without_account_parks():
    v = automatability_rule("https://adobe.wd5.myworkdayjobs.com/x/job/y_R1", workday_accounts=[])
    assert v.result == "REJECT" and v.code == "account_required"

def test_workday_with_account_ok():
    v = automatability_rule("https://adobe.wd5.myworkdayjobs.com/x/job/y_R1", workday_accounts=["adobe"])
    assert v.result == "PASS"

def test_manual_ats_rejected():
    v = automatability_rule("https://www.linkedin.com/jobs/view/123", workday_accounts=[])
    assert v.result == "REJECT" and v.code == "manual_ats"

def test_supported_ats_ok():
    assert automatability_rule("https://boards.greenhouse.io/chime/jobs/1", workday_accounts=[]).result == "PASS"
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_gate_rules.py -v`

- [ ] **Step 3: Implement in `src/applypilot/gate/rules.py`**

Append:

```python
from applypilot.identity import parse_ats_url

_MGMT_RE = re.compile(r"\b(manager|director|head of|vp|vice president|chief|"
                      r"people manager|hiring manager)\b", re.I)
_MGMT_TITLE_RE = re.compile(r"\bmanager\b|,\s*manager\b", re.I)
_EARLY_RE = re.compile(r"\b(intern|internship|apprentice|apprenticeship|"
                       r"fellow|fellowship|new[ -]?grad|new[ -]?graduate|co[ -]?op)\b", re.I)
_LEAD_IC_RE = re.compile(r"\blead\b", re.I)  # "Lead" is IC, not management

_SPONSOR_BLOCK_RE = re.compile(
    r"(without sponsorship|no (visa )?sponsorship|(cannot|unable to|do not|"
    r"does not) sponsor|must be (a )?(us|u\.s\.) citizen|us citizenship required|"
    r"security clearance|requires? .* clearance|no .* sponsorship|"
    r"not able to sponsor|opt/cpt not)", re.I)

_MANUAL_ATS_RE = re.compile(
    r"(linkedin\.com/jobs|indeed\.com|glassdoor\.com|ziprecruiter\.com|ibegin\.tcs)", re.I)


def seniority_rule(title: str, policy: dict) -> Verdict:
    t = (title or "").strip()
    if _EARLY_RE.search(t):
        m = _EARLY_RE.search(t)
        return Verdict("REJECT", "seniority_early_career", m.group(0))
    if policy.get("ic_only"):
        # "Lead X" is IC; "Manager"/"Director"/etc. is management
        if _MGMT_RE.search(t) and not (_LEAD_IC_RE.search(t) and not _MGMT_TITLE_RE.search(t)):
            m = _MGMT_RE.search(t)
            return Verdict("REJECT", "seniority_management_track", m.group(0))
    return Verdict("PASS", "seniority_ok", t)


def sponsorship_rule(description: str, *, needs_sponsorship: bool) -> Verdict:
    if not needs_sponsorship:
        return Verdict("PASS", "sponsorship_not_applicable")
    m = _SPONSOR_BLOCK_RE.search(description or "")
    if m:
        start = max(0, m.start() - 30)
        return Verdict("REJECT", "sponsorship_blocked", (description or "")[start:m.end() + 30])
    return Verdict("UNKNOWN", "sponsorship_unknown")


# NOTE (spec §5.2 rule 3): the H-1B LCA company-level sponsorship prior — an
# offline dataset that biases sponsorship ranking by a company's historical
# filing behavior — is DEFERRED to Phase 4 (it improves ranking, not the hard
# gate). v1 sponsorship logic is the hard-marker regex above + sponsorship_unknown
# → review. Do not implement the LCA prior in this phase.


def automatability_rule(url: str, *, workday_accounts: list[str]) -> Verdict:
    u = url or ""
    if _MANUAL_ATS_RE.search(u):
        return Verdict("REJECT", "manual_ats", u)
    ref = parse_ats_url(u)
    if ref is None:
        return Verdict("UNKNOWN", "automatability_unknown", u)
    if ref.ats == "workday":
        if ref.token.lower() in {a.lower() for a in (workday_accounts or [])}:
            return Verdict("PASS", "automatable_workday")
        return Verdict("REJECT", "account_required", ref.token)
    if ref.ats in {"greenhouse", "lever", "ashby"}:
        return Verdict("PASS", "automatable_supported")
    return Verdict("UNKNOWN", "automatability_unknown", u)
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_gate_rules.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```powershell
git reset
git add src/applypilot/gate/rules.py tests/test_gate_rules.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "gate: seniority (IC/mgmt), sponsorship (hard-marker + unknown), automatability rules"
```

---

### Task 5: Gate engine — compose rules, persist verdict, gate_version

**Files:**
- Create: `src/applypilot/gate/engine.py`
- Modify: `src/applypilot/database.py` (`_ALL_COLUMNS` + `init_db` CREATE TABLE)
- Test: `tests/test_gate_engine.py`

- [ ] **Step 1: Add gate columns to the schema**

In `src/applypilot/database.py`, add to `_ALL_COLUMNS` (after the discovery section, with a header comment):

```python
    # Gate (v2 spine) — set at ingest; NULL = not yet gated.
    "identity_id": "TEXT",
    "ats": "TEXT",
    "board_token": "TEXT",
    "ats_job_id": "TEXT",
    "gate_result": "TEXT",       # "eligible" | "ineligible" | "unknown"
    "gate_reasons": "TEXT",      # JSON list of {rule, result, code, evidence}
    "automatability": "TEXT",    # "auto" | "account_required" | "manual" | "unknown"
    "gate_version": "INTEGER",
    "gated_at": "TEXT",
```

Add the identical column names+types inside the `CREATE TABLE IF NOT EXISTS jobs (...)` block in `init_db` (schema doc requires both places). Place them after the discovery columns.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_gate_engine.py
from applypilot.gate.engine import gate_job

PROFILE = {
    "geo": {"remote_ok": True, "remote_scope": "US", "onsite_regions": ["us-ca", "us-wa-seattle", "us-ny-nyc"]},
    "seniority": {"accept_bands": ["mid", "senior"], "ic_only": True},
    "needs_sponsorship": True,
    "workday_accounts": ["adobe"],
}

def test_eligible_greenhouse_remote_us():
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US",
                  "full_description": "Design systems work.",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/1"}, PROFILE)
    assert r["gate_result"] == "eligible"
    assert r["automatability"] == "auto"
    assert r["ats"] == "greenhouse" and r["identity_id"] == "greenhouse:chime:1"

def test_ineligible_manager_short_circuits():
    r = gate_job({"title": "Design Manager", "location": "Remote - US",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/2"}, PROFILE)
    assert r["gate_result"] == "ineligible"
    assert any(x["code"] == "seniority_management_track" for x in r["gate_reasons"])

def test_sponsorship_unknown_is_unknown_not_eligible():
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US",
                  "full_description": "Great pay.",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/3"}, PROFILE)
    assert r["gate_result"] == "unknown"  # visa user + no sponsorship signal → review, not auto

def test_workday_without_account_is_ineligible():
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US",
                  "full_description": "x", "application_url": "https://sap.wd3.myworkdayjobs.com/x/job/y_R1"}, PROFILE)
    assert r["automatability"] == "account_required"
    assert r["gate_result"] == "ineligible"

def test_gate_version_stamped():
    from applypilot.gate import GATE_VERSION
    r = gate_job({"title": "Senior Product Designer", "location": "Remote - US", "full_description": "x",
                  "application_url": "https://boards.greenhouse.io/chime/jobs/4"}, PROFILE)
    assert r["gate_version"] == GATE_VERSION and r["gated_at"]
```

- [ ] **Step 3: Run — expect failure** (module missing)

Run: `& $PY -m pytest tests/test_gate_engine.py -v`

- [ ] **Step 4: Implement `src/applypilot/gate/engine.py`**

```python
"""Compose the deterministic rules into one verdict per job. UNKNOWN never
becomes eligible. REJECT short-circuits (first failing rule wins the headline
but all evaluated rules are recorded). Run at ingest; re-runnable via
gate_version bump (see cli 'gate --rerun')."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import GATE_VERSION
from .rules import location_rule, seniority_rule, sponsorship_rule, automatability_rule
from applypilot.identity import identity_id, parse_ats_url


def gate_job(job: dict, profile: dict) -> dict:
    apply_url = job.get("application_url") or job.get("url") or ""
    title = job.get("title") or ""
    location = job.get("location") or ""
    desc = job.get("full_description") or job.get("description") or ""

    reasons = []
    verdicts = {
        "automatability": automatability_rule(apply_url, workday_accounts=profile.get("workday_accounts", [])),
        "location": location_rule(location, profile.get("geo", {}), description=desc),
        "seniority": seniority_rule(title, profile.get("seniority", {})),
        "sponsorship": sponsorship_rule(desc, needs_sponsorship=profile.get("needs_sponsorship", False)),
    }
    for rule_name, v in verdicts.items():
        reasons.append({"rule": rule_name, "result": v.result, "code": v.code, "evidence": v.evidence})

    if any(v.is_reject for v in verdicts.values()):
        result = "ineligible"
    elif any(v.result == "UNKNOWN" for v in verdicts.values()):
        result = "unknown"
    else:
        result = "eligible"

    automatability = {
        "manual_ats": "manual", "account_required": "account_required",
        "automatable_supported": "auto", "automatable_workday": "auto",
    }.get(verdicts["automatability"].code, "unknown")

    ref = parse_ats_url(apply_url)
    return {
        "identity_id": identity_id(apply_url, company=job.get("site"), title=title, location=location),
        "ats": ref.ats if ref else None,
        "board_token": ref.token if ref else None,
        "ats_job_id": ref.job_id if ref else None,
        "gate_result": result,
        "gate_reasons": reasons,          # caller json.dumps() before DB write
        "automatability": automatability,
        "gate_version": GATE_VERSION,
        "gated_at": datetime.now(timezone.utc).isoformat(),
    }
```

- [ ] **Step 5: Run — expect pass**

Run: `& $PY -m pytest tests/test_gate_engine.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/gate/engine.py src/applypilot/database.py tests/test_gate_engine.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "gate: engine composes rules into one verdict; add gate columns to schema"
```

---

### Task 6: Wire gate-at-ingest (all 5 sources) + the `gate` pipeline stage + `gate --rerun`

**Files:**
- Create: `src/applypilot/gate/profile_map.py` (the one `gate_profile` helper — neutral module, no circular imports)
- Modify: `src/applypilot/database.py` (new `store_gated()` + `update_gate()` helpers + `pending_gate`/`pending_score` predicates)
- Modify: ALL FIVE discovery INSERT sites: `discovery/ats_boards.py:241-269`, `discovery/jobspy.py:183-195`, `discovery/workday.py:303-340`, `discovery/theirstack.py:~314`, `discovery/smartextract.py:~110` (gate before insert)
- Modify: `src/applypilot/pipeline.py` (STAGE_ORDER/_UPSTREAM/_STAGE_RUNNERS/_PENDING_SQL), `cli.py` (VALID_STAGES + `gate --rerun` command)
- Test: `tests/test_gate_ingest.py`

- [ ] **Step 0: Create the shared `gate_profile` mapper (fixes the arity/import blocker)**

The review caught that `_gate_profile` was called with 1 arg in one place and 2 in others, and imported across modules ambiguously. Define it ONCE in a neutral module so `ats_boards`, `jobspy`, `workday`, `theirstack`, `smartextract`, `pipeline`, and `cli` all import the same 2-arg function:

```python
# src/applypilot/gate/profile_map.py
"""Map profile.json + searches.yaml into the gate engine's policy shape.
One definition, imported everywhere the gate runs (no circular imports:
this module imports nothing from applypilot except stdlib)."""
from __future__ import annotations


def gate_profile(profile: dict, search_cfg: dict | None = None) -> dict:
    p = profile or {}
    wa = p.get("work_authorization", {}) or {}
    exp = p.get("experience", {}) or {}
    search_cfg = search_cfg or {}
    band = (exp.get("target_role_band") or "senior").lower()
    return {
        "geo": {
            "remote_ok": True,
            "remote_scope": "US",
            # onsite_regions come from the compiled profile in Phase 4; v1 seeds
            # from the operator's known targets. Keep in sync with searches.yaml.
            "onsite_regions": p.get("geo_onsite_regions") or ["us-ca", "us-wa-seattle", "us-ny-nyc"],
        },
        "seniority": {
            "accept_bands": p.get("accept_bands") or [band, "mid"],
            "ic_only": bool(p.get("ic_only", True)),
        },
        "needs_sponsorship": bool(wa.get("require_sponsorship", False)),
        "workday_accounts": search_cfg.get("workday_accounts", []) or [],
    }
```

Import as `from applypilot.gate.profile_map import gate_profile` at every call site. **Use the name `gate_profile` (not `_gate_profile`) everywhere** — the leading underscore in the original draft implied module-private, which broke the cross-module imports.

- [ ] **Step 1: Write failing integration test**

```python
# tests/test_gate_ingest.py
import json, sqlite3
from applypilot import database as db
from applypilot.gate.engine import gate_job

PROFILE = {"geo": {"remote_ok": True, "remote_scope": "US", "onsite_regions": ["us-ca"]},
           "seniority": {"accept_bands": ["senior"], "ic_only": True},
           "needs_sponsorship": False, "workday_accounts": []}

def test_store_gated_persists_verdict(tmp_path):
    conn = db.get_connection(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    job = {"url": "https://boards.greenhouse.io/chime/jobs/1", "title": "Senior Product Designer",
           "location": "Remote - US", "full_description": "design", "site": "Chime (greenhouse)",
           "application_url": "https://boards.greenhouse.io/chime/jobs/1"}
    g = gate_job(job, PROFILE)
    db.store_gated(conn, job, g, strategy="ats_api:greenhouse")
    row = dict(conn.execute("SELECT gate_result, identity_id, gate_reasons, automatability FROM jobs").fetchone())
    assert row["gate_result"] == "eligible"
    assert row["identity_id"] == "greenhouse:chime:1"
    assert json.loads(row["gate_reasons"])[0]["rule"]
    assert row["automatability"] == "auto"

def test_only_gated_jobs_are_pending_score(tmp_path):
    conn = db.get_connection(tmp_path / "t2.db")
    db.init_db(tmp_path / "t2.db")
    # an ungated legacy row must NOT be pending_score
    conn.execute("INSERT INTO jobs (url, full_description, fit_score) VALUES ('u1','desc',NULL)")
    conn.commit()
    pending = db.get_jobs_by_stage(conn, "pending_score", min_score=8)
    assert all(r.get("gated_at") for r in pending) or pending == []
```

- [ ] **Step 2: Run — expect failure** (`store_gated` missing)

Run: `& $PY -m pytest tests/test_gate_ingest.py -v`

- [ ] **Step 3: Add `store_gated()` to `database.py`**

```python
# Full column list stored/updated by BOTH store_gated and update_gate — keep in sync.
_GATE_COLS = ["identity_id", "ats", "board_token", "ats_job_id", "gate_result",
              "gate_reasons", "automatability", "gate_version", "gated_at"]


def _gate_values(gate: dict) -> list:
    import json as _json
    return [gate["identity_id"], gate["ats"], gate["board_token"], gate["ats_job_id"],
            gate["gate_result"], _json.dumps(gate["gate_reasons"]), gate["automatability"],
            gate["gate_version"], gate["gated_at"]]


def store_gated(conn, job: dict, gate: dict, *, strategy: str) -> bool:
    """Insert a discovered job with its gate verdict. Returns True if new.
    Mirrors store_jobs' INSERT+IntegrityError dedup on the url PK, adding the
    gate columns. Jobs are ALWAYS stored regardless of verdict (audit + re-gate).
    Preserves detail_error (workday/theirstack populate it)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    url = job.get("url")
    if not url:
        return False
    try:
        conn.execute(
            "INSERT INTO jobs (url, title, salary, description, full_description, "
            "application_url, location, site, strategy, discovered_at, detail_scraped_at, "
            "detail_error, identity_id, ats, board_token, ats_job_id, gate_result, "
            "gate_reasons, automatability, gate_version, gated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [url, job.get("title"), job.get("salary"), job.get("description"),
             job.get("full_description") or job.get("description"),
             job.get("application_url") or url, job.get("location"), job.get("site"),
             strategy, job.get("posted_at") or now, job.get("detail_scraped_at") or now,
             job.get("detail_error")] + _gate_values(gate),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def update_gate(conn, url: str, gate: dict) -> None:
    """Re-stamp an existing row's gate columns (used by the catch-up 'gate'
    stage and 'gate --rerun'). Does NOT touch discovery/enrichment columns."""
    conn.execute(
        f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in _GATE_COLS)} WHERE url = ?",
        _gate_values(gate) + [url],
    )
    conn.commit()
```

- [ ] **Step 4: Gate predicates — require the gate stamp, eligibility, AND that the gate reflects the enriched description**

In `database.py` `get_jobs_by_stage` conditions dict, change `pending_score` and add `pending_gate`:

```python
        # Ungated OR gated before enrichment finished (thin→full description transition):
        # the catch-up gate stage must re-gate so sponsorship/location verdicts use the full text.
        "pending_gate": (
            "gated_at IS NULL "
            "OR (detail_scraped_at IS NOT NULL AND gated_at IS NOT NULL AND detail_scraped_at > gated_at)"
        ),
        "pending_score": (
            "full_description IS NOT NULL AND fit_score IS NULL "
            "AND gated_at IS NOT NULL AND gate_result = 'eligible' "
            "AND (detail_scraped_at IS NULL OR gated_at >= detail_scraped_at)"
        ),
```

Why the `detail_scraped_at`/`gated_at` comparison: a `jobspy` row is gated at ingest on a thin snippet, then enriched later with a full description. The gate stage (between enrich and score) re-gates it so the sponsorship/location verdict reflects the real text; `pending_score` refuses rows whose gate predates their enrichment. `ats_boards` rows arrive with `full_description` already set and `detail_scraped_at == gated_at`, so they pass straight through. This ensures the scorer only ever sees eligible, freshly-gated jobs; ineligible jobs never cost a score.

- [ ] **Step 5: Wire ALL FIVE ingest sites**

Every discovery source must gate at ingest so no row is ever both ungated and unscored. In `discovery/ats_boards.py` (loop at 241-269), where `ats`/`company` are known, replace the raw `conn.execute("INSERT INTO jobs ...")` with:

```python
                job_row = {
                    "url": url, "title": j.get("title"), "salary": j.get("salary"),
                    "description": j.get("description"), "full_description": j.get("description"),
                    "application_url": url, "location": j.get("location"),
                    "site": site_label, "posted_at": j.get("posted_at"),
                }
                if _db.store_gated(conn, job_row, gate_job(job_row, _policy), strategy=strategy):
                    total_new += 1
                else:
                    total_dup += 1
```

At the TOP of each discovery function (once per run, hoisted above the loop), add:

```python
    from applypilot.gate.engine import gate_job
    from applypilot.gate.profile_map import gate_profile
    from applypilot import database as _db
    from applypilot.config import load_profile, load_search_config
    _policy = gate_profile(load_profile(), load_search_config())
```

Apply the same pattern at all five sites:
- `discovery/jobspy.py:183-195` — ATS unknown; the gate parses `apply_url`/`url` itself. Build `job_row` with `application_url=apply_url`, `full_description=full_description`.
- `discovery/workday.py:303-340` — **preserve the existing `url` construction block (lines 310-316) before building `job_row`**; carry `detail_error` and `detail_scraped_at` through into `job_row` (store_gated now persists both).
- `discovery/theirstack.py:~314` — theirstack sometimes stores `full_description=NULL`; that's fine (gate runs on the location field + whatever description exists; sponsorship → `sponsorship_unknown` → review). Carry `detail_error`/`detail_scraped_at`.
- `discovery/smartextract.py:~110` — same pattern.

(Wiring all five keeps the "one gate at ingest" invariant true; the `gate` pipeline stage is then only a catch-up/re-gate safety net, not the primary path for theirstack/smartextract.)

- [ ] **Step 6: Add the `gate` pipeline stage**

In `pipeline.py`: add `"gate"` to `STAGE_ORDER` between `enrich` and `score`; add `STAGE_META["gate"] = {"desc": "Eligibility gate (location/visa/seniority/automatability)"}`; set `_UPSTREAM["gate"] = "enrich"` and `_UPSTREAM["score"] = "gate"`; add `_run_gate` to `_STAGE_RUNNERS` (re-gates any `pending_gate` rows — ungated OR gated-before-enrichment); add `_PENDING_SQL["gate"] = "SELECT COUNT(*) FROM jobs WHERE gated_at IS NULL OR (detail_scraped_at IS NOT NULL AND gated_at IS NOT NULL AND detail_scraped_at > gated_at)"` (must match the `pending_gate` predicate from Step 4). In `cli.py`, add `"gate"` to `VALID_STAGES`.

`_run_gate` implementation (mirror `_run_score` structure):

```python
def _run_gate(min_score=None, **kwargs):
    from applypilot import database as db
    from applypilot.gate.engine import gate_job
    from applypilot.gate.profile_map import gate_profile
    from applypilot.config import load_profile, load_search_config
    conn = db.get_connection()
    policy = gate_profile(load_profile(), load_search_config())
    rows = db.get_jobs_by_stage(conn, "pending_gate")
    n = 0
    for row in rows:
        db.update_gate(conn, row["url"], gate_job(dict(row), policy))
        n += 1
    return {"status": "ok", "gated": n}
```

(`database.update_gate` was added alongside `store_gated` in Step 3.)

- [ ] **Step 7: Add `applypilot gate --rerun` command**

In `cli.py`:

```python
@app.command("gate")
def gate_cmd(
    rerun: bool = typer.Option(False, "--rerun", help="Re-gate all rows whose gate_version is stale."),
) -> None:
    """Run (or re-run) the eligibility gate over stored jobs."""
    _bootstrap()
    from applypilot import database as db
    from applypilot.gate import GATE_VERSION
    from applypilot.gate.engine import gate_job
    from applypilot.gate.profile_map import gate_profile
    from applypilot.config import load_profile, load_search_config
    conn = db.get_connection()
    policy = gate_profile(load_profile(), load_search_config())
    if rerun:
        rows = conn.execute("SELECT * FROM jobs WHERE gate_version IS NULL OR gate_version < ?",
                            (GATE_VERSION,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs WHERE gated_at IS NULL AND full_description IS NOT NULL").fetchall()
    n = 0
    for row in rows:
        db.update_gate(conn, row["url"], gate_job(dict(row), policy))
        n += 1
    console.print(f"Gated [bold]{n}[/bold] jobs at version {GATE_VERSION}.")
```

- [ ] **Step 8: Run all gate tests + full suite**

Run: `& $PY -m pytest tests/test_gate_ingest.py tests/test_gate_engine.py tests/test_gate_rules.py tests/test_gate_location.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -q`
Expected: no regressions.

- [ ] **Step 9: Commit**

```powershell
git reset
git add src/applypilot/database.py src/applypilot/gate src/applypilot/discovery src/applypilot/pipeline.py src/applypilot/cli.py tests/test_gate_ingest.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "gate: wire at-ingest + 'gate' pipeline stage + 'gate --rerun'; scorer only sees eligible rows"
```

---

## Group C — Canary hardening

### Task 7: Canary classifier + deterministic resolver

**Files:**
- Create: `src/applypilot/apply/canary.py`
- Test: `tests/test_canary.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_canary.py
from applypilot.apply.canary import is_canary, resolve_canary

PROFILE = {
    "work_authorization": {"legally_authorized_to_work": False, "require_sponsorship": True,
                           "work_permit_type": "F-1 OPT"},
    "compensation": {"salary_expectation": "150000", "salary_currency": "USD"},
    "eeo_voluntary": {"gender": "Female", "race_ethnicity": "Decline to self-identify",
                      "veteran_status": "I am not a veteran", "disability_status": "No"},
    "personal": {"address": "1 Main St", "city": "San Jose", "province_state": "CA",
                 "country": "USA", "postal_code": "95110", "password": "SECRET"},
    "availability": {"earliest_start_date": "2 weeks"},
}

def test_sponsorship_is_canary():
    assert is_canary("Will you now or in the future require visa sponsorship?")
    assert is_canary("Are you legally authorized to work in the US?")
    assert is_canary("What is your expected salary?")
    assert is_canary("What is your gender?")
    assert is_canary("What is your date of birth?")

def test_non_canary():
    assert not is_canary("Describe a product you shipped that you're proud of")
    assert not is_canary("Why do you want to work here?")

def test_resolve_sponsorship_polarity_positive():
    # require_sponsorship=True → "require sponsorship?" answers Yes
    assert resolve_canary("Will you require sponsorship?", PROFILE) in ("Yes", "yes")

def test_resolve_sponsorship_polarity_negated():
    # "work WITHOUT sponsorship?" with require_sponsorship=True → No
    assert resolve_canary("Can you work without sponsorship?", PROFILE) in ("No", "no")

def test_resolve_salary():
    assert "150000" in (resolve_canary("What is your expected salary?", PROFILE) or "")

def test_resolve_dob_is_none():
    # profile has NO date-of-birth field → never guess
    assert resolve_canary("What is your date of birth?", PROFILE) is None

def test_resolve_never_leaks_password():
    for q in ("What is your address?", "What is your password?"):
        ans = resolve_canary(q, PROFILE) or ""
        assert "SECRET" not in ans

def test_ambiguous_polarity_returns_none():
    # if we can't confidently determine polarity, refuse (park, don't guess)
    assert resolve_canary("Sponsorship?", PROFILE) is None
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_canary.py -v`

- [ ] **Step 3: Implement `src/applypilot/apply/canary.py`**

```python
"""Canary classes — work-auth, sponsorship, citizenship/legal, compensation,
EEO, address, DOB — resolve ONLY from exact profile paths, with explicit
polarity handling. Never fuzzy-matched, never LLM-answered, never cached.
An unresolvable canary returns None so the caller keeps the field UNRESOLVED
(which blocks deterministic auto-submit — the interlock)."""
from __future__ import annotations

import re

# marker → detection regex (word-boundary)
_MARKERS = {
    "workauth": re.compile(r"\b(authoriz\w+|eligible to work|legally able|work permit)\b", re.I),
    "sponsorship": re.compile(r"\b(sponsor\w*|visa)\b", re.I),
    "citizenship": re.compile(r"\b(citizen\w*|us person|green card|permanent resident)\b", re.I),
    "salary": re.compile(r"\b(salary|compensation|pay expectation|expected (pay|comp))\b", re.I),
    "eeo_gender": re.compile(r"\bgender\b", re.I),
    "eeo_race": re.compile(r"\b(race|ethnicit\w+)\b", re.I),
    "eeo_veteran": re.compile(r"\bveteran\b", re.I),
    "eeo_disability": re.compile(r"\bdisabilit\w+\b", re.I),
    "address": re.compile(r"\b(street address|mailing address|home address|zip|postal code|address)\b", re.I),
    "dob": re.compile(r"\b(date of birth|birth ?date|dob)\b", re.I),
    "clearance": re.compile(r"\b(security clearance|clearance)\b", re.I),
}

_NEGATION_RE = re.compile(r"\b(without|not require|don'?t require|do not require|no need)\b", re.I)


def is_canary(question: str) -> bool:
    q = question or ""
    return any(rx.search(q) for rx in _MARKERS.values())


def _yn(flag: bool) -> str:
    return "Yes" if flag else "No"


def resolve_canary(question: str, profile: dict) -> str | None:
    """Deterministic answer from exact profile paths, or None (→ stay unresolved)."""
    q = question or ""
    wa = profile.get("work_authorization", {}) or {}
    comp = profile.get("compensation", {}) or {}
    eeo = profile.get("eeo_voluntary", {}) or {}
    per = profile.get("personal", {}) or {}
    avail = profile.get("availability", {}) or {}

    # sponsorship / workauth: polarity-sensitive
    if _MARKERS["sponsorship"].search(q) or _MARKERS["workauth"].search(q) or _MARKERS["citizenship"].search(q):
        requires = bool(wa.get("require_sponsorship"))
        authorized = bool(wa.get("legally_authorized_to_work"))
        negated = _NEGATION_RE.search(q) is not None
        # "require sponsorship?" → Yes iff requires; "work WITHOUT sponsorship?" → No iff requires
        if _MARKERS["sponsorship"].search(q):
            # need enough context to know polarity; a bare "Sponsorship?" is ambiguous
            if not re.search(r"\b(require|need|without|now or in the future|will you)\b", q, re.I):
                return None
            return _yn(requires) if not negated else _yn(not requires)
        if _MARKERS["workauth"].search(q):
            return _yn(authorized) if not negated else _yn(not authorized)
        if _MARKERS["citizenship"].search(q):
            # citizenship is not sponsorship; only answer if profile states it — it does not → refuse
            return None

    if _MARKERS["salary"].search(q):
        val = comp.get("salary_expectation")
        cur = comp.get("salary_currency", "")
        return f"{val} {cur}".strip() if val else None

    if _MARKERS["eeo_gender"].search(q):
        return eeo.get("gender") or None
    if _MARKERS["eeo_race"].search(q):
        return eeo.get("race_ethnicity") or None
    if _MARKERS["eeo_veteran"].search(q):
        return eeo.get("veteran_status") or None
    if _MARKERS["eeo_disability"].search(q):
        return eeo.get("disability_status") or None

    if _MARKERS["dob"].search(q):
        return None  # no DOB in profile — never guess

    if _MARKERS["address"].search(q):
        parts = [per.get("address"), per.get("city"), per.get("province_state"),
                 per.get("postal_code"), per.get("country")]
        joined = ", ".join(p for p in parts if p)
        return joined or None

    if _MARKERS["clearance"].search(q):
        return None  # not in profile — refuse

    return None
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_canary.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```powershell
git reset
git add src/applypilot/apply/canary.py tests/test_canary.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "canary: classifier + polarity-aware deterministic resolver (exact profile paths only)"
```

---

### Task 8: Harden answer_cache + gate its greenhouse consumer with canary

**Files:**
- Modify: `src/applypilot/apply/answer_cache.py` (`_nearest` skip, `answer` no-cache, load-time scrub)
- Modify: `src/applypilot/apply/adapters/greenhouse.py:514-536` (canary-first in the free-text flow)
- Test: `tests/test_answer_cache_canary.py` (new) + `tests/test_answer_cache.py` (UPDATE existing seed-canary tests — see Step 4b)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_answer_cache_canary.py
import json
from applypilot.apply.answer_cache import AnswerCache

PROFILE = {"work_authorization": {"legally_authorized_to_work": False, "require_sponsorship": True}}

def test_canary_question_never_marker_force_hit():
    ac = AnswerCache(PROFILE)
    # seed bank answers "require sponsorship?" = Yes; a polarity-flipped question must NOT force-hit it
    r = ac.answer("Are you able to work WITHOUT sponsorship?",
                  llm_fn=lambda q, c: (_ for _ in ()).throw(AssertionError("LLM must not be called for canary")))
    # canary → resolver or unresolved, never the fuzzy seed
    assert r.answer in ("No", None, "")  # correct polarity or refusal, never seeded "Yes"

def test_canary_answer_not_persisted(tmp_path):
    bank = tmp_path / "bank.json"
    ac = AnswerCache(PROFILE, bank_path=bank)
    ac.answer("What is your expected salary?", llm_fn=lambda q, c: "should-not-persist")
    saved = json.loads(bank.read_text()) if bank.exists() else []
    assert all("salary" not in e.get("q", "").lower() for e in saved)

def test_poisoned_bank_entry_scrubbed_on_load(tmp_path):
    bank = tmp_path / "bank.json"
    bank.write_text(json.dumps([{"q": "Will you require sponsorship?", "a": "No"}]))  # WRONG for this profile
    ac = AnswerCache(PROFILE, bank_path=bank)
    # the poisoned sponsorship entry must not survive into the live cache
    r = ac.answer("Will you require sponsorship?", llm_fn=lambda q, c: "unused")
    assert r.answer != "No"  # not served from the poisoned bank

def test_non_canary_still_caches(tmp_path):
    bank = tmp_path / "bank.json"
    ac = AnswerCache(PROFILE, bank_path=bank)
    ac.answer("Why are you interested in this role?", llm_fn=lambda q, c: "I love the mission.")
    assert any("interested" in e.get("q", "").lower() for e in json.loads(bank.read_text()))
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_answer_cache_canary.py -v`

- [ ] **Step 3: Harden `answer_cache.py`**

At the top, import the canary API:

```python
from applypilot.apply.canary import is_canary, resolve_canary
```

In `__init__` (after loading the bank, before `for e in self._entries: e["_vec"] = ...`), scrub poisoned canary entries:

```python
        # Canary classes must never be served from the bank — drop any that leaked in.
        self._entries = [e for e in self._entries if not is_canary(e.get("q", ""))]
```

In `answer()`, at the very top (before `self._nearest`):

```python
        if is_canary(question):
            det = resolve_canary(question, self.profile)
            # never cache, never persist, never LLM for canary classes
            return AnswerResult(det if det is not None else "", "profile" if det else "unresolved",
                                1.0 if det else 0.0, False, None)
```

In `_nearest()`, guard the marker-channel shortcut so it can never fire for canary questions (defense in depth — note this path is unreachable for canary via the public `answer()` guard above; it only matters if `_nearest` is ever called directly, so keep it as a belt-and-suspenders):

```python
            if qmk and e.get("_mk") == qmk and s >= 0.30 and s > mk_best_s and not is_canary(q):
                mk_best, mk_best_s = e, s
```

- [ ] **Step 3b: Update the existing seed-canary tests in `tests/test_answer_cache.py`**

The new canary top-guard + load-time scrub change behavior that two existing passing tests assert. Update them (do NOT leave them asserting the old seed-hit path, or Step 5's full-suite run fails):

- `test_seeded_question_zero_llm` (currently asserts `r.source == "seed"` for "Are you legally authorized to work in the US?"): this question is now canary → change the assertion to `r.source == "profile"` and `r.llm_called is False` (answered by `resolve_canary`, not the seed).
- `test_seeded_paraphrase_still_hits` (depends on seed sponsorship/workauth entries the `__init__` scrub now deletes): retarget it to a NON-canary seed (e.g. "Are you willing to relocate?" / "Have you previously worked here?") so it still exercises the paraphrase-hit path, OR assert the canary resolver answers the workauth paraphrase deterministically. Pick whichever keeps the test's intent; document the choice in a one-line comment.

Read `tests/test_answer_cache.py` first and adjust exactly these two; leave all other tests unchanged.

- [ ] **Step 4: Make the greenhouse free-text flow canary-first**

In `adapters/greenhouse.py:514-536`, before `ans = answer_cache.answer(lab, context=ctx).answer`, classify the label:

```python
            from applypilot.apply.canary import is_canary, resolve_canary
            if is_canary(lab):
                det = resolve_canary(lab, profile)
                if det:
                    loc.fill(det, timeout=interaction_ms)
                    res.fields_filled.append(f"canary:{lab[:24]}")
                else:
                    still.append(u)   # unresolvable canary stays UNRESOLVED → blocks auto-submit
                continue
            ans = answer_cache.answer(lab, context=ctx).answer
```

This is the submit interlock: an unresolvable canary keeps `res.unresolved` non-empty, so the real `do_submit` gate — `do_submit = (submit is True) or (submit == "auto" and not res.unresolved and bool(res.fields_filled))` (greenhouse.py:553-554) — cannot fire on the `"auto"` path, and the job falls through to the LLM/review path instead of live-submitting a guessed legal attestation.

- [ ] **Step 5: Run canary + adapter + existing answer-cache tests + full suite**

Run: `& $PY -m pytest tests/test_answer_cache_canary.py tests/test_answer_cache.py tests/test_greenhouse_adapter.py -v`
Expected: ALL PASS (including the two seed-canary tests updated in Step 3b).
Run: `& $PY -m pytest tests/ -q`
Expected: no regressions.

- [ ] **Step 6: Commit**

```powershell
git reset
git add src/applypilot/apply/answer_cache.py src/applypilot/apply/adapters/greenhouse.py tests/test_answer_cache_canary.py tests/test_answer_cache.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "canary: harden answer_cache (no fuzzy/no cache/scrub-on-load) + canary-first greenhouse free-text"
```

---

## Group D — Submit broker + network containment

### Task 9: File-based one-shot submit broker

**Files:**
- Create: `src/applypilot/apply/submit_broker.py`
- Test: `tests/test_submit_broker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_submit_broker.py
from applypilot.apply.submit_broker import SubmitBroker

def test_no_ticket_means_no_submit(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json")
    assert b.ticket_open("greenhouse:chime:1") is False

def test_issue_then_open(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json")
    b.issue("greenhouse:chime:1")
    assert b.ticket_open("greenhouse:chime:1") is True
    assert b.ticket_open("greenhouse:other:2") is False   # scoped to identity

def test_consume_is_one_shot(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json")
    b.issue("greenhouse:chime:1")
    assert b.consume("greenhouse:chime:1") is True
    assert b.ticket_open("greenhouse:chime:1") is False   # consumed
    assert b.consume("greenhouse:chime:1") is False       # second submit blocked

def test_ttl_expiry(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json", ttl_s=0)
    b.issue("greenhouse:chime:1")
    assert b.ticket_open("greenhouse:chime:1") is False   # already expired

def test_dry_run_broker_never_opens(tmp_path):
    b = SubmitBroker(tmp_path / "ticket.json", dry_run=True)
    b.issue("greenhouse:chime:1")
    assert b.ticket_open("greenhouse:chime:1") is False   # dry-run: structurally no submit

def test_survives_separate_process_read(tmp_path):
    # the stream MCP server is a separate process; a broker constructed on the same
    # file must see the issued ticket
    p = tmp_path / "ticket.json"
    SubmitBroker(p).issue("greenhouse:chime:1")
    assert SubmitBroker(p).ticket_open("greenhouse:chime:1") is True
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_submit_broker.py -v`

- [ ] **Step 3: Implement `src/applypilot/apply/submit_broker.py`**

```python
"""One-shot submit tickets, file-backed so the separate stream-MCP process
sees the same state. A submission may proceed ONLY while an unconsumed,
unexpired ticket exists for the current job identity. Dry-run never opens a
ticket — submit is structurally impossible, not prompt-forbidden."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class SubmitBroker:
    def __init__(self, ticket_path: str | Path, *, ttl_s: float = 900.0, dry_run: bool = False):
        self.path = Path(ticket_path)
        self.ttl_s = ttl_s
        self.dry_run = dry_run

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write(self, data: dict | None) -> None:
        if data is None:
            self.path.unlink(missing_ok=True)
            return
        # atomic write
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def issue(self, identity_id: str) -> None:
        if self.dry_run:
            return  # never issue in dry-run
        self._write({"identity_id": identity_id, "issued_at": time.time(), "consumed": False})

    def ticket_open(self, identity_id: str) -> bool:
        if self.dry_run:
            return False
        t = self._read()
        if not t or t.get("consumed") or t.get("identity_id") != identity_id:
            return False
        return (time.time() - t.get("issued_at", 0)) <= self.ttl_s

    def consume(self, identity_id: str) -> bool:
        if not self.ticket_open(identity_id):
            return False
        t = self._read()
        t["consumed"] = True
        self._write(t)
        return True
```

- [ ] **Step 4: Run — expect pass**

Run: `& $PY -m pytest tests/test_submit_broker.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```powershell
git reset
git add src/applypilot/apply/submit_broker.py tests/test_submit_broker.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "safety: file-based one-shot SubmitBroker (identity-scoped, TTL, dry-run never opens)"
```

---

### Task 10: CDP network containment + broker wiring at the submit hooks

**Files:**
- Modify: `src/applypilot/apply/browser_stream.py:604-620` (install `context.route` on the session-lifetime connection)
- Modify: `src/applypilot/apply/stream_executor.py:220-226` and `effective_allow_submit` (broker check)
- Modify: `src/applypilot/apply/launcher.py` (issue ticket after dedup guards; pass broker to adapter pass + browser stream; consume on adapter submit)
- Test: `tests/test_network_containment.py`, extend `tests/test_dry_run_submit_guard.py`

- [ ] **Step 1: Write a failing unit test for the route policy decision**

The route handler's decision is pure and must be unit-testable without a browser. Extract it:

```python
# tests/test_network_containment.py
from applypilot.apply.browser_stream import should_block_request

# (method, url, ticket_open, dry_run) -> blocked?
def test_blocks_post_to_ats_without_ticket():
    assert should_block_request("POST", "https://boards.greenhouse.io/chime/jobs/1", ticket_open=False, dry_run=False) is True

def test_allows_post_with_open_ticket():
    assert should_block_request("POST", "https://boards.greenhouse.io/chime/jobs/1", ticket_open=True, dry_run=False) is False

def test_dry_run_blocks_even_with_ticket():
    assert should_block_request("POST", "https://jobs.lever.co/x/y/apply", ticket_open=True, dry_run=True) is True

def test_allows_get_always():
    assert should_block_request("GET", "https://boards.greenhouse.io/chime/jobs/1", ticket_open=False, dry_run=False) is False

def test_ignores_non_ats_hosts():
    assert should_block_request("POST", "https://analytics.example.com/track", ticket_open=False, dry_run=False) is False

def test_ashby_graphql_submit_blocked():
    assert should_block_request("POST", "https://jobs.ashbyhq.com/api/non-user-graphql", ticket_open=False, dry_run=False) is True

def test_dry_run_fails_closed_on_unknown_host():
    # §10.1: unknown/vanity submit-shaped endpoints fail closed in dry-run
    # (the Twilio incident was a vanity greenhouse embed on careers.twilio.com)
    assert should_block_request("POST", "https://careers.airbnb.com/positions/apply", ticket_open=True, dry_run=True) is True
    assert should_block_request("POST", "https://apply.someats.io/submit", ticket_open=True, dry_run=True) is True

def test_dry_run_allows_safe_analytics_host():
    # explicit safe-allowlist so dry-run doesn't break page assets/telemetry
    assert should_block_request("POST", "https://www.google-analytics.com/collect", ticket_open=True, dry_run=True) is False
```

- [ ] **Step 2: Run — expect failure** (`should_block_request` missing)

Run: `& $PY -m pytest tests/test_network_containment.py -v`

- [ ] **Step 3: Implement the pure policy + route installer in `browser_stream.py`**

Add near the top of `browser_stream.py`:

```python
import re as _re

_ATS_HOST_RE = _re.compile(r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com)$", _re.I)
_MUTATION_METHODS = {"POST", "PUT", "PATCH"}
# Hosts whose mutating requests are always safe (page assets, analytics, CDNs).
# Kept deliberately small; anything not here is subject to the dry-run fail-closed rule.
_SAFE_MUTATION_HOSTS = _re.compile(
    r"(google-analytics\.com|googletagmanager\.com|doubleclick\.net|"
    r"segment\.(io|com)|sentry\.io|datadoghq\.com|cloudflareinsights\.com|"
    r"fullstory\.com|hotjar\.com|fonts\.googleapis\.com|gstatic\.com)$", _re.I)


def should_block_request(method: str, url: str, *, ticket_open: bool, dry_run: bool) -> bool:
    """Pure route decision.
    LIVE: block mutating requests to known ATS hosts unless a submit ticket is open;
          leave everything else alone (minimal interference).
    DRY-RUN: fail closed — block EVERY mutating request except an explicit safe-host
          allowlist, so a vanity/embedded/unknown ATS submit (the Twilio incident class)
          physically cannot POST. GET/HEAD/OPTIONS always allowed."""
    from urllib.parse import urlparse
    if (method or "").upper() not in _MUTATION_METHODS:
        return False
    host = urlparse(url or "").netloc.lower()
    if _SAFE_MUTATION_HOSTS.search(host):
        return False
    if dry_run:
        return True   # fail closed: any non-safe mutation is blocked in dry-run
    # live: only gate known ATS submit hosts on the ticket; ignore the rest
    if not _ATS_HOST_RE.search(host):
        return False
    return not ticket_open
```

In `BrowserStateStream.__init__`, accept `broker=None`, `identity_id=None`, `dry_run=False`. In `_run()`, right after `browser = pw.chromium.connect_over_cdp(...)` (line ~612), install the route on every context and re-install on newly-appeared contexts each poll:

```python
            def _guard(route):
                req = route.request
                open_ = bool(self.broker and self.identity_id and self.broker.ticket_open(self.identity_id))
                if should_block_request(req.method, req.url, ticket_open=open_, dry_run=self.dry_run):
                    self._publish(BrowserObservation(observed_at=time.time(),
                                                     error=f"BLOCKED_SUBMIT {req.method} {req.url}"))
                    route.abort()
                else:
                    # consume the one-shot ticket on the actual submit POST
                    if open_ and should_block_request(req.method, req.url, ticket_open=False, dry_run=False):
                        self.broker.consume(self.identity_id)
                    route.continue_()
            _routed = set()
            def _ensure_routes():
                for ctx in browser.contexts:
                    if id(ctx) not in _routed:
                        try:
                            ctx.route("**/*", _guard)
                            _routed.add(id(ctx))
                        except Exception:
                            pass
            _ensure_routes()
```

Call `_ensure_routes()` once each iteration of the poll loop (covers contexts opened mid-session). **Fail-closed:** if route install raises in live mode, publish an error and set a flag the worker reads to downgrade the job to `needs_review` rather than proceed unguarded.

- [ ] **Step 4: Wire the broker into the stream submit gate (thread the params explicitly)**

`_execute_one`'s real signature is `_execute_one(page, raw, *, allow_submit, timeout_ms, tabs)` (stream_executor.py:204) — it has no broker/identity params today. Add `broker=None, identity_id=None` keyword params to `_execute_one`, and thread them from `execute_stream_actions_cdp` (63) → `_execute_stream` (131) → `_execute_one`. In `_execute_one` (lines 220-226):

```python
    if is_final_submit:
        if not allow_submit:
            return StreamActionResult(False, action, target, "submit_refused_allow_submit_false", control_id=control.control_id)
        if broker is not None and identity_id and not broker.ticket_open(identity_id):
            return StreamActionResult(False, action, target, "submit_refused_no_broker_ticket", control_id=control.control_id)
```

**MCP-server process note (separate process):** `stream_mcp_server` runs as its own process and does NOT know the current job's `identity_id`. So: `build_server`/`main` take a `--broker-file` AND a `--job-identity` arg (both wired like `--dry-run` in `_make_mcp_config`, launcher.py 108-143; the launcher writes the current `ident` into the per-worker MCP config). The server constructs `SubmitBroker(broker_file)` and calls `ticket_open(job_identity)`. Treat this MCP-path gate as secondary — **the primary, always-on containment is the `browser_stream` route (Step 3), which runs in the launcher process and already has `ident` and the live broker.**

- [ ] **Step 5: Issue/consume the ticket in launcher — broker owned by worker_loop, keyed by identity_id**

Two blockers the review caught: (a) the broker must be created in `worker_loop` (where `BrowserStateStream` is constructed, launcher.py 3167-3175), NOT inside `run_job` (a different function — a `run_job`-local broker is out of scope at the stream construction site). (b) It must be keyed by `identity_id` everywhere, NOT the sha256 `idempotency_key` — mixing them means `ticket_open` never matches and every submit deadlocks.

In `worker_loop`, compute the identity once and create the broker BEFORE constructing the stream:

```python
    from applypilot.apply.submit_broker import SubmitBroker
    from applypilot.identity import identity_id as _identity_id
    ident = _identity_id(row["url"], company=row.get("site"),
                         title=row.get("title"), location=row.get("location"))
    broker = SubmitBroker(config.APP_DIR / f".submit-ticket-{worker_id}.json", dry_run=dry_run)
    browser_stream = BrowserStateStream(port, broker=broker, identity_id=ident, dry_run=dry_run, ...).start()
```

Pass `broker` and `ident` into `run_job(...)` as parameters. Inside `run_job`, immediately AFTER the two `possible_duplicate_guard` short-circuits (2280-2297) and BEFORE the MCP-config write (2311-2313), issue the ticket:

```python
    broker.issue(ident)   # never issues in dry_run (SubmitBroker.dry_run short-circuits)
```

Write both `--broker-file <path>` and `--job-identity <ident>` into the per-worker MCP config. Pass `broker` and `ident` into `_greenhouse_adapter_pass` (2453-2455) and require `broker.ticket_open(ident)` before calling `fill_greenhouse(submit="auto")`; the actual one-shot consume happens in the route handler (Step 3) on the real submit POST.

- [ ] **Step 6: Extend the dry-run guard test**

```python
# tests/test_dry_run_submit_guard.py — append
from applypilot.apply.browser_stream import should_block_request

def test_dry_run_network_layer_blocks_all_ats_mutations():
    for url in ("https://boards.greenhouse.io/x/jobs/1",
                "https://jobs.lever.co/x/uuid/apply",
                "https://jobs.ashbyhq.com/api/non-user-graphql",
                "https://sap.wd3.myworkdayjobs.com/x/apply"):
        assert should_block_request("POST", url, ticket_open=True, dry_run=True) is True
```

- [ ] **Step 7: Run tests + full suite**

Run: `& $PY -m pytest tests/test_network_containment.py tests/test_dry_run_submit_guard.py tests/test_submit_broker.py tests/test_stream_executor.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -q`
Expected: no regressions.

- [ ] **Step 8: Commit**

```powershell
git reset
git add src/applypilot/apply/browser_stream.py src/applypilot/apply/stream_executor.py src/applypilot/apply/stream_mcp_server.py src/applypilot/apply/launcher.py tests/test_network_containment.py tests/test_dry_run_submit_guard.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "safety: CDP network containment (block ATS POSTs without broker ticket) + broker wiring at all 3 submit paths"
```

Note: The network route is defense below the parser — a mislabeled submit button or the raw @playwright/mcp path (which has no Python gate) still cannot POST without an open ticket. Manual live verification of the route (one dry-run per ATS, capture the blocked-POST log line) is deferred to Phase 3's soak test; flag this in the task summary.

---

### Task 10A: Two-phase submission ledger (durable INTENT→CONFIRMED — the anti-double-submit guarantee)

The broker (Task 9) is a per-run, per-worker ticket file — it prevents a *second* submit within one run, but it is not a durable record: it does not survive a crash/restart, so it can't answer "did we already apply to this identity in a previous run?". Spec §10.4 requires a durable two-phase ledger so the Reddit-double-submit class is structurally dead across runs. This is the incident class the whole spec is built to kill — it must ship in Phase 1.

**Files:**
- Create: `src/applypilot/submission_ledger.py`
- Modify: `src/applypilot/database.py` (`submission_ledger` table in `init_db` + a `create_submission_ledger()` / helpers)
- Modify: `src/applypilot/apply/launcher.py` (write INTENT before submit; transition CONFIRMED/FAILED after verify; watchdog-exempt grace)
- Test: `tests/test_submission_ledger.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_submission_ledger.py
from applypilot import database as db
from applypilot.submission_ledger import SubmissionLedger

def test_intent_then_confirm(tmp_path):
    conn = db.get_connection(tmp_path / "s.db"); db.init_db(tmp_path / "s.db")
    led = SubmissionLedger(conn)
    led.record_intent("greenhouse:chime:1", worker_id=0)
    assert led.has_open_intent("greenhouse:chime:1") is True
    assert led.has_confirmed("greenhouse:chime:1") is False
    led.confirm("greenhouse:chime:1", confidence=0.9)
    assert led.has_open_intent("greenhouse:chime:1") is False
    assert led.has_confirmed("greenhouse:chime:1") is True

def test_intent_then_fail_clears_open(tmp_path):
    conn = db.get_connection(tmp_path / "s2.db"); db.init_db(tmp_path / "s2.db")
    led = SubmissionLedger(conn)
    led.record_intent("greenhouse:chime:2", worker_id=0)
    led.fail("greenhouse:chime:2", reason="validation_error")
    assert led.has_open_intent("greenhouse:chime:2") is False
    assert led.has_confirmed("greenhouse:chime:2") is False

def test_dangling_intent_is_durable(tmp_path):
    # a crash between intent and verdict leaves a dangling INTENT that persists
    # on disk and must be visible to a later read (refuse to blindly re-apply)
    import sqlite3
    p = tmp_path / "s3.db"
    conn = db.get_connection(p); db.init_db(p)
    SubmissionLedger(conn).record_intent("greenhouse:chime:3", worker_id=0)
    # re-open the DB file directly (proves durability, no shared in-memory state)
    raw = sqlite3.connect(p); raw.row_factory = sqlite3.Row
    led2 = SubmissionLedger(raw)
    assert led2.has_open_intent("greenhouse:chime:3") is True   # dangling — needs reconciliation
    assert led2.dangling_count() == 1

def test_already_confirmed_blocks_reapply(tmp_path):
    conn = db.get_connection(tmp_path / "s4.db"); db.init_db(tmp_path / "s4.db")
    led = SubmissionLedger(conn)
    led.record_intent("greenhouse:chime:4", worker_id=0); led.confirm("greenhouse:chime:4", confidence=0.9)
    # a confirmed submission is the hard re-apply block signal for queue_policy/gate
    assert led.has_confirmed("greenhouse:chime:4") is True
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_submission_ledger.py -v`

- [ ] **Step 3: Add the `submission_ledger` table to `database.py`**

In `init_db`, after the `jobs` CREATE TABLE, add:

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submission_ledger (
            identity_id  TEXT NOT NULL,
            state        TEXT NOT NULL,          -- 'intent' | 'confirmed' | 'failed'
            worker_id    INTEGER,
            reason       TEXT,
            confidence   REAL,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (identity_id, created_at)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_ledger_identity ON submission_ledger(identity_id, state)")
    conn.commit()
```

- [ ] **Step 4: Implement `src/applypilot/submission_ledger.py`**

```python
"""Durable two-phase submission record. INTENT is written immediately before a
submit is attempted; the same identity transitions to CONFIRMED (verified) or
FAILED (rejected/unverified). A dangling INTENT (process died between the two)
is visible across restarts and must be reconciled by a human — never blindly
retried. This is what makes the Reddit-double-submit class structurally dead."""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubmissionLedger:
    def __init__(self, conn):
        self.conn = conn

    def record_intent(self, identity_id: str, *, worker_id: int) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO submission_ledger (identity_id, state, worker_id, created_at, updated_at) "
            "VALUES (?, 'intent', ?, ?, ?)", (identity_id, worker_id, now, now))
        self.conn.commit()

    def _resolve_open(self, identity_id: str, state: str, *, reason=None, confidence=None) -> None:
        self.conn.execute(
            "UPDATE submission_ledger SET state=?, reason=?, confidence=?, updated_at=? "
            "WHERE identity_id=? AND state='intent'",
            (state, reason, confidence, _now(), identity_id))
        self.conn.commit()

    def confirm(self, identity_id: str, *, confidence: float) -> None:
        self._resolve_open(identity_id, "confirmed", confidence=confidence)

    def fail(self, identity_id: str, *, reason: str) -> None:
        self._resolve_open(identity_id, "failed", reason=reason)

    def has_open_intent(self, identity_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM submission_ledger WHERE identity_id=? AND state='intent' LIMIT 1",
            (identity_id,)).fetchone() is not None

    def has_confirmed(self, identity_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM submission_ledger WHERE identity_id=? AND state='confirmed' LIMIT 1",
            (identity_id,)).fetchone() is not None

    def confirmed_count_for_token(self, board_token: str, since_iso: str) -> int:
        # company cooldown: count confirmed applies to this board within a window.
        # identity_id is 'ats:token:job_id', so match the ':token:' segment.
        return self.conn.execute(
            "SELECT COUNT(*) FROM submission_ledger WHERE state='confirmed' "
            "AND updated_at >= ? AND identity_id LIKE ?",
            (since_iso, f"%:{board_token}:%")).fetchone()[0]

    def dangling_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM submission_ledger WHERE state='intent'").fetchone()[0]
```

- [ ] **Step 5: Wire INTENT/CONFIRMED into `run_job`**

In `launcher.py run_job`, right where the ticket is issued (Task 10 Step 5, after the dedup guards):

```python
    from applypilot.submission_ledger import SubmissionLedger
    from datetime import datetime, timezone, timedelta
    ledger = SubmissionLedger(db.get_connection())
    # (1) hard re-apply block across runs: a prior CONFIRMED submission to this identity
    if ledger.has_confirmed(ident):
        return "needs_review:already_applied_identity", int((time.time() - run_started) * 1000), None
    # (2) dangling-INTENT block: a prior run died mid-submit for this identity — never
    #     blindly re-apply over an unreconciled intent (this is the double-submit guard)
    if ledger.has_open_intent(ident):
        return "needs_review:dangling_submission_intent", int((time.time() - run_started) * 1000), None
    # (3) company cooldown (spec §5.1/§5.2 rule 5): >=2 confirmed applies to this board in 30d
    ref = parse_ats_url(_effective_apply_url(job) or "")
    if ref and ref.token:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        if ledger.confirmed_count_for_token(ref.token, since) >= 2:
            return "needs_review:company_cooldown", int((time.time() - run_started) * 1000), None
    ledger.record_intent(ident, worker_id=worker_id)   # INTENT before any submit
    broker.issue(ident)
```

In this task also add the cooldown defaults to `config.DEFAULTS` (they're needed here, before Task 11): `"company_cooldown_max": 2`, `"company_cooldown_days": 30`. (`parse_ats_url` and `_effective_apply_url` are already imported/available in `launcher.py`.)

**Explicitly deferred to Phase 4 (write this in the plan, don't silently drop it):** §10.3 "first apply per company lands in the review queue" requires the trust/review surface that does not exist until Phase 4 — the mechanism (route first-apply-per-board_token to `needs_review` instead of auto-submit) is a one-line gate but has no review UI to land in during Phase 1. The confirmed-identity block, dangling-intent block, and company cooldown above are the Phase-1 blast-radius caps; first-apply-per-company review is a Phase-4 trust-layer task. Similarly, the general "pre-commit polarity contradiction check across ALL committed answers" beyond the canary resolver (§10.3) is deferred to Phase 4 (the canary polarity check in Task 7 covers the high-stakes classes for Phase 1).

After `_verify_submission_success` returns its verdict (the existing verify call ~2486-2489):

```python
    if verdict.get("verified"):
        ledger.confirm(ident, confidence=verdict.get("confidence", 0.0))
    else:
        ledger.fail(ident, reason=job_meta.get("failure_class") or "unverified")
```

**Watchdog-exempt grace:** the section from `ledger.record_intent` to the `confirm`/`fail` call is submit-critical — a watchdog kill here strands a dangling INTENT. Phase 1 has no watchdog rewrite (that's Phase 3), but add a code comment marking this region `# SUBMIT-CRITICAL: do not interrupt; Phase 3 watchdog must grant +15s grace here` so the Phase 3 work preserves it. Dangling INTENTs are surfaced by `dangling_count()` (wired into `status`/`report` in a later task or Phase 4 UI).

- [ ] **Step 6: Run tests + full suite**

Run: `& $PY -m pytest tests/test_submission_ledger.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -q`
Expected: no regressions.

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/submission_ledger.py src/applypilot/database.py src/applypilot/apply/launcher.py tests/test_submission_ledger.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "safety: durable two-phase submission ledger (INTENT->CONFIRMED); hard-block re-apply to a confirmed identity"
```

---

## Group E — Spend ledger

### Task 11: Metered LLM client + ledger + budget cap

**Files:**
- Create: `src/applypilot/spend_ledger.py`
- Modify: `src/applypilot/llm.py:393-403` (wrap the singleton)
- Modify: `src/applypilot/config.py:164-181` (budget defaults)
- Test: `tests/test_spend_ledger.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_spend_ledger.py
from applypilot.spend_ledger import SpendLedger, estimate_cost

def test_records_and_sums(tmp_path):
    led = SpendLedger(tmp_path / "ledger.jsonl")
    led.record(stage="score", model="gemini-2.0-flash", tokens_in=1000, tokens_out=200, identity_id="greenhouse:x:1")
    led.record(stage="apply", model="gemini-2.0-flash", tokens_in=500, tokens_out=100)
    assert led.spent_today() > 0
    assert len(list(led.entries())) == 2

def test_over_cap_detected(tmp_path):
    led = SpendLedger(tmp_path / "l.jsonl", daily_cap_usd=0.0)
    led.record(stage="score", model="gpt-4o-mini", tokens_in=100000, tokens_out=1000)
    assert led.over_cap() is True

def test_estimate_cost_positive():
    assert estimate_cost("claude-haiku-4-5-20251001", 50000, 20000) > 0
```

```python
# tests/test_metered_client.py
from applypilot.spend_ledger import MeteredClient, SpendLedger

class _Fake:
    def chat(self, messages, temperature=0.0, max_tokens=4096): return "hello world"
    def close(self): pass

def test_metered_client_records(tmp_path):
    led = SpendLedger(tmp_path / "l.jsonl")
    c = MeteredClient(_Fake(), led, model="gemini-2.0-flash", stage="score")
    out = c.chat([{"role": "user", "content": "hi"}])
    assert out == "hello world"
    assert led.spent_today() >= 0  # recorded one event
    assert len(list(led.entries())) == 1
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_spend_ledger.py tests/test_metered_client.py -v`

- [ ] **Step 3: Implement `src/applypilot/spend_ledger.py`**

```python
"""Append-only spend ledger + a metering wrapper around the LLM client.
chat() returns only text (usage is discarded upstream), so cost is estimated
from token counts (char/4 heuristic when exact counts unavailable) × per-model
rates. One enforcement point: the pipeline checks over_cap() before batches."""
from __future__ import annotations

import json
import time
from pathlib import Path

# USD per 1M tokens (input, output). Extend as providers are added.
_RATES = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
}
_DEFAULT_RATE = (1.0, 5.0)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    ri, ro = _RATES.get((model or "").strip(), _DEFAULT_RATE)
    return (tokens_in / 1_000_000) * ri + (tokens_out / 1_000_000) * ro


def _est_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


class SpendLedger:
    def __init__(self, path: str | Path, *, daily_cap_usd: float | None = None):
        self.path = Path(path)
        self.daily_cap_usd = daily_cap_usd

    def record(self, *, stage: str, model: str, tokens_in: int, tokens_out: int,
               identity_id: str | None = None) -> float:
        cost = estimate_cost(model, tokens_in, tokens_out)
        row = {"ts": time.time(), "stage": stage, "model": model, "identity_id": identity_id,
               "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": round(cost, 6)}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return cost

    def entries(self):
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)

    def spent_today(self) -> float:
        cutoff = time.time() - 86400
        return round(sum(e["cost_usd"] for e in self.entries() if e["ts"] >= cutoff), 6)

    def over_cap(self) -> bool:
        return self.daily_cap_usd is not None and self.spent_today() >= self.daily_cap_usd


class MeteredClient:
    """Delegates chat/ask/close to the wrapped provider client, recording spend
    per logical call. Duck-typed — works for both LLMClient and ClaudeCodeClient."""
    def __init__(self, inner, ledger: SpendLedger, *, model: str, stage: str = "llm"):
        self._inner = inner
        self._ledger = ledger
        self._model = model
        self._stage = stage

    def chat(self, messages, temperature: float = 0.0, max_tokens: int = 4096) -> str:
        out = self._inner.chat(messages, temperature=temperature, max_tokens=max_tokens)
        tin = sum(_est_tokens(m.get("content", "")) for m in messages)
        self._ledger.record(stage=self._stage, model=self._model,
                            tokens_in=tin, tokens_out=_est_tokens(out))
        return out

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **{
            k: v for k, v in kwargs.items() if k in ("temperature", "max_tokens")})

    def close(self):
        if hasattr(self._inner, "close"):
            self._inner.close()
```

- [ ] **Step 4: Add budget defaults to `config.py`**

In `DEFAULTS` (config.py 164-181) add:

```python
    "daily_budget_usd": 5.0,
    "monthly_budget_usd": 50.0,
    "company_cooldown_max": 2,      # max confirmed applies to one board_token...
    "company_cooldown_days": 30,    # ...within this window (Task 10A cooldown)
```

And a path constant near the others (config.py 12-30): `SPEND_LEDGER_PATH = LOG_DIR / "spend_ledger.jsonl"`.

- [ ] **Step 5: Wrap the `get_client()` singleton (INSIDE the None-guard — a review caught a re-wrap bug)**

`get_client()` is `if _instance is None: <build> ; return _instance`. The `model` local exists ONLY inside the `if _instance is None:` block, and wrapping outside it would (a) NameError on `model` and (b) re-wrap the singleton on every call → `MeteredClient(MeteredClient(...))` with multiplied ledger writes. Place the wrap as the LAST statement INSIDE the `if _instance is None:` block, at the same indentation as the `if base_url == "claude-code": ... else: ...` assignment:

```python
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        if base_url == "claude-code":
            _instance = ClaudeCodeClient(model)
        else:
            _instance = LLMClient(base_url, model, api_key)
        # meter every logical call (transparent chat/ask/close pass-through)
        from applypilot.spend_ledger import SpendLedger, MeteredClient
        from applypilot import config
        led = SpendLedger(config.SPEND_LEDGER_PATH, daily_cap_usd=config.DEFAULTS.get("daily_budget_usd"))
        _instance = MeteredClient(_instance, led, model=model, stage="llm")
    return _instance
```

- [ ] **Step 6: Add the single enforcement point (over-cap → paused, with a resume path)**

The ledger only *observes* until something checks it. Add an `engine_control` kv table and check the cap at the apply-batch boundary (spec §5.4: "one enforcement point").

In `database.py init_db`, add:

```python
    conn.execute("CREATE TABLE IF NOT EXISTS engine_control (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
```

Add helpers `set_paused(conn, reason: str | None)` (UPSERT `('paused', reason)`; `None` clears) and `paused_reason(conn) -> str | None`. Extend `SpendLedger` with a monthly window:

```python
    def spent_month(self) -> float:
        cutoff = time.time() - 30 * 86400
        return round(sum(e["cost_usd"] for e in self.entries() if e["ts"] >= cutoff), 6)

    def over_cap(self) -> bool:
        d = self.daily_cap_usd is not None and self.spent_today() >= self.daily_cap_usd
        m = self.monthly_cap_usd is not None and self.spent_month() >= self.monthly_cap_usd
        return bool(d or m)
```

(Add `monthly_cap_usd=None` to `SpendLedger.__init__`; pass `config.DEFAULTS["monthly_budget_usd"]` when constructing it.) In `launcher.py` `worker_loop`, BEFORE dequeuing each job (before `acquire_job`):

```python
    from applypilot.spend_ledger import SpendLedger
    from applypilot import database as db
    _led = SpendLedger(config.SPEND_LEDGER_PATH,
                       daily_cap_usd=config.DEFAULTS["daily_budget_usd"],
                       monthly_cap_usd=config.DEFAULTS["monthly_budget_usd"])
    if _led.over_cap():
        db.set_paused(db.get_connection(), "budget")
        logger.warning("Paused: spend cap reached (today=$%.2f). Raise the cap or resume tomorrow.", _led.spent_today())
        break   # stop dispatching; resume path = raise cap / new day / `applypilot resume`
```

Add a test asserting `over_cap()` true causes `worker_loop` to set `paused='budget'` and stop (inject a pre-filled ledger). Resume path: a later task / the Phase-4 UI clears `engine_control.paused`; for Phase 1 a one-line `applypilot resume` command (`set_paused(conn, None)`) is sufficient — add it to `cli.py`.

- [ ] **Step 7: Run tests + full suite**

Run: `& $PY -m pytest tests/test_spend_ledger.py tests/test_metered_client.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -q`
Expected: no regressions (MeteredClient is a transparent pass-through for chat/ask/close).

- [ ] **Step 8: Commit**

```powershell
git reset
git add src/applypilot/spend_ledger.py src/applypilot/llm.py src/applypilot/config.py src/applypilot/database.py src/applypilot/apply/launcher.py src/applypilot/cli.py tests/test_spend_ledger.py tests/test_metered_client.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "spine: spend ledger + metered client + single enforcement point (over-cap -> paused:budget + resume)"
```

---

## Group F — Queue policy unification

### Task 12: Single queue_policy() and route the drifting predicates through it

**Files:**
- Modify: `src/applypilot/database.py` (add `queue_policy()`)
- Modify: `src/applypilot/apply/launcher.py` (route `_fetch_apply_candidates` + `acquire_job` through it; add gate-eligibility)
- Modify: `src/applypilot/webui/server.py:226-233,292-301` (use the fragment)
- Test: `tests/test_queue_policy.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_queue_policy.py
from applypilot import database as db

def test_queue_policy_returns_fragment_and_params():
    frag, params = db.queue_policy(min_score=8, max_age_hours=24)
    assert "fit_score >= ?" in frag
    assert "gate_result = 'eligible'" in frag   # v2: only gated-eligible rows are queue-visible
    assert "automatability = 'auto'" in frag
    assert 8 in params

def test_queue_policy_excludes_ineligible(tmp_path):
    conn = db.get_connection(tmp_path / "q.db")
    db.init_db(tmp_path / "q.db")
    conn.executemany(
        "INSERT INTO jobs (url, fit_score, gate_result, automatability, gated_at, application_url, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?)",
        [("u1", 9, "eligible", "auto", "t", "https://boards.greenhouse.io/x/jobs/1", None, None),
         ("u2", 9, "ineligible", "manual", "t", "https://linkedin.com/jobs/2", None, None),
         ("u3", 9, "unknown", "auto", "t", "https://boards.greenhouse.io/x/jobs/3", None, None)],
    )
    conn.commit()
    frag, params = db.queue_policy(min_score=8)
    rows = conn.execute(f"SELECT url FROM jobs WHERE {frag}", params).fetchall()
    urls = {r["url"] for r in rows}
    assert urls == {"u1"}   # ineligible + unknown excluded
```

- [ ] **Step 2: Run — expect failure**

Run: `& $PY -m pytest tests/test_queue_policy.py -v`

- [ ] **Step 3: Implement `queue_policy()` in `database.py`**

```python
def queue_policy(*, min_score: int = 8, max_age_hours: int | None = None,
                 include_attempt_cap: bool = True) -> tuple[str, list]:
    """The single SQL predicate for 'may this job be applied to'. Returns a
    WHERE-fragment + ordered params for callers to embed. v2 rule: only
    gated-eligible + auto-automatable rows are ever queue-visible.

    NOTE: Python-level filters (config.is_manual_ats on resolved URLs, live
    location re-check) are applied post-fetch by callers — this covers only the
    SQL-expressible predicate."""
    from applypilot import config
    parts = [
        "fit_score >= ?",
        "applied_at IS NULL",
        "(apply_status IS NULL OR apply_status = 'failed')",
        "gate_result = 'eligible'",
        "automatability = 'auto'",
    ]
    params: list = [min_score]
    if include_attempt_cap:
        parts.append("(apply_attempts IS NULL OR apply_attempts < ?)")
        params.append(int(config.DEFAULTS["max_apply_attempts"]))
    if max_age_hours and max_age_hours > 0:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        parts.append("discovered_at IS NOT NULL AND discovered_at >= ?")
        params.append(cutoff)
    return " AND ".join(parts), params
```

- [ ] **Step 4: Route the launcher SQL through it**

In `launcher.py`, collapse the duplication first: make `acquire_job` call `_fetch_apply_candidates(conn, min_score=..., max_age_hours=..., site_contains=..., limit=50)` inside its `BEGIN IMMEDIATE` transaction (instead of the copy-pasted 1779-1840 query), then claim the first returned row via the existing `UPDATE ... in_progress` (1936-1946). In `_fetch_apply_candidates`, replace the base `WHERE` clauses (`fit_score >= ?`, attempt cap, `apply_status`, age) with the `queue_policy()` fragment, keeping the `(site,title)` dedup NOT EXISTS clause, the per-site `in_progress` lock, and the ROW_NUMBER windowing (preserve their intent comments verbatim). Add a regression test comparing `preview_apply_queue` output before/after — it must be identical for eligible rows.

- [ ] **Step 5: Route the web UI through it**

In `webui/server.py`, replace the two hardcoded `fit_score >= 8 AND application_url IS NOT NULL ...` eligible predicates (226-233, 292-301) with:

```python
from applypilot.database import queue_policy
_frag, _params = queue_policy(min_score=8)
# summary:
eligible = conn.execute(f"SELECT discovered_at FROM jobs WHERE {_frag}", _params).fetchall()
# jobs view: where["eligible"] = (_frag, _params)
```

This makes the UI "eligible" count match what `acquire_job` will actually pick (they drift today: UI omits the attempt cap, gate, and dedup).

- [ ] **Step 6: Add gate-eligibility regression + full suite**

```python
# tests/test_queue_policy.py — append
def test_preview_matches_acquire_eligibility(tmp_path):
    # both must use queue_policy — a job that's ineligible by gate is picked by neither
    conn = db.get_connection(tmp_path / "p.db")
    db.init_db(tmp_path / "p.db")
    conn.execute("INSERT INTO jobs (url, fit_score, gate_result, automatability, gated_at, discovered_at, application_url) "
                 "VALUES ('u1', 9, 'ineligible', 'manual', 't', '2026-07-01T00:00:00+00:00', 'https://linkedin.com/x')")
    conn.commit()
    frag, params = db.queue_policy(min_score=8)
    assert conn.execute(f"SELECT COUNT(*) c FROM jobs WHERE {frag}", params).fetchone()["c"] == 0
```

Run: `& $PY -m pytest tests/test_queue_policy.py -v`
Expected: ALL PASS.
Run: `& $PY -m pytest tests/ -q`
Expected: no regressions (esp. `tests/test_apply_stability.py`, `tests/test_apply_freshness_gate.py` — the queue tests).

- [ ] **Step 7: Commit**

```powershell
git reset
git add src/applypilot/database.py src/applypilot/apply/launcher.py src/applypilot/webui/server.py tests/test_queue_policy.py
git diff --cached --stat
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "spine: single queue_policy() (gated-eligible + auto only); route launcher + webui through it, dedupe acquire_job"
```

---

### Task 13: Phase 1 verification

- [ ] **Step 1: Full suite green**

Run: `& $PY -m pytest tests/ -q`
Expected: ALL PASS.

- [ ] **Step 2: Manual smoke — gate a real DB slice (dry, read-only)**

Run: `& $PY -m applypilot gate --rerun`
Expected: prints "Gated N jobs at version 1"; no errors. Then:
Run: `& $PY -m applypilot status`
Expected: runs clean. Spot-check a few rows: `& $PY -c "import sqlite3,os; c=sqlite3.connect(os.environ['APPLYPILOT_DIR']+'/applypilot.db'); [print(r) for r in c.execute('SELECT gate_result, automatability, count(*) FROM jobs GROUP BY 1,2')]"` — verify sponsorship-unknown/ineligible buckets are populated (proves the gate ran and is filtering).

- [ ] **Step 2b: Spec §5/§10 checklist — confirm each Phase-1 safety property is live**

Verify by test/inspection, not assertion:
- Canary: `& $PY -m pytest tests/test_canary.py tests/test_answer_cache_canary.py -q` green → sponsorship/salary/EEO/address never fuzzy-answered, never cached.
- Submission ledger: `& $PY -m pytest tests/test_submission_ledger.py -q` green → INTENT→CONFIRMED durable; confirmed identity and dangling intent both hard-block re-apply.
- Network containment: `& $PY -m pytest tests/test_network_containment.py -q` green → dry-run fails closed on all mutating requests except the safe-host allowlist.
- Broker one-key: grep the launcher wiring to confirm `identity_id` (not `idempotency_key`) is used at the broker issue site AND the stream/adapter check sites.
- Spend cap: `& $PY -m pytest -k over_cap -q` green → over-cap sets `engine_control.paused='budget'` and stops dispatch; `applypilot resume` clears it.
- Cooldown: confirm `company_cooldown_max`/`_days` are read in the run_job cooldown check.

- [ ] **Step 3: Tree clean, report**

Run: `git status --short` → EMPTY.
Summarize to the user: spine + safety kernel landed under the current engine — canary questions can no longer be fuzzy-answered or live-submitted; the durable two-phase ledger makes cross-run double-submits (the Reddit class) structurally impossible and hard-blocks re-apply to a confirmed identity + enforces company cooldown; dry-run submits are blocked at the network layer (fail-closed on unknown hosts); ineligible jobs no longer reach the scorer or the apply queue; spend is metered with a real over-cap pause. Note the explicit Phase-4 deferrals (first-apply-per-company→review; general pre-commit polarity check; H-1B LCA sponsorship prior), that live end-to-end verification of the network route (one dry-run per ATS) is the first item of Phase 3's soak test, and that this is the point to consider an authorized live batch to measure the waste-reduction delta before building Phase 2.
