"""Deterministic Greenhouse application pre-fill via CDP.

Connects to an already-running Chrome over CDP (launched by another module),
fills the standard Greenhouse application form fields, then disconnects.

Why this exists: doing first_name/last_name/email/phone/resume via an LLM
loop costs ~30 round-trips per application. These five fields have stable
selectors on Greenhouse, so we fill them deterministically and let the LLM
handle only the long-tail custom questions.

Critical invariant: this module MUST NOT kill Chrome. It connects over CDP
and only releases the CDP socket on cleanup. Chrome continues running for
downstream steps (LLM-driven question answering, submit, etc.).
"""

import logging
import re
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page

logger = logging.getLogger(__name__)

GREENHOUSE_HOSTS = ("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io")
ASHBY_HOSTS = ("ashbyhq.com", "jobs.ashbyhq.com")
WORKDAY_HOSTS = ("myworkdayjobs.com",)
GH_FIELDS = [
    ("first_name", "#first_name"),
    ("last_name",  "#last_name"),
    ("email",      "#email"),
    ("phone",      "#phone"),
]
GH_RESUME_PRIMARY = "input[type='file']#resume"
GH_RESUME_FALLBACK = "input[type='file'][name*='resume' i]"

GH_PROFILE_FIELDS = [
    ("linkedin", ("input[name*='linkedin' i]", "input[id*='linkedin' i]"), ("linkedin",), "linkedin_url"),
    ("portfolio", ("input[name*='portfolio' i]", "input[id*='portfolio' i]"), ("portfolio",), "portfolio_url"),
    ("website", ("input[name*='website' i]", "input[id*='website' i]"), ("website", "personal site"), "website_url"),
]

GH_LOCATION_LABELS = ("current location", "location (city", "where are you located", "city")
GH_PHONE_COUNTRY_SELECTORS = (
    "input[name='phone_country_code']",
    "input[id*='phone_country' i]",
    "input[aria-label*='phone country' i]",
    "input[aria-label*='country' i]",
)


def _canonicalize_greenhouse_url(url: str) -> str:
    """Rewrite vanity Greenhouse URLs to canonical boards.greenhouse.io form.

    careers.airbnb.com/positions/X?gh_jid=Y     -> boards.greenhouse.io/airbnb/jobs/Y
    careers.duolingo.com/jobs/X?gh_jid=Y        -> boards.greenhouse.io/duolingo/jobs/Y
    instacart.careers/job/?gh_jid=Y             -> boards.greenhouse.io/instacart/jobs/Y
    www.brex.com/careers/X?gh_jid=Y             -> boards.greenhouse.io/brex/jobs/Y

    Vanity hosts often lazy-load the form behind iframes or "Apply" gates,
    causing _find_form_root to timeout. Canonical URLs render the form
    synchronously. URLs already on boards.greenhouse.io / job-boards.greenhouse.io
    are returned unchanged. Unknown vanity patterns also return unchanged.
    """
    if not url:
        return url
    lowered = url.lower()
    if "boards.greenhouse.io" in lowered or "job-boards.greenhouse.io" in lowered:
        return url
    m = re.search(r"[?&]gh_jid=(\d+)", url, re.IGNORECASE)
    if not m:
        return url
    gh_jid = m.group(1)
    host = urlparse(url).netloc.lower()
    company: str | None = None
    if host.startswith("careers."):
        company = host.split(".")[1]
    elif host.endswith(".careers"):
        company = host.rsplit(".", 1)[0]
    elif host.startswith("jobs."):
        company = host.split(".")[1]
    else:
        # Fallback: www.<co>.<tld>/careers|positions|jobs?gh_jid= (brex,
        # and other companies that host the Greenhouse form on their main
        # marketing domain). The gh_jid param already proved this is a
        # Greenhouse job; derive the board slug from the registered domain
        # label (www.brex.com -> "brex"), gated on a careers-ish path so we
        # don't rewrite unrelated www links that merely carry a gh_jid.
        path = urlparse(url).path.lower()
        if any(seg in path for seg in ("/careers", "/positions", "/jobs", "/job")):
            labels = [p for p in host.split(".") if p and p != "www"]
            # labels like ["brex","com"] -> "brex"; skip if it looks like a
            # known ATS host we don't want to slugify.
            if len(labels) >= 2 and labels[0] not in (
                "greenhouse", "lever", "ashbyhq", "myworkdayjobs", "icims",
            ):
                company = labels[0]
    if not company:
        return url
    return f"https://boards.greenhouse.io/{company}/jobs/{gh_jid}"


def _detect_ats(url: str) -> str:
    if not url:
        return "unsupported"
    lowered = url.lower()
    for host in GREENHOUSE_HOSTS:
        if host in lowered:
            return "greenhouse"
    for host in ASHBY_HOSTS:
        if host in lowered:
            return "ashby"
    for host in WORKDAY_HOSTS:
        if host in lowered:
            return "workday"
    # Vanity domains. Many companies host the Greenhouse form on
    # careers.<company>.com or <company>.careers but Greenhouse always
    # appends ?gh_jid=<id> as the canonical job-id parameter. Treat any
    # URL with that param as Greenhouse — the DOM is the same form.
    if "gh_jid=" in lowered or "?gh_src=" in lowered:
        return "greenhouse"
    # Ashby fallback — only the explicit application-form path. Don't match a
    # bare "/application" + substring "ashby" anywhere; that false-positives
    # on URLs that happen to contain both tokens (e.g. a Greenhouse JD whose
    # query string mentions an Ashby competitor).
    if "ashby_applicationform" in lowered:
        return "ashby"
    return "unsupported"


def _split_name(full: str) -> tuple[str, str]:
    if not full:
        return ("", "")
    parts = full.strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _has_first_name(scope) -> bool:
    try:
        return scope.locator("#first_name").count() > 0
    except Exception:
        return False


