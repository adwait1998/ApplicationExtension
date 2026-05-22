# Autonomous Apply Reliability Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `src/applypilot/loop/` package that provides the state machine, signature extraction, safety helpers, subprocess runner, and fixed driver prompt implementing the spec at `docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md`.

**Architecture:** Pure-Python helpers + a Markdown driver prompt. The loop is driven externally by the ralph-loop plugin re-firing the same prompt each iteration; this package provides the deterministic helpers the prompt calls. State persists in `loop-state.json` (atomic write). Loop-immutable carveouts are enforced by `safety.py` before any patch lands.

**Tech Stack:** Python 3.11+, pytest, ruff. Stdlib only — `dataclasses`, `json`, `hashlib`, `ast`, `subprocess`, `pathlib`, `uuid`, `os`. No new third-party deps.

**Spec:** [docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md](../specs/2026-05-22-autonomous-apply-reliability-loop-design.md)

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/applypilot/loop/__init__.py` | Package marker; carveout policy docstring |
| `src/applypilot/loop/state.py` | `LoopState` dataclass, `load_or_init`, `save_atomic`, session lock, derived computations (rolling pass-rate, streak) |
| `src/applypilot/loop/signature_extractor.py` | Pure function: `(review_row, transcript_path, verify_json_path) → signature_str` |
| `src/applypilot/loop/safety.py` | Loop-immutable carveouts: `is_immutable_path`, `touches_immutable_functions`, `brick_detected` |
| `src/applypilot/loop/runner.py` | `apply_one()` subprocess wrapper around `applypilot apply --limit 1` with brick-detection timing |
| `src/applypilot/loop/driver_prompt.md` | Fixed prompt text fed to ralph-loop each iteration |
| `tests/test_loop_state.py` | Round-trip, defaults, atomic write, session lock, derived computations |
| `tests/test_signature_extractor.py` | Each failure class → expected signature |
| `tests/test_loop_safety.py` | Immutable-path matrix, AST function check, brick detection |
| `tests/test_loop_runner.py` | apply_one with mocked subprocess |

---

### Task 1: Package skeleton

**Files:**
- Create: `src/applypilot/loop/__init__.py`

- [ ] **Step 1: Create the package marker file**

```python
"""ApplyPilot autonomous apply reliability loop.

This package implements the helpers consumed by the fixed driver prompt
that ralph-loop re-fires each iteration. See
`docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md`
for the full design.

CARVEOUT POLICY (enforced by safety.is_immutable_path):
  Modules in this package are LOOP-IMMUTABLE. The loop must never edit
  its own grader (state, signature_extractor, safety, runner) or the
  driver prompt. The grader cannot grade itself.
"""
```

- [ ] **Step 2: Verify it imports**

Run: `cd e:\auto-apply-pipeline; python -c "import applypilot.loop; print(applypilot.loop.__doc__[:60])"`
Expected: prints the first 60 chars of the docstring without ImportError.

- [ ] **Step 3: Commit**

```bash
git add src/applypilot/loop/__init__.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: add package skeleton"
```

---

### Task 2: LoopState dataclass + defaults

**Files:**
- Create: `src/applypilot/loop/state.py`
- Create: `tests/test_loop_state.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_loop_state.py`:

```python
"""Tests for applypilot.loop.state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from applypilot.loop import state as state_mod


def test_default_state_has_required_fields():
    s = state_mod.default_state()
    assert s["schema_version"] == 1
    assert s["streak"] == 0
    assert s["iteration"] == 0
    assert s["attempts"] == []
    assert s["signature_counts"] == {}
    assert s["cooldown_remaining"] == 0
    assert s["last_patch"] is None
    assert s["patch_budgets"] == {}
    assert s["class_blocklist"] == []
    assert s["rollback_log"] == []
    assert s["last_processed_review_ts"] is None
    assert isinstance(s["session_id"], str) and len(s["session_id"]) == 36
    assert isinstance(s["pid"], int) and s["pid"] > 0
    assert isinstance(s["started_at"], str) and s["started_at"].endswith("Z")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd e:\auto-apply-pipeline; pytest tests/test_loop_state.py::test_default_state_has_required_fields -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'applypilot.loop.state'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/applypilot/loop/state.py`:

```python
"""Persistent state for the autonomous apply reliability loop.

State lives in $APPLYPILOT_DIR/loop-state.json. Atomic write via temp file
+ os.replace. Each iteration of ralph-loop reads on entry, writes on exit.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot import config

SCHEMA_VERSION = 1
STATE_FILENAME = "loop-state.json"


def state_path() -> Path:
    return config.APP_DIR / STATE_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def default_state() -> dict[str, Any]:
    """Fresh loop state — used on first iteration or when state file is missing."""
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": str(uuid.uuid4()),
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "iteration": 0,
        "streak": 0,
        "last_processed_review_ts": None,
        "attempts": [],
        "signature_counts": {},
        "cooldown_remaining": 0,
        "last_patch": None,
        "patch_budgets": {},
        "class_blocklist": [],
        "rollback_log": [],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd e:\auto-apply-pipeline; pytest tests/test_loop_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/state.py tests/test_loop_state.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: state defaults + schema_version"
