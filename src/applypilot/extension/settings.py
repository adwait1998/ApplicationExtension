"""Persisted tier-toggle settings for the extension service.

Lets the operator flip tiers 5/6 (answer bank / draft) and the draft cap
from the extension's own UI (GET/POST /settings in server.py) instead of
exporting environment variables by hand.

Precedence, highest first -- mirrored by ``answers.answers_enabled()`` /
``answers.drafts_enabled()`` / ``answers._max_draft_calls()``, which all
delegate to :func:`effective_settings`:

1. An explicit env var (``APPLYPILOT_ANSWERS`` / ``APPLYPILOT_DRAFTS`` /
   ``APPLYPILOT_MAX_DRAFTS``), if set at all -- existing scripts and tests
   that rely on env-var control keep working exactly as before, unchanged.
2. The persisted settings file (``extension_settings.json`` under the
   caller's ``app_dir``).
3. Built-in defaults -- see ``DEFAULT_SETTINGS``. Tier 5 (answer bank)
   defaults ON: its hits are either profile-derived seeds or the
   operator's own past answers, both model-free, so there is no reason to
   hide them behind an opt-in. Tier 6 (draft) defaults OFF: it puts
   model-written text under a real person's name and must stay opt-in.

Deliberately ``app_dir``-scoped rather than defaulting to
``config.APP_DIR`` internally -- this mirrors server.py's own
``token_path`` / ``get_or_create_token``, which also always takes an
explicit ``app_dir``. A caller that wants the real, persisted, machine-wide
file passes ``config.APP_DIR`` explicitly (server.py's ``create_app``
does, via its own ``app_dir`` parameter). A caller that passes ``None``
(every pre-existing zero-arg call site: resolve.py, the whole pre-existing
test suite) gets pure in-memory defaults and never touches disk -- this is
what keeps those call sites $0/offline exactly as before.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SETTINGS_FILENAME = "extension_settings.json"

DEFAULT_SETTINGS: dict = {
    "answers_enabled": True,
    "drafts_enabled": False,
    "max_drafts": 5,
}

ANSWERS_ENV = "APPLYPILOT_ANSWERS"
DRAFTS_ENV = "APPLYPILOT_DRAFTS"
MAX_DRAFTS_ENV = "APPLYPILOT_MAX_DRAFTS"


class InvalidSettings(ValueError):
    """A POST /settings body failed validation -- server.py turns this into
    a 422, never a stack trace."""


def settings_path(app_dir: str | Path) -> Path:
    return Path(app_dir) / SETTINGS_FILENAME


def load_settings(app_dir: str | Path | None = None) -> dict:
    """The persisted settings, repaired against ``DEFAULT_SETTINGS`` for
    any missing/invalid key. Never raises -- a missing or corrupt file (or
    ``app_dir=None``) reads as the defaults, exactly like the fail-safe
    profile reads elsewhere in this package. No disk I/O at all when
    ``app_dir`` is None.
    """
    out = dict(DEFAULT_SETTINGS)
    if app_dir is None:
        return out
    path = settings_path(app_dir)
    try:
        if not path.exists():
            return out
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001 -- fail-safe, mirrors server.py's profile reads
        return out
    if not isinstance(raw, dict):
        return out
    if isinstance(raw.get("answers_enabled"), bool):
        out["answers_enabled"] = raw["answers_enabled"]
    if isinstance(raw.get("drafts_enabled"), bool):
        out["drafts_enabled"] = raw["drafts_enabled"]
    md = raw.get("max_drafts")
    if isinstance(md, int) and not isinstance(md, bool) and md >= 0:
        out["max_drafts"] = md
    return out


def save_settings(app_dir: str | Path, updates: dict) -> dict:
    """Validate and merge ``updates`` (any subset of the three keys) over
    the persisted settings, write atomically (tempfile + os.replace,
    mirrors server.py's ``_backup_and_write_profile``), and return the
    resulting full settings dict. Raises ``InvalidSettings`` -- never a
    raw exception -- for a bad value.
    """
    if not isinstance(updates, dict):
        raise InvalidSettings("settings body must be a JSON object")

    current = load_settings(app_dir)

    if "answers_enabled" in updates:
        if not isinstance(updates["answers_enabled"], bool):
            raise InvalidSettings("answers_enabled must be a boolean")
        current["answers_enabled"] = updates["answers_enabled"]

    if "drafts_enabled" in updates:
        if not isinstance(updates["drafts_enabled"], bool):
            raise InvalidSettings("drafts_enabled must be a boolean")
        current["drafts_enabled"] = updates["drafts_enabled"]

    if "max_drafts" in updates:
        md = updates["max_drafts"]
        if isinstance(md, bool) or not isinstance(md, int) or md < 0:
            raise InvalidSettings("max_drafts must be a non-negative integer")
        current["max_drafts"] = md

    path = settings_path(app_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return current


def _env_bool_override(name: str) -> bool | None:
    """None when the var is unset -- distinct from an explicit falsy
    value -- so effective_settings() can tell "no opinion" (fall through
    to the settings file) apart from "explicitly turned off"."""
    if name not in os.environ:
        return None
    val = os.environ.get(name, "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


def env_override_answers() -> bool | None:
    return _env_bool_override(ANSWERS_ENV)


def env_override_drafts() -> bool | None:
    return _env_bool_override(DRAFTS_ENV)


def env_override_max_drafts() -> int | None:
    raw = os.environ.get(MAX_DRAFTS_ENV, "")
    if raw == "":
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n >= 0 else None


def effective_settings(app_dir: str | Path | None = None) -> dict:
    """The settings actually in effect right now: env var overrides (if
    set) applied on top of the persisted file (or defaults, when
    ``app_dir`` is None / no file yet exists).
    """
    base = load_settings(app_dir)

    answers_env = env_override_answers()
    if answers_env is not None:
        base["answers_enabled"] = answers_env

    drafts_env = env_override_drafts()
    if drafts_env is not None:
        base["drafts_enabled"] = drafts_env

    max_drafts_env = env_override_max_drafts()
    if max_drafts_env is not None:
        base["max_drafts"] = max_drafts_env

    # Drafts require answers regardless of which source turned each on --
    # same invariant answers.py has always enforced.
    if not base["answers_enabled"]:
        base["drafts_enabled"] = False

    return base
