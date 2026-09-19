import json
import os
import subprocess
import sys
import textwrap

SNIPPET = textwrap.dedent("""
    import json, sys
    import applypilot.__main__ as m
    m.bind_profile(sys.argv[1:])
    from applypilot import config
    print(json.dumps({"app_dir": str(config.APP_DIR), "db": str(config.DB_PATH)}))
""")


def _run(tmp_path, argv, env_extra):
    env = dict(os.environ)
    env.pop("APPLYPILOT_PROFILE", None)
    env["APPLYPILOT_ROOT"] = str(tmp_path)
    env.pop("APPLYPILOT_DIR", None)
    env.update(env_extra)
    script = tmp_path / "snip.py"
    script.write_text(SNIPPET, encoding="utf-8")
    out = subprocess.run([sys.executable, str(script), *argv], env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_flag_binds_app_dir_to_profile(tmp_path):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    got = _run(tmp_path, ["--profile", "adwait"], {})
    assert got["app_dir"] == str(tmp_path / "profiles" / "adwait")
    assert got["db"] == str(tmp_path / "profiles" / "adwait" / "applypilot.db")


def test_env_binds_app_dir(tmp_path):
    for p in ("nida", "adwait"):
        (tmp_path / "profiles" / p).mkdir(parents=True)
    got = _run(tmp_path, [], {"APPLYPILOT_PROFILE": "nida"})
    assert got["app_dir"] == str(tmp_path / "profiles" / "nida")


def test_legacy_dir_is_used_directly(tmp_path):
    """An explicit APPLYPILOT_DIR containing profile.json bypasses resolution.
    This is what the existing suite relies on."""
    (tmp_path / "profile.json").write_text("{}", encoding="utf-8")
    got = _run(tmp_path, [], {"APPLYPILOT_DIR": str(tmp_path)})
    assert got["app_dir"] == str(tmp_path)


def test_binding_is_noop_when_already_bound(tmp_path):
    """bind_profile must not override an APPLYPILOT_DIR the caller set on purpose
    (the UI spawns subprocesses this way)."""
    (tmp_path / "profiles" / "nida").mkdir(parents=True)
    explicit = tmp_path / "profiles" / "nida"
    got = _run(tmp_path, [], {"APPLYPILOT_DIR": str(explicit)})
    assert got["app_dir"] == str(explicit)
