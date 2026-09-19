"""One-shot, idempotent migration from the single-profile layout to profiles/.

Takes a full backup first. On failure it does NOT attempt a partial rollback —
the backup is the recovery path, and the error names it.

DDL-rewrite safety note: to move the four atlas tables into the shared
database we copy each table's DDL out of `sqlite_master.sql` and re-target it
at `atlas.<table>`. A naive `ddl.replace(table, f"atlas.{table}", 1)` relies on
the table name's FIRST occurrence in the DDL text being the one in
`CREATE TABLE <name> (` — which is fragile in general (a comment or another
identifier mentioning the table name earlier would silently rewrite the wrong
spot). This was checked against the four real DDLs in `database.py` (boards,
source_runs, mapping_cache, submit_endpoints as of the schema at the time of
writing): `sqlite3` strips `IF NOT EXISTS` from what it persists in
`sqlite_master.sql`, and none of the four tables' names recur earlier in their
own column comments, so the naive replace happens to be safe for all four
today. Rather than depend on that staying true, `_qualify_create_table` below
anchors the rewrite to the START of the DDL string and requires the table name
there to be a whole word, so a coincidental earlier occurrence elsewhere in
the DDL can never be mistaken for the declaration, and a DDL that doesn't
match the expected shape raises loudly instead of producing broken SQL.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ATLAS_TABLES = ("boards", "source_runs", "mapping_cache", "submit_endpoints")

# Never moved into the profile dir.
_ROOT_ONLY = {"profiles", "shared", "active_profile"}


def _qualify_create_table(ddl: str, table: str, schema: str = "atlas") -> str:
    """Rewrite a `CREATE TABLE <table> (...)` statement, as extracted verbatim
    from `sqlite_master.sql`, into `CREATE TABLE IF NOT EXISTS <schema>.<table> (...)`.

    Anchored to the start of the string with a word boundary after the table
    name, so a coincidental later (or, if the DDL were ever malformed,
    earlier-looking) occurrence of the table name elsewhere in the statement
    can't be mistaken for the declaration. Raises if the DDL doesn't have the
    expected shape rather than silently emitting invalid or misdirected SQL.
    """
    pattern = re.compile(
        r"^CREATE TABLE\s+(?:IF NOT EXISTS\s+)?" + re.escape(table) + r"\b",
        re.IGNORECASE,
    )
    new_ddl, n = pattern.subn(
        f"CREATE TABLE IF NOT EXISTS {schema}.{table}", ddl, count=1
    )
    if n == 0:
        raise RuntimeError(
            f"unexpected DDL shape while migrating table {table!r}: {ddl[:120]!r}"
        )
    return new_ddl


def _extract_atlas(db_path: Path, atlas_path: Path) -> dict:
    """Copy the atlas tables into atlas_path, then drop them from db_path."""
    atlas_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    counts: dict[str, int] = {}
    try:
        conn.execute("ATTACH DATABASE ? AS atlas", (str(atlas_path),))
        for t in ATLAS_TABLES:
            row = conn.execute(
                "SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            if not row or not row[0]:
                continue
            # Recreate the table verbatim (schema-qualified) in atlas.
            ddl = _qualify_create_table(row[0], t)
            conn.execute(ddl)
            conn.execute(f"INSERT INTO atlas.{t} SELECT * FROM main.{t}")
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM atlas.{t}").fetchone()[0]
            conn.execute(f"DROP TABLE main.{t}")
        conn.commit()
    finally:
        conn.close()
    return counts


def migrate(root: Path, *, profile_id: str = "nida") -> dict:
    """Move a legacy single-profile data root to the profiles/ layout.

    Idempotent: if `<root>/profiles/<profile_id>/profile.json` already exists,
    this is a no-op that returns {"already_migrated": True, ...} without
    touching anything. Takes a full backup of `root` BEFORE moving anything;
    the returned dict always carries the backup path (None only when already
    migrated). On any failure, no partial rollback is attempted — the
    exception names the backup directory as the recovery path.
    """
    root = Path(root)
    pdir = root / "profiles" / profile_id

    if (pdir / "profile.json").exists():
        return {"already_migrated": True, "profile_id": profile_id,
                "profile_dir": str(pdir), "backup": None, "atlas_counts": {}}

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = root.parent / f"{root.name}_backup_{stamp}"
    shutil.copytree(root, backup)

    try:
        pdir.mkdir(parents=True, exist_ok=True)
        (root / "shared").mkdir(parents=True, exist_ok=True)

        for entry in list(root.iterdir()):
            if entry.name in _ROOT_ONLY:
                continue
            shutil.move(str(entry), str(pdir / entry.name))

        atlas_counts = {}
        moved_db = pdir / "applypilot.db"
        if moved_db.exists():
            atlas_counts = _extract_atlas(moved_db, root / "shared" / "atlas.db")

        pj = pdir / "profile.json"
        if pj.exists():
            data = json.loads(pj.read_text(encoding="utf-8"))
            data["profile_id"] = profile_id
            pj.write_text(json.dumps(data, indent=2), encoding="utf-8")

        (root / "active_profile").write_text(profile_id, encoding="utf-8")
    except Exception as exc:                      # noqa: BLE001
        raise RuntimeError(
            f"migration failed ({exc}). Your data is intact at {backup} — "
            "restore from there."
        ) from exc

    return {"already_migrated": False, "profile_id": profile_id,
            "profile_dir": str(pdir), "backup": str(backup),
            "atlas_counts": atlas_counts}