def _find_form_root(page: Page, timeout_ms: int):
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if _has_first_name(page):
            return page
        try:
            frames = list(page.frames)
        except Exception:
            frames = []
        for fr in frames:
            if fr is page.main_frame:
                continue
            if _has_first_name(fr):
                return fr
        time.sleep(0.3)
    return None


def _remember(result: dict, field: str) -> None:
    if field not in result["fields_filled"]:
        result["fields_filled"].append(field)


def _dispatch_input_events(locator) -> None:
    locator.evaluate(
        "el => { "
        "el.dispatchEvent(new Event('input', {bubbles: true})); "
        "el.dispatchEvent(new Event('change', {bubbles: true})); "
        "}"
    )


def _fill_text_locator(root, selector: str, value: str, timeout_ms: int = 1200) -> bool:
    loc = root.locator(selector).first
    if loc.count() <= 0:
        return False
    loc.fill(value, timeout=timeout_ms)
    _dispatch_input_events(loc)
    try:
        return loc.input_value(timeout=800).strip() == value.strip()
    except Exception:
        return True


def _fill_text_by_label(root, label_needles: tuple[str, ...], value: str) -> bool:
    """Fill the first visible input/textarea whose label contains a needle."""
    return bool(root.evaluate(
        """({labelNeedles, value}) => {
          const needles = labelNeedles.map(s => s.toLowerCase());
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const setValue = el => {
            if (!el || el.disabled || el.readOnly) return false;
            if (!/^(INPUT|TEXTAREA)$/.test(el.tagName)) return false;
            const type = (el.getAttribute('type') || 'text').toLowerCase();
            if (['hidden', 'file', 'checkbox', 'radio', 'submit', 'button'].includes(type)) return false;
            el.focus();
            const proto = Object.getPrototypeOf(el);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, value); else el.value = value;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.blur();
            return true;
          };
          for (const label of document.querySelectorAll('label')) {
            const text = norm(label.innerText || label.textContent);
            if (!needles.some(n => text.includes(n))) continue;
            const forId = label.getAttribute('for');
            if (forId && setValue(document.getElementById(forId))) return true;
            if (setValue(label.querySelector('input, textarea'))) return true;
            let container = label.parentElement;
            for (let i = 0; container && i < 6; i += 1, container = container.parentElement) {
              if (setValue(container.querySelector('input:not([type=file]), textarea'))) return true;
            }
          }
          return false;
        }""",
        {"labelNeedles": list(label_needles), "value": value},
    ))


def _build_interest_answer(company: str, profile: dict | None, page_text: str = "") -> str:
    """Build a short, factual answer for required motivation fields."""
    experience = (profile or {}).get("experience", {}) or {}
    skills = (profile or {}).get("skills_boundary", {}) or {}
    current_title = experience.get("current_title") or "Product Designer"
    tools = set(skills.get("tools") or []) | set(skills.get("frameworks") or [])
    figma_clause = " including hands-on Figma experience" if "Figma" in tools else ""
    text = page_text.lower()
    if "customer education" in text or "learning resource" in text or "figma learn" in text:
        role_clause = (
            "customer education, design craft, and helping people learn complex products "
            "through clear experiences"
        )
        closing = "I would be excited to support learning resources that help users build confidence and succeed."
    else:
        role_clause = "product design, user research, and turning complex workflows into clear experiences"
        closing = "I would be excited to help build useful, polished product experiences for users."
    if company.lower() == "figma":
        alignment = (
            "Figma's mission to make design accessible aligns with how I approach product design: "
            "translating complex workflows into intuitive, polished experiences for broad audiences."
        )
    else:
        alignment = (
            "The role aligns with how I approach product design: translating complex workflows "
            "into intuitive, polished experiences for broad audiences."
        )
    return (
        f"I am interested in {company} because the role combines {role_clause}. "
        f"My background as a {current_title} includes SaaS and AI-enabled workflows, research, "
        f"interaction design, and cross-functional execution{figma_clause}. "
        f"{alignment} "
        f"{closing}"
    )


def _select_by_label(root, label_needles: tuple[str, ...], preferred: tuple[str, ...]) -> bool:
    """Set native select controls by nearby label text when options match."""
    return bool(root.evaluate(
        """({labelNeedles, preferred}) => {
          const needles = labelNeedles.map(s => s.toLowerCase());
          const prefs = preferred.map(s => s.toLowerCase());
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const choose = sel => {
            if (!sel || sel.disabled) return false;
            const options = Array.from(sel.options || []);
            const opt = options.find(o => prefs.some(p => norm(o.textContent).includes(p) || norm(o.value).includes(p)));
            if (!opt) return false;
            sel.value = opt.value;
            sel.dispatchEvent(new Event('input', {bubbles: true}));
            sel.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
          };
          for (const label of document.querySelectorAll('label')) {
            const text = norm(label.innerText || label.textContent);
            if (!needles.some(n => text.includes(n))) continue;
            const forId = label.getAttribute('for');
            if (forId && choose(document.getElementById(forId))) return true;
            const container = label.closest('div, fieldset, li') || label.parentElement;
            if (container && choose(container.querySelector('select'))) return true;
          }
          return false;
        }""",
        {"labelNeedles": list(label_needles), "preferred": list(preferred)},
    ))


