"""Durable batch registry + pidfile so a UI-launched batch is never orphaned
across a server restart (constraint 7), and so the Runs page has history. One
JSON record per batch under SHARED_DIR/ui_runs/; current.pid names the active
batch. Only one batch runs at a time, globally, across all profiles — records
are tagged with the `profile` they belong to so the Runs history shows whose
batch each was. reconcile_on_start() adopts a live batch or closes a dead
one."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

ORPHANED = -999                                  # returncode sentinel for a lost batch


def _dir():
    from applypilot import config
    d = config.SHARED_DIR / "ui_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path, obj) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def pid_alive(pid: int) -> bool:
    """Windows-safe liveness. On Windows use OpenProcess via ctypes; on POSIX
    use os.kill(pid, 0)."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def open_batch(*, kind: str, dry_run: bool, args: list[str], pid: int,
               profile: str) -> dict:
    rec = {
        "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "kind": kind, "dry_run": bool(dry_run), "args": list(args), "pid": int(pid),
        "profile": profile,
        "started_at": _now(), "finished_at": None, "returncode": None, "outcome": None,
    }
    d = _dir()
    _atomic_write(d / f"{rec['id']}.json", rec)
    (d / "current.pid").write_text(rec["id"], encoding="utf-8")
    return rec


def get_batch(batch_id: str) -> dict | None:
    p = _dir() / f"{batch_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        return None


def close_batch(batch_id: str, *, returncode: int | None, outcome: dict | None) -> None:
    rec = get_batch(batch_id)
    if rec is None:
        return
    rec["finished_at"] = _now()
    rec["returncode"] = returncode
    rec["outcome"] = outcome or {}
    _atomic_write(_dir() / f"{batch_id}.json", rec)
    cur = _dir() / "current.pid"
    try:
        if cur.exists() and cur.read_text(encoding="utf-8").strip() == batch_id:
            cur.unlink()
    except OSError:
        pass


def history(*, limit: int = 50) -> list[dict]:
    recs = []
    for p in _dir().glob("*.json"):
        try:
            recs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:                        # noqa: BLE001
            continue
    recs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return recs[:limit]


def active_batch() -> dict | None:
    cur = _dir() / "current.pid"
    if not cur.exists():
        return None
    return get_batch(cur.read_text(encoding="utf-8").strip())


def reconcile_on_start() -> dict:
    """On server boot: if a batch was mid-flight, ADOPT it when its pid is still
    alive (monitor re-attaches), else close it as orphaned. Returns
    {'adopted': <rec|None>, 'orphaned': [<id>...]}. Never leaves a live batch
    untracked and never lets the server think it can start a second one."""
    active = active_batch()
    orphaned = []
    adopted = None
    if active and active.get("finished_at") is None:
        if pid_alive(active.get("pid", 0)):
            adopted = active
        else:
            close_batch(active["id"], returncode=ORPHANED, outcome={"note": "orphaned_on_restart"})
            orphaned.append(active["id"])
    return {"adopted": adopted, "orphaned": orphaned}
