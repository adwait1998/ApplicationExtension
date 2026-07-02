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
