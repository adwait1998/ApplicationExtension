"""Low-FPS visual trace recorder for debugging apply runs.

This is intentionally outside the model control loop. It records compact
viewport frames so an operator or developer can inspect what happened after a
run without paying screenshot/vision cost on every agent step.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class VisualTraceRecorder:
    """Capture a low-FPS viewport trace from an existing Chrome CDP port."""

    def __init__(
        self,
        cdp_port: int,
        out_dir: str | Path,
        *,
        interval_s: float = 2.0,
        max_frames: int = 240,
        jpeg_quality: int = 55,
    ) -> None:
        self.cdp_port = cdp_port
        self.out_dir = Path(out_dir)
        self.interval_s = max(0.5, float(interval_s))
        self.max_frames = max(1, int(max_frames))
        self.jpeg_quality = max(20, min(90, int(jpeg_quality)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_action = ""
        self._started = time.time()
        self._frame_count = 0
        self.manifest_path = self.out_dir / "manifest.jsonl"
        self.index_path = self.out_dir / "index.html"

    def start(self) -> "VisualTraceRecorder":
        if self._thread is not None:
            return self
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._write_index()
        self._thread = threading.Thread(
            target=self._run,
            name=f"applypilot-visual-trace-{self.cdp_port}",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._write_index()

    def mark_action(self, action: str) -> None:
        with self._lock:
            self._last_action = str(action or "")[:160]

    @property
    def latest_action(self) -> str:
        with self._lock:
            return self._last_action

    def _run(self) -> None:
        pw = browser = None
        try:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.cdp_port}",
                timeout=5000,
            )
            while not self._stop.is_set() and self._frame_count < self.max_frames:
                self._capture_frame(browser)
                self._stop.wait(self.interval_s)
        except Exception:
            log.debug("visual trace recorder failed", exc_info=True)
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                pass

    def _capture_frame(self, browser) -> None:
        pages = [p for ctx in browser.contexts for p in ctx.pages]
        if not pages:
            return
        page = pages[-1]
        frame_no = self._frame_count + 1
        rel_name = f"frame_{frame_no:04d}.jpg"
        frame_path = self.out_dir / rel_name
        try:
            page.screenshot(
                path=str(frame_path),
                type="jpeg",
                quality=self.jpeg_quality,
                full_page=False,
                timeout=3000,
            )
        except Exception:
            return

        self._frame_count = frame_no
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            url = page.url
        except Exception:
            url = ""
        item: dict[str, Any] = {
            "ts": time.time(),
            "elapsed_s": round(time.time() - self._started, 2),
            "frame": rel_name,
            "url": url,
            "title": title,
            "last_action": self.latest_action,
        }
        with self.manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        if frame_no == 1 or frame_no % 10 == 0:
            self._write_index()

    def _write_index(self) -> None:
        rows = []
        if self.manifest_path.exists():
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        cards = []
        for row in rows:
            frame = row.get("frame", "")
            cards.append(
                "<figure>"
                f"<img src='{frame}' loading='lazy'>"
                f"<figcaption>{row.get('elapsed_s', '')}s | {row.get('last_action', '')}<br>"
                f"{row.get('url', '')}</figcaption>"
                "</figure>"
            )
        html = """<!doctype html>
<meta charset="utf-8">
<title>ApplyPilot Visual Trace</title>
<style>
body{font-family:system-ui,Segoe UI,sans-serif;margin:16px;background:#111;color:#eee}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
figure{margin:0;border:1px solid #333;background:#1a1a1a}
img{width:100%;display:block}
figcaption{font-size:12px;line-height:1.35;padding:8px;color:#ccc;word-break:break-word}
</style>
<h1>ApplyPilot Visual Trace</h1>
<div class="grid">
""" + "\n".join(cards) + "\n</div>\n"
        self.index_path.write_text(html, encoding="utf-8")
