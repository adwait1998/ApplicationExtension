"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error,
                    application_url_source, application_url_resolved_at,
                    application_url_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Gate (v2 spine) — set at ingest; NULL = not yet gated.
            identity_id           TEXT,
            ats                   TEXT,
            board_token           TEXT,
            ats_job_id            TEXT,
            gate_result           TEXT,
            gate_reasons          TEXT,
            automatability        TEXT,
            gate_version          INTEGER,
            gated_at              TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,
            application_url_source TEXT,
            application_url_resolved_at TEXT,
            application_url_error TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence REAL,
            apply_result_json     TEXT,
            verification_evidence_json TEXT,
            idempotency_key       TEXT,
            submit_attempt_count  INTEGER DEFAULT 0,
            checkpoint_json       TEXT,
            last_failure_class    TEXT,

            -- Skill Playbook v1 (Phase 4)
            skill_used            TEXT,
            replay_duration_ms    INTEGER,
            patch_duration_ms     INTEGER
        )
    """)

    # Durable two-phase submission ledger (Phase 1 Task 10A). SECOND table:
    # ensure_columns/_ALL_COLUMNS only migrate the jobs table, so this must be
    # created here. init_db runs CREATE TABLE IF NOT EXISTS and is called on
    # every apply invocation (cli._bootstrap -> init_db), so live DBs get it.
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sub_ledger_identity "
        "ON submission_ledger(identity_id, state)"
    )

    # Engine control kv (Phase 1 Task 11b): single pause flag consumed by the
    # worker loop. Also a SECOND table (not in the jobs migration path), so
    # created here alongside submission_ledger.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS engine_control "
        "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )

    # Board Atlas (v2 Phase 2). Profile-AGNOSTIC registry of ATS boards:
    # one row per (ats, token). status/ring drive the freshness scheduler;
    # job_id_set_hash powers the incremental poller's cheap unchanged-board
    # short-circuit. Created here (standalone table, not in _ALL_COLUMNS).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS boards (
            ats               TEXT NOT NULL,     -- 'greenhouse' | 'lever' | 'ashby'
            token             TEXT NOT NULL,     -- board slug, lowercased
            company_name      TEXT,              -- best-effort display name
            status            TEXT NOT NULL DEFAULT 'candidate',
                                                 -- 'candidate' (unvalidated) | 'active'
                                                 -- | 'dead' (validation/poll failing)
            ring              INTEGER,           -- 0 hot | 1 warm | 2 cold | NULL unassigned
            first_seen        TEXT NOT NULL,
            last_checked      TEXT,              -- last successful poll/validation
            last_changed      TEXT,              -- last time job_id_set_hash changed
            job_id_set_hash   TEXT,              -- sha1 of sorted job-id set (incremental diff)
            job_count         INTEGER,           -- ids seen at last poll (posting-rate signal)
            new_last_poll     INTEGER DEFAULT 0, -- new ids at last poll (posting-rate signal)
            error_streak      INTEGER NOT NULL DEFAULT 0,
            source            TEXT,              -- 'yaml_import' | 'db_mining' | 'cc_snapshot' | 'search'
            PRIMARY KEY (ats, token)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_boards_ring_status "
        "ON boards(ring, status)"
    )

    # Per-source run accounting (v2 Phase 2, spec §7.2). One row per Atlas tick
    # (or per source per tick). Requests/yield/cost join down-funnel so zero-yield
    # sources demote automatically. Standalone table (not in _ALL_COLUMNS).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_runs (
            run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,        -- 'atlas' | 'atlas:greenhouse' | ...
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            boards_polled  INTEGER DEFAULT 0,
            requests       INTEGER DEFAULT 0,
            jobs_seen      INTEGER DEFAULT 0,
            jobs_new       INTEGER DEFAULT 0,
            jobs_eligible  INTEGER DEFAULT 0,
            cost_usd       REAL DEFAULT 0.0,
            error          TEXT
        )
    """)

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
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    ensure_indexes(conn)

    return conn


# ---------------------------------------------------------------------------
# Engine control (Phase 1 Task 11b): single pause flag. The worker loop checks
# paused_reason before dispatch; `applypilot resume` clears it.
# ---------------------------------------------------------------------------

def set_paused(conn, reason: str | None) -> None:
    """Set or clear the engine pause flag. reason=None clears it."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if reason is None:
        conn.execute("DELETE FROM engine_control WHERE key='paused'")
    else:
        conn.execute(
            "INSERT INTO engine_control (key, value, updated_at) VALUES ('paused', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (reason, now))
    conn.commit()


def paused_reason(conn) -> str | None:
    row = conn.execute("SELECT value FROM engine_control WHERE key='paused'").fetchone()
    return row[0] if row else None


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
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
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    "application_url_source": "TEXT",
    "application_url_resolved_at": "TEXT",
    "application_url_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "REAL",
    "apply_result_json": "TEXT",
    "verification_evidence_json": "TEXT",
    "idempotency_key": "TEXT",
    "submit_attempt_count": "INTEGER DEFAULT 0",
    "checkpoint_json": "TEXT",
    "last_failure_class": "TEXT",
    # Skill Playbook v1 (Phase 4) — additive, NULL when legacy run_job path was used.
    "skill_used": "TEXT",
    "replay_duration_ms": "INTEGER",
    "patch_duration_ms": "INTEGER",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def ensure_indexes(conn: sqlite3.Connection | None = None) -> None:
    """Create apply-path indexes used by the robust launcher flow."""
    if conn is None:
        conn = get_connection()

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_apply_queue "
        "ON jobs(apply_status, fit_score, apply_attempts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_idempotency_key "
        "ON jobs(idempotency_key)"
    )
    conn.commit()


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = _count_applyable_jobs(conn, min_score=7)

    return stats


_INVALID_URL_TEXT = {"", "none", "nan", "nat", "null"}


def _valid_http_url(value) -> str | None:
    """Return a usable HTTP(S) URL, ignoring common stringified nulls."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _INVALID_URL_TEXT:
        return None
    if not text.lower().startswith(("http://", "https://")):
        return None
    return text


