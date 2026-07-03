"""One-shot submit tickets, file-backed so the separate stream-MCP process
sees the same state. A submission may proceed ONLY while an unconsumed,
unexpired ticket exists for the current job identity. Dry-run never opens a
ticket - submit is structurally impossible, not prompt-forbidden. Keyed by
identity_id (the ats:token:job_id string), never the sha256 idempotency key."""
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
        # atomic write so a concurrent reader never sees a half-written ticket
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

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