def _select_combobox_by_label(root, label_needles: tuple[str, ...], preferred: tuple[str, ...]) -> bool:
    """Set a custom Greenhouse combobox by nearby label text.

    Greenhouse forms use react-select-style widgets where:
      - The visible trigger is a <div class="select__control">.
      - Options render asynchronously in a portal after click — they don't
        exist in the DOM until ~50-200ms after the trigger is clicked.

    Strategy: find the label, click the trigger, poll up to 800ms for options
    to appear, click the one whose text matches a preferred value. Falls back
    to native <select> first if one is present.

    Returns True iff a value was successfully selected.
    """
    selected = bool(root.evaluate(
        """async ({labelNeedles, preferred}) => {
          const needles = labelNeedles.map(s => s.toLowerCase());
          const prefs = preferred.map(s => s.toLowerCase()).filter(Boolean);
          if (!prefs.length) return false;
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          // Match: exact, OR option-text contains preferred, OR preferred
          // contains option-text WHEN BOTH are sufficiently long (≥4 chars).
          // The length floor stops bogus matches like option "No" against
          // preferred "i am not a protected veteran" (which contains "no"
          // via "not").
          const matches = text => {
            const t = norm(text);
            if (!t) return false;
            return prefs.some(p =>
              t === p ||
              t.includes(p) ||
              (p.length >= 4 && t.length >= 4 && p.includes(t))
            );
          };
          const sleep = ms => new Promise(r => setTimeout(r, ms));

          const trySelect = sel => {
            if (!sel || sel.disabled) return false;
            const options = Array.from(sel.options || []);
            const opt = options.find(o => matches(o.textContent) || matches(o.value));
            if (!opt) return false;
            const proto = Object.getPrototypeOf(sel);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(sel, opt.value); else sel.value = opt.value;
            sel.dispatchEvent(new Event('input', {bubbles: true}));
            sel.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
          };

          // react-select option finder. Greenhouse forms also include a
          // separate phone-country picker whose options have role="option"
          // and live in the DOM permanently — querying [role="option"] picks
          // those up and gives wrong matches. Use [class*="select__option"]
          // (the react-select-specific class) instead.
          const findOption = () => {
            const opts = Array.from(document.querySelectorAll(
              '[class*="select__option"]:not([class*="select__option-noresults"])'
            ));
            return opts.find(o => matches(o.textContent));
          };

          const tryCombobox = async trigger => {
            if (!trigger) return false;
            try {
              trigger.scrollIntoView({block: 'center'});
              // react-select listens for mousedown to open
              trigger.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
              trigger.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}));
              trigger.click();
            } catch (e) { return false; }
            // Poll up to 800ms for the options portal to render
            for (let i = 0; i < 16; i++) {
              await sleep(50);
              const opt = findOption();
              if (opt) {
                try {
                  opt.scrollIntoView({block: 'center'});
                  opt.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
                  opt.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}));
                  opt.click();
                  await sleep(80);
                  return true;
                } catch (e) { return false; }
              }
            }
            // Close any open dropdown by clicking elsewhere
            try { document.body.click(); } catch (e) {}
            return false;
          };

          // For each label whose text matches a needle, find the SCOPED
          // combobox/select that field is bound to.
          const labels = Array.from(document.querySelectorAll('label, legend'));
          for (const label of labels) {
            const text = norm(label.innerText || label.textContent);
            if (!text || !needles.some(n => text.includes(n))) continue;

            // 1. Native <select> via for=
            const forId = label.getAttribute('for');
            const target = forId ? document.getElementById(forId) : null;
            if (target?.tagName === 'SELECT' && trySelect(target)) return true;

            // 2. For Greenhouse react-select widgets, the label's `for=`
            //    points to a hidden <input> NESTED INSIDE the visible
            //    .select__control. So .closest() walks up from the input to
            //    the trigger directly. This is unambiguous — no scoping
            //    guesswork needed.
            let trigger = null;
            if (target) {
              trigger = target.closest('[class*="select__control" i], [class*="Select__control"]');
            }
            // Fall back: from the label, find the next .select__control
            // descendant in its tightest container.
            if (!trigger) {
              const container = label.closest('div, fieldset, li') || label.parentElement;
              if (container) {
                for (const sel of container.querySelectorAll('select')) {
                  if (trySelect(sel)) return true;
                }
                trigger = container.querySelector(
                  '[class*="select__control" i], [class*="Select__control"]'
                );
              }
            }
            if (trigger && await tryCombobox(trigger)) return true;
          }
          return false;
        }""",
        {"labelNeedles": list(label_needles), "preferred": list(preferred)},
    ))
    # Let react-select fully close before the next call hits the form. Without
    # this settle, consecutive comboboxes race each other and only the first
    # one sticks (subsequent ones see stale state and silently fail).
    if selected:
        time.sleep(0.25)
    return selected


def _combobox_committed(root, label_needles: tuple[str, ...], preferred: tuple[str, ...]) -> bool:
    """Did the react-select scoped to `label_needles` actually commit a value?

    The portal-click path in `_select_combobox_by_label` can report success
    (it found and clicked an option) while react-select's internal state never
    commits — the rendered control still shows the "Select..." placeholder and
    its hidden input keeps `value=""` / `aria-invalid="true"`. Greenhouse forms
    (Chime, Robinhood EEO) exhibit exactly this desync. This checks the *real*
    committed state so the caller can fall back to the keyboard path.
    """
    try:
        return bool(root.evaluate(
            """({labelNeedles, preferred}) => {
              const needles = labelNeedles.map(s => s.toLowerCase());
              const prefs = preferred.map(s => (s||'').toLowerCase()).filter(Boolean);
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const labels = Array.from(document.querySelectorAll('label, legend'));
              for (const label of labels) {
                const text = norm(label.innerText || label.textContent);
                if (!text || !needles.some(n => text.includes(n))) continue;
                const container = label.closest('div, fieldset, li') || label.parentElement;
                if (!container) continue;
                // Native <select> committed?
                for (const sel of container.querySelectorAll('select')) {
                  const v = norm(sel.options[sel.selectedIndex]?.textContent || sel.value);
                  if (v && prefs.some(p => v.includes(p) || p.includes(v))) return true;
                }
                // react-select: a committed value renders .select__single-value
                const sv = container.querySelector('[class*="singleValue" i], [class*="single-value" i]');
                if (sv) {
                  const v = norm(sv.textContent);
                  if (v && prefs.some(p => v.includes(p) || (p.length>=3 && v.includes(p)))) return true;
                }
                // Hidden input with a non-empty, non-invalid value
                const hidden = container.querySelector('input[aria-invalid], input[name], input[id*="react-select" i]');
                if (hidden && hidden.value && hidden.getAttribute('aria-invalid') !== 'true') return true;
              }
              return false;
            }""",
            {"labelNeedles": list(label_needles), "preferred": list(preferred)},
        ))
    except Exception:
        return False


