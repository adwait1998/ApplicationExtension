"""Persisted operator settings for the UI control plane. Lives under APP_DIR
(NOT env vars) so autopilot state + caps survive restarts. Every read fully
defaults; a missing/corrupt file resolves to the SAFE side (autopilot OFF)."""
from __future__ import annotations

import json
import os
import tempfile

DEFAULT_SETTINGS: dict = {
    "autopilot_enabled": False,                 # constraint 3: OFF by default
    "max_live_applies_per_day": 20,             # hard cap (CONFIRMED ledger rows / rolling day)
    "batch": {
        "limit": 10,
        "model": "claude-haiku-4-5-20251001",
        "workers": 1,
        "headless": True,
        "dry_run": True,                        # a fresh install defaults to REHEARSAL
        "min_score": 8,
        "max_age_hours": 24,
        "site_contains": None,
    },
}


def _path():
    from applypilot import config
    return config.APP_DIR / "ui_settings.json"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    p = _path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:                            # noqa: BLE001 — fail-safe (invariant 8)
        raw = {}
    return _deep_merge(DEFAULT_SETTINGS, raw)


def _validate(s: dict) -> None:
    if int(s["max_live_applies_per_day"]) < 0:
        raise ValueError("max_live_applies_per_day must be >= 0")
    b = s.get("batch", {})
    if int(b.get("limit", 1)) < 1:
        raise ValueError("batch.limit must be >= 1")
    if int(b.get("workers", 1)) < 1:
        raise ValueError("batch.workers must be >= 1")


def save_settings(s: dict) -> dict:
    merged = _deep_merge(DEFAULT_SETTINGS, s or {})
    _validate(merged)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return merged


def update_settings(patch: dict) -> dict:
    return save_settings(_deep_merge(load_settings(), patch or {}))
