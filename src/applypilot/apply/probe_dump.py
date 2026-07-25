"""Live-DOM probe harness (spec §13 Phase 4 empiricism, invariant 12). Navigate a
live application URL, collect ONE BrowserObservation, and dump the raw controls +
widget hints + selectors + frame paths to JSON. This is the ground truth the
Ashby/Lever front-end tasks read BEFORE writing a parser — no widget census
exists, so markup assumptions are empirical, never guessed. Dev/ops only; NOT on
any apply hot path."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from applypilot.apply.browser_stream import collect_browser_observation
from applypilot.apply.prefill import _detect_ats


def serialize(obs, *, ats: str, url: str) -> dict:
    def _ctrl(c) -> dict:
        return {"label": c.label, "role": c.role, "control_type": c.control_type,
                "selector": c.selector, "required": c.required, "visible": c.visible,
                "frame_index": c.frame_index, "frame_url": c.frame_url}
    controls = [_ctrl(c) for c in (obs.controls or [])]
    return {
        "ats": ats, "url": url, "captured_at": datetime.now(timezone.utc).isoformat(),
        "page_text_sample": (obs.page_text_sample or "")[:2000],
        "counts": {"controls": len(controls), "submit_buttons": len(obs.submit_buttons or [])},
        "controls": controls,
        "submit_buttons": [_ctrl(b) for b in (obs.submit_buttons or [])],
        "validation_errors": list(obs.validation_errors or []),
    }


def dump(obs, *, ats: str, url: str, out_dir) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{ats}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    path = out / f"{stem}.json"
    path.write_text(json.dumps(serialize(obs, ats=ats, url=url), indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def probe_url(url: str, *, out_dir, headless: bool = True) -> Path:
    """Standalone live probe: launch a throwaway headless Chromium, navigate,
    collect, dump. Out-of-band from the worker's persistent Chrome so it never
    touches a live apply session."""
    from playwright.sync_api import sync_playwright
    ats = _detect_ats(url)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless)
        page = b.new_context().new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        obs = collect_browser_observation(page)
        b.close()
    return dump(obs, ats=ats, url=url, out_dir=out_dir)
