# src/applypilot/webui/shared_settings.py
"""Global operator settings: one wallet, one runner. Per-profile apply caps and
batch defaults live in webui/settings.py instead. A missing or corrupt file
resolves to the SAFE side (autopilot OFF)."""
from __future__ import annotations

import json
import os
import tempfile

DEFAULT_SHARED_SETTINGS: dict = {
    "autopilot_enabled": False,       # global: one runner, one switch
    "autopilot_profiles": [],         # ordered; >1 alternates batches
    "spend_cap_usd_per_day": 5.0,     # global: one wallet
}


def _path():
    from applypilot import config
    return config.SHARED_DIR / "settings.json"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_shared_settings() -> dict:
    p = _path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:                 # noqa: BLE001 — fail-safe
        raw = {}
    return _deep_merge(DEFAULT_SHARED_SETTINGS, raw)


def _validate(s: dict) -> None:
    if float(s["spend_cap_usd_per_day"]) <= 0:
        raise ValueError("spend_cap_usd_per_day must be > 0")
    if not isinstance(s["autopilot_profiles"], list):
        raise ValueError("autopilot_profiles must be a list")


def save_shared_settings(s: dict) -> dict:
    merged = _deep_merge(DEFAULT_SHARED_SETTINGS, s or {})
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


def update_shared_settings(patch: dict) -> dict:
    return save_shared_settings(_deep_merge(load_shared_settings(), patch or {}))
