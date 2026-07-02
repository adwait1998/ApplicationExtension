"""FastAPI backend for the ApplyPilot dashboard.

Safety contract (deliberate):
- The UI can run *safe* actions only: discover, score, prune-expired, and
  DRY-RUN applies. It can never launch a live apply — the server hard-codes
  `--dry-run` and the run whitelist. Live submissions stay a conscious,
  human-typed terminal command.
- Job-level actions (park / reset / mark-applied) are explicit single-row
  updates, never bulk.

The module avoids importing the apply machinery (playwright etc.) — it reads
the SQLite DB and logs directly, and shells out to the CLI for pipeline runs.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"


# Module-level request models: with `from __future__ import annotations`,
# closure-local models can't be resolved by FastAPI's type-hint evaluation.
class JobAction(BaseModel):
    url: str
    action: str  # park | reset | mark_applied


class RunRequest(BaseModel):
    kind: str
    source: str | None = None
    limit: int | None = None
    min_score: int | None = None

# ---------------------------------------------------------------------------
# Run manager — one whitelisted subprocess at a time
# ---------------------------------------------------------------------------

# kind -> builder(params) -> list[str] CLI args (appended to `python -m applypilot`)
def _build_run_args(kind: str, params: dict) -> list[str]:
    src = params.get("source") or ""
    limit = int(params.get("limit") or 1)
    min_score = int(params.get("min_score") or 7)
    if kind == "discover":
        args = ["run", "discover", "enrich"]
        if src:
            args += ["--source", src]
        return args
    if kind == "score":
        return ["run", "score"]
    if kind == "prune":
        return ["prune-expired", "--min-score", str(min_score)]
    if kind == "dryrun_apply":
        # --dry-run is hard-coded; the UI cannot remove it.
        return [
            "apply", "--dry-run", "--headless",
            "--limit", str(max(1, min(limit, 5))),
            "--workers", "1",
        ]
    raise ValueError(f"unknown run kind: {kind}")


ALLOWED_RUN_KINDS = ("discover", "score", "prune", "dryrun_apply")


class RunManager:
    """Spawns `python -m applypilot <args>` and buffers output for the UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self.kind: str | None = None
        self.started_at: str | None = None
        self.returncode: int | None = None
        self.lines: deque[str] = deque(maxlen=800)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, kind: str, params: dict) -> None:
        if kind not in ALLOWED_RUN_KINDS:
            raise ValueError(f"run kind not allowed: {kind}")
        with self._lock:
            if self.running:
                raise RuntimeError("a run is already in progress")
            args = _build_run_args(kind, params)
            cmd = [sys.executable, "-m", "applypilot", *args]
            self.kind = kind
            self.returncode = None
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.lines.clear()
            self.lines.append(f"$ {' '.join(cmd)}")
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self.lines.append(line.rstrip("\n"))
        proc.wait()
        self.returncode = proc.returncode
        self.lines.append(f"[exit code {proc.returncode}]")

    def stop(self) -> None:
        with self._lock:
            if self.running and self._proc is not None:
                self._proc.terminate()
                self.lines.append("[terminated by user]")

    def status(self) -> dict:
        return {
            "running": self.running,
            "kind": self.kind,
            "started_at": self.started_at,
            "returncode": self.returncode,
            "lines": list(self.lines)[-400:],
        }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_JOB_COLS = (
    "url, title, site, location, fit_score, apply_status, apply_error, "
    "last_failure_class, discovered_at, applied_at, application_url, "
    "verification_confidence, apply_attempts, skill_used, score_reasoning"
)


