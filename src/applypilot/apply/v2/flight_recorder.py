"""Flight recorder (spec §11): per-attempt bundle = form IR + per-field
provenance/CommitResults + network log + captured real DOM + per-phase timings.
The bundle is what 'applypilot fixtures promote' turns into a CI regression, so
recorded REAL DOM finally replaces synthetic-only tests. Storage-capped by the
caller's retention policy (90-day artifacts, rows forever).

Privacy invariant 3 applies here too: `provenance` is a BINDING string
(profile.<path> | answer:<qfp> | policy.<k> | oracle), NEVER the literal answer
text or a profile value. The recorder is pure file I/O — no browser, no DB, no
LLM — so recording can never touch the apply outcome (the caller wraps commit()
in its own exception guard so a write failure never changes a submit result)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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
    """Accumulate one apply attempt's telemetry, then dump ONE json bundle.

    Lifecycle:
      rec = FlightRecorder(run_dir=..., job_url=..., ats=..., company=...)
      rec.set_schema(schema)                 # for template/questions fingerprints
      rec.record_field(...)                  # once per resolved/attempted field
      rec.record_network(method, url, code)  # once per observed request
      rec.record_phase(phase, ms)            # per-phase timing (parse/fill/...)
      rec.set_dom(captured_html)             # the real DOM (NON-hot-path only)
      path = rec.commit(status=...)          # write <company>_<utc>.json
    """

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
