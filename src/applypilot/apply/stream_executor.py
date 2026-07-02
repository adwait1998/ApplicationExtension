"""Direct browser actions against BrowserStateStream observations.

This is the low-level executor behind the ApplyPilot stream MCP server. It
lets a planner (Claude) act on structured stream controls without first asking
Playwright MCP for a browser_snapshot element ref.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from applypilot.apply.browser_stream import (
    BrowserObservation,
    ControlObservation,
    collect_browser_observation,
    _observation_signature,
)


@dataclass
class StreamActionResult:
    ok: bool
    action: str
    target: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    value: str | None = None
    control_id: str | None = None


def observation_for_claude(obs: BrowserObservation, *, max_controls: int = 40) -> dict[str, Any]:
    """Return a compact JSON shape suitable for MCP tool output."""
    return {
        "url": obs.url,
        "title": obs.title,
        "ready_state": obs.ready_state,
        "observed_at": obs.observed_at,
        "signature": _observation_signature(obs),
        "required_missing": list(obs.required_missing),
        "validation_errors": list(obs.validation_errors[:10]),
        "submit_enabled": obs.submit_enabled,
        "resume_present": obs.resume_present,
        "controls": [_control_for_claude(c) for c in obs.controls[:max_controls]],
        "submit_buttons": [_control_for_claude(b) for b in obs.submit_buttons[:10]],
        "tabs": list(obs.tabs),
        "page_text_sample": obs.page_text_sample[:2000],
        "error": obs.error,
    }


def effective_allow_submit(model_allow_submit: bool, dry_run: bool) -> bool:
    """Server-side dry-run enforcement for submit actions.

    `allow_submit` is an argument THE MODEL passes to stream_execute. In
    dry-run mode the prompt forbids submitting, but a model can (and did —
    Twilio, 2026-06-12) pass allow_submit=true anyway; the only protection
    was prose. Dry-run must be structural: when dry_run is set the server
    refuses final submits regardless of what the model requests.
    """
    return bool(model_allow_submit) and not dry_run


def execute_stream_actions_cdp(
    cdp_port: int,
    actions: list[dict[str, Any]],
    *,
    allow_submit: bool = False,
    timeout_ms: int = 4000,
) -> dict[str, Any]:
    """Connect to an existing Chrome CDP port, run actions, and disconnect."""
    pw = browser = None
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}", timeout=timeout_ms
        )
        page, tabs = _active_page_and_tabs(browser)
        if page is None:
            return {
                "ok": False,
                "error": "no_page",
                "duration_ms": _elapsed_ms(started),
                "results": [],
                "observation": None,
            }
        return execute_stream_actions_on_page(
            page,
            actions,
            allow_submit=allow_submit,
            timeout_ms=timeout_ms,
            tabs=tabs,
            started=started,
        )
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "duration_ms": _elapsed_ms(started),
            "results": [],
            "observation": None,
        }
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


def execute_stream_actions_on_page(
    page,
    actions: list[dict[str, Any]],
    *,
    allow_submit: bool = False,
    timeout_ms: int = 4000,
    tabs: list[dict[str, Any]] | None = None,
    started: float | None = None,
) -> dict[str, Any]:
    """Run a batch of stream actions against an already-open Playwright page."""
    started = started or time.monotonic()
    results: list[StreamActionResult] = []
    for raw in actions or []:
        result = _execute_one(page, raw, allow_submit=allow_submit, timeout_ms=timeout_ms, tabs=tabs)
        results.append(result)
        if not result.ok:
            break
        page.wait_for_timeout(120)
    obs = collect_browser_observation(page, tabs=tabs)
    return {
        "ok": all(r.ok for r in results),
        "duration_ms": _elapsed_ms(started),
        "results": [asdict(r) for r in results],
        "observation": observation_for_claude(obs),
    }


def wait_for_stream_change_cdp(
    cdp_port: int,
    *,
    previous_signature: str | None = None,
    timeout_ms: int = 3000,
    poll_ms: int = 200,
) -> dict[str, Any]:
    """Poll CDP until the structured observation changes or timeout elapses."""
    from applypilot.apply.browser_stream import _observation_signature

    pw = browser = None
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}", timeout=min(timeout_ms, 5000)
        )
        deadline = time.monotonic() + timeout_ms / 1000.0
        last_obs = None
        while time.monotonic() <= deadline:
            page, tabs = _active_page_and_tabs(browser)
            if page is None:
                break
            obs = collect_browser_observation(page, tabs=tabs)
            last_obs = obs
            sig = _observation_signature(obs)
            if previous_signature is None or sig != previous_signature:
                return {
                    "changed": True,
                    "signature": sig,
                    "duration_ms": _elapsed_ms(started),
                    "observation": observation_for_claude(obs),
                }
            time.sleep(max(0.05, poll_ms / 1000.0))
        if last_obs is None:
            return {"changed": False, "error": "no_page", "duration_ms": _elapsed_ms(started)}
        return {
            "changed": False,
            "signature": _observation_signature(last_obs),
            "duration_ms": _elapsed_ms(started),
            "observation": observation_for_claude(last_obs),
        }
    except Exception as e:
        return {"changed": False, "error": f"{type(e).__name__}: {e}", "duration_ms": _elapsed_ms(started)}
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


def _execute_one(page, raw: dict[str, Any], *, allow_submit: bool, timeout_ms: int, tabs) -> StreamActionResult:
    action = _action_kind(raw)
    target = _target_dict(raw)
    if action == "press" and not target:
        key = str(raw.get("key") or raw.get("value") or "")
        if not key:
            return StreamActionResult(False, action, target, "missing_key")
        page.keyboard.press(key)
        return StreamActionResult(True, action, target, "ok", value=key)
    obs = collect_browser_observation(page, tabs=tabs)
    control = _find_control(obs, target, include_buttons=action in {"click", "submit"})
    if control is None:
        return StreamActionResult(False, action, target, "target_not_found")
    if control.disabled:
        return StreamActionResult(False, action, target, "target_disabled", control_id=control.control_id)

    is_final_submit = action == "submit" or (action == "click" and _looks_final_submit(control))
    if is_final_submit:
        if not allow_submit:
            return StreamActionResult(False, action, target, "submit_refused_allow_submit_false", control_id=control.control_id)
        guard = _submission_guard(obs, control)
        if guard:
            return StreamActionResult(False, action, target, guard, control_id=control.control_id)

    value = raw.get("value")
    try:
        try:
            loc = _resolve_locator(page, control, target, timeout_ms)
        except Exception:
            if action == "select" and value is not None and _select_by_bbox(page, control, str(value), timeout_ms):
                return StreamActionResult(True, action, target, "ok_bbox", value=str(value), control_id=control.control_id)
            if action in {"click", "submit"} and _click_control_bbox(page, control):
                return StreamActionResult(True, action, target, "ok_bbox", control_id=control.control_id)
            raise
        if action == "fill":
            if value is None:
                return StreamActionResult(False, action, target, "missing_value", control_id=control.control_id)
            loc.fill(str(value), timeout=timeout_ms)
            _dispatch_events(loc)
        elif action == "select":
            if value is None:
                return StreamActionResult(False, action, target, "missing_value", control_id=control.control_id)
            _select_value(page, loc, control, str(value), timeout_ms)
        elif action == "upload":
            if value is None:
                return StreamActionResult(False, action, target, "missing_file_path", control_id=control.control_id)
            loc.set_input_files(str(value), timeout=timeout_ms)
        elif action == "check":
            try:
                loc.check(timeout=timeout_ms)
            except Exception:
                try:
                    loc.click(timeout=timeout_ms)
                except Exception:
                    if not _click_control_bbox(page, control):
                        raise
        elif action == "uncheck":
            try:
                loc.uncheck(timeout=timeout_ms)
            except Exception:
                try:
                    loc.click(timeout=timeout_ms)
                except Exception:
                    if not _click_control_bbox(page, control):
                        raise
        elif action in {"click", "submit"}:
            loc.click(timeout=timeout_ms)
        elif action == "type":
            if value is None:
                return StreamActionResult(False, action, target, "missing_value", control_id=control.control_id)
            loc.click(timeout=timeout_ms)
            page.keyboard.type(str(value), delay=10)
            _dispatch_events(loc)
        elif action == "press":
            key = str(raw.get("key") or value or "")
            if not key:
                return StreamActionResult(False, action, target, "missing_key", control_id=control.control_id)
            if target:
                loc.click(timeout=timeout_ms)
            page.keyboard.press(key)
            _dispatch_events(loc)
        else:
            return StreamActionResult(False, action, target, f"unsupported_action:{action}", control_id=control.control_id)
    except Exception as e:
        return StreamActionResult(False, action, target, f"{type(e).__name__}: {e}", control_id=control.control_id)

    return StreamActionResult(True, action, target, "ok", value=str(value) if value is not None else None, control_id=control.control_id)


def _action_kind(raw: dict[str, Any]) -> str:
    return str(raw.get("action") or raw.get("kind") or raw.get("type") or "").strip().lower()


def _target_dict(raw: dict[str, Any]) -> dict[str, Any]:
    target = raw.get("target")
    if isinstance(target, dict):
        merged = dict(target)
    else:
        merged = {}
    for key in ("control_id", "selector", "label", "role", "control_type", "frame_url", "frame_index"):
        if key in raw and key not in merged:
            merged[key] = raw[key]
    return merged


def _find_control(
    obs: BrowserObservation,
    target: dict[str, Any],
    *,
    include_buttons: bool,
) -> ControlObservation | None:
    candidates = list(obs.controls)
    if include_buttons:
        candidates.extend(obs.submit_buttons)
    control_id = str(target.get("control_id") or "")
    if control_id:
        for c in candidates:
            if c.control_id == control_id:
                return c
    selector = str(target.get("selector") or "")
    frame_index = target.get("frame_index")
    frame_url = str(target.get("frame_url") or "")
    label = _norm(target.get("label"))
    role = _norm(target.get("role"))
    control_type = _norm(target.get("control_type"))
    scored = []
    for c in candidates:
        score = 0
        if selector and c.selector == selector:
            score += 5
        if frame_index is not None and c.frame_index == int(frame_index):
            score += 2
        if frame_url and c.frame_url == frame_url:
            score += 2
        if label and (label in _norm(c.label) or _norm(c.label) in label):
            score += 4
        if role and role == _norm(c.role):
            score += 1
        if control_type and control_type == _norm(c.control_type):
            score += 1
        if score:
            scored.append((score, c.confidence, c))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _resolve_locator(page, control: ControlObservation, target: dict[str, Any], timeout_ms: int):
    frames = _candidate_frames(page, control, target)
    selectors = [str(target.get("selector") or ""), control.selector]
    for frame in frames:
        for selector in selectors:
            if not selector or _is_generic_selector(selector):
                continue
            try:
                loc = frame.locator(selector).first
                if loc.count() > 0:
                    return loc
            except Exception:
                pass
        if control.label:
            try:
                loc = frame.get_by_label(control.label, exact=False).first
                if loc.count() > 0:
                    return loc
            except Exception:
                pass
        if control.role and (control.label or control.value):
            try:
                loc = frame.get_by_role(control.role, name=control.label or control.value, exact=False).first
                if loc.count() > 0:
                    return loc
            except Exception:
                pass
        try:
            from applypilot.apply.healing import ElementSpec, heal

            spec = ElementSpec(
                role=control.role or None,
                name=control.label or control.value or None,
                label=control.label or None,
                tag=_tag_from_selector(control.selector),
                fingerprint={
                    "tag": _tag_from_selector(control.selector) or "input",
                    "label_text": control.label,
                    "role": control.role,
                },
                css_fallbacks=[s for s in selectors if s],
            )
            loc, _tier = heal(frame, spec, timeout_ms=min(timeout_ms, 1000))
            if loc is not None and loc.count() > 0:
                return loc
        except Exception:
            pass
    raise RuntimeError(f"no locator matched control_id={control.control_id}")


def _candidate_frames(page, control: ControlObservation, target: dict[str, Any]):
    try:
        frames = list(page.frames)
    except Exception:
        return [page]
    frame_index = target.get("frame_index", control.frame_index)
    frame_url = str(target.get("frame_url") or control.frame_url or "")
    ordered = []
    try:
        if frame_index is not None:
            ordered.append(frames[int(frame_index)])
    except Exception:
        pass
    if frame_url:
        for frame in frames:
            try:
                if frame.url == frame_url and frame not in ordered:
                    ordered.append(frame)
            except Exception:
                pass
    for frame in frames:
        if frame not in ordered:
            ordered.append(frame)
    return ordered or [page]


def _select_value(page, loc, control: ControlObservation, value: str, timeout_ms: int) -> None:
    if control.control_type == "select":
        try:
            loc.select_option(label=value, timeout=timeout_ms)
            return
        except Exception:
            loc.select_option(value=value, timeout=timeout_ms)
            return

    # Greenhouse/react-select controls often expose an input whose raw value can
    # be changed without committing the selected option. For combobox-style
    # controls, drive the same click/type/option/Enter path a user would use.
    if control.role == "combobox" or "select" in (control.selector + " " + control.control_type).lower():
        try:
            loc.click(timeout=timeout_ms)
        except Exception:
            _click_control_bbox(page, control)
        try:
            loc.fill("", timeout=min(timeout_ms, 1000))
        except Exception:
            try:
                page.keyboard.press("Control+A")
            except Exception:
                pass
        page.keyboard.type(value, delay=10)
        page.wait_for_timeout(120)
        if _click_visible_option(page, value, timeout_ms=min(timeout_ms, 1500)):
            _dispatch_events(loc)
            return
        try:
            if loc.input_value(timeout=500).strip() == value.strip():
                _dispatch_events(loc)
                return
        except Exception:
            pass
        _click_control_bbox(page, control)
        page.wait_for_timeout(120)
        page.keyboard.type(value, delay=10)
        page.wait_for_timeout(120)
        if _click_visible_option(page, value, timeout_ms=min(timeout_ms, 1500)):
            _dispatch_events(loc)
            return
        if _combobox_has_open_menu(page, loc):
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
        _dispatch_events(loc)
        return

    try:
        loc.fill(value, timeout=timeout_ms)
        _dispatch_events(loc)
        try:
            if loc.input_value(timeout=500).strip() == value.strip():
                return
        except Exception:
            return
    except Exception:
        pass
    loc.click(timeout=timeout_ms)
    page.keyboard.type(value, delay=10)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    _dispatch_events(loc)


def _click_visible_option(page, value: str, *, timeout_ms: int) -> bool:
    """Click a visible dropdown option matching value, across portal menus."""
    candidates = [
        page.get_by_role("option", name=value, exact=True),
        page.get_by_text(value, exact=True),
        page.get_by_text(value, exact=False),
    ]
    for candidate in candidates:
        try:
            loc = candidate.last
            if loc.count() and loc.is_visible(timeout=min(timeout_ms, 500)):
                loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


def _combobox_has_open_menu(page, loc) -> bool:
    try:
        expanded = loc.evaluate("el => el.getAttribute('aria-expanded') === 'true'")
        if expanded:
            return True
    except Exception:
        pass
    try:
        return bool(page.locator('[role="listbox"], [role="option"], [class*="menu"]').filter(has_text="").count())
    except Exception:
        return False


def _submission_guard(obs: BrowserObservation, control: ControlObservation) -> str | None:
    if _looks_nonfinal_next(control):
        return None
    if obs.required_missing:
        return "submit_guard_required_missing:" + ", ".join(obs.required_missing[:4])
    if obs.validation_errors:
        return "submit_guard_validation_errors:" + ", ".join(obs.validation_errors[:3])
    if control.disabled:
        return "submit_guard_button_disabled"
    return None


def _looks_final_submit(control: ControlObservation) -> bool:
    text = _norm(" ".join([control.label, control.value, control.selector]))
    if _looks_nonfinal_next(control):
        return False
    return "submit" in text or "submit application" in text or "submit my application" in text


def _looks_nonfinal_next(control: ControlObservation) -> bool:
    text = _norm(" ".join([control.label, control.value, control.selector]))
    return ("next" in text or "continue" in text) and not any(token in text for token in ("submit", "application"))


def _dispatch_events(loc) -> None:
    try:
        loc.evaluate(
            "el => { "
            "el.dispatchEvent(new Event('input', {bubbles: true})); "
            "el.dispatchEvent(new Event('change', {bubbles: true})); "
            "}"
        )
    except Exception:
        pass


def _select_by_bbox(page, control: ControlObservation, value: str, timeout_ms: int) -> bool:
    if not _click_control_bbox(page, control):
        return False
    page.wait_for_timeout(120)
    if _click_visible_option(page, value, timeout_ms=min(timeout_ms, 1500)):
        return True
    page.keyboard.type(value, delay=10)
    page.wait_for_timeout(120)
    if _click_visible_option(page, value, timeout_ms=min(timeout_ms, 1500)):
        return True
    try:
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _click_control_bbox(page, control: ControlObservation) -> bool:
    bbox = control.bbox or {}
    try:
        x = float(bbox.get("x", 0)) + float(bbox.get("width", 0)) / 2
        y = float(bbox.get("y", 0)) + float(bbox.get("height", 0)) / 2
    except Exception:
        return False
    if x <= 0 or y <= 0:
        return False
    page.mouse.click(x, y)
    return True


def _control_for_claude(control: ControlObservation) -> dict[str, Any]:
    data = asdict(control)
    if len(data.get("value") or "") > 160:
        data["value"] = data["value"][:157] + "..."
    return data


def _active_page_and_tabs(browser) -> tuple[Any | None, list[dict[str, Any]]]:
    pages = [p for ctx in browser.contexts for p in ctx.pages]
    tabs = []
    for i, page in enumerate(pages):
        try:
            tabs.append({"index": i, "url": page.url, "title": page.title()})
        except Exception:
            tabs.append({"index": i, "url": "", "title": ""})
    return (pages[-1] if pages else None), tabs


def _tag_from_selector(selector: str) -> str | None:
    if not selector:
        return None
    head = selector.split("[", 1)[0].split("#", 1)[0].split(".", 1)[0].strip()
    return head or None


def _is_generic_selector(selector: str) -> bool:
    return selector.strip().lower() in {
        "div", "span", "input", "button", "textarea", "select", "a", "*"
    }


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