def _row_to_job(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _age_bucket(discovered_at: str | None, now: datetime) -> str:
    if not discovered_at:
        return "unknown"
    try:
        d = datetime.fromisoformat(discovered_at.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    days = (now - d).days
    if days <= 1:
        return "0-1d"
    if days <= 3:
        return "2-3d"
    if days <= 7:
        return "4-7d"
    if days <= 14:
        return "8-14d"
    return ">14d"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(db_path: Path | None = None, app_dir: Path | None = None) -> FastAPI:
    if db_path is None or app_dir is None:
        from applypilot import config

        db_path = db_path or config.DB_PATH
        app_dir = app_dir or config.APP_DIR
    review_log = Path(app_dir) / "logs" / "review.jsonl"

    app = FastAPI(title="ApplyPilot Dashboard", docs_url=None, redoc_url=None)
    runs = RunManager()
    app.state.runs = runs  # exposed for tests

    # -- static -------------------------------------------------------------
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # -- summary ------------------------------------------------------------
    @app.get("/api/summary")
    def summary() -> dict:
        now = datetime.now(timezone.utc)
        conn = _connect(db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            by_status = dict(
                conn.execute(
                    "SELECT COALESCE(apply_status,'queued'), COUNT(*) FROM jobs GROUP BY 1"
                ).fetchall()
            )
            scores = dict(
                conn.execute(
                    "SELECT fit_score, COUNT(*) FROM jobs WHERE fit_score >= 7 GROUP BY 1"
                ).fetchall()
            )
            eligible = conn.execute(
                """
                SELECT discovered_at FROM jobs
                WHERE fit_score >= 8 AND application_url IS NOT NULL
                  AND (apply_status IS NULL OR apply_status='failed')
                  AND applied_at IS NULL
                """
            ).fetchall()
            freshness = Counter(_age_bucket(r[0], now) for r in eligible)
            applied_total = by_status.get("applied", 0)
            needs_review = by_status.get("needs_review", 0)
        finally:
            conn.close()

        # recent attempts from review.jsonl
        attempts: list[dict] = []
        if review_log.exists():
            with review_log.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            attempts.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        recent = attempts[-100:]
        statuses = Counter(str(a.get("status", "?")) for a in recent)

        def _fail_key(a: dict) -> str:
            # Older review.jsonl rows lack last_failure_class — fall back to
            # the status suffix ("needs_review:unverified_submission" → the part
            # after the colon) so the chart doesn't collapse into one "-" bar.
            fc = a.get("last_failure_class") or a.get("failure_class")
            if fc:
                return str(fc)
            st = str(a.get("status", ""))
            return st.split(":", 1)[1] if ":" in st else (st or "-")

        fails = Counter(
            _fail_key(a)
            for a in recent
            if not str(a.get("status", "")).startswith(("applied", "dry_run:applied"))
        )
        last_attempt_ts = attempts[-1].get("ts") if attempts else None

        return {
            "generated_at": now.isoformat(),
            "total_jobs": total,
            "by_status": by_status,
            "score_dist": scores,
            "eligible_count": len(eligible),
            "eligible_freshness": dict(freshness),
            "applied_total": applied_total,
            "needs_review": needs_review,
            "recent_attempts": {
                "count": len(recent),
                "statuses": dict(statuses),
                "failure_classes": dict(fails.most_common(10)),
                "last_ts": last_attempt_ts,
            },
        }

    # -- jobs list ------------------------------------------------------------
    @app.get("/api/jobs")
    def jobs(view: str = "eligible", q: str = "", limit: int = 200) -> JSONResponse:
        limit = max(1, min(limit, 1000))
        where = {
            "eligible": (
                "fit_score >= 8 AND application_url IS NOT NULL "
                "AND (apply_status IS NULL OR apply_status='failed') AND applied_at IS NULL"
            ),
            "needs_review": "apply_status = 'needs_review'",
            "applied": "apply_status = 'applied'",
            "failed": "apply_status IN ('failed','expired')",
            "all": "1=1",
        }.get(view)
        if where is None:
            raise HTTPException(400, "invalid view")
        sql = f"SELECT {_JOB_COLS} FROM jobs WHERE {where}"
        params: list = []
        if q:
            sql += " AND (title LIKE ? OR site LIKE ? OR location LIKE ?)"
            like = f"%{q}%"
            params += [like, like, like]
        sql += " ORDER BY fit_score DESC, COALESCE(applied_at, discovered_at) DESC LIMIT ?"
        params.append(limit)
        conn = _connect(db_path)
        try:
            rows = [_row_to_job(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
        return JSONResponse({"view": view, "count": len(rows), "jobs": rows})

    # -- job actions ----------------------------------------------------------
    @app.post("/api/job/action")
    def job_action(body: JobAction) -> dict:
        updates = {
            "park": (
                "UPDATE jobs SET apply_status='manual', "
                "apply_error='parked_via_ui' WHERE url=?"
            ),
            "reset": (
                "UPDATE jobs SET apply_status=NULL, apply_error=NULL, "
                "apply_attempts=0, last_failure_class=NULL WHERE url=? "
                "AND apply_status != 'applied'"
            ),
            "mark_applied": (
                "UPDATE jobs SET apply_status='applied', "
                "applied_at=COALESCE(applied_at, datetime('now')), "
                "apply_error=NULL WHERE url=?"
            ),
        }
        sql = updates.get(body.action)
        if sql is None:
            raise HTTPException(400, "invalid action")
        conn = _connect(db_path)
        try:
            cur = conn.execute(sql, (body.url,))
            conn.commit()
            changed = cur.rowcount
        finally:
            conn.close()
        if changed == 0:
            raise HTTPException(404, "job not found (or action not applicable)")
        return {"ok": True, "action": body.action, "url": body.url}

    # -- attempts feed ----------------------------------------------------------
    @app.get("/api/attempts")
    def attempts_feed(limit: int = 50) -> dict:
        limit = max(1, min(limit, 500))
        rows: list[dict] = []
        if review_log.exists():
            with review_log.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        return {"attempts": rows[-limit:][::-1]}

    # -- runs ----------------------------------------------------------------
    @app.post("/api/run")
    def start_run(body: RunRequest) -> dict:
        try:
            runs.start(body.kind, body.model_dump())
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "kind": body.kind}

    @app.get("/api/run/status")
    def run_status() -> dict:
        return runs.status()

    @app.post("/api/run/stop")
    def run_stop() -> dict:
        runs.stop()
        time.sleep(0.2)
        return runs.status()

    return app
