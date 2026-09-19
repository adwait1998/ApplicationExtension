"""Root + profile resolution.

Imported by __main__.py BEFORE applypilot.config, so this module must import
nothing from applypilot — standard library only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

ACTIVE_FILE = "active_profile"
PROFILES_DIRNAME = "profiles"
SHARED_DIRNAME = "shared"


class ProfileError(Exception):
    """Profile could not be resolved. The message is shown to the operator."""


def is_valid_id(pid: str) -> bool:
    return bool(pid) and isinstance(pid, str) and bool(_ID_RE.match(pid))


def data_root() -> Path:
    """The data ROOT holding shared/ and profiles/.

    APPLYPILOT_ROOT wins; otherwise fall back to APPLYPILOT_DIR so an existing
    installation keeps its location; otherwise ~/.applypilot.
    """
    for var in ("APPLYPILOT_ROOT", "APPLYPILOT_DIR"):
        val = os.environ.get(var)
        if val:
            return Path(val)
    return Path.home() / ".applypilot"


def is_legacy_layout(root: Path) -> bool:
    """True when `root` is a single-profile data dir (a profile.json sits in it,
    or it has no profiles/ subdir). Legacy dirs bypass profile resolution, which
    is what the existing test suite relies on."""
    root = Path(root)
    if (root / "profile.json").exists():
        return True
    return not (root / PROFILES_DIRNAME).is_dir()


def profiles_dir(root: Path) -> Path:
    return Path(root) / PROFILES_DIRNAME


def shared_dir(root: Path) -> Path:
    return Path(root) / SHARED_DIRNAME


def profile_dir(root: Path, pid: str) -> Path:
    if not is_valid_id(pid):
        raise ValueError(f"invalid profile id: {pid!r}")
    return profiles_dir(root) / pid


def list_profiles(root: Path) -> list[str]:
    d = profiles_dir(root)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and is_valid_id(p.name))


def get_active(root: Path) -> str | None:
    p = Path(root) / ACTIVE_FILE
    try:
        pid = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pid if is_valid_id(pid) and pid in list_profiles(root) else None


def set_active(root: Path, pid: str) -> None:
    if pid not in list_profiles(root):
        raise ValueError(f"unknown profile: {pid!r}")
    p = Path(root) / ACTIVE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pid, encoding="utf-8")


def profile_from_argv(argv: list[str]) -> str | None:
    """Minimal scan for --profile. Tolerates `--profile X` and `--profile=X`.
    Deliberately does not validate — resolve() reports the error with context."""
    for i, a in enumerate(argv):
        if a == "--profile":
            return argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith("--profile="):
            return a.split("=", 1)[1] or None
    return None


def resolve(root: Path, argv: list[str] | None = None) -> str:
    """Resolve the bound profile: flag > env > active file > sole profile."""
    argv = list(argv if argv is not None else [])
    available = list_profiles(root)

    requested = profile_from_argv(argv) or os.environ.get("APPLYPILOT_PROFILE") or None
    if requested:
        if requested not in available:
            raise ProfileError(
                f"unknown profile {requested!r}. Available: {', '.join(available) or '(none)'}"
            )
        return requested

    active = get_active(root)
    if active:
        return active
    if len(available) == 1:
        return available[0]
    if not available:
        raise ProfileError(
            f"no profiles found under {profiles_dir(root)}. "
            "Run `applypilot profile migrate` or `applypilot profile add <id>`."
        )
    raise ProfileError(
        "multiple profiles and no active one selected: "
        f"{', '.join(available)}. Use --profile <id> or `applypilot profile use <id>`."
    )
