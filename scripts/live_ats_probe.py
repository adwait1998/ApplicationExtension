"""Real-page probe: run the extension's scanner + the local service against LIVE,
public job-application pages — with a synthetic applicant, never submitting.

Why: every Workday/Greenhouse fix so far was proven only on mocks, and a mock
built from guesses can only confirm the guesses. This opens real postings
(Greenhouse job-boards + legacy boards, Ashby, Lever, and company career sites
that embed Greenhouse in an iframe), scans every frame, resolves the fields
against a SYNTHETIC profile, fills them, reads them back, and reports what
actually happened per field — including fields the scanner never saw.

Safety, in order of importance:
  * the applicant is fictional (see PROFILE) and no résumé is stored, so no
    real personal data is typed anywhere and nothing is uploaded;
  * the scanner's capturing submit shield is installed in every frame before
    the first fill;
  * once filling starts, every non-GET request from the page is aborted, so no
    autosave, upload or submit can leave the browser even if something slipped;
  * nothing is ever clicked except through the scanner's own guarded helpers.

Usage (Python 3.12 with Playwright):
  python scripts/live_ats_probe.py --per-ats 2 --out <dir> [--scanner path/to/scanner.js]
  python scripts/live_ats_probe.py --url https://job-boards.greenhouse.io/acme/jobs/123

URLs come from the operator's jobs DB (read-only) unless given with --url.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
DB = pathlib.Path(os.environ.get("APPLYPILOT_DB", r"E:\applypilot-data\applypilot.db"))

PROFILE = {
    "profile_id": "probe",
    "personal": {
        "full_name": "Jordan Quill Testperson", "preferred_name": "Jordan",
        "email": "jordan.testperson@example.com", "phone": "+1 206 555 0147",
        "address": "100 Example Ave", "city": "Seattle", "province_state": "Washington",
        "country": "United States", "postal_code": "98101",
        "linkedin_url": "https://www.linkedin.com/in/jordan-testperson-example",
        "portfolio_url": "https://jordan-testperson.example.com",
        "website_url": "https://jordan-testperson.example.com", "github_url": "",
    },
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False},
    "compensation": {"salary_expectation": "", "salary_currency": "USD"},
    "availability": {"earliest_start_date": "Two weeks notice"},
    "experience": {"years_of_experience_total": "6", "current_job_title": "Senior Product Designer",
                   "current_company": "Example Labs", "education_level": "Bachelor's"},
    "eeo_voluntary": {},
    "screening": {"criminal_conviction": "No", "background_check_consent": "Yes",
                  "drug_test_consent": "Yes", "non_compete": "No", "willing_to_travel": "Yes",
                  "how_heard": "LinkedIn"},
    "skills_boundary": {"skills": ["Figma", "Prototyping", "User Research", "Design Systems", "SQL"]},
    "work_history": [
        {"title": "Senior Product Designer", "company": "Example Labs", "location": "Seattle, WA",
         "start": "03/2022", "end": "", "current": True,
         "description": "- Led design for the analytics suite\n- Built the design system"},
        {"title": "Product Designer", "company": "Sample Corp", "location": "Portland, OR",
         "start": "06/2019", "end": "02/2022", "current": False,
         "description": "- Shipped onboarding redesign"},
    ],
    "education": [
        {"school": "University of Washington", "degree": "Bachelor of Design", "field": "Interaction Design",
         "start": "09/2015", "end": "06/2019", "gpa": ""},
    ],
}

# The same fictional person, shaped like the real operator's key paths: needs
# sponsorship on a work visa, has a salary expectation, EEO all unset (so
# every EEO question exercises the decline path).
PROFILE_SPONSOR = json.loads(json.dumps(PROFILE))
PROFILE_SPONSOR["profile_id"] = "probe-sponsor"
PROFILE_SPONSOR["work_authorization"] = {"legally_authorized_to_work": True, "require_sponsorship": True,
                                         "work_permit_type": "H-1B"}
PROFILE_SPONSOR["compensation"] = {"salary_expectation": "120000", "salary_currency": "USD"}

ATS_PATTERNS = {
    "gh-job-boards": "job-boards.greenhouse.io",
    "gh-legacy": "boards.greenhouse.io",
    "ashby": "jobs.ashbyhq.com",
    "lever": "jobs.lever.co",
}
# Career sites seen in the jobs DB that embed Greenhouse's form in an iframe.
EMBED_HOSTS = ("stripe.com", "databricks.com", "brex.com", "airbnb.com", "coinbase.com",
               "instacart.careers", "asana.com", "pinterestcareers.com", "roblox.com")

CAPTURE_SRC = (REPO / "extension" / "capture.js").read_text(encoding="utf-8")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")


_BOT_TEXT_RE = re.compile(
    r"access (is )?(temporarily )?(restricted|denied)|are you a robot|verify (that )?you are (a )?human|"
    r"checking your browser|unusual traffic|request (was )?blocked|press (&|and) hold|"
    r"complete the security check", re.I)
_BOT_FRAME_RE = re.compile(r"hcaptcha\.com/captcha|challenges\.cloudflare\.com|captcha-delivery\.com|arkoselabs",
                           re.I)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15))


def _slugs(rows: list[str], host: str) -> list[str]:
    out = []
    for u in rows:
        m = re.search(r"//" + re.escape(host) + r"/(?:embed/job_app\?for=)?([A-Za-z0-9_.-]+)", u, re.I)
        if m and m.group(1).lower() not in out:
            out.append(m.group(1).lower())
    return out


def _open_greenhouse(slug: str, want_host: str) -> str | None:
    jobs = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").get("jobs", [])
    for j in jobs[:40]:
        au = (j.get("absolute_url") or "").lower()
        if want_host == "embed":
            if au and "greenhouse.io" not in au:
                return j["absolute_url"]
        elif want_host in au:
            return j["absolute_url"]
    if want_host == "job-boards.greenhouse.io" and jobs:
        return f"https://job-boards.greenhouse.io/{slug}/jobs/{jobs[0]['id']}"
    return None


def _open_lever(slug: str) -> str | None:
    posts = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=3")
    return posts[0].get("applyUrl") if posts else None


def _open_ashby(slug: str) -> str | None:
    jobs = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", [])
    for j in jobs:
        url = j.get("applyUrl") or (j.get("jobUrl", "").rstrip("/") + "/application")
        if url:
            return url
    return None


def pick_urls(per_ats: int, seed: int) -> list[tuple[str, str]]:
    """Currently-OPEN postings, found through each ATS's public job API for
    companies in the operator's jobs DB (read-only). The DB's own URLs go
    stale — a closed Greenhouse job redirects to the board's job list, which
    proves nothing about the application form."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = [r[0] for r in con.execute(
        "select application_url from jobs where application_url is not null and application_url != ''")]
    rng = random.Random(seed)
    plan = [
        ("gh-job-boards", _slugs(rows, "job-boards.greenhouse.io"), lambda s: _open_greenhouse(s, "job-boards.greenhouse.io")),
        ("gh-legacy", _slugs(rows, "boards.greenhouse.io"), lambda s: _open_greenhouse(s, "boards.greenhouse.io")),
        ("gh-embed", _slugs(rows, "boards.greenhouse.io") + _slugs(rows, "job-boards.greenhouse.io"),
         lambda s: _open_greenhouse(s, "embed")),
        ("ashby", _slugs(rows, "jobs.ashbyhq.com"), _open_ashby),
        ("lever", _slugs(rows, "jobs.lever.co"), _open_lever),
    ]
    out = []
    for ats, slugs, finder in plan:
        rng.shuffle(slugs)
        got = 0
        for slug in slugs[:25]:
            if got >= per_ats:
                break
            try:
                url = finder(slug)
            except Exception:
                url = None
            if url and url not in [u for _, u in out]:
                out.append((ats, url))
                got += 1
    return out


