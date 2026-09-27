"""packaging/stage_extension.py: what ships in the friend's extension folder."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("stage_extension", ROOT / "packaging" / "stage_extension.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_allow_list_stages_cleanly(tmp_path):
    stage = _load()
    dest = tmp_path / "extension"
    assert stage.stage(dest) == []
    assert (dest / "manifest.json").exists() and (dest / "llm_bridge.js").exists() and (dest / "icons").is_dir()
    for dev_only in ("selftest.js", "test-page.html", "fixtures_scanned_fields.jsonl", "llm_bridge_selftest.js", "README.md"):
        assert not (dest / dev_only).exists(), dev_only


def _fake_ext(tmp_path, manifest, pages=None, extra=()):
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, html in (pages or {}).items():
        (src / name).write_text(html, encoding="utf-8")
    for name in extra:
        (src / name).write_text("x", encoding="utf-8")
    return src


def test_missing_listed_file_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"key": "k"})
    problems = stage.stage(tmp_path / "out", src=src, entries=["manifest.json", "gone.js"])
    assert problems == ["missing: gone.js"]


def test_manifest_without_key_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"name": "x"})
    assert any("key" in p for p in stage.stage(tmp_path / "out", src=src, entries=["manifest.json"]))


def test_page_referencing_an_unstaged_script_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"key": "k"}, pages={"p.html": '<script src="a.js"></script><script src="b.js"></script>'},
                    extra=["a.js"])
    problems = stage.stage(tmp_path / "out", src=src, entries=["manifest.json", "p.html", "a.js"])
    assert problems == ["p.html references b.js, which isn't staged"]


def test_manifest_referencing_an_unstaged_file_is_reported(tmp_path):
    stage = _load()
    src = _fake_ext(tmp_path, {"key": "k", "background": {"service_worker": "bg.js"}, "options_page": "o.html"},
                    extra=["o.html"])
    problems = stage.stage(tmp_path / "out", src=src, entries=["manifest.json", "o.html"])
    assert problems == ["manifest.json references bg.js, which isn't staged"]
