"""On-device AI bridge: the local service asks an extension page to run the model.

Chrome's built-in model (Gemini Nano, the Prompt API's ``LanguageModel``) lives in
the browser, so Python can't call it. Every AI call in the extension service goes
through ``llm_util.get_llm_client().chat(...)``. ``BridgeClient`` has that same
``chat()`` signature: it queues a job and waits for an extension page (the side
panel or the options page, running ``extension/llm_bridge.js``) to take it from
``GET /llm/next`` and answer through ``POST /llm/result``.

Nothing here touches the network or the disk; the endpoints live in server.py.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

PICKUP_TIMEOUT_S = 10.0    # a page must take the job this quickly...
ANSWER_TIMEOUT_S = 180.0   # ...and answer within this (CPU-only machines are slow)
FRESH_S = 60.0             # a page that reported "available" this recently counts as live
POLL_HOLD_S = 20.0         # how long GET /llm/next waits for a job before answering "none"
STATUSES = ("available", "downloadable", "downloading", "unavailable")


@dataclass
class Job:
    id: str
    messages: list
    temperature: float
    picked: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    text: str | None = None
    error: str | None = None


class Bridge:
    """Thread-safe queue of chat jobs plus the model status pages report."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cond = threading.Condition()
        self._queue: deque[Job] = deque()
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._status = "unavailable"
        self._status_at: float | None = None

    def report(self, status: str) -> None:
        """A page's view of LanguageModel.availability()."""
        with self._cond:
            self._status = status if status in STATUSES else "unavailable"
            self._status_at = self._clock()

    def status(self) -> str:
        with self._cond:
            if self._status_at is None or self._clock() - self._status_at > FRESH_S:
                return "unavailable"
            return self._status

    def live(self) -> bool:
        """True when a page reported the model ready within FRESH_S seconds."""
        return self.status() == "available"

    def submit(self, messages: list, temperature: float) -> Job:
        with self._cond:
            job = Job(id=str(next(self._ids)), messages=list(messages), temperature=float(temperature))
            self._queue.append(job)
            self._jobs[job.id] = job
            self._cond.notify_all()
            return job

    def next_job(self, hold_s: float = POLL_HOLD_S) -> Job | None:
        """The oldest queued job, waiting up to ``hold_s`` for one to arrive."""
        deadline = time.monotonic() + hold_s
        with self._cond:
            while not self._queue:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            job = self._queue.popleft()
            job.picked.set()
            return job

    def complete(self, job_id: str, text: str | None = None, error: str | None = None) -> bool:
        """Record a page's answer. False when the job is unknown or already done."""
        with self._cond:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        job.text, job.error = text, error
        job.done.set()
        return True

    def withdraw(self, job: Job) -> None:
        """Forget a job its caller gave up on."""
        with self._cond:
            try:
                self._queue.remove(job)
            except ValueError:
                pass
            self._jobs.pop(job.id, None)


# The service's one bridge. Looked up at call time (``llm_bridge.BRIDGE``) so
# tests can swap it with monkeypatch.
BRIDGE = Bridge()


class BridgeClient:
    """Drop-in for ``applypilot.llm.LLMClient``: same ``chat``/``ask``/``close``."""

    def __init__(self, bridge: Bridge | None = None, pickup_timeout_s: float = PICKUP_TIMEOUT_S,
                 answer_timeout_s: float = ANSWER_TIMEOUT_S) -> None:
        self._bridge = bridge if bridge is not None else BRIDGE
        self._pickup = pickup_timeout_s
        self._answer = answer_timeout_s

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096,
             response_format: dict | None = None) -> str:
        """``max_tokens`` and ``response_format`` are accepted for compatibility;
        the on-device model has no equivalent."""
        job = self._bridge.submit(messages, temperature)
        if not job.picked.wait(self._pickup):
            self._bridge.withdraw(job)
            raise RuntimeError("the on-device model didn't respond — keep the ApplyPilot side panel "
                               "or Settings page open while it works")
        if not job.done.wait(self._answer):
            self._bridge.withdraw(job)
            raise RuntimeError("the on-device model took too long to answer")
        if job.error:
            raise RuntimeError(f"on-device model error: {job.error}")
        return job.text or ""

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        pass
