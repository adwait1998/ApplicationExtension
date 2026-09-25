"""A local log of the applications the Copilot helped fill.

One JSON line per fill in the active profile's own directory
(application_log.jsonl): when, which page, which job (title/company when
known), and how the fill went (counts only — never field values). The
extension cannot know whether the applicant then pressed Submit, so every
entry starts as "filled" and the applicant marks it "applied" (or
"skipped") themselves. Exportable as CSV. Nothing leaves the machine.
"""
from __future__ import annotations

import csv
import io
import json
import time
import uuid
from pathlib import Path

LOG_NAME = "application_log.jsonl"
STATUSES = ("filled", "applied", "skipped")
_COUNT_KEYS = ("filled", "drafts", "needs_you", "failed", "unreadable")


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id"):
            out.append(row)
    return out


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def record(path: Path, url: str, title: str = "", company: str = "", counts: dict | None = None,
           now: float | None = None) -> dict:
    """Add a fill. A second fill of the same page within the hour updates
    that entry rather than adding a duplicate (re-running Fill, step 2 of a
    multi-step form)."""
    path = Path(path)
    now = time.time() if now is None else now
    counts = {k: int((counts or {}).get(k) or 0) for k in _COUNT_KEYS}
    rows = _read(path)
    for row in reversed(rows):
        if row.get("url") == url and now - row.get("updated", 0) < 3600:
            row.update(updated=now, counts=counts, fills=row.get("fills", 1) + 1)
            if title and not row.get("title"):
                row["title"] = title
            if company and not row.get("company"):
                row["company"] = company
            _write(path, rows)
            return row
    row = {"id": uuid.uuid4().hex[:12], "url": url, "title": title, "company": company,
           "created": now, "updated": now, "status": "filled", "counts": counts, "fills": 1}
    rows.append(row)
    _write(path, rows)
    return row


def entries(path: Path) -> list[dict]:
    return sorted(_read(Path(path)), key=lambda r: r.get("updated", 0), reverse=True)


def set_status(path: Path, entry_id: str, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    rows = _read(Path(path))
    for row in rows:
        if row.get("id") == entry_id:
            row["status"] = status
            row["updated"] = time.time()
            _write(Path(path), rows)
            return True
    return False


def to_csv(path: Path) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "status", "company", "title", "url", "filled", "drafts", "needs_you", "failed"])
    for r in entries(path):
        c = r.get("counts", {})
        w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("created", 0))), r.get("status", ""),
                    r.get("company", ""), r.get("title", ""), r.get("url", ""), c.get("filled", 0),
                    c.get("drafts", 0), c.get("needs_you", 0), c.get("failed", 0)])
    return buf.getvalue()
