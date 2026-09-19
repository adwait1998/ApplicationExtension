"""Refuse to act on a profile whose declared identity disagrees with the
directory it was loaded from — the realistic path to a wrong-name submission."""
from __future__ import annotations

from pathlib import Path


class ProfileIdentityError(Exception):
    """profile.json's profile_id does not match its directory."""


def assert_profile_identity(profile: dict, app_dir: Path) -> None:
    app_dir = Path(app_dir)
    # Legacy single-profile layouts are not under a profiles/ parent and carry
    # no profile_id; they are exempt.
    if app_dir.parent.name != "profiles":
        return
    declared = (profile or {}).get("profile_id")
    expected = app_dir.name
    if declared != expected:
        raise ProfileIdentityError(
            f"profile.json declares profile_id={declared!r} but was loaded from "
            f"{app_dir} (expected {expected!r}). Refusing to act under an "
            "ambiguous identity."
        )