def _commit_combobox_keyboard(root, label_needles: tuple[str, ...], preferred: tuple[str, ...]) -> bool:
    """Keyboard-driven react-select commit — the reliable path.

    Synthetic mousedown/click on portal options often fails to fire
    react-select's onChange (the desync). Driving the component through its
    OWN keyboard handlers (focus inner input → type query → ArrowDown →
    Enter) updates internal state correctly. This is the same pattern
    `_set_greenhouse_phone_country` already uses successfully.

    Strategy: JS tags the scoped react-select's inner <input> with a unique
    data attribute, then Playwright drives it with real key events (which
    `page.evaluate` cannot synthesize).
    """
    MARK = "data-applypilot-kbd"
    try:
        tagged = bool(root.evaluate(
            """({labelNeedles, MARK}) => {
              const needles = labelNeedles.map(s => s.toLowerCase());
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              document.querySelectorAll('[' + MARK + ']').forEach(e => e.removeAttribute(MARK));
              const labels = Array.from(document.querySelectorAll('label, legend'));
              for (const label of labels) {
                const text = norm(label.innerText || label.textContent);
                if (!text || !needles.some(n => text.includes(n))) continue;
                const forId = label.getAttribute('for');
                const byFor = forId ? document.getElementById(forId) : null;
                const container = label.closest('div, fieldset, li') || label.parentElement;
                // Prefer the react-select inner input; it's a text input
                // inside .select__control (often id=react-select-N-input).
                let input = null;
                if (byFor && byFor.tagName === 'INPUT') input = byFor;
                if (!input && container) {
                  input = container.querySelector(
                    '[class*="select__control" i] input, input[id*="react-select" i], [role="combobox"]'
                  );
                }
                if (input) { input.setAttribute(MARK, '1'); return true; }
              }
              return false;
            }""",
            {"labelNeedles": list(label_needles), "MARK": MARK},
        ))
        if not tagged:
            return False
        loc = root.locator(f"[{MARK}='1']").first
        if loc.count() <= 0:
            return False
        for value in preferred:
            if not value:
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=1000)
                loc.click(timeout=1500)
                # Clear any prior text, then type the query so react-select
                # filters its options down.
                try:
                    loc.fill("", timeout=800)
                except Exception:
                    pass
                loc.type(value, delay=25, timeout=2500)
                time.sleep(0.35)  # let react-select filter/render
                loc.press("ArrowDown", timeout=800)
                loc.press("Enter", timeout=800)
                time.sleep(0.3)
                if _combobox_committed(root, label_needles, (value,)):
                    return True
            except Exception as e:
                logger.debug("prefill: keyboard combobox attempt failed (%r): %s", value, e)
                continue
        return False
    except Exception as e:
        logger.debug("prefill: _commit_combobox_keyboard failed: %s", e)
        return False
    finally:
        try:
            root.evaluate(
                "(MARK) => document.querySelectorAll('['+MARK+']')"
                ".forEach(e => e.removeAttribute(MARK))",
                MARK,
            )
        except Exception:
            pass


def _select_combobox_robust(root, label_needles: tuple[str, ...], preferred: tuple[str, ...]) -> bool:
    """Portal-click first (fast, works on most forms); if the value didn't
    actually commit (react-select desync), fall back to the keyboard path.

    Returns True only when the value is *verified committed* — never a false
    positive (which previously caused prefill to report EEO/screening fields
    as filled when React had silently dropped them, e.g. Chime/Robinhood).
    """
    if _select_combobox_by_label(root, label_needles, preferred):
        if _combobox_committed(root, label_needles, preferred):
            return True
        logger.debug(
            "prefill: combobox %s click reported success but value did NOT "
            "commit — falling back to keyboard", label_needles,
        )
    return _commit_combobox_keyboard(root, label_needles, preferred)