def _effective_apply_url(row: dict) -> str | None:
    return _valid_http_url(row.get("application_url")) or _valid_http_url(row.get("url"))


def queue_policy(*, min_score: int = 8, max_age_hours: int | None = None,
                 include_attempt_cap: bool = True) -> tuple[str, list]:
    """The single SQL predicate for 'may this job be applied to'. Returns a
    WHERE-fragment + ordered params for callers to embed in their own SELECT.
    v2 rule: only gated-eligible + auto-automatable rows are ever queue-visible.

    NOTE: Python-level filters (config.is_manual_ats on resolved URLs, live
    location re-check) stay in the callers — this covers the SQL-expressible
    predicate only. Param order is documented and load-bearing: min_score,
    [max_apply_attempts], [age cutoff]."""
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


def _count_applyable_jobs(conn: sqlite3.Connection, min_score: int = 7) -> int:
    """Count jobs the apply runner can actually open automatically.

    Routed through queue_policy() (Task 12) so the count reflects the SAME
    gated-eligible + auto + attempt-cap predicate the launcher acquires by;
    the Python is_manual_ats pass then removes rows whose resolved URL is a
    manual ATS (that filter is not SQL-expressible)."""
    from applypilot import config

    frag, params = queue_policy(min_score=min_score)
    rows = conn.execute(
        f"SELECT url, application_url FROM jobs WHERE {frag}", params
    ).fetchall()
    ready = 0
    for row in rows:
        row_dict = dict(row)
        apply_url = _effective_apply_url(row_dict)
        if apply_url and not config.is_manual_ats(apply_url):
            ready += 1
    return ready


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


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
    Mirrors store_jobs' INSERT+IntegrityError dedup on the url PK. Jobs are
    ALWAYS stored regardless of verdict (audit + retroactive re-gate)."""
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
             # An explicit full_description key wins even when None (thin rows
             # must await enrichment, not become scoreable on the snippet).
             job.get("full_description") if "full_description" in job else job.get("description"),
             job.get("application_url") or url, job.get("location"), job.get("site"),
             # discovered_at must never be NULL; detail_scraped_at MUST stay NULL
             # for thin rows so the enrich stage still picks them up.
             strategy, job.get("posted_at") or now, job.get("detail_scraped_at"),
             job.get("detail_error")] + _gate_values(gate),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def update_gate(conn, url: str, gate: dict) -> None:
    """Re-stamp an existing row's gate columns (catch-up 'gate' stage and
    'gate --rerun'). Does NOT touch discovery/enrichment columns."""
    conn.execute(
        f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in _GATE_COLS)} WHERE url = ?",
        _gate_values(gate) + [url],
    )
    conn.commit()


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    # "pending_apply" is a queue-visibility (may-apply) stage — route it
    # through the single queue_policy() predicate (Task 12) so it enforces the
    # SAME gated-eligible + auto + attempt-cap rule as acquire_job, instead of
    # the old fit_score-only WHERE that overstated the queue. queue_policy
    # returns its own ordered params, so pending_apply skips the generic
    # min_score/`?` handling below and injects them directly.
    if stage == "pending_apply":
        where, params = queue_policy(min_score=min_score if min_score is not None else 7)
    else:
        conditions = {
            "discovered": "1=1",
            "pending_detail": "detail_scraped_at IS NULL",
            "enriched": "full_description IS NOT NULL",
            # Ungated OR gated before enrichment finished (thin->full description):
            # the gate stage re-gates so sponsorship/location verdicts use the full text.
            "pending_gate": (
                "(gated_at IS NULL "
                "OR (detail_scraped_at IS NOT NULL AND gated_at IS NOT NULL AND detail_scraped_at > gated_at))"
            ),
            "pending_score": (
                "full_description IS NOT NULL AND fit_score IS NULL "
                "AND gated_at IS NOT NULL AND gate_result = 'eligible' "
                "AND (detail_scraped_at IS NULL OR gated_at >= detail_scraped_at)"
            ),
            "scored": "fit_score IS NOT NULL",
            "pending_tailor": (
                "fit_score >= ? AND full_description IS NOT NULL "
                "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
            ),
            "tailored": "tailored_resume_path IS NOT NULL",
            "applied": "applied_at IS NOT NULL",
        }

        where = conditions.get(stage, "1=1")
        params = []

        if "?" in where and min_score is not None:
            params.append(min_score)
        elif "?" in where:
            params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        result = [dict(zip(columns, row)) for row in rows]
    else:
        result = []

    if stage == "pending_apply":
        from applypilot import config
        result = [
            row for row in result
            if (apply_url := _effective_apply_url(row))
            and not config.is_manual_ats(apply_url)
        ]
    return result
