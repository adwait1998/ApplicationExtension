"""Answer memory: remember the answers the applicant typed themselves.

When a fill leaves questions for the human ("needs you"), the applicant
answers them on the page. On an explicit "Remember my answers" click the
extension sends those question/answer pairs here, and they join the active
profile's own answer_bank.json, so the answer-bank tier fills them next
time. Nothing is read or saved without that click.

What is never remembered (the reason is reported back per question):

* canary questions (work authorization, sponsorship, EEO, salary, address,
  ...) — those come only from the profile, and the bank would scrub them
  on load anyway;
* screening attestations (criminal record, background check, ...) — set
  once in Settings instead, where the rules for when they apply live;
* questions about one specific employer ("why do you want to work here?")
  — the answer is wrong for every other company;
* anything credential-shaped, empty, or implausibly long.

Entries this module writes carry ``"source": "you"`` so the bank can tell
the applicant's own answers apart from anything else in the file; entries
it did not write are preserved untouched.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from applypilot.apply import canary
from applypilot.extension import answers, screening

MAX_ANSWER_CHARS = 4000
_SECRET_RE = re.compile(
    r"\b(password|passwd|pwd|ssn|social\s*security|credit\s*card|cvv|cvc|api[_ -]?key|secret|"
    r"bank\s*account|routing\s*number)\b", re.I)


def normalize_question(q: str) -> str:
    q = re.sub(r"\s+", " ", str(q or "")).strip()
    q = re.sub(r"\s*\((required|optional)\)\s*$", "", q, flags=re.I)
    q = q.strip(" *:?.").strip()
    return q.lower()


def learnable(question: str, answer: str) -> tuple[bool, str]:
    q = re.sub(r"\s+", " ", str(question or "")).strip()
    a = str(answer if answer is not None else "").strip()
    if len(normalize_question(q)) < 3:
        return False, "no question text"
    if not a:
        return False, "empty answer"
    if len(a) > MAX_ANSWER_CHARS:
        return False, "answer too long to reuse"
    if _SECRET_RE.search(q):
        return False, "credential-like field — never stored"
    if canary.is_canary(q):
        return False, "comes from your profile (work authorization / EEO / salary / address ...)"
    fam = screening.family_of(q)
    if fam is not None and fam != "how_heard":
        return False, "set this once in Settings → Common screening questions"
    if answers.is_company_directed(q):
        return False, "specific to this company — would be wrong elsewhere"
    return True, ""


def _read(bank_path: Path) -> list[dict]:
    if not bank_path.exists():
        return []
    try:
        data = json.loads(bank_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write(bank_path: Path, entries: list[dict]) -> None:
    bank_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = bank_path.with_suffix(bank_path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(bank_path)


def learn(bank_path: str | Path, items: list[dict]) -> dict:
    """Save learnable {question, answer} items; latest answer to the same
    (normalized) question wins. Returns {"saved": [q...], "skipped":
    [{"question", "reason"}...]}."""
    path = Path(bank_path)
    entries = _read(path)
    saved: list[str] = []
    skipped: list[dict] = []
    now = int(time.time())
    for item in items or []:
        q = re.sub(r"\s+", " ", str((item or {}).get("question") or "")).strip()
        a = str((item or {}).get("answer") if (item or {}).get("answer") is not None else "").strip()
        ok, why = learnable(q, a)
        if not ok:
            skipped.append({"question": q, "reason": why})
            continue
        key = normalize_question(q)
        entries = [e for e in entries if normalize_question(e.get("q", "")) != key]
        entries.append({"q": q, "a": a, "source": "you", "ts": now})
        saved.append(q)
    if saved:
        _write(path, entries)
    return {"saved": saved, "skipped": skipped}


def list_answers(bank_path: str | Path) -> list[dict]:
    """Every entry in the bank, newest first, with who wrote it. Entries
    that the answer-bank tier would never serve (canary questions) are
    flagged rather than hidden, so the applicant sees the whole file."""
    out = []
    for e in _read(Path(bank_path)):
        q, a = e.get("q"), e.get("a")
        if not q or a is None:
            continue
        out.append({"question": q, "answer": a, "source": e.get("source") or "earlier run",
                    "ts": e.get("ts"), "used": not canary.is_canary(q)})
    out.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return out


def forget(bank_path: str | Path, question: str) -> bool:
    path = Path(bank_path)
    entries = _read(path)
    key = normalize_question(question)
    kept = [e for e in entries if normalize_question(e.get("q", "")) != key]
    if len(kept) == len(entries):
        return False
    _write(path, kept)
    return True
