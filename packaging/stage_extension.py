"""Copies the shippable extension files (packaging/extension_files.txt) into a build folder.

    python packaging/stage_extension.py --dest build/friend/windows/extension

Exit 1 when a listed file is missing, when manifest.json has no "key" (a friend's
unpacked copy must keep the pinned extension ID the native host allows), or when
a staged HTML page or the manifest references a file that isn't staged.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
LIST = Path(__file__).resolve().parent / "extension_files.txt"
REF_RE = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"', re.I)


def read_list(path: Path = LIST) -> list[str]:
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _manifest_refs(manifest: dict) -> list[str]:
    refs = []
    if isinstance(manifest.get("background"), dict) and manifest["background"].get("service_worker"):
        refs.append(manifest["background"]["service_worker"])
    if isinstance(manifest.get("side_panel"), dict) and manifest["side_panel"].get("default_path"):
        refs.append(manifest["side_panel"]["default_path"])
    if manifest.get("options_page"):
        refs.append(manifest["options_page"])
    refs.extend((manifest.get("icons") or {}).values())
    action = manifest.get("action") or {}
    if isinstance(action.get("default_icon"), dict):
        refs.extend(action["default_icon"].values())
    return refs


def stage(dest: Path, src: Path = EXT, entries: list[str] | None = None) -> list[str]:
    """Copy ``entries`` from ``src`` into a fresh ``dest``. Returns problems (empty = ok)."""
    entries = read_list() if entries is None else entries
    problems: list[str] = []
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for entry in entries:
        name = entry.rstrip("/")
        source = Path(src) / name
        if not source.exists():
            problems.append(f"missing: {entry}")
            continue
        if source.is_dir():
            shutil.copytree(source, dest / name)
        else:
            shutil.copy2(source, dest / name)
    manifest_path = dest / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("key"):
            problems.append('manifest.json has no "key" (the pinned extension ID)')
        for ref in _manifest_refs(manifest):
            if not (dest / ref).exists():
                problems.append(f"manifest.json references {ref}, which isn't staged")
    for page in sorted(dest.glob("*.html")):
        for ref in REF_RE.findall(page.read_text(encoding="utf-8")):
            if "://" not in ref and not (dest / ref).exists():
                problems.append(f"{page.name} references {ref}, which isn't staged")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", required=True, type=Path)
    args = ap.parse_args(argv)
    problems = stage(args.dest)
    for p in problems:
        print(f"stage_extension: {p}", file=sys.stderr)
    if not problems:
        print(f"staged extension -> {args.dest}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