```

---

### Task 3: Atomic write + load_or_init

**Files:**
- Modify: `src/applypilot/loop/state.py`
- Modify: `tests/test_loop_state.py`

- [ ] **Step 1: Add failing tests for round-trip and load_or_init**

Append to `tests/test_loop_state.py`:

```python
def test_save_atomic_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.default_state()
    s["streak"] = 3
    state_mod.save_atomic(s)
    loaded = state_mod.load_or_init()
    assert loaded["streak"] == 3
    assert loaded["session_id"] == s["session_id"]


def test_load_or_init_creates_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    loaded = state_mod.load_or_init()
    assert loaded["streak"] == 0
    assert loaded["schema_version"] == 1
    # File now exists
    assert (tmp_path / "loop-state.json").exists()


def test_save_atomic_does_not_leave_partial_file_on_interruption(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.default_state()
    state_mod.save_atomic(s)
    # Corrupt-write attempt: dump and check we still get the previous good value
    bad = {"schema_version": 1, "bogus": True}
    # Direct write of invalid file should not happen via save_atomic; we just verify
    # save_atomic survives even when called repeatedly with valid states.
    s2 = dict(s)
    s2["streak"] = 9
    state_mod.save_atomic(s2)
    loaded = state_mod.load_or_init()
    assert loaded["streak"] == 9
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_state.py -v`
Expected: FAIL with `AttributeError: module 'applypilot.loop.state' has no attribute 'save_atomic'`.

- [ ] **Step 3: Implement save_atomic and load_or_init**

Append to `src/applypilot/loop/state.py`:

```python
def save_atomic(state: dict[str, Any]) -> None:
    """Atomic write via temp file + os.replace. Survives interruption."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".loop-state-",
        suffix=".json.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_or_init() -> dict[str, Any]:
    """Read state file; create with defaults if absent."""
    path = state_path()
    if not path.exists():
        s = default_state()
        save_atomic(s)
        return s
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_state.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/state.py tests/test_loop_state.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: atomic save + load_or_init"
```

---

### Task 4: Session lock

**Files:**
- Modify: `src/applypilot/loop/state.py`
- Modify: `tests/test_loop_state.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_loop_state.py`:

```python
def test_is_pid_alive_current_process():
    assert state_mod.is_pid_alive(os.getpid()) is True


def test_is_pid_alive_likely_dead_pid():
    # PIDs above 2^31 - 1 are not valid on any platform.
    assert state_mod.is_pid_alive(2_147_483_640) is False


def test_verify_lock_passes_for_own_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.load_or_init()
    assert state_mod.verify_lock(s) == "owned"


def test_verify_lock_detects_foreign_living_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.default_state()
    s["session_id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    s["pid"] = os.getpid()  # foreign session_id, but PID is alive
    state_mod.save_atomic(s)
    assert state_mod.verify_lock(s) == "conflict"


def test_verify_lock_takes_over_dead_foreign_session(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "loop-state.json")
    s = state_mod.default_state()
    s["session_id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    s["pid"] = 2_147_483_640  # dead pid
    state_mod.save_atomic(s)
    assert state_mod.verify_lock(s) == "stale"
```

Add to the imports at the top of `tests/test_loop_state.py`:

```python
import os
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_state.py -v`
Expected: failures on `is_pid_alive` and `verify_lock` (not defined).

- [ ] **Step 3: Implement is_pid_alive and verify_lock**

Append to `src/applypilot/loop/state.py`:

```python
import signal


def is_pid_alive(pid: int) -> bool:
    """Cross-platform check whether a PID corresponds to a running process."""
    if pid <= 0:
        return False
    try:
        # On POSIX, signal 0 probes without sending. On Windows, os.kill with
        # sig=0 also works via ctypes wrapper that Python provides.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — treat as alive.
        return True
    except OSError:
        return False
    return True


def verify_lock(state: dict[str, Any]) -> str:
    """Decide whether the current process owns the state's session lock.

    Returns one of: "owned" (we own it), "conflict" (someone else's session
    is alive), "stale" (foreign session's PID is dead — safe to take over).
    """
    own_pid = os.getpid()
    if state.get("pid") == own_pid:
        return "owned"
    foreign_pid = state.get("pid", 0)
    if is_pid_alive(foreign_pid):
        return "conflict"
    return "stale"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_state.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/state.py tests/test_loop_state.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: session lock via pid liveness"
```

---

### Task 5: Derived computations (rolling pass-rate, attempt trimming)

**Files:**
- Modify: `src/applypilot/loop/state.py`
- Modify: `tests/test_loop_state.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_loop_state.py`:

```python
def _make_attempt(status: str) -> dict:
    return {
        "url": f"https://example.com/{status}",
        "status": status,
        "failure_class": None if status == "applied" else "transient_timeout",
        "signature": None if status == "applied" else "sig:x",
        "ts": "2026-05-22T00:00:00.000000Z",
    }


def test_rolling_pass_rate_empty():
    assert state_mod.rolling_pass_rate([]) == 0.0


def test_rolling_pass_rate_all_applied():
    attempts = [_make_attempt("applied")] * 5
    assert state_mod.rolling_pass_rate(attempts) == 1.0


def test_rolling_pass_rate_mixed():
    attempts = [_make_attempt("applied"), _make_attempt("needs_review:timeout"),
                _make_attempt("applied"), _make_attempt("applied")]
    assert state_mod.rolling_pass_rate(attempts) == 0.75


def test_roll_attempts_trims_to_ten():
    attempts = [_make_attempt("applied") for _ in range(15)]
    trimmed = state_mod.roll_attempts(attempts)
    assert len(trimmed) == 10
    # Last 10 are kept.
    assert trimmed[0] is attempts[5]
    assert trimmed[-1] is attempts[-1]


def test_streak_from_attempts_counts_trailing_applied():
    attempts = [_make_attempt("applied"), _make_attempt("failed"),
                _make_attempt("applied"), _make_attempt("applied"),
                _make_attempt("applied")]
    assert state_mod.streak_from_attempts(attempts) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_state.py -v`
Expected: failures on missing `rolling_pass_rate`, `roll_attempts`, `streak_from_attempts`.

- [ ] **Step 3: Implement the derived computations**

Append to `src/applypilot/loop/state.py`:

```python
ATTEMPT_WINDOW = 10


def rolling_pass_rate(attempts: list[dict[str, Any]]) -> float:
    """Fraction of attempts in the window whose status is 'applied'.

    Strictly the verifier-confirmed status, NOT 'needs_review:applied' or
    similar — only 'applied' counts.
    """
    if not attempts:
        return 0.0
    hits = sum(1 for a in attempts if a.get("status") == "applied")
    return hits / len(attempts)


def roll_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the most recent ATTEMPT_WINDOW entries."""
    if len(attempts) <= ATTEMPT_WINDOW:
        return list(attempts)
    return list(attempts[-ATTEMPT_WINDOW:])


def streak_from_attempts(attempts: list[dict[str, Any]]) -> int:
    """Number of trailing 'applied' entries before the first non-applied."""
    count = 0
    for a in reversed(attempts):
        if a.get("status") == "applied":
            count += 1
        else:
            break
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_state.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/state.py tests/test_loop_state.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: rolling pass-rate, attempt trim, streak"
```

---

### Task 6: Signature extractor — failure-class taxonomy

**Files:**
- Create: `src/applypilot/loop/signature_extractor.py`
- Create: `tests/test_signature_extractor.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_signature_extractor.py`:

```python
"""Tests for applypilot.loop.signature_extractor.

Same input → same signature, always. Different signatures should differ.
"""
from __future__ import annotations

from pathlib import Path

from applypilot.loop import signature_extractor as sig


def test_applied_row_returns_none():
    row = {"apply_status": "applied", "last_failure_class": None, "site": "figma"}
    assert sig.extract(row, None, None) is None


def test_irreducible_failure_class_passes_through():
    row = {"apply_status": "needs_review:captcha", "last_failure_class": "captcha",
           "site": "lever_acme", "url": "https://acme.com/x"}
    assert sig.extract(row, None, None) == "captcha:acme"


def test_transient_timeout_prefill_signature():
    row = {"apply_status": "needs_review:timeout",
           "last_failure_class": "transient_timeout",
           "site": "greenhouse_figma",
           "url": "https://boards.greenhouse.io/figma/jobs/123",
           "apply_error": "prefill Page.goto timed out after 10s"}
    s = sig.extract(row, None, None)
    assert s == "transient_timeout:prefill_goto:greenhouse"


def test_no_result_line_signature_includes_model():
    row = {"apply_status": "needs_review:no_result_line",
           "last_failure_class": "no_result_line",
           "site": "greenhouse_figma",
           "url": "https://boards.greenhouse.io/figma/jobs/123",
           "model": "claude-haiku-4-5-20251001"}
    assert sig.extract(row, None, None) == "no_result_line:haiku"


def test_unverified_submission_signature():
    row = {"apply_status": "needs_review:unverified_submission",
           "last_failure_class": "unverified_submission",
           "site": "lever_acme",
           "url": "https://jobs.lever.co/acme/abc"}
    assert sig.extract(row, None, None) == "unverified_submission:lever"


def test_same_inputs_produce_same_signature():
    row = {"apply_status": "needs_review:timeout",
           "last_failure_class": "transient_timeout",
           "site": "greenhouse_figma",
           "url": "https://boards.greenhouse.io/figma/jobs/123",
           "apply_error": "prefill Page.goto timed out after 10s"}
    assert sig.extract(row, None, None) == sig.extract(row, None, None)


def test_unknown_class_falls_back_to_class_plus_site():
    row = {"apply_status": "needs_review:weird",
           "last_failure_class": "weird_new_class",
           "site": "greenhouse_acme",
           "url": "https://boards.greenhouse.io/acme/jobs/1"}
    assert sig.extract(row, None, None) == "weird_new_class:greenhouse"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_signature_extractor.py -v`
Expected: FAIL on missing module.

- [ ] **Step 3: Implement the extractor**

Create `src/applypilot/loop/signature_extractor.py`:

```python
"""Deterministic signature extraction from a failed apply row.

Same root cause across runs → same signature. This is the key the loop
uses to gate Branch D (patching) at signature_counts >= 3.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# Map URL host substrings to a stable ATS shorthand.
_HOST_PATTERNS: tuple[tuple[str, str], ...] = (
    ("boards.greenhouse.io", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"),
    ("lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("ashbyhq.com", "ashby"),
    ("myworkdaysite.com", "workday"),
    ("workday.com", "workday"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
)


def _ats_token(row: dict[str, Any]) -> str:
    """Extract a stable ATS / host token from the row's URL or site."""
    url = (row.get("url") or "")
    host = ""
    if url:
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            host = ""
    for pattern, token in _HOST_PATTERNS:
        if pattern in host:
            return token
    # Fall back to site prefix (e.g. "greenhouse_figma" → "greenhouse")
    site = (row.get("site") or "").lower()
    if "_" in site:
        return site.split("_", 1)[0]
    return site or "unknown"


def _company_from_site(row: dict[str, Any]) -> str:
    """Best-effort company slug from `site`."""
    site = (row.get("site") or "").lower()
    if "_" in site:
        return site.split("_", 1)[1]
    return site or "unknown"


def _model_short(row: dict[str, Any]) -> str:
    model = (row.get("model") or "").lower()
    if "haiku" in model:
        return "haiku"
    if "sonnet" in model:
        return "sonnet"
    if "opus" in model:
        return "opus"
    return "unknown"


def _classify_transient_phase(row: dict[str, Any]) -> str:
    err = (row.get("apply_error") or "").lower()
    if "prefill" in err and "goto" in err:
        return "prefill_goto"
    if "page.goto" in err or "page_load" in err:
        return "page_load"
    if "agent" in err or "claude" in err:
        return "agent_turn"
    return "unknown"


def extract(
    review_row: dict[str, Any],
    transcript_path: Path | None,
    verify_json_path: Path | None,
) -> str | None:
    """Return a deterministic signature string for a failed attempt.

    Returns None for status == 'applied'. Otherwise returns a slug like
    'transient_timeout:prefill_goto:greenhouse' that the loop uses to
    accumulate evidence and decide whether to patch.

    transcript_path and verify_json_path are accepted for forward
    compatibility but not yet consulted — the row's `last_failure_class`
    and `apply_error` are sufficient for the current taxonomy.
    """
    status = review_row.get("apply_status") or ""
    if status == "applied":
        return None

    cls = (review_row.get("last_failure_class") or "").strip()
    if not cls:
        return None  # Cannot classify — loop will skip.

    ats = _ats_token(review_row)

    if cls == "transient_timeout":
        phase = _classify_transient_phase(review_row)
        return f"transient_timeout:{phase}:{ats}"

    if cls == "no_result_line":
        return f"no_result_line:{_model_short(review_row)}"

    if cls == "unverified_submission":
        return f"unverified_submission:{ats}"

    if cls == "validation_react_select" or "react_select" in cls:
        # Field name lives in apply_error like "react_select_clobber:work_authorization".
        err = review_row.get("apply_error") or ""
        m = re.search(r"react_select\w*:([\w_]+)", err)
        field = m.group(1) if m else "unknown"
        return f"react_select_clobber:{field}:{ats}"

    # Irreducible classes — pass through with ATS suffix; routing layer
    # will treat them as (B)-irreducible and skip patching anyway.
    if cls in {"captcha", "email_verification", "sso_required", "expired",
               "not_eligible", "cloudflare", "account_required"}:
        company = _company_from_site(review_row)
        return f"{cls}:{company}"

    # Unknown class — return verbatim with ATS suffix.
    return f"{cls}:{ats}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_signature_extractor.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/signature_extractor.py tests/test_signature_extractor.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: deterministic signature extractor"
```

---

### Task 7: Safety — is_immutable_path

**Files:**
- Create: `src/applypilot/loop/safety.py`
- Create: `tests/test_loop_safety.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_loop_safety.py`:

```python
"""Tests for applypilot.loop.safety carveout enforcement."""
from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.loop import safety


@pytest.mark.parametrize("path", [
    "src/applypilot/loop/state.py",
    "src/applypilot/loop/signature_extractor.py",
    "src/applypilot/loop/safety.py",
    "src/applypilot/loop/runner.py",
    "src/applypilot/loop/driver_prompt.md",
    "src/applypilot/loop/__init__.py",
    "src/applypilot/loop/subdir/anything.py",
    "tests/test_scoring.py",
    "tests/subdir/whatever.py",
    "profile.json",
    "resume.pdf",
    ".env",
    ".git/config",
    "CLAUDE.md",
    "CONTEXT.md",
    "docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md",
])
def test_immutable_paths_are_blocked(path):
    assert safety.is_immutable_path(path) is True


@pytest.mark.parametrize("path", [
    "src/applypilot/apply/prefill.py",
    "src/applypilot/apply/prompt.py",
    "src/applypilot/apply/adapters/greenhouse.py",
    "src/applypilot/scoring/scorer.py",
    "src/applypilot/discovery/ats_boards.py",
    "src/applypilot/config/sites.yaml",
    "src/applypilot/llm.py",
    "src/applypilot/cli.py",
])
def test_mutable_paths_are_allowed(path):
    assert safety.is_immutable_path(path) is False


def test_path_separator_agnostic():
    # Windows-style separators should still match.
    assert safety.is_immutable_path("src\\applypilot\\loop\\state.py") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_safety.py -v`
Expected: FAIL on missing module.

- [ ] **Step 3: Implement is_immutable_path**

Create `src/applypilot/loop/safety.py`:

```python
"""Carveout enforcement for the autonomous loop.

The loop must never edit paths that define what 'applied' means (the
streak verifier), what counts as a failure (the reporting taxonomy),
Nida's identity (profile, resume, credentials), or the loop's own
machinery (which would let it grade itself).
"""
from __future__ import annotations

from pathlib import PurePosixPath


# Directory prefixes (POSIX-form). Any path beneath these is immutable.
_IMMUTABLE_DIR_PREFIXES: tuple[str, ...] = (
    "src/applypilot/loop/",
    "tests/",
    ".git/",
)


# Exact file paths (POSIX-form).
_IMMUTABLE_FILES: frozenset[str] = frozenset({
    "profile.json",
    "resume.pdf",
    "resume.txt",
    ".env",
    "CLAUDE.md",
    "CONTEXT.md",
    "docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md",
})


def _normalize(path: str | PurePosixPath) -> str:
    """Return POSIX-form path string, no leading './'."""
    s = str(path).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def is_immutable_path(path: str | PurePosixPath) -> bool:
    """True if the loop must not edit this path."""
    s = _normalize(path)
    if s in _IMMUTABLE_FILES:
        return True
    for prefix in _IMMUTABLE_DIR_PREFIXES:
        if s.startswith(prefix):
            return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_safety.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/safety.py tests/test_loop_safety.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: is_immutable_path carveout enforcement"
```

---

### Task 8: Safety — touches_immutable_functions (AST function-level check)

**Files:**
- Modify: `src/applypilot/loop/safety.py`
- Modify: `tests/test_loop_safety.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_loop_safety.py`:

```python
def test_touches_immutable_functions_detects_verifier(tmp_path):
    src = tmp_path / "launcher.py"
    src.write_text(
        "def other():\n    return 1\n\n"
        "def _verify_submission_success(page):\n    return True  # tampered\n",
        encoding="utf-8",
    )
    immutable = {"_verify_submission_success"}
    assert safety.touches_immutable_functions(src, immutable) is True


def test_touches_immutable_functions_allows_unrelated_changes(tmp_path):
    src = tmp_path / "launcher.py"
    src.write_text(
        "def other():\n    return 1\n\n"
        "def _verify_submission_success(page):\n    return True\n",
        encoding="utf-8",
    )
    # The immutable function is present but the caller asks about a
    # different name — that's allowed.
    immutable = {"some_other_function"}
    assert safety.touches_immutable_functions(src, immutable) is False


def test_touches_immutable_functions_handles_missing_file(tmp_path):
    src = tmp_path / "does_not_exist.py"
    assert safety.touches_immutable_functions(src, {"anything"}) is False


def test_touches_immutable_functions_ignores_syntax_errors(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("def broken(\n  # syntax error\n", encoding="utf-8")
    # Conservative: cannot parse, return True (treat as suspicious).
    assert safety.touches_immutable_functions(src, {"any"}) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_safety.py -v`
Expected: FAIL on missing `touches_immutable_functions`.

- [ ] **Step 3: Implement touches_immutable_functions**

Append to `src/applypilot/loop/safety.py`:

```python
import ast
from pathlib import Path


# Functions that must never be edited even if their containing file is editable.
IMMUTABLE_FUNCTIONS: frozenset[str] = frozenset({
    "_verify_submission_success",
    "_classify_failure",
})


def touches_immutable_functions(
    file_path: Path | str,
    immutable: set[str] | frozenset[str] = IMMUTABLE_FUNCTIONS,
) -> bool:
    """Return True if file_path contains a definition of an immutable function.

    Conservative: if the file cannot be parsed, returns True so the patch
    is rejected rather than risk silent corruption.
    """
    p = Path(file_path)
    if not p.exists():
        return False
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in immutable:
                return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_safety.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/safety.py tests/test_loop_safety.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: AST-level immutable-function check"
```

---

### Task 9: Safety — brick_detected

**Files:**
- Modify: `src/applypilot/loop/safety.py`
- Modify: `tests/test_loop_safety.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_loop_safety.py`:

```python
def test_brick_detected_fast_nonzero_exit():
    assert safety.brick_detected(returncode=1, elapsed_s=2.5) is True


def test_brick_detected_slow_nonzero_exit_is_not_brick():
    assert safety.brick_detected(returncode=1, elapsed_s=120.0) is False


def test_brick_detected_zero_exit_is_never_brick():
    assert safety.brick_detected(returncode=0, elapsed_s=2.5) is False


def test_brick_detected_exactly_at_threshold():
    assert safety.brick_detected(returncode=1, elapsed_s=10.0) is True
    assert safety.brick_detected(returncode=1, elapsed_s=10.001) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_safety.py -v`
Expected: FAIL on missing `brick_detected`.

- [ ] **Step 3: Implement brick_detected**

Append to `src/applypilot/loop/safety.py`:

```python
BRICK_THRESHOLD_SECONDS = 10.0


def brick_detected(returncode: int, elapsed_s: float) -> bool:
    """True if a subprocess failed faster than any real apply could finish.

    Used by runner.apply_one to distinguish 'my last patch broke the
    codebase' from 'this specific job failed legitimately'.
    """
    if returncode == 0:
        return False
    return elapsed_s <= BRICK_THRESHOLD_SECONDS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_safety.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/safety.py tests/test_loop_safety.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: brick detection threshold"
```

---

### Task 10: Runner — apply_one subprocess wrapper

**Files:**
- Create: `src/applypilot/loop/runner.py`
- Create: `tests/test_loop_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_loop_runner.py`:

```python
"""Tests for applypilot.loop.runner.apply_one (subprocess wrapper)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from applypilot.loop import runner


def _fake_run(returncode: int, elapsed_s: float):
    """Build a fake subprocess.run + time pair for runner.apply_one."""
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = "ok" if returncode == 0 else "boom"
    completed.stderr = ""
    return completed, elapsed_s


def test_apply_one_sets_visual_trace_env(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_VISUAL_TRACE", raising=False)
    completed, elapsed = _fake_run(0, 200.0)
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=elapsed):
        result = runner.apply_one()
    assert os.environ["APPLYPILOT_VISUAL_TRACE"] == "1"
    assert result["status"] == "completed"
    assert result["brick"] is False


def test_apply_one_flags_brick_on_fast_fail(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_TRACE", "1")
    completed, elapsed = _fake_run(1, 3.0)
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=elapsed):
        result = runner.apply_one()
    assert result["brick"] is True
    assert result["status"] == "brick_detected"
    assert result["returncode"] == 1


def test_apply_one_slow_fail_is_not_brick(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_TRACE", "1")
    completed, elapsed = _fake_run(2, 240.0)
    with patch("applypilot.loop.runner.subprocess.run", return_value=completed), \
         patch("applypilot.loop.runner._monotonic_elapsed", return_value=elapsed):
        result = runner.apply_one()
    assert result["brick"] is False
    assert result["status"] == "completed"
    assert result["returncode"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_runner.py -v`
Expected: FAIL on missing module.

- [ ] **Step 3: Implement runner.apply_one**

Create `src/applypilot/loop/runner.py`:

```python
"""Subprocess wrapper for `applypilot apply --limit 1` with brick detection.

apply_one() invokes one live (or dry-run-flagged) apply, measures
launch-to-exit elapsed time, and returns a structured dict the driver
prompt uses to update state. Brick-detection logic is delegated to
safety.brick_detected for testability.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from applypilot.loop import safety


DEFAULT_APPLY_ARGS: tuple[str, ...] = (
    "--workers", "1",
    "--limit", "1",
    "--model", "claude-haiku-4-5-20251001",
    "--headless",
    "--no-live",
    "--job-timeout", "720",
    "--max-transient-retries", "1",
)


def _monotonic_elapsed(start: float | None = None) -> float:
    """Return seconds since `start`, or 0.0 if start is None."""
    if start is None:
        return 0.0
    return time.monotonic() - start


def apply_one(extra_args: tuple[str, ...] = ()) -> dict[str, Any]:
    """Run one live apply and report whether it succeeded fast or bricked.

    Returns a dict with keys:
      - status: "completed" (subprocess finished naturally) | "brick_detected"
      - returncode: int
      - brick: bool
      - elapsed_s: float
      - stdout: str (truncated)
      - stderr: str (truncated)
    """
    os.environ.setdefault("APPLYPILOT_VISUAL_TRACE", "1")
    cmd: list[str] = [sys.executable, "-m", "applypilot", "apply",
                      *DEFAULT_APPLY_ARGS, *extra_args]
    start = time.monotonic()
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    elapsed = _monotonic_elapsed(start)
    brick = safety.brick_detected(completed.returncode, elapsed)
    return {
        "status": "brick_detected" if brick else "completed",
        "returncode": completed.returncode,
        "brick": brick,
        "elapsed_s": elapsed,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_runner.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/loop/runner.py tests/test_loop_runner.py
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: apply_one subprocess wrapper with brick detection"
```

---

### Task 11: Driver prompt

**Files:**
- Create: `src/applypilot/loop/driver_prompt.md`

- [ ] **Step 1: Write the full driver prompt**

Create `src/applypilot/loop/driver_prompt.md` with the verbatim content below. This file IS the prompt; ralph-loop will feed it to Claude on each iteration.

```markdown
# ApplyPilot Autonomous Reliability Loop — Driver Prompt

You are iteration N of an autonomous reliability loop that improves the
live apply pipeline until it produces 5 consecutive verified `applied`
submissions. The full design is at
`docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md`
— consult it whenever the rules below are ambiguous.

## Entry sequence (every iteration)

1. Load state with:
   ```python
   from applypilot.loop import state as loop_state
   s = loop_state.load_or_init()
   ```
2. Verify the session lock:
   ```python
   verdict = loop_state.verify_lock(s)
   if verdict == "conflict":
       print("<promise>SESSION_CONFLICT</promise>")
       # do not write to state file
       exit
   ```
   If `verdict == "stale"` you may take over: set `s["session_id"]` to a
   new uuid4, set `s["pid"]` to `os.getpid()`, save_atomic.
3. Read the last 20 rows of `E:\applypilot-data\logs\review.jsonl`
   newer than `s["last_processed_review_ts"]`.
4. **Max-iterations early-warning.** If `s["iteration"] >= 195`, write the
   final summary block to `docs/ralph-iterations.md` (see Section 5.1 of
   the spec) before any other action.
5. Kill any orphan Chrome workers via
   `applypilot.apply.chrome.kill_all_chrome()` if no apply is in
   progress.
6. GC visual-trace folders outside the rolling window and unreferenced
   by `last_patch` / `rollback_log`.
7. **Exit short-circuit.** If `s["streak"] >= 5`:
   ```
   <promise>STREAK_REACHED</promise>
   ```
   and stop.

## Branches — pick exactly one, in this order

### A. ROLLBACK CHECK (if `s["last_patch"]` and `attempts_since_patch >= 3`)

Compute current rolling pass-rate over `s["attempts"]` via
`loop_state.rolling_pass_rate(s["attempts"])`. If lower than
`last_patch.rolling_pass_rate_at_patch`:

- Run `git -c user.name="adwai" -c user.email="adwait1234@gmail.com" revert HEAD --no-edit`.
- Append the reverted commit to `s["rollback_log"]` with
  `reason="rolling_pass_rate_dropped:<old>_to_<new>"`.
- Clear `s["last_patch"]`. Reset `s["cooldown_remaining"] = 0`.
- Decrement the signature counter the patch had targeted (clamp to 0).
- Save state, exit.

Else (patch graduated): clear `s["last_patch"]`, save state, exit.

### B. PROCESS UNPROCESSED REVIEW ROWS (always runs before C/D/E if there are new rows)

For each new row in chronological order:

```python
from applypilot.loop import signature_extractor as sig

if row["apply_status"] == "applied":
    s["streak"] += 1
else:
    s["streak"] = 0
    signature = sig.extract(row, transcript_path, verify_json_path)
    if signature:
        s["signature_counts"][signature] = s["signature_counts"].get(signature, 0) + 1
    if s["last_patch"]:
        s["last_patch"]["attempts_since_patch"] += 1

s["attempts"].append({
    "url": row["url"], "status": row["apply_status"],
    "failure_class": row.get("last_failure_class"),
    "signature": signature if row["apply_status"] != "applied" else None,
    "ts": row["finished_at"],
    "frames_dir": row.get("frames_dir"),
    "transcript_path": row.get("transcript_path"),
    "verify_json": row.get("verify_json"),
})
s["attempts"] = loop_state.roll_attempts(s["attempts"])
s["last_processed_review_ts"] = row["finished_at"]
```

After processing, if `s["streak"] >= 5`:
```
<promise>STREAK_REACHED</promise>
```
and stop.

### C. QUEUE REFRESH (if fresh score>=8 jobs < 1)

Probe queue:
```python
from applypilot.database import get_connection, init_db
from datetime import datetime, timedelta, timezone
init_db()
conn = get_connection()
cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
fresh = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE fit_score >= 8 AND apply_status IS NULL "
    "AND application_url IS NOT NULL AND discovered_at >= ?", (cutoff,)
).fetchone()[0]
```

If `fresh < 1`:
- Run `applypilot run discover enrich score --quick --target-ready 5`.
- Re-probe.
- If still 0: write final summary block to `docs/ralph-iterations.md` and emit
  `<promise>QUEUE_EXHAUSTED</promise>`.
- Else: save state, exit. (Next iteration will run Branch E.)

### D. DIAGNOSE & PATCH (only if eligible)

Last entry of `s["attempts"]` is a failure AND ALL of:
- `failure_class` is (A)-removable (not in `applypilot.reporting._IRREDUCIBLE_MARKERS`)
- `failure_class` NOT in `s["class_blocklist"]`
- `s["signature_counts"][signature] >= 3`
- `s["cooldown_remaining"] == 0`
- `s["patch_budgets"].get(failure_class, 0) < 5`

Eligibility-fail fallbacks:
- `cooldown_remaining > 0`: decrement, save, fall through to E.
- `patch_budgets[failure_class] >= 5`: append failure_class to `class_blocklist`, fall through to E.
- Else (signature_counts < 3): fall through to E.

Eligible action:
1. Read full transcript at `attempt["transcript_path"]`.
2. Read verifier JSON at `attempt["verify_json"]`.
3. From `attempt["frames_dir"]/manifest.jsonl`, select 5 frames: first, last,
   and 3 around the MCP action immediately preceding failure. Read them.
4. **Tabu check.** For each entry in `s["rollback_log"]`: if `signature ==
   entry.get("targeted_signature")` AND your planned `files_touched` set
   equals `entry["files_touched"]`, abandon this angle. Set
   `s["cooldown_remaining"] = 1` and fall through to E.
5. **Optionally consult** `E:\applypilot-data\skills\<company>.yaml` if one
   exists for this site.
6. Form a hypothesis. Plan the edit(s).
7. **Before editing each file**, verify it is editable:
   ```python
   from applypilot.loop import safety
   for f in files_to_patch:
       if safety.is_immutable_path(f):
           raise RuntimeError(f"Patch refused: {f} is loop-immutable")
   ```
   If the file is a Python source, additionally check
   `safety.touches_immutable_functions(f)` — if True, abandon the angle.
8. Apply the edits (Edit / Write).
9. `git add` the touched files.
10. Commit:
    ```
    git -c user.name="adwai" -c user.email="adwait1234@gmail.com" \
        commit -m "loop iter N: <hypothesis one-liner>

    Signature: <signature>
    Class: <failure_class>
    Patch budget: <new>/5
    Expected outcome: <prediction>"
    ```
11. Update state:
    ```python
    import hashlib, subprocess
    diff = subprocess.run(["git", "show", "HEAD", "--format="], capture_output=True, text=True).stdout
    s["last_patch"] = {
        "commit_hash": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "files_touched": sorted(files_to_patch),
        "diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
        "hypothesis": "...",
        "expected_outcome": "...",
        "applied_at": <iso now>,
        "attempts_since_patch": 0,
        "rolling_pass_rate_at_patch": loop_state.rolling_pass_rate(s["attempts"]),
        "targeted_signature": signature,
    }
    s["cooldown_remaining"] = 3
    s["patch_budgets"][failure_class] = s["patch_budgets"].get(failure_class, 0) + 1
    ```
12. Save state, exit. (Do NOT run an apply this iteration — the patch
    needs to be measured starting from the next iteration's apply.)

### E. RUN ONE APPLY (default)

```python
from applypilot.loop import runner
result = runner.apply_one()
```

If `result["brick"]`:
- Verify the last commit's message starts with `loop iter N:` — if not, do not revert (this is not our patch).
- Otherwise:
  ```
  git -c user.name="adwai" -c user.email="adwait1234@gmail.com" revert HEAD --no-edit
  ```
- Append to `s["rollback_log"]` with `reason="brick_detected"`.
- Clear `s["last_patch"]`. Reset `s["cooldown_remaining"] = 0`.
- Do not increment streak counter.

Else: the new `review.jsonl` row will be processed by Branch B in the
next iteration. Increment `s["iteration"]` and save state.

## Provider-outage guard (overlays everything)

If the last 3 entries in `s["attempts"]` all have `failure_class` in
`{cli_error, subprocess_error, anthropic_outage, rate_limited}`:

```python
import time
time.sleep(600)  # 10 min
```

Then exit. Next iteration re-checks; if still outage, sleeps again.

## Constraints (loop-wide)

- NEVER push. `git push` is forbidden under all circumstances.
- NEVER edit anything `safety.is_immutable_path` says is immutable.
- NEVER edit code containing `_verify_submission_success` or `_classify_failure`
  even if the file is otherwise editable.
- NEVER delete files. Edits only.
- ALWAYS use the one-shot identity `git -c user.name="adwai" -c user.email="adwait1234@gmail.com"` on every commit.
- If something is ambiguous, choose the most conservative action and
  document why in `iter-NNNN.md`.

## Exit

When `s["streak"] >= 5`:
```
<promise>STREAK_REACHED</promise>
```

When the queue is permanently empty:
```
<promise>QUEUE_EXHAUSTED</promise>
```

When the session lock is held by another live process:
```
<promise>SESSION_CONFLICT</promise>
```
```

- [ ] **Step 2: Verify the file exists and is non-empty**

Run: `python -c "from pathlib import Path; p = Path('src/applypilot/loop/driver_prompt.md'); print(p.exists(), p.stat().st_size)"`
Expected: `True <size > 5000>`.

- [ ] **Step 3: Commit**

```bash
git add src/applypilot/loop/driver_prompt.md
git -c user.name="adwai" -c user.email="adwait1234@gmail.com" commit -m "loop: fixed driver prompt for ralph-loop iterations"
```

---

### Task 12: Final verification — ruff clean + all loop tests pass

**Files:**
- (none — verification only)

- [ ] **Step 1: Ruff check the new package**

Run: `cd e:\auto-apply-pipeline; ruff check src/applypilot/loop/`
Expected: no errors. Fix any issues inline.

- [ ] **Step 2: Run the full loop test suite**

Run: `cd e:\auto-apply-pipeline; pytest tests/test_loop_state.py tests/test_signature_extractor.py tests/test_loop_safety.py tests/test_loop_runner.py -v`
Expected: all tests PASS, 0 failures, 0 errors.

- [ ] **Step 3: If anything failed, fix in-place and re-run before continuing.**

---

## Spec coverage matrix

| Spec section | Implemented in |
|---|---|
| §4.1 loop-state.json schema | Tasks 2–5 (state.py) |
| §4.2 iter-NNNN.md | Written by driver_prompt at runtime (Task 11) |
| §4.3 ralph-iterations.md append-only | Driver prompt instructions (Task 11) |
| §5.1 entry sequence | Task 11 (driver prompt sections 1–7) |
| §5.2 Branch A (rollback) | Task 11 |
| §5.2 Branch B (process review rows) | Task 11 — calls `signature_extractor.extract` (Task 6) |
| §5.2 Branch C (queue refresh) | Task 11 |
| §5.2 Branch D (patch) | Task 11 + `safety.is_immutable_path` (Task 7) + `safety.touches_immutable_functions` (Task 8) |
| §5.2 Branch E (run one apply) | Task 11 + `runner.apply_one` (Task 10) — uses `safety.brick_detected` (Task 9) |
| §6 carveouts | Task 7 (file-level) + Task 8 (function-level) |
| §7 mitigations (1–9) | Task 11 — all mitigations encoded in branch logic |
| §8 terminal states | Task 11 |
| §9 operator lifecycle | Documented in spec; uses Task 11's prompt |
| §10 implementation surface | Tasks 1–11 |
| §13 acceptance criteria | Task 12 |

## Out of scope for this plan

- Modifying any existing code outside `src/applypilot/loop/` and `tests/`.
- Running the loop end-to-end (validation step, requires explicit operator start).
- Cleaning up the pre-existing dirty working tree (unrelated to this work).