def _set_greenhouse_phone_country(root) -> bool:
    """Set Greenhouse's React phone-country combobox to United States/+1."""
    try:
        for selector in GH_PHONE_COUNTRY_SELECTORS:
            loc = root.locator(selector).first
            if loc.count() <= 0:
                continue
            loc.click(timeout=1200)
            loc.fill("United States", timeout=1200)
            loc.press("ArrowDown", timeout=800)
            loc.press("Enter", timeout=800)
            try:
                value = loc.input_value(timeout=800).lower()
                if "united" in value or "+1" in value:
                    return True
            except Exception:
                return True
    except Exception as e:
        logger.debug("prefill: phone country direct combobox failed: %s", e)

    try:
        return bool(root.evaluate(
            """() => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const candidates = Array.from(document.querySelectorAll('[role="combobox"], input, button, [aria-haspopup="listbox"]'));
              const target = candidates.find(el => {
                const text = norm([
                  el.getAttribute('aria-label'),
                  el.getAttribute('name'),
                  el.getAttribute('id'),
                  el.closest('label')?.textContent,
                  el.closest('div, fieldset, li')?.textContent,
                ].filter(Boolean).join(' '));
                return text.includes('phone') && text.includes('country');
              }) || candidates.find(el => norm(el.closest('div, fieldset, li')?.textContent).includes('united states +1'));
              if (!target) return false;
              target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
              target.click();
              const input = target.matches('input') ? target : target.querySelector('input');
              if (input) {
                input.focus();
                input.value = 'United States';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
                input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
              }
              const options = Array.from(document.querySelectorAll('[role="option"], li, div'));
              const option = options.find(el => /united states|\\+1/.test(norm(el.textContent)));
              if (option) {
                option.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                option.click();
              }
              return true;
            }"""
        ))
    except Exception as e:
        logger.debug("prefill: phone country JS fallback failed: %s", e)

    try:
        return bool(root.evaluate(
            """() => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const phone = document.querySelector('#phone, input[name="phone"], input[type="tel"]');
              const controls = Array.from(document.querySelectorAll('button, [role="combobox"], [aria-haspopup="listbox"], [class*="select__control"]'));
              const scored = controls.map((el, idx) => {
                const box = el.getBoundingClientRect();
                const phoneBox = phone ? phone.getBoundingClientRect() : {top: 0, bottom: 0, left: 0};
                const text = norm([
                  el.textContent,
                  el.getAttribute('aria-label'),
                  el.getAttribute('id'),
                  el.getAttribute('name'),
                  el.closest('label')?.textContent,
                  el.closest('div, fieldset, li')?.textContent,
                ].filter(Boolean).join(' '));
                let score = 0;
                if (text.includes('country')) score += 8;
                if (text.includes('phone')) score += 8;
                if (text.includes('toggle flyout')) score += 4;
                if (phone && box.top < phoneBox.bottom + 80 && box.bottom > phoneBox.top - 80) score += 6;
                if (phone && box.left < phoneBox.left) score += 2;
                return {el, idx, score};
              }).filter(x => x.score > 0).sort((a, b) => b.score - a.score);
              const target = scored[0]?.el;
              if (!target) return false;
              target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
              target.click();
              const active = document.activeElement;
              const input = target.matches('input') ? target : (
                target.querySelector('input') ||
                (active && active.matches && active.matches('input') ? active : null)
              );
              if (input) {
                input.focus();
                input.value = 'United States';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
              }
              const option = Array.from(document.querySelectorAll('[role="option"], li, div'))
                .find(el => /united states|\\+1/.test(norm(el.textContent)));
              if (option) {
                option.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                option.click();
                return true;
              }
              if (input) {
                input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
                input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                return true;
              }
              return false;
            }"""
        ))
    except Exception as e:
        logger.debug("prefill: phone country scored fallback failed: %s", e)
        return False


def _set_greenhouse_location(root, value: str) -> bool:
    """Set Greenhouse's required location autocomplete and select a suggestion."""
    city_token = value.split(",", 1)[0].strip().lower()
    try:
        loc = root.locator("#candidate-location").first
        if loc.count() > 0:
            loc.scroll_into_view_if_needed(timeout=1500)
            loc.click(timeout=1500)
            loc.fill(value, timeout=1500)
            time.sleep(1.2)
            loc.press("ArrowDown", timeout=1000)
            loc.press("Enter", timeout=1000)
            time.sleep(0.7)
            selected = loc.evaluate(
                """el => {
                  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const container = el.closest('.select__container');
                  const selected = container?.querySelector('[class*="singleValue"], [class*="multiValue"]');
                  return norm(selected?.textContent || el.value || '');
                }"""
            )
            if city_token and city_token in selected and "united states" in selected:
                return True
    except Exception as e:
        logger.debug("prefill: location locator path failed: %s", e)

    try:
        filled = bool(root.evaluate(
            """async ({value}) => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const wanted = norm(value);
              const tokens = wanted.split(/[^a-z0-9]+/).filter(t => t.length >= 3);
              const cityToken = tokens[0] || '';
              const setNative = (el, nextValue) => {
                el.focus();
                const proto = Object.getPrototypeOf(el);
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, nextValue); else el.value = nextValue;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
              };
              const inputNear = label => {
                const forId = label.getAttribute('for');
                const byFor = forId ? document.getElementById(forId) : null;
                if (byFor) return byFor;
                const local = label.querySelector('input:not([type=hidden]), textarea');
                if (local) return local;
                let container = label.parentElement;
                for (let i = 0; container && i < 6; i += 1, container = container.parentElement) {
                  const nested = container.querySelector('input:not([type=hidden]), textarea');
                  if (nested) return nested;
                }
                return null;
              };
              const labels = Array.from(document.querySelectorAll('label'));
              let target = null;
              for (const label of labels) {
                const text = norm(label.innerText || label.textContent);
                if (!text.includes('location') && !text.includes('city')) continue;
                target = inputNear(label);
                if (target) break;
              }
              if (!target) return false;
              setNative(target, value);
              await new Promise(r => setTimeout(r, 600));
              const options = Array.from(document.querySelectorAll('[class*="select__option"]'))
                .filter(el => {
                  const text = norm(el.textContent);
                  if (!text || text.length > 250) return false;
                  if (cityToken && !text.includes(cityToken)) return false;
                  if (!text.includes('united states')) return false;
                  return tokens.filter(t => text.includes(t)).length >= Math.min(2, tokens.length);
                });
              const option = options[0];
              if (option) {
                option.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                option.click();
                await new Promise(r => setTimeout(r, 300));
              } else {
                target.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
                target.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                await new Promise(r => setTimeout(r, 300));
              }
              const finalValue = target.value || target.closest('div, fieldset, li')?.textContent || '';
              const finalText = norm(finalValue);
              return cityToken ? finalText.includes(cityToken) : !!finalText;
            }""",
            {"value": value},
        ))
        return filled
    except Exception as e:
        logger.debug("prefill: location autocomplete failed: %s", e)
        return False