def start_service(tmp: pathlib.Path, port: int, profile: dict = PROFILE):
    (tmp / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    env = dict(os.environ)
    env["APPLYPILOT_DIR"] = str(tmp)
    env["APPLYPILOT_ROOT"] = str(tmp)
    env.pop("APPLYPILOT_PROFILE", None)
    env.pop("APPLYPILOT_DRAFTS", None)
    env.pop("APPLYPILOT_LAYA", None)
    proc = subprocess.Popen([PY, "-m", "applypilot", "serve-extension", "--port", str(port)],
                            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tok_file = tmp / "extension_token.txt"
    for _ in range(90):
        try:
            if tok_file.exists():
                req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                             headers={"X-ApplyPilot-Token": tok_file.read_text().strip()})
                urllib.request.urlopen(req, timeout=3)
                return proc, tok_file.read_text().strip()
        except Exception:
            pass
        time.sleep(1)
    proc.kill()
    raise SystemExit("extension service never came up")


def resolve(port: int, token: str, url: str, fields: list[dict]) -> dict:
    body = json.dumps({"url": url, "fields": fields}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/resolve", data=body,
                                 headers={"Content-Type": "application/json", "X-ApplyPilot-Token": token})
    return json.load(urllib.request.urlopen(req, timeout=60))


SCAN_JS = """() => {
  const res = ApplyPilotScanner.scanAll(document);
  window.__AP_REG = res.registry;
  window.__AP_SHIELD_BLOCKED = 0;
  if (!window.__AP_SHIELD) {
    window.__AP_SHIELD = ApplyPilotScanner.installSubmitShield(document, () => { window.__AP_SHIELD_BLOCKED++; });
  }
  return { fields: res.fields, skippedFrames: res.skippedFrames || 0 };
}"""

FILL_JS = """async ([id, value, values, isCanary]) => {
  const entry = (window.__AP_REG || {})[id];
  if (!entry) return { ok: false, reason: 'no registry entry' };
  const v = (values && values.length && entry.kind === 'wd-prompt') ? values : value;
  let ok, detail = null;
  try {
    const r = await Promise.race([
      Promise.resolve(ApplyPilotScanner.applyFill(entry, v, { canary: !!isCanary })),
      new Promise(res => setTimeout(() => res({ ok: false, reason: 'timed out (12s)' }), 12000)),
    ]);
    ok = (r && typeof r === 'object') ? !!r.ok : !!r;
    if (r && typeof r === 'object') detail = r.reason || null;
    if (!detail && entry._lastReason) detail = String(entry._lastReason);
  } catch (e) { ok = false; detail = String(e).slice(0, 160); }
  let readback = '';
  try { readback = String(ApplyPilotScanner.getCurrentValue(entry) || ''); } catch (e) {}
  // Reviewer round 5, blocker A: the option's own text/label scanner.js's applyFill() actually
  // committed -- set only for a "pick one of several rendered options" widget kind (radio-group,
  // select, combobox, button-group, wd-dropdown, checkbox-group, wd-checkbox-group,
  // lever-location; see that function's own doc comment), null for anything else. THIS, never
  // the raw `value` the service sent (which can be phrased completely differently from what the
  // page itself renders -- "Decline to self-identify" vs. a page's own "I don't wish to
  // answer"), is what _stuck() below judges a later re-read against.
  let committedText = null;
  try { committedText = (entry._committedText == null) ? null : String(entry._committedText); } catch (e) {}
  let optionsSeen = null;
  try { optionsSeen = entry._lastOptions ? Array.prototype.slice.call(entry._lastOptions, 0, 60) : null; } catch (e) {}
  return { ok, detail, readback: readback.slice(0, 200), committedText, kind: entry.kind, optionsSeen };
}"""

# Interactive things on the page the scanner did NOT report as fields — the
# "couldn't read" count the review list should surface. Structure only.
UNSEEN_JS = """(ids) => {
  const seen = new Set();
  const isEl = (x) => x && typeof x === 'object' && x.nodeType === 1;
  for (const k in (window.__AP_REG || {})) {
    const e = window.__AP_REG[k];
    for (const v of Object.values(e || {})) {
      if (isEl(v)) seen.add(v);
      else if (Array.isArray(v)) v.forEach(x => { if (isEl(x)) seen.add(x); });
    }
  }
  const out = [];
  const q = 'input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea, ' +
            '[role=combobox], [role=radiogroup], [role=listbox], [role=radio], [role=checkbox], [role=switch], ' +
            '[contenteditable=true], button[aria-haspopup], button[aria-pressed], fieldset button[type=button]';
  document.querySelectorAll(q).forEach(el => {
    if (seen.has(el) || !ApplyPilotScanner.isVisible(el)) return;
    if (el.closest && [...seen].some(s => s.contains && s.contains(el))) return;
    let label = '';
    try { label = ApplyPilotScanner.getLabel(el) || ''; } catch (e) {}
    out.push({ tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
               role: el.getAttribute('role') || '', label: label.slice(0, 90),
               cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60) });
  });
  let shadowHosts = 0;
  document.querySelectorAll('*').forEach(el => { if (el.shadowRoot) shadowHosts++; });
  if (shadowHosts) out.push({ tag: '#shadow-roots', type: String(shadowHosts), role: '',
                              label: 'elements with a shadow root (not scanned)', cls: '' });
  return out.slice(0, 60);
}"""

# Fields revealed after filling: re-scan and keep only fields none of whose
# elements were in the previous registry (element identity, not positional
# ids). The new registry replaces the old one so the next fills resolve.
RESCAN_JS = r"""() => {
  const isEl = (x) => x && typeof x === 'object' && x.nodeType === 1;
  const elsOf = (e) => {
    const out = [];
    for (const v of Object.values(e || {})) {
      if (isEl(v)) out.push(v);
      else if (Array.isArray(v)) v.forEach(x => { if (isEl(x)) out.push(x); });
    }
    return out;
  };
  const prev = new Set();
  for (const k in (window.__AP_REG || {})) elsOf(window.__AP_REG[k]).forEach(x => prev.add(x));
  const res = ApplyPilotScanner.scanAll(document);
  window.__AP_REG = res.registry;
  const fresh = res.fields.filter(f => {
    const els = elsOf(res.registry[f.id]);
    return els.length && !els.some(x => prev.has(x));
  });
  return { fields: fresh };
}"""


# After every fill in a frame: blur, let the page settle, and read EVERYTHING
# back. A value that reverted (React re-render, a combobox clearing typed text
# on blur) is "didn't stick", never "filled".
REREAD_JS = r"""async (ids) => {
  try { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); } catch (e) {}
  await new Promise(r => setTimeout(r, 900));
  // What the PAGE holds as committed, not what we typed:
  //  - a react-select / ARIA combobox input's own text is only the search
  //    box; the committed value is the single-value (or multi-value chips)
  //    rendered in its control, and nothing means nothing was selected;
  //  - a radio group reads back as the checked radio's LABEL ("on" hides it).
  const committedCombobox = (el) => {
    const control = el.closest('[class*="select__control"], [class*="-control"], [class*="Select-control"]')
      || el.parentElement;
    if (!control) return '';
    const single = control.querySelector('[class*="single-value"], [class*="singleValue"], [class*="Select-value"]');
    if (single) return single.textContent || '';
    const chips = control.querySelectorAll('[class*="multi-value__label"], [class*="multiValue"] [class*="label"]');
    return Array.prototype.map.call(chips, c => c.textContent || '').join(', ');
  };
  const out = {};
  for (const id of ids) {
    const entry = (window.__AP_REG || {})[id];
    let v = '';
    try {
      if (!entry) v = '';
      else if (entry.kind === 'element' && entry.el && entry.el.getAttribute('role') === 'combobox') v = committedCombobox(entry.el);
      else if (entry.kind === 'radio-group') {
        const on = (entry.elements || []).filter(r => r.checked)[0];
        v = on ? (ApplyPilotScanner.getLabel(on) || on.value || 'checked') : '';
      } else v = String(ApplyPilotScanner.getCurrentValue(entry) || '');
    } catch (e) {}
    out[id] = String(v).replace(/\s+/g, ' ').trim().slice(0, 200);
  }
  return out;
}"""


def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


_PLACEHOLDER = re.compile(r"^(select\.{0,3}|choose.*|please select.*|--.*|)$", re.I)


def _is_choice(field: dict) -> bool:
    return bool(field.get("options")) or field.get("type") in ("radio", "checkbox") or bool(field.get("widget"))


def _stuck(intended, settled: str, field: dict, committed_text: str | None = None) -> bool:
    """Did the value survive blur + settle? Text must read back as written
    (phone formatting aside); a choice field's wording can legitimately differ
    from the intended value ("Decline to self-identify" -> "I don't wish to
    answer"), so it is judged against `committed_text` — what FILL_JS actually
    saw scanner.js's applyFill() commit at fill time (entry._committedText),
    never merely "is something non-placeholder showing now". Reviewer round 5,
    blocker A: the old version accepted ANY non-empty, non-placeholder choice
    reading unconditionally — a field that reverted to a DIFFERENT, WRONG
    option (or kept some stale prior value) after the fill would still be
    reported "stuck" as long as something was showing, which is not what
    "stuck" is supposed to mean. When `committed_text` isn't available (a kind
    applyFill() doesn't stash it for, e.g. a plain checkbox) this falls back
    to the old, looser "something non-placeholder" signal rather than
    regressing every such field to always FAILED."""
    got, want = _norm(settled), _norm(intended)
    if not got or _PLACEHOLDER.match(got):
        return False
    if _is_choice(field):
        if committed_text:
            return got == _norm(committed_text)
        return True
    if field.get("type") == "tel":
        return re.sub(r"\D", "", got) == re.sub(r"\D", "", want)
    return got == want or bool(want and want in got)


def probe(page, url: str, port: int, token: str, scanner_src: str, shots: pathlib.Path, tag: str) -> dict:
    rec: dict = {"url": url, "frames": [], "error": None}
    t0 = time.time()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        try:
            page.wait_for_selector("input, textarea, select, iframe", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3500)  # let SPAs (Ashby) and embedded iframes render
    except Exception as e:
        rec["error"] = f"load: {str(e)[:200]}"
        return rec
    rec["load_s"] = round(time.time() - t0, 1)
    rec["final_url"] = page.url
    body_text = ""
    try:
        body_text = page.inner_text("body", timeout=3000)[:4000].lower()
        if re.search(r"no longer (open|available|accepting)|job (is )?not found|position has been filled",
                     body_text):
            rec["error"] = "posting closed (page says so)"
            return rec
    except Exception:
        pass

    # From here on nothing may leave the browser but plain GETs.
    blocked = []

    def guard(route, request):
        if request.method != "GET":
            blocked.append(f"{request.method} {request.url[:100]}")
            return route.abort()
        return route.continue_()

    page.route("**/*", guard)

    for fi, frame in enumerate(page.frames):
        furl = frame.url or ""
        if not furl.startswith("http"):
            continue
        try:
            frame.evaluate(scanner_src)
            scan = frame.evaluate(SCAN_JS)
        except Exception as e:
            rec["frames"].append({"frame": furl[:120], "error": f"scan: {str(e)[:160]}"})
            continue
        fields = scan["fields"]
        if not fields:
            continue
        frec = {"frame": furl[:120], "top": frame == page.main_frame, "n_fields": len(fields),
                "skipped_cross_origin_frames": scan.get("skippedFrames", 0), "fields": []}
        # Pass 1 = the scanned fields; passes 2-3 = fields REVEALED by earlier
        # answers (e.g. Lever's EEO survey after "location"), exactly as the
        # extension's content.js re-scans after its verify sweep.
        pass_fields = fields
        for pass_no in (1, 2, 3):
            if not pass_fields:
                break
            t1 = time.time()
            try:
                plan = resolve(port, token, page.url, pass_fields)
            except Exception as e:
                frec["error"] = f"resolve: {str(e)[:160]}"
                break
            frec.setdefault("resolve_s", round(time.time() - t1, 2))
            results = {}
            t2 = time.time()
            for fill in plan.get("fills", []):
                if not fill.get("auto_fill"):
                    continue
                try:
                    r = frame.evaluate(FILL_JS, [fill["id"], fill["value"], fill.get("values") or [],
                                                 fill.get("source") == "canary"])
                except Exception as e:
                    r = {"ok": False, "detail": f"evaluate: {str(e)[:120]}", "readback": ""}
                results[fill["id"]] = (fill, r)
            # Second, option-aware resolve (as content.js does): a choice that
            # failed returns the options it saw; the service answers again with
            # them, and only an answer that IS one of those options is applied.
            by_id = {f["id"]: f for f in pass_fields}
            retry = [dict(by_id[fid], options=r["optionsSeen"]) for fid, (_f, r) in results.items()
                     if not r.get("ok") and r.get("optionsSeen") and fid in by_id]
            if retry:
                try:
                    plan2 = resolve(port, token, page.url, retry)
                except Exception:
                    plan2 = {}
                for fill in plan2.get("fills", []):
                    seen = next((f["options"] for f in retry if f["id"] == fill["id"]), [])
                    if not fill.get("auto_fill") or fill.get("value") not in seen:
                        continue
                    try:
                        r = frame.evaluate(FILL_JS, [fill["id"], fill["value"], [], fill.get("source") == "canary"])
                    except Exception as e:
                        r = {"ok": False, "detail": f"evaluate: {str(e)[:120]}", "readback": ""}
                    r["second_resolve"] = True
                    results[fill["id"]] = (fill, r)
            frec["fill_s"] = round(frec.get("fill_s", 0) + time.time() - t2, 1)
            try:
                settled = frame.evaluate(REREAD_JS, [fid for fid, (_f, r) in results.items() if r.get("ok")])
            except Exception:
                settled = {}
            skipped = {s_["id"]: s_ for s_ in plan.get("skipped", [])}
            for f in pass_fields:
                row = {"label": (f.get("label") or f.get("name") or "")[:90], "tag": f.get("tag"),
                       "type": f.get("type"), "widget": f.get("widget") or "", "pass": pass_no,
                       "n_options": len(f.get("options") or []), "required": f.get("required", False)}
                if f["id"] in results:
                    fill, r = results[f["id"]]
                    settled_val = settled.get(f["id"], "")
                    if not r.get("ok"):
                        status = "FAILED"
                    elif _stuck(fill["value"], settled_val, f, r.get("committedText")):
                        status = "verified"
                    else:
                        status = "DIDNT-STICK"
                    row.update(status=status, source=fill["source"],
                               value=str(fill["value"])[:80], readback=settled_val[:80],
                               committed=(r.get("committedText") or "")[:80],
                               detail=r.get("detail"), draft=fill.get("draft", False),
                               choice=_is_choice(f))
                elif f["id"] in skipped:
                    s_ = skipped[f["id"]]
                    row.update(status="left", source=s_.get("source"), detail=s_.get("reason", "")[:140])
                else:
                    row.update(status="no-plan")
                frec["fields"].append(row)
            if pass_no == 3:
                break
            try:
                pass_fields = frame.evaluate(RESCAN_JS)["fields"]
            except Exception:
                pass_fields = []
        frec["n_fields"] = len(frec["fields"])
        try:
            frec["unseen"] = frame.evaluate(UNSEEN_JS, [])
            frec["shield_blocked"] = frame.evaluate("() => window.__AP_SHIELD_BLOCKED || 0")
        except Exception:
            pass
        # Structure-only capture (capture.js never reads values) — real markup
        # to build mocks from, instead of guessing it.
        try:
            frame.evaluate(CAPTURE_SRC)
            structure = frame.evaluate("() => ApplyPilotCapture.captureStructure(document)")
            (shots.parent / f"structure-{tag}-f{fi}.json").write_text(json.dumps(structure, indent=1),
                                                                        encoding="utf-8")
        except Exception:
            pass
        rec["frames"].append(frec)

    # A bot check is not an empty form (reviewer round 4: SmartRecruiters
    # served "Access is temporarily restricted"). Judged only when NO frame
    # yielded a single field, so a real form is never reported as blocked.
    if not any(fr.get("fields") for fr in rec["frames"]):
        bot_text = _BOT_TEXT_RE.search(body_text or "")
        challenge = [f.url for f in page.frames if _BOT_FRAME_RE.search(f.url or "")]
        if bot_text or challenge:
            rec["error"] = "blocked by a bot check (" + (bot_text.group(0) if bot_text else challenge[0][:60]) + ")"
    rec["blocked_requests"] = blocked[:20]
    rec["total_s"] = round(time.time() - t0, 1)
    try:
        page.screenshot(path=str(shots / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    return rec


def summarize(results: list[dict]) -> str:
    lines = ["# Live ATS probe", "",
             "Status per field: verified = read back after blur + settle; DIDNT-STICK = reported filled but "
             "the value was gone or a placeholder after settling; FAILED = the fill itself reported failure; "
             "left = the service left it for the applicant; no-plan = scanned but not resolved.",
             "NOTE: the probe scans EVERY frame with Playwright. The extension injects only the frames it is "
             "allowed into, so compare TOP-frame numbers for what a user gets without all-frames support.",
             ""]
    for ats, rec in results:
        lines.append(f"## {ats} — {rec['url'][:110]}")
        if rec.get("error"):
            lines.append(f"- ERROR {rec['error']}")
            continue
        if not rec["frames"]:
            lines.append("- no fields found in any frame")
        for fr in rec["frames"]:
            if fr.get("error"):
                lines.append(f"- frame {fr['frame']}: {fr['error']}")
                continue
            st = {}
            for row in fr["fields"]:
                st[row["status"]] = st.get(row["status"], 0) + 1
            revealed = sum(1 for row in fr["fields"] if row.get("pass", 1) > 1)
            if revealed:
                st["revealed-by-answers"] = revealed
            lines.append(f"- frame {'TOP' if fr['top'] else fr['frame']}: {fr['n_fields']} fields {st}; "
                         f"unseen interactive: {len(fr.get('unseen', []))}; resolve {fr.get('resolve_s')}s, "
                         f"fill {fr.get('fill_s')}s")
            for row in fr["fields"]:
                if row["status"] in ("FAILED", "DIDNT-STICK"):
                    lines.append(f"    - {row['status']} [{row['tag']}/{row['type']}/{row['widget']}] "
                                 f"{row['label']!r} <- {row.get('value')!r}: {row.get('detail')} "
                                 f"(after settle {row.get('readback')!r})")
                elif row["status"] == "verified" and row.get("choice"):
                    # Reviewer round 5, blocker A: shows the settled read-back is judged against
                    # what applyFill() actually COMMITTED (entry._committedText), not against the
                    # service's own `value` — a differently-worded but correct choice ("Decline
                    # to self-identify" -> "I don't wish to answer") is expected to show
                    # committed == after-settle while value legitimately differs.
                    lines.append(f"    - chose [{row['type'] or row['tag']}] {row['label'][:70]!r} -> "
                                 f"{row.get('readback')!r} (committed {row.get('committed')!r}, "
                                 f"intended {row.get('value')!r}, {row.get('source')})")
            for u in fr.get("unseen", [])[:12]:
                lines.append(f"    - unseen {u['tag']}[{u['type'] or u['role']}] {u['label']!r} .{u['cls']}")
        lines.append(f"- total {rec.get('total_s')}s; non-GET requests blocked: {len(rec.get('blocked_requests', []))}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-ats", type=int, default=1)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--scanner", default=str(REPO / "extension" / "scanner.js"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--sponsor-profile", action="store_true",
                    help="use the synthetic applicant who needs sponsorship (H-1B) and has a salary set")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    out = pathlib.Path(args.out or tempfile.mkdtemp(prefix="apc-live-"))
    out.mkdir(parents=True, exist_ok=True)
    shots = out / "shots"
    shots.mkdir(exist_ok=True)
    scanner_src = pathlib.Path(args.scanner).read_text(encoding="utf-8")
    targets = [("url", u) for u in args.url] or pick_urls(args.per_ats, args.seed)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="apc-live-svc-"))
    port = free_port()
    proc, token = start_service(tmp, port, PROFILE_SPONSOR if args.sponsor_profile else PROFILE)
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(user_agent=UA, bypass_csp=True, viewport={"width": 1280, "height": 900})
            for i, (ats, url) in enumerate(targets):
                page = ctx.new_page()
                rec = probe(page, url, port, token, scanner_src, shots, f"{i:02d}-{ats}")
                results.append((ats, rec))
                print(f"[{ats}] {url[:90]} -> "
                      f"{rec.get('error') or sum(len(f.get('fields', [])) for f in rec['frames'])} fields "
                      f"({rec.get('total_s')}s)", flush=True)
                page.close()
            browser.close()
    finally:
        proc.kill()
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(summarize(results), encoding="utf-8")
    print(f"wrote {out / 'summary.md'}")


if __name__ == "__main__":
    main()
