"""Enable `python -m applypilot`.

The bound profile MUST be resolved before applypilot.cli is imported, because
applypilot.config computes its path constants at import time.
"""

import os
import sys


def bind_profile(argv: list[str] | None = None) -> None:
    """Point APPLYPILOT_DIR at the resolved profile directory.

    No-op when APPLYPILOT_DIR is already set (the UI and tests set it on purpose)
    or when the root is a legacy single-profile layout.
    """
    from applypilot import profiles

    if os.environ.get("APPLYPILOT_DIR"):
        return

    root = profiles.data_root()
    if profiles.is_legacy_layout(root):
        os.environ["APPLYPILOT_DIR"] = str(root)
        return

    pid = profiles.resolve(root, argv if argv is not None else sys.argv[1:])
    os.environ["APPLYPILOT_DIR"] = str(profiles.profile_dir(root, pid))
    os.environ["APPLYPILOT_PROFILE"] = pid


def main() -> None:
    from applypilot import profiles
    try:
        bind_profile(sys.argv[1:])
    except profiles.ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    from applypilot.cli import app
    app()


if __name__ == "__main__":
    main()