def _force_visible_greenhouse_basics(root, first: str, last: str, email: str, phone: str, location: str) -> list[str]:
    """Final pass for visible Greenhouse fields after React re-renders."""
    try:
        return list(root.evaluate(
            """async ({first, last, email, phone, location}) => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const lower = s => norm(s).toLowerCase();
              const visible = el => {
                if (!el) return false;
                const box = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const setNative = (el, value) => {
                if (!el || !visible(el) || el.disabled || el.readOnly) return false;
                const tag = el.tagName;
                if (!/^(INPUT|TEXTAREA)$/.test(tag)) return false;
                const type = (el.getAttribute('type') || 'text').toLowerCase();
                if (['hidden', 'file', 'checkbox', 'radio', 'submit', 'button'].includes(type)) return false;
                el.focus();
                const proto = Object.getPrototypeOf(el);
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value); else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur();
                return norm(el.value).length > 0;
              };
              const fieldFor = needles => {
                for (const label of Array.from(document.querySelectorAll('label')).filter(visible)) {
                  const text = lower(label.innerText || label.textContent);
                  if (!needles.some(n => text.includes(n))) continue;
                  const forId = label.getAttribute('for');
                  const byFor = forId ? document.getElementById(forId) : null;
                  if (visible(byFor)) return byFor;
                  const local = label.querySelector('input:not([type=hidden]), textarea');
                  if (visible(local)) return local;
                  let container = label.parentElement;
                  for (let i = 0; container && i < 6; i += 1, container = container.parentElement) {
                    const nested = container.querySelector('input:not([type=hidden]), textarea');
                    if (visible(nested)) return nested;
                  }
                }
                return null;
              };
              const selectedText = el => {
                const container = el?.closest?.('.select__container');
                const selected = container?.querySelector('[class*="singleValue"], [class*="multiValue"]');
                return lower(selected?.textContent || el?.value || '');
              };
              const changed = [];
              if (first && setNative(fieldFor(['first name']), first)) changed.push('first_name');
              if (last && setNative(fieldFor(['last name']), last)) changed.push('last_name');
              if (email && setNative(fieldFor(['email']), email)) changed.push('email');
              if (phone && setNative(fieldFor(['phone']), phone)) changed.push('phone');

              const loc = fieldFor(['location', 'city']);
              if (loc && location) {
                setNative(loc, location + ', United States');
                await new Promise(r => setTimeout(r, 500));
                const option = Array.from(document.querySelectorAll('[class*="select__option"]'))
                  .filter(visible)
                  .find(el => lower(el.textContent).includes('san jose') &&
                    lower(el.textContent).includes('california') &&
                    lower(el.textContent).includes('united states'));
                if (option) {
                  option.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                  option.click();
                  await new Promise(r => setTimeout(r, 250));
                } else {
                  loc.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
                  loc.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                  await new Promise(r => setTimeout(r, 250));
                }
                const locText = selectedText(loc);
                if (locText.includes('san jose') && locText.includes('united states')) {
                  changed.push('location');
                }
              }
              return changed;
            }""",
            {
                "first": first,
                "last": last,
                "email": email,
                "phone": phone,
                "location": location,
            },
        ))
    except Exception as e:
        logger.debug("prefill: final visible basics pass failed: %s", e)
        return []


def _company_from_apply_url(apply_url: str) -> str:
    parts = [p for p in urlparse(apply_url).path.split("/") if p]
    if parts:
        raw = parts[0]
    else:
        raw = urlparse(apply_url).netloc.split(".")[0]
    raw = raw.replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", raw).title() or "the company"


def _prefill_basic_adapter_fields(root, profile: dict, resume_pdf_path: str, result: dict) -> None:
    """Best-effort non-Greenhouse prefill for Ashby/Workday/custom forms."""
    personal = (profile or {}).get("personal", {}) or {}
    full_name = personal.get("full_name", "") or ""
    first, last = _split_name(full_name)
    email = personal.get("email", "") or ""
    phone = personal.get("phone", "") or ""

    basic_text_fields = [
        ("first_name", first, (
            "input[name*='first' i]", "input[id*='first' i]", "input[autocomplete='given-name']",
        ), ("first name", "given name")),
        ("last_name", last, (
            "input[name*='last' i]", "input[id*='last' i]", "input[autocomplete='family-name']",
        ), ("last name", "family name", "surname")),
        ("email", email, (
            "input[type='email']", "input[name*='email' i]", "input[id*='email' i]",
        ), ("email", "email address")),
        ("phone", phone, (
            "input[type='tel']", "input[name*='phone' i]", "input[id*='phone' i]",
        ), ("phone", "mobile", "phone number")),
    ]

    for short, value, selectors, labels in basic_text_fields:
        if not value:
            continue
        try:
            filled = False
            for selector in selectors:
                if _fill_text_locator(root, selector, value, timeout_ms=1600):
                    filled = True
                    break
            if not filled:
                filled = _fill_text_by_label(root, labels, value)
            if filled:
                _remember(result, short)
        except Exception as e:
            logger.debug("prefill: %s adapter fill failed for %s: %s", result.get("ats"), short, e)

    # Resume upload (best effort): works for Ashby and many Workday/custom forms.
    try:
        uploaded = False
        for sel in (
            "input[type='file'][name*='resume' i]",
            "input[type='file'][id*='resume' i]",
            "input[type='file'][name*='cv' i]",
            "input[type='file'][id*='cv' i]",
            # Intentionally NOT falling through to a bare input[type='file'].
            # Pages with cover-letter or portfolio uploads have multiple
            # file inputs; an unguarded fallback puts the resume into the
            # wrong field and silently reports success.
        ):
            inp = root.locator(sel).first
            if inp.count() <= 0:
                continue
            try:
                inp.set_input_files(resume_pdf_path, timeout=3000)
                uploaded = True
                break
            except Exception as e:
                logger.debug("prefill: %s resume upload via %s failed: %s", result.get("ats"), sel, e)
        if uploaded:
            _remember(result, "resume")
    except Exception as e:
        logger.debug("prefill: %s resume adapter flow failed: %s", result.get("ats"), e)


def prefill_application(
    cdp_port: int,
    apply_url: str,
    profile: dict,
    resume_pdf_path: str,
    timeout_s: int = 12,
) -> dict:
    """Pre-fill deterministic ATS fields via CDP. Returns
    {"ats", "fields_filled", "error", "duration_ms"}. Does not kill Chrome.
    """
    started = time.monotonic()
    result: dict = {
        "ats": "unsupported",
        "fields_filled": [],
        "error": None,
        "duration_ms": 0,
    }

    ats = _detect_ats(apply_url)
    result["ats"] = ats
    if ats not in {"greenhouse", "ashby", "workday"}:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    # Rewrite vanity Greenhouse URLs (careers.airbnb.com, instacart.careers, ...)
    # to the canonical boards.greenhouse.io form. Vanity hosts lazy-load the
    # form behind iframes/buttons and cause _find_form_root timeouts.
    if ats == "greenhouse":
        apply_url = _canonicalize_greenhouse_url(apply_url)

    if ats in {"ashby", "workday"}:
        pw = None
        browser = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{cdp_port}", timeout=4000
            )
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if apply_url not in (page.url or ""):
                page.goto(
                    apply_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_s * 1000,
                )
            _prefill_basic_adapter_fields(page, profile, resume_pdf_path, result)
            return result
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            return result
        finally:
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass

    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}", timeout=4000
        )
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if apply_url not in (page.url or ""):
            page.goto(
                apply_url,
                wait_until="domcontentloaded",
                timeout=timeout_s * 1000,
            )

        root = _find_form_root(page, timeout_ms=5000)
        if root is None:
            result["error"] = "no_form_detected"
            return result

        # Wait for React to mount custom comboboxes (.select__control). Without
        # this, the field-fill loop races ahead and the combobox helper finds
        # zero triggers. We poll up to 3s; if no comboboxes appear, the form
        # is plain text-only and we proceed.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                if root.locator(
                    '[class*="select__control"], [role="combobox"]'
                ).count() > 0:
                    break
            except Exception:
                pass
            time.sleep(0.15)

        personal = (profile or {}).get("personal", {}) or {}
        full_name = personal.get("full_name", "") or ""
        first, last = _split_name(full_name)
        email = personal.get("email", "") or ""
        phone = personal.get("phone", "") or ""
        city = personal.get("city", "") or ""
        state = personal.get("province_state", "") or ""
        work_location = ", ".join(part for part in (city, state) if part)
        preferred_name = personal.get("preferred_name", "") or first
        company = _company_from_apply_url(apply_url)

        values = {
            "first_name": first,
            "last_name":  last,
            "email":      email,
            "phone":      phone,
        }

        for short, selector in GH_FIELDS:
            value = values.get(short, "")
            if not value:
                continue
            try:
                loc = root.locator(selector).first
                # Playwright's .fill() simulates real keystrokes which React's
                # input listeners pick up correctly for most fields.
                loc.fill(value, timeout=1500)
                actual = loc.input_value(timeout=1000)
                if actual == value:
                    _remember(result, short)
                    continue
                # React rejected the value — try the prototype value setter
                # path (React's synthetic event system listens for the
                # native `value` property descriptor's set call).
                loc.evaluate(
                    "(el, value) => { "
                    "el.focus(); "
                    "const proto = Object.getPrototypeOf(el); "
                    "const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set; "
                    "if (setter) setter.call(el, value); else el.value = value; "
                    "el.dispatchEvent(new Event('input', {bubbles: true})); "
                    "el.dispatchEvent(new Event('change', {bubbles: true})); "
                    "el.blur(); "
                    "}",
                    value,
                )
                actual = loc.input_value(timeout=1000)
                if actual == value:
                    _remember(result, short)
                else:
                    logger.debug(
                        "prefill: %s did not persist (wanted %r, got %r) - React rejected",
                        short, value, actual,
                    )
            except Exception as e:
                logger.debug("prefill: failed to fill %s (%s): %s", short, selector, e)

        if phone:
            try:
                if _select_by_label(root, ("phone country", "country"), ("united states", "+1")) or \
                        _select_combobox_by_label(root, ("phone country", "country"), ("united states", "+1")) or \
                        _set_greenhouse_phone_country(root):
                    _remember(result, "phone_country")
            except Exception as e:
                logger.debug("prefill: failed to fill phone country: %s", e)

        for short, selectors, labels, profile_key in GH_PROFILE_FIELDS:
            value = personal.get(profile_key, "") or ""
            if short == "website" and not value:
                value = personal.get("portfolio_url", "") or ""
            if not value:
                continue
            try:
                filled = False
                for selector in selectors:
                    if _fill_text_locator(root, selector, value):
                        filled = True
                        break
                if not filled:
                    filled = _fill_text_by_label(root, labels, value)
                if filled:
                    _remember(result, short)
            except Exception as e:
                logger.debug("prefill: failed to fill %s: %s", short, e)

        location_value = ", ".join(
            part for part in [
                personal.get("city", ""),
                personal.get("province_state", ""),
            ]
            if part
        )
        if location_value:
            try:
                if _set_greenhouse_location(root, f"{location_value}, United States") or \
                        _fill_text_by_label(root, GH_LOCATION_LABELS, location_value):
                    _remember(result, "location")
            except Exception as e:
                logger.debug("prefill: failed to fill location: %s", e)

        try:
            if preferred_name and _fill_text_by_label(root, ("preferred first name", "preferred name"), preferred_name):
                _remember(result, "preferred_name")
        except Exception as e:
            logger.debug("prefill: failed to fill preferred name: %s", e)

        try:
            page_text = root.evaluate("() => document.body?.innerText || ''")
            why_answer = _build_interest_answer(company, profile, page_text)
            if _fill_text_by_label(
                root,
                ("why do you want to join", "why do you want", "why are you interested"),
                why_answer,
            ):
                _remember(result, "why_join")
        except Exception as e:
            logger.debug("prefill: failed to fill why-join text: %s", e)

        try:
            if work_location and _fill_text_by_label(
                root,
                ("from where do you intend to work", "where do you intend to work", "work location"),
                work_location,
            ):
                _remember(result, "intended_work_location")
        except Exception as e:
            logger.debug("prefill: failed to fill intended work location: %s", e)

        # Work auth + sponsorship: try native <select> first, then custom combobox.
        # Most Greenhouse forms use react-select for these, so the combobox path
        # is the one that usually fires.
        work_auth = (profile or {}).get("work_authorization", {}) or {}
        auth_value = str(work_auth.get("legally_authorized_to_work", "")).lower()
        needs_sponsorship = str(work_auth.get("require_sponsorship", "")).lower()
        try:
            if auth_value:
                if "yes" in auth_value or "true" in auth_value:
                    if "yes" in needs_sponsorship or "true" in needs_sponsorship:
                        auth_yes = (
                            "valid work permit which needs to be sponsored",
                            "needs to be sponsored by the company",
                        )
                    else:
                        auth_yes = (
                            "valid work permit and do not need a company to sponsor",
                            "do not need a company to sponsor",
                        )
                else:
                    auth_yes = ("not authorized", "no")
                auth_labels = (
                    "eligible to work",
                    "authorized to work",
                    "legally authorized",
                    "legally eligible",
                    "work authorization",
                    "currently eligible",
                    "authorized",
                )
                if _select_by_label(root, auth_labels, auth_yes) or \
                        _select_combobox_robust(root, auth_labels, auth_yes):
                    _remember(result, "work_authorization")
            if needs_sponsorship:
                sponsorship_value = ("yes",) if "yes" in needs_sponsorship or "true" in needs_sponsorship else ("no",)
                spons_labels = ("sponsorship", "sponsor", "visa", "require sponsorship")
                if _select_by_label(root, spons_labels, sponsorship_value) or \
                        _select_combobox_robust(root, spons_labels, sponsorship_value):
                    _remember(result, "sponsorship")
        except Exception as e:
            logger.debug("prefill: failed to fill work auth dropdowns: %s", e)

        # Other common Greenhouse screening questions. These have stable wording
        # across companies and stable expected answers from the profile.
        try:
            # Have you previously worked at <Company>? -> No
            if _select_by_label(root, ("previously worked", "worked here", "worked at"), ("no",)) or \
                    _select_combobox_robust(root, ("previously worked", "worked here", "worked at", "employee or a contractor"), ("no",)):
                _remember(result, "previously_worked")
            # Are you 18 or older? -> Yes
            if _select_by_label(root, ("18 or older", "at least 18", "over 18"), ("yes",)) or \
                    _select_combobox_robust(root, ("18 or older", "at least 18", "over 18"), ("yes",)):
                _remember(result, "age_18")
        except Exception as e:
            logger.debug("prefill: failed to fill standard screening dropdowns: %s", e)

        eeo = (profile or {}).get("eeo_voluntary", {}) or {}
        # For EEO we accept the profile value OR any "decline" variant. Order
        # matters: profile value first (so we don't overwrite a real answer
        # with "decline"), then fallbacks.
        # "i don't wish to answer" / "i do not wish to answer" / "decline to
        # self-identify" / "prefer not to answer" are all the same intent but
        # appear with various wordings/contractions across Greenhouse forms.
        # Keep all of them in fallbacks so substring matching catches each.
        decline_variants = (
            "decline to self-identify",
            "decline",
            "i do not wish to answer",
            "i don't wish to answer",
            "do not wish",
            "don't wish",
            "wish to answer",
            "prefer not to answer",
            "prefer not",
        )
        eeo_targets = [
            ("gender", ("gender",),
             (eeo.get("gender", ""),) + decline_variants),
            ("race", ("race", "ethnicity", "identify your race"),
             (eeo.get("race_ethnicity", ""),) + decline_variants),
            ("hispanic_latino", ("hispanic", "latino"),
             decline_variants + ("no", "not hispanic", "not latino")),
            ("veteran", ("veteran status", "veteran"),
             (eeo.get("veteran_status", ""),
              "i am not a protected veteran",
              "not a protected veteran",
              "not a protected",
             ) + decline_variants),
            ("disability", ("disability status", "disability"),
             (eeo.get("disability_status", ""),
              "no, i do not have a disability",
              "i don't have a disability",
             ) + decline_variants),
        ]
        for short, labels, preferred in eeo_targets:
            choices = tuple(str(p) for p in preferred if p)
            if not choices:
                continue
            try:
                if _select_by_label(root, labels, choices) or \
                        _select_combobox_robust(root, labels, choices):
                    _remember(result, short)
            except Exception as e:
                logger.debug("prefill: failed to fill %s EEO dropdown: %s", short, e)

        if resume_pdf_path:
            uploaded = False
            for sel in (GH_RESUME_PRIMARY, GH_RESUME_FALLBACK):
                try:
                    file_input = root.locator(sel).first
                    if file_input.count() <= 0:
                        continue
                    # set_input_files internally fires input+change events on
                    # the input element. Greenhouse re-renders the form after
                    # accepting the file, which can detach the original input
                    # element — so we must NOT touch the locator after this
                    # call (any post-upload .evaluate() will time out waiting
                    # for the now-detached node).
                    file_input.set_input_files(resume_pdf_path, timeout=2500)
                    _remember(result, "resume")
                    uploaded = True
                    break
                except Exception as e:
                    logger.debug("prefill: resume upload via %s failed: %s", sel, e)
            if not uploaded:
                logger.debug("prefill: no resume input matched")

        try:
            for short in _force_visible_greenhouse_basics(
                root, first, last, email, phone, work_location
            ):
                _remember(result, short)
        except Exception as e:
            logger.debug("prefill: failed final visible basics pass: %s", e)

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        # Browser.close() on a CDP-connected browser releases only the CDP socket;
        # Chrome itself keeps running. This is the load-bearing invariant.
        if browser is not None:
            try:
                browser.close()
            except Exception as e:
                logger.debug("prefill: browser.close() failed: %s", e)
        if pw is not None:
            try:
                pw.stop()
            except Exception as e:
                logger.debug("prefill: playwright.stop() failed: %s", e)
        result["duration_ms"] = int((time.monotonic() - started) * 1000)

    return result
