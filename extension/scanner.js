/**
 * ApplyPilot Copilot — field scanning + filling core.
 *
 * This file has NO Chrome-extension APIs in it on purpose: it only touches the DOM. That lets
 * the exact same code run as a content script (loaded by manifest.json's `scripting` calls)
 * and be exercised offline in Node via jsdom in selftest.js, so scanning logic can be verified
 * without a live browser.
 *
 * Exposed as `ApplyPilotScanner` on the global object (content-script world's `self`, or a
 * jsdom window when eval'd for testing). Also exports via `module.exports` when a CommonJS
 * `module` global is present, though the shipped extension never uses that path.
 */
(function (root, factory) {
  if (typeof module === 'object' && module && module.exports) {
    module.exports = factory();
  } else {
    root.ApplyPilotScanner = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var EXCLUDED_INPUT_TYPES = ['hidden', 'submit', 'button', 'image', 'reset'];

  // ---------------------------------------------------------------------
  // small utilities
  // ---------------------------------------------------------------------

  function cleanText(s) {
    return (s || '').replace(/\s+/g, ' ').trim();
  }

  // Strips a trailing required-question marker ("*", "✱", possibly preceded by whitespace)
  // off a resolved question/group label -- Greenhouse and Ashby use "*", Lever uses "✱". Only
  // ever applied to a LABEL string, never to an option's own text.
  function stripRequiredMarker(s) {
    return cleanText(String(s || '').replace(/\s*[*✱]+\s*$/, ''));
  }

  function cssEscape(str) {
    str = String(str);
    if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(str);
    // Minimal fallback: escape anything that isn't a safe identifier character.
    return str.replace(/([^\w-])/g, '\\$1');
  }

  function realmOf(el) {
    return (el && el.ownerDocument && el.ownerDocument.defaultView) || (typeof window !== 'undefined' ? window : null);
  }

  function ownerDoc(el) {
    return (el && el.ownerDocument) || (typeof document !== 'undefined' ? document : null);
  }

  // ---------------------------------------------------------------------
  // visibility
  // ---------------------------------------------------------------------

  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    var view = realmOf(el);
    if (!view || !view.getComputedStyle) return true; // best-effort if we somehow lack a view
    var node = el;
    while (node && node.nodeType === 1) {
      var style = view.getComputedStyle(node);
      if (!style) break;
      if (style.display === 'none') return false;
      if (style.visibility === 'hidden' || style.visibility === 'collapse') return false;
      if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') return false;
      node = node.parentElement;
    }
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  // ---------------------------------------------------------------------
  // custom select widgets (select2 / Chosen / React-select / Avature, ...)
  // ---------------------------------------------------------------------
  //
  // These libraries hide the real <select> (display:none, visually-hidden, or
  // aria-hidden) and render a styled div/span in its place that the operator
  // actually sees and interacts with. The plain isVisible() check above
  // correctly drops the hidden native <select> -- which makes the whole
  // field vanish from the scan, even though it is perfectly fillable: set
  // the hidden select's value programmatically and dispatch `change`, and
  // every one of these widget libraries re-renders itself off that event.
  //
  // Detection is purely structural, never by clicking anything (see
  // isClickSafe()'s file-level invariant -- this module must never grow a
  // new .click() path):
  //   - well-known marker classes/attributes these libraries put on the
  //     <select> itself (select2-hidden-accessible, chosen-select/
  //     chosen-processed/chzn-done, aria-hidden="true");
  //   - well-known marker classes/roles on the replacement widget node
  //     (select2/select2-container, chosen-container, role=combobox,
  //     aria-haspopup=listbox);
  //   - falling back to "the select's next visible sibling, or another
  //     visible child of its immediate wrapper" when the select itself
  //     carries one of those hidden-native markers, so a widget library we
  //     don't recognize by name still gets picked up as long as it left
  //     evidence that it took the select over.

  var CUSTOM_WIDGET_CLASS_RE = /select2|chosen|combobox|custom-select|react-select|dropdown/i;
  var HIDDEN_NATIVE_SELECT_MARKER_RE = /select2-hidden-accessible|chosen-select|chosen-processed|chzn-done/i;

  function looksLikeSelectWidget(node) {
    if (!node || node.nodeType !== 1) return false;
    var cls = (node.getAttribute && node.getAttribute('class')) || '';
    if (CUSTOM_WIDGET_CLASS_RE.test(cls)) return true;
    var role = node.getAttribute && node.getAttribute('role');
    if (role === 'combobox' || role === 'listbox') return true;
    if (node.getAttribute && node.getAttribute('aria-haspopup') === 'listbox') return true;
    if (node.querySelector) {
      try {
        if (node.querySelector('[role="combobox"], [aria-haspopup="listbox"]')) return true;
      } catch (e) { /* ignore */ }
    }
    return false;
  }

  /**
   * Finds the visible custom widget standing in for a hidden native
   * <select>, if any. Returns the widget element, or null when `select` is
   * just an ordinary hidden field with no replacement widget (correctly
   * left excluded from the scan).
   */
  function findPairedWidget(select) {
    if (!select || select.tagName !== 'SELECT') return null;

    var cls = select.className || '';
    var markedHidden = HIDDEN_NATIVE_SELECT_MARKER_RE.test(String(cls)) ||
      (select.getAttribute && select.getAttribute('aria-hidden') === 'true');

    // 1. next element sibling(s) -- select2/Chosen both insert their widget
    //    immediately after the original <select> in the DOM.
    var sib = select.nextElementSibling;
    var hops = 0;
    while (sib && hops < 3) {
      if (isVisible(sib) && looksLikeSelectWidget(sib)) return sib;
      sib = sib.nextElementSibling;
      hops++;
    }

    // 2. shared wrapper -- a custom widget (Avature-style, React select, ...)
    //    that renders both the hidden <select> and its styled replacement as
    //    sibling children of the same parent. Deliberately bounded to a
    //    SMALL parent (a dedicated per-field wrapper div realistically has
    //    only the select + its widget, maybe + an error span -- a handful
    //    of children at most). Without this bound, a select sitting
    //    directly in a big <form> (the common case -- most fields on a real
    //    application form are direct children of one <form>) would treat
    //    every other field's widget-shaped element anywhere in that form as
    //    "its" pairing, which is exactly the kind of wrong-field mistake
    //    this module cannot afford.
    var parent = select.parentElement;
    var MAX_WRAPPER_CHILDREN = 6;
    if (parent && parent.children.length <= MAX_WRAPPER_CHILDREN) {
      var children = Array.prototype.slice.call(parent.children);
      for (var i = 0; i < children.length; i++) {
        var child = children[i];
        if (child === select) continue;
        if (isVisible(child) && looksLikeSelectWidget(child)) return child;
      }
    }

    // 3. last resort: the select itself carries a known "I've been replaced"
    //    marker, but nothing nearby matched a widget class/role by name --
    //    fall back to the nearest visible sibling rather than silently
    //    dropping the field, but ONLY when there is real evidence (a
    //    marker) that a widget library actually took this select over, and
    //    only within that same small-wrapper bound as step 2.
    if (markedHidden) {
      var s2 = select.nextElementSibling;
      if (s2 && isVisible(s2)) return s2;
      if (parent && parent.children.length <= MAX_WRAPPER_CHILDREN) {
        var kids = Array.prototype.slice.call(parent.children).filter(function (c) { return c !== select; });
        for (var k = 0; k < kids.length; k++) {
          if (isVisible(kids[k])) return kids[k];
        }
      }
    }

    return null;
  }

  // ---------------------------------------------------------------------
  // label resolution
  // ---------------------------------------------------------------------

  function nodeText(node) {
    if (!node) return '';
    if (node.nodeType === 3) { // TEXT_NODE
      var t = cleanText(node.textContent);
      return t.length >= 2 ? t : '';
    }
    if (node.nodeType === 1) { // ELEMENT_NODE
      var tag = node.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'INPUT' || tag === 'SELECT' ||
          tag === 'TEXTAREA' || tag === 'BUTTON') return '';
      var t2 = cleanText(node.textContent);
      if (t2 && t2.length < 200) return t2;
    }
    return '';
  }

  function getPrecedingText(el) {
    var node = el;
    for (var depth = 0; depth < 4 && node; depth++) {
      var sib = node.previousSibling;
      while (sib) {
        var t = nodeText(sib);
        if (t) return t;
        sib = sib.previousSibling;
      }
      node = node.parentElement;
    }
    return '';
  }

  function getLabel(el) {
    var doc = ownerDoc(el);

    // 1. <label for="id">
    if (el.id && doc) {
      var forLabel = null;
      try { forLabel = doc.querySelector('label[for="' + cssEscape(el.id) + '"]'); } catch (e) { /* ignore */ }
      if (forLabel) {
        var t1 = cleanText(forLabel.textContent);
        if (t1) return t1;
      }
    }

    // 2. wrapping <label>
    var wrap = el.closest ? el.closest('label') : null;
    if (wrap) {
      var clone = wrap.cloneNode(true);
      var controls = clone.querySelectorAll('input, select, textarea, script, style');
      for (var i = 0; i < controls.length; i++) controls[i].remove();
      var t2 = cleanText(clone.textContent);
      if (t2) return t2;
    }

    // 3. aria-label
    var ariaLabel = el.getAttribute && el.getAttribute('aria-label');
    if (ariaLabel) {
      var t3 = cleanText(ariaLabel);
      if (t3) return t3;
    }

    // 4. aria-labelledby
    var labelledBy = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledBy && doc) {
      var ids = labelledBy.split(/\s+/).filter(Boolean);
      var parts = [];
      for (var j = 0; j < ids.length; j++) {
        var ref = doc.getElementById(ids[j]);
        if (ref) {
          var pt = cleanText(ref.textContent);
          if (pt) parts.push(pt);
        }
      }
      if (parts.length) return parts.join(' ');
    }

    // 5. placeholder
    if (el.placeholder) {
      var t5 = cleanText(el.placeholder);
      if (t5) return t5;
    }

    // 6. nearby preceding text node
    var nearby = getPrecedingText(el);
    if (nearby) return nearby;

    return '';
  }

  // For a hidden native <select> paired with a visible custom widget: try the
  // select's own label first (a `<label for>` pointing at the select's id is
  // still valid even though the select itself is hidden), then fall back to
  // labelling from the widget the operator actually sees.
  function getFieldLabel(el, widget) {
    var label = getLabel(el);
    if (label) return label;
    if (widget) {
      var widgetLabel = getLabel(widget);
      if (widgetLabel) return widgetLabel;
    }
    return '';
  }

  // ---------------------------------------------------------------------
  // section context — which repeating block (Work Experience 2, Education 1, ...) a
  // field belongs to. Workday-style "My Experience" steps render N near-identical blocks
  // of fields; without this, the fill service has no way to map a field to
  // `work_history[index]` / `education[index]` in the profile.
  // ---------------------------------------------------------------------

  // Only these words make a nearby number look like a section index. Deliberately narrow:
  // a phone number, a year, a zip code, a "2 years experience" select option etc. must NOT
  // be mistaken for a section index just because a digit sits near it.
  // Every alternative here is either multi-word or a noun that does not show up next to a
  // stray number in normal form copy. Deliberately NOT included: bare "experience" and bare
  // "years" — "5+ years of experience" would otherwise read as section index 5, which is
  // exactly the class of false positive that puts your current job in the wrong block.
  // `s?` on the pluralisable ones: real headings say "Prior Roles", not "Prior Role".
  var SECTION_KEYWORD_SRC = '(' + [
    'work\\s*experience', 'work\\s*history', 'employment\\s*history',
    'career\\s*history', 'employment', 'employers?',
    'prior\\s*roles?', 'previous\\s*roles?', 'roles?',
    'education', 'positions?', 'schools?', 'universit(?:y|ies)',
    'colleges?', 'degrees?', 'jobs?',
  ].join('|') + ')';

  function extractSectionIndex(text) {
    if (!text) return null;
    var s = String(text);

    // Array/bracket style: "experience[1].title", "workHistory[2]" — a strong, low-risk
    // signal on its own (bounded to 1-2 digits so it can't swallow a longer id/hash run).
    var bracket = s.match(/\[(\d{1,2})\](?!\d)/);
    if (bracket) return parseInt(bracket[1], 10);

    // Keyword immediately followed by a short separator run then a 1-2 digit number, e.g.
    // "workExperience-2--jobTitle", "education_3_school", "Work Experience 2".
    var reAfter = new RegExp(SECTION_KEYWORD_SRC + '[^a-z0-9]{0,3}(\\d{1,2})(?!\\d)', 'i');
    var mAfter = s.match(reAfter);
    if (mAfter) return parseInt(mAfter[2], 10);

    // Number-before-keyword order, e.g. "2-education-school".
    var reBefore = new RegExp('(\\d{1,2})(?!\\d)[^a-z0-9]{0,3}' + SECTION_KEYWORD_SRC, 'i');
    var mBefore = s.match(reBefore);
    if (mBefore) return parseInt(mBefore[1], 10);

    return null;
  }

  function isHeadingLike(node) {
    if (!node || node.nodeType !== 1) return false;
    var tag = node.tagName;
    if (tag === 'LABEL' || tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' ||
        tag === 'BUTTON' || tag === 'SCRIPT' || tag === 'STYLE') return false;
    if (/^H[1-6]$/.test(tag)) return true;
    var role = node.getAttribute && node.getAttribute('role');
    if (role === 'heading') return true;
    var cls = (node.getAttribute && node.getAttribute('class')) || '';
    if (/title|heading/i.test(cls)) return true;
    return false;
  }

  // Nearest preceding heading-like element, walking sibling-then-up. Bounded at the
  // enclosing <form> (or body/html) so a page-level <h1> title never gets mistaken for a
  // field's section — that boundary is what keeps this "nearest enclosing", not "any
  // heading anywhere above this field on the page".
  function findPrecedingHeading(el) {
    var node = el;
    for (var depth = 0; depth < 8 && node; depth++) {
      if (node.tagName === 'FORM' || node.tagName === 'BODY' || node.tagName === 'HTML') break;
      var sib = node.previousElementSibling;
      while (sib) {
        if (isHeadingLike(sib)) {
          var t = cleanText(sib.textContent);
          if (t) return t;
        }
        if (sib.querySelector) {
          var inner = null;
          try { inner = sib.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"]'); } catch (e) { inner = null; }
          if (inner) {
            var t2 = cleanText(inner.textContent);
            if (t2) return t2;
          }
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    return '';
  }

  // A wrapping group (fieldset-like container that isn't a real <fieldset>) whose own
  // aria-labelledby points at the section's heading text.
  function findAriaLabelledbyGroup(el, doc) {
    var node = el.parentElement;
    for (var depth = 0; depth < 8 && node; depth++) {
      if (node.tagName === 'FORM' || node.tagName === 'BODY' || node.tagName === 'HTML') break;
      var lb = node.getAttribute && node.getAttribute('aria-labelledby');
      if (lb && doc) {
        var ids = lb.split(/\s+/).filter(Boolean);
        var parts = [];
        for (var i = 0; i < ids.length; i++) {
          var ref = doc.getElementById(ids[i]);
          if (ref) {
            var t = cleanText(ref.textContent);
            if (t) parts.push(t);
          }
        }
        if (parts.length) return parts.join(' ');
      }
      node = node.parentElement;
    }
    return '';
  }

  /**
   * Resolves { section, section_index } for `el`:
   *   1. nearest ancestor <fieldset>'s <legend>
   *   2. nearest preceding sibling/ancestor heading (h1-h6, role=heading, class*=title|heading)
   *   3. aria-labelledby on a wrapping group
   * `section_index` prefers a number parsed out of that heading text; when the heading has
   * none (or there is no heading at all), it falls back to a number parsed from the field's
   * own name/id (Workday-style `workExperience-2--jobTitle`).
   */
  function getSectionContext(el) {
    var doc = ownerDoc(el);
    var section = '';

    var fieldset = el.closest ? el.closest('fieldset') : null;
    if (fieldset) {
      var legend = fieldset.querySelector('legend');
      if (legend) {
        var t = cleanText(legend.textContent);
        if (t) section = t;
      }
    }

    if (!section) section = findPrecedingHeading(el);
    if (!section) section = findAriaLabelledbyGroup(el, doc);

    var sectionIndex = section ? extractSectionIndex(section) : null;
    if (sectionIndex === null) {
      var nameIndex = extractSectionIndex(el.name || el.id || '');
      if (nameIndex !== null) sectionIndex = nameIndex;
    }

    return { section: section, section_index: sectionIndex };
  }

  function getGroupLabel(group) {
    if (!group.length) return '';
    var first = group[0];
    var fieldset = first.closest ? first.closest('fieldset') : null;
    if (fieldset) {
      var legend = fieldset.querySelector('legend');
      if (legend) {
        var t = cleanText(legend.textContent);
        if (t) return stripRequiredMarker(t);
      }
    }
    var container = (first.closest && (first.closest('[role="radiogroup"]') || first.closest('[role="group"]'))) || null;
    if (container) {
      var ariaLabel = container.getAttribute('aria-label');
      if (ariaLabel) return stripRequiredMarker(cleanText(ariaLabel));
      var labelledBy = container.getAttribute('aria-labelledby');
      var doc = ownerDoc(first);
      if (labelledBy && doc) {
        var ref = doc.getElementById(labelledBy.split(/\s+/)[0]);
        if (ref) return stripRequiredMarker(cleanText(ref.textContent));
      }
    }
    // Lever: a native-<select>-free radio/checkbox group named "cards[<uuid>][fieldN]" (or a
    // survey "surveysResponses[...]" group) has no <fieldset>/<legend> at all -- Lever doesn't
    // use one -- and the group's own `name` is just the opaque card id, never the question
    // text. The real question text lives in a sibling structure: the nearest ancestor
    // ".application-question" carries the label in its own ".application-label .text" (with a
    // "✱" required marker to strip). See docs/research/2026-09-24-ats-widget-ground-truth.md §6.4.
    var question = first.closest ? first.closest('.application-question') : null;
    if (question && question.querySelector) {
      var leverLabel = question.querySelector('.application-label .text');
      if (leverLabel) {
        var lt = cleanText(leverLabel.textContent);
        if (lt) return stripRequiredMarker(lt);
      }
    }
    return stripRequiredMarker(getPrecedingText(first) || '');
  }

  // ---------------------------------------------------------------------
  // selector generation
  // ---------------------------------------------------------------------

  function isGoodId(id) {
    if (!id) return false;
    if (id.length > 40) return false;
    if (/^\d+$/.test(id)) return false;
    if (/^(react|mui|radix|headlessui|ember|css)[-:]/i.test(id)) return false;
    if (/^:r[0-9a-z]+:$/i.test(id)) return false; // React 18 useId() pattern, e.g. ":r3:"
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) return false; // uuid
    if (/^[a-f0-9]{16,}$/i.test(id)) return false; // long hex hash
    return true;
  }

  function isUnique(sel, doc) {
    try {
      return doc.querySelectorAll(sel).length === 1;
    } catch (e) {
      return false;
    }
  }

  function buildSelector(el) {
    var doc = ownerDoc(el);

    if (el.id && isGoodId(el.id)) {
      var idSel = '#' + cssEscape(el.id);
      if (isUnique(idSel, doc)) return idSel;
    }

    if (el.name) {
      var nameSel = el.tagName.toLowerCase() + '[name="' + cssEscape(el.name) + '"]';
      if (isUnique(nameSel, doc)) return nameSel;
    }

    // Guaranteed-unique fallback: the full nth-child path from the document root.
    var path = [];
    var node = el;
    while (node && node.nodeType === 1) {
      var parent = node.parentElement;
      if (!parent) {
        path.unshift(node.tagName.toLowerCase());
        break;
      }
      var siblings = Array.prototype.slice.call(parent.children);
      var index = siblings.indexOf(node) + 1;
      path.unshift(node.tagName.toLowerCase() + ':nth-child(' + index + ')');
      node = parent;
    }
    var pathSel = path.join(' > ');
    return pathSel;
  }

  // ---------------------------------------------------------------------
  // Workday widget ground truth — shared helpers
  // ---------------------------------------------------------------------
  //
  // Every selector below comes from the source of two published, real-Workday-tested
  // autofillers (berellevy/job_app_filler, ankitsharma38/Workday-Autofill-Assistant), not from
  // guessing at Workday's markup — this extension's Workday support was built twice before on
  // guesses, and both times a self-authored mock (built from the same guesses) passed while the
  // operator's real Workday form failed. Nothing here is invented; where the two sources
  // disagreed, berellevy's is preferred (reverse-engineered from real DOM across many field
  // variants). See the project brief for the exact provenance of each selector.

  var WD_FORM_FIELD_SELECTOR = '[data-automation-id^="formField-"]';
  // Applied in EVERY new Workday guard below, regardless of what else that guard already
  // checks: an element whose OWN automation id names Workday's real navigation actions is
  // refused outright, no matter how much it otherwise looks like a safe target. Also covers
  // the terms/consent checkbox (`agreementCheckbox`) -- see the project brief: that is a legal
  // attestation, never something this extension may tick on the applicant's behalf, no matter
  // how confidently a value might otherwise seem to match it.
  var WD_HARD_DENY_AUTOMATION_RE = /bottom-navigation|submit|next|save|agreementCheckbox|terms/i;

  function hasWorkdayHardDenyAutomationId(el) {
    if (!el || !el.getAttribute) return false;
    var auto = el.getAttribute('data-automation-id') || '';
    return WD_HARD_DENY_AUTOMATION_RE.test(auto);
  }

  function findWorkdayFormField(el) {
    return (el && el.closest) ? el.closest(WD_FORM_FIELD_SELECTOR) : null;
  }

  /** Label for a Workday formField-* container: its own <label>, else the usual getLabel() resolution. */
  function getWorkdayFieldLabel(container) {
    if (!container) return '';
    var lbl = container.querySelector ? container.querySelector('label') : null;
    if (lbl) {
      var clone = lbl.cloneNode(true);
      var ctrls = clone.querySelectorAll('input, select, textarea, button, script, style');
      for (var i = 0; i < ctrls.length; i++) ctrls[i].remove();
      var t = cleanText(clone.textContent);
      if (t) return t;
    }
    var direct = getLabel(container);
    if (direct) return direct;
    return getPrecedingText(container);
  }

  /**
   * `keyCode` is optional (5th arg) — S1 sets `keyCode:38, which:38` on the ArrowUp keydown a
   * Workday spinbutton needs; the prompt's Enter needs `keyCode:13`. Neither property can
   * actually be set through the KeyboardEventInit dictionary (it's a legacy getter with no
   * standard constructor support — browsers silently ignore it there, real or not), so when a
   * `keyCode` is given this defines it directly on the created instance afterwards, which is
   * what makes it observable to a page's own `e.keyCode`/`e.which` check.
   */
  function dispatchKeyboardEvent(el, type, key, code, keyCode) {
    var view = realmOf(el);
    var Ctor = (view && view.KeyboardEvent) || (typeof KeyboardEvent !== 'undefined' ? KeyboardEvent : null);
    if (!Ctor) return;
    var evt = new Ctor(type, { key: key, code: code || key, bubbles: true, cancelable: true });
    if (typeof keyCode === 'number') {
      try { Object.defineProperty(evt, 'keyCode', { value: keyCode, configurable: true }); } catch (e1) { /* best effort */ }
      try { Object.defineProperty(evt, 'which', { value: keyCode, configurable: true }); } catch (e2) { /* best effort */ }
    }
    el.dispatchEvent(evt);
  }

  /**
   * A real pointer/mouse event sequence (ankitsharma38) dispatched on `el`, ending in a plain
   * `click` — the technique Workday's portalled dropdown/prompt options need instead of a bare
   * `.click()`. Falls back gracefully across constructors so this still works in jsdom (no
   * PointerEvent in older versions) as well as a real browser.
   */
  function dispatchPointerClickSequence(el) {
    var view = realmOf(el);
    var seq = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
    for (var i = 0; i < seq.length; i++) {
      var type = seq[i];
      var isPointer = type.indexOf('pointer') === 0;
      var Ctor = null;
      if (isPointer && view && view.PointerEvent) Ctor = view.PointerEvent;
      else if (!isPointer && view && view.MouseEvent) Ctor = view.MouseEvent;
      else Ctor = (view && view.Event) || (typeof Event !== 'undefined' ? Event : null);
      if (!Ctor) continue;
      var evt;
      try { evt = new Ctor(type, { bubbles: true, cancelable: true, view: view || undefined }); }
      catch (e) {
        try { evt = new Ctor(type, { bubbles: true, cancelable: true }); }
        catch (e2) { continue; }
      }
      el.dispatchEvent(evt);
    }
  }

  /**
   * Polls `fn()` until it returns a truthy value, or `timeoutMs` elapses (returning whatever
   * `fn()` last produced, possibly falsy). Uses a MutationObserver so a DOM change anywhere
   * under `doc` re-checks immediately rather than waiting for the next poll tick. Shared by
   * every "wait for Workday's own async UI to catch up" spot below (dropdown popup, prompt
   * results, selected-item confirmation, upload confirmation).
   */
  function waitFor(fn, timeoutMs, doc) {
    return new Promise(function (resolve) {
      var settled = false;
      var observer = null;
      var timer = null;
      function finish(v) {
        if (settled) return;
        settled = true;
        if (observer) observer.disconnect();
        if (timer) clearTimeout(timer);
        resolve(v);
      }
      var first = fn();
      if (first) { finish(first); return; }
      if (typeof MutationObserver !== 'undefined') {
        observer = new MutationObserver(function () {
          var v = fn();
          if (v) finish(v);
        });
        var root = (doc && (doc.body || doc.documentElement)) || (typeof document !== 'undefined' ? document.body : null);
        if (root) observer.observe(root, { childList: true, subtree: true, attributes: true, characterData: true });
      }
      timer = setTimeout(function () { finish(fn()); }, timeoutMs);
    });
  }

  function optionAccessibleText(el) {
    var t = cleanText(el.textContent);
    if (t) return t;
    var aria = el.getAttribute && el.getAttribute('aria-label');
    return aria ? cleanText(aria) : '';
  }

  /**
   * A real macrotask yield, used everywhere a Workday widget needs its OWN async commit to
   * catch up before we read it back — see the project brief: a synchronous read-back after a
   * synthetic keydown sees only our own write, not whatever Workday's controlled re-render (a
   * React 18 root can commit in a microtask or later) settles on.
   */
  function wdSleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  // ---------------------------------------------------------------------
  // split month/year date inputs (Workday "From"/"To" MM/YYYY pickers)
  // ---------------------------------------------------------------------
  //
  // Workday commonly renders a date as two separate controls — a month
  // input/select and a year input — behind a visual "MM/YYYY" label, using
  // data-automation-id values like "dateSectionMonth-input" /
  // "dateSectionYear-input" (often role="spinbutton"), or a month <select>
  // paired with a year <input>. Scanned individually those look like two
  // unrelated, mislabeled number fields; paired, they are exactly the same
  // shape the structured tier already expects from a single MM/YYYY field.
  //
  // Detection is purely structural, same discipline as findPairedWidget()
  // above: an element only becomes a "month" or "year" candidate when its
  // own data-automation-id/name/id/aria-label literally says so, and two
  // candidates are only paired when they share a small common ancestor
  // (bounded, so a huge form page doesn't get false-paired across sections).

  var MONTH_HINT_RE = /month/i;
  var YEAR_HINT_RE = /year/i;
  var MAX_DATE_WRAPPER_CHILDREN = 8;
  var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
    'August', 'September', 'October', 'November', 'December'];

  function elementDateHint(el) {
    var dataAuto = (el.getAttribute && el.getAttribute('data-automation-id')) || '';
    var hay = [dataAuto, el.name || '', el.id || '', (el.getAttribute && el.getAttribute('aria-label')) || ''].join(' ');
    if (MONTH_HINT_RE.test(hay)) return 'month';
    if (YEAR_HINT_RE.test(hay)) return 'year';
    return null;
  }

  function isDatePartCandidate(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.disabled) return false;
    var tag = el.tagName;
    if (tag !== 'INPUT' && tag !== 'SELECT') return false;
    if (tag === 'INPUT') {
      var type = (el.type || 'text').toLowerCase();
      if (EXCLUDED_INPUT_TYPES.indexOf(type) !== -1) return false;
    }
    // A real Workday dateInputWrapper (aria-label="Month"/"Year" spinbutton pair) is handled
    // exclusively by findWorkdayDateWrappers()/setWorkdaySpinnerValue() below — plain
    // value-setting is silently ignored by that widget, so it must never also be picked up
    // here and filled with the wrong (plain-value) technique.
    if (el.closest && el.closest('[data-automation-id="dateInputWrapper"]')) return false;
    return !!elementDateHint(el);
  }

  // Looks for a partner with the given hint ("month" or "year") sharing a
  // small enough common container with `el`, walking up to 3 ancestor
  // levels — mirrors findPairedWidget()'s bounded-wrapper reasoning so one
  // "From" pair on a big page never gets matched against another section's
  // month/year inputs elsewhere on the same page.
  function findDatePartner(el, hint, candidates, used) {
    var node = el;
    for (var depth = 0; depth < 3 && node; depth++) {
      var container = node.parentElement;
      if (!container) break;
      if (container.children.length <= MAX_DATE_WRAPPER_CHILDREN) {
        for (var i = 0; i < candidates.length; i++) {
          var cand = candidates[i];
          if (cand === el || used.indexOf(cand) !== -1) continue;
          if (elementDateHint(cand) !== hint) continue;
          if (container.contains(cand)) return cand;
        }
      }
      node = container;
    }
    return null;
  }

  function commonAncestor(a, b) {
    var ancestors = [];
    var node = a;
    while (node) { ancestors.push(node); node = node.parentElement; }
    node = b;
    while (node) {
      if (ancestors.indexOf(node) !== -1) return node;
      node = node.parentElement;
    }
    return null;
  }

  // The visible label for a merged pair — e.g. "From" / "To" / "Start date".
  // Reuses getLabel()'s existing resolution (id/for, wrapping <label>,
  // aria-label, aria-labelledby, nearby text) pointed at the pair's shared
  // container, since that is exactly where Workday puts the group's
  // aria-labelledby/aria-label in practice.
  function findDatePairLabel(monthEl, yearEl) {
    var container = commonAncestor(monthEl, yearEl) || monthEl.parentElement;
    if (!container) return '';
    var lbl = getLabel(container);
    if (lbl) return lbl;
    return getPrecedingText(container);
  }

  /**
   * Finds every month+year pair in `root`. Returns an array of
   * { monthEl, yearEl, label }. An unpaired month or year candidate (no
   * partner found) is left alone entirely — scanFields() then scans it as
   * its own ordinary field rather than silently dropping it.
   */
  function findDatePartPairs(root) {
    var all = Array.prototype.slice.call(root.querySelectorAll('input, select')).filter(isDatePartCandidate);
    var used = [];
    var pairs = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (used.indexOf(el) !== -1) continue;
      if (elementDateHint(el) !== 'month') continue; // anchor on the month half, look for its year partner
      var yearEl = findDatePartner(el, 'year', all, used);
      if (!yearEl) continue;
      if (!isVisible(el) || !isVisible(yearEl)) continue;
      used.push(el, yearEl);
      pairs.push({ monthEl: el, yearEl: yearEl, label: findDatePairLabel(el, yearEl) });
    }
    return pairs;
  }

  var MM_YYYY_RE = /^\s*(\d{1,2})\s*\/\s*(\d{4})\s*$/;

  function setDatePartMonth(el, monthNum) {
    var mm2 = monthNum < 10 ? '0' + monthNum : String(monthNum);
    if (el.tagName === 'SELECT') {
      var idx = findOptionMatch(el, mm2);
      if (idx === -1) idx = findOptionMatch(el, String(monthNum));
      if (idx === -1) idx = findOptionMatch(el, MONTH_NAMES[monthNum - 1]);
      if (idx === -1) return false;
      var setter = nativeSetterFor(el, 'selectedIndex');
      if (setter) setter.call(el, idx); else el.selectedIndex = idx;
      fireEvents(el, ['input', 'change']);
      return el.selectedIndex === idx;
    }
    setNativeValue(el, mm2);
    return String(el.value) === mm2 || String(el.value) === String(monthNum);
  }

  function setDatePartYear(el, yearStr) {
    if (el.tagName === 'SELECT') {
      var idx = findOptionMatch(el, yearStr);
      if (idx === -1) return false;
      var setter = nativeSetterFor(el, 'selectedIndex');
      if (setter) setter.call(el, idx); else el.selectedIndex = idx;
      fireEvents(el, ['input', 'change']);
      return el.selectedIndex === idx;
    }
    setNativeValue(el, yearStr);
    return String(el.value) === yearStr;
  }

  function clearDatePart(el) {
    if (el.tagName === 'SELECT') setSelectValue(el, '');
    else setNativeValue(el, '');
  }

  /** Splits a service-provided "MM/YYYY" value across the pair. Never guesses at a format it wasn't given. */
  function setDatePartsValue(entry, value) {
    var str = String(value == null ? '' : value).trim();
    if (!str) {
      clearDatePart(entry.monthEl);
      clearDatePart(entry.yearEl);
      return true;
    }
    var m = MM_YYYY_RE.exec(str);
    if (!m) return false; // not MM/YYYY -- report failure honestly rather than guessing at a split
    var monthNum = parseInt(m[1], 10);
    if (monthNum < 1 || monthNum > 12) return false;
    var okMonth = setDatePartMonth(entry.monthEl, monthNum);
    var okYear = setDatePartYear(entry.yearEl, m[2]);
    return okMonth && okYear;
  }

  function readDatePartMonth(el) {
    if (el.tagName === 'SELECT') {
      var opt = el.options[el.selectedIndex];
      if (!opt) return '';
      var optText = cleanText(opt.textContent);
      var num = parseInt(opt.value, 10);
      if (!num) num = MONTH_NAMES.indexOf(optText) + 1;
      if (!num) num = parseInt(optText, 10);
      if (!num || num < 1 || num > 12) return '';
      return num < 10 ? '0' + num : String(num);
    }
    return String(el.value || '').trim();
  }

  /** Reads the pair back as "MM/YYYY" — used for undo and honest read-back verification. */
  function getDatePartsValue(entry) {
    var monthVal = readDatePartMonth(entry.monthEl);
    var yearVal = String(entry.yearEl.value || '').trim();
    if (!monthVal && !yearVal) return '';
    return monthVal + '/' + yearVal;
  }

  // ---------------------------------------------------------------------
  // Workday spinner dates (dateInputWrapper) — ground truth structure (verbatim NVIDIA capture,
  // tarunravisankar/tso_autofiller tests/nvidia-date.html): ONE markup, not two generations —
  // the "-input" element itself carries BOTH the automation id and the aria-label:
  //   div[data-automation-id^="formField-"] -> div[data-automation-id="dateInputWrapper"]
  //     -> div#..-dateSection{Month,Day,Year}
  //          -> div[aria-hidden][data-automation-id="dateSection{P}-display"]   "MM"/"DD"/"YYYY"
  //          -> input[role=spinbutton][aria-label="{P}"]
  //               [data-automation-id="dateSection{P}-input"]
  // Shapes seen: Month+Year (work experience), Year-only (education), Month+Day+Year
  // (Self-Identify "Date"). The earlier claim on this line that "dateSectionMonth-input"
  // appears in NEITHER working source was wrong — S2, S4, S6, S7 and S10 all use it. Detection
  // below keys on data-automation-id FIRST (locale-independent — an aria-label can be
  // localized, e.g. "Monat" on a German tenant), falling back to aria-label for a tenant that
  // has one but not the other.
  // ---------------------------------------------------------------------

  var WD_DATE_PART_NAMES = ['Month', 'Day', 'Year'];
  var WD_DATE_SETTLE_MS = 60;   // wait after an ArrowUp for Workday's own async commit (S2)
  var WD_DATE_YIELD_MS = 150;   // wait between parts, and before the re-check-month pass (S1/S4)
  var WD_DATE_PLACEHOLDER_RE = /^[mdy]+$/i; // the "-display"/masked empty state, e.g. "MM", "YYYY"

  /** The `dateSection{Part}-input` (falling back to `input[aria-label=Part]`) inside `wrapper`. */
  function findWorkdayDatePart(wrapper, partName) {
    return wrapper.querySelector('[data-automation-id="dateSection' + partName + '-input"]')
      || wrapper.querySelector('input[aria-label="' + partName + '"]')
      || null;
  }

  /**
   * The `-display` companion of a `dateSection*-input` — the id-suffix swap first (matches the
   * verbatim NVIDIA capture exactly), then a same-parent data-automation-id sibling lookup (for
   * a tenant whose ids don't follow that exact convention).
   */
  function findWorkdayDateSectionDisplay(inputEl) {
    if (!inputEl) return null;
    var doc = ownerDoc(inputEl);
    if (inputEl.id && doc && doc.getElementById) {
      var byId = doc.getElementById(inputEl.id.replace(/-input$/, '-display'));
      if (byId) return byId;
    }
    var parent = inputEl.parentElement;
    if (!parent) return null;
    var auto = (inputEl.getAttribute && inputEl.getAttribute('data-automation-id')) || '';
    if (auto) {
      var byAuto = parent.querySelector('[data-automation-id="' + auto.replace(/-input$/, '-display') + '"]');
      if (byAuto) return byAuto;
    }
    return parent.querySelector('[data-automation-id$="-display"]');
  }

  /**
   * Finds every Workday date wrapper in `root`. Returns { shape, wrapper, monthEl, dayEl,
   * yearEl, maskedEl, container, label } where `shape` is 'mdy' (Day present), 'my'
   * (Month+Year), 'y' (Year only) or 'masked' (ankitsharma38's single-input fallback, ONLY when
   * no real Month/Day/Year part was found at all AND the wrapper holds exactly one `<input>`).
   */
  function findWorkdayDateWrappers(root) {
    var wrappers = Array.prototype.slice.call(root.querySelectorAll('[data-automation-id="dateInputWrapper"]'));
    var out = [];
    for (var i = 0; i < wrappers.length; i++) {
      var w = wrappers[i];
      if (!isVisible(w)) continue;
      var monthEl = findWorkdayDatePart(w, 'Month');
      var dayEl = findWorkdayDatePart(w, 'Day');
      var yearEl = findWorkdayDatePart(w, 'Year');
      var container = findWorkdayFormField(w) || w;
      var label = getWorkdayFieldLabel(container);
      if (yearEl) {
        var shape = dayEl ? 'mdy' : (monthEl ? 'my' : 'y');
        out.push({ shape: shape, wrapper: w, monthEl: monthEl || null, dayEl: dayEl || null, yearEl: yearEl, maskedEl: null, container: container, label: label });
        continue;
      }
      var allInputs = w.querySelectorAll('input');
      if (allInputs.length === 1 && !monthEl && !dayEl) {
        out.push({ shape: 'masked', wrapper: w, monthEl: null, dayEl: null, yearEl: null, maskedEl: allInputs[0], container: container, label: label });
      }
    }
    return out;
  }

  /**
   * Gated on the WRAPPER's own visibility, never the input's — a real spinbutton can sit
   * visually behind its own `-display` div (S2: "a section may still be backed by an input
   * that is visually hidden; prefer it"), and requiring the input itself to be visible refused
   * every real date on the spot.
   */
  function isWorkdaySpinnerInputSafe(el) {
    if (!el || el.tagName !== 'INPUT') return false;
    if (hasWorkdayHardDenyAutomationId(el)) return false;
    var auto = (el.getAttribute && el.getAttribute('data-automation-id')) || '';
    var aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
    var looksLikePart = /^dateSection(Month|Day|Year)-input$/.test(auto) || WD_DATE_PART_NAMES.indexOf(aria) !== -1;
    if (!looksLikePart) return false;
    var wrapper = el.closest ? el.closest('[data-automation-id="dateInputWrapper"]') : null;
    if (!wrapper) return false;
    if (hasWorkdayHardDenyAutomationId(wrapper)) return false;
    if (el.disabled) return false;
    if (!isVisible(wrapper)) return false;
    return true;
  }

  function isWorkdayMaskedDateInputSafe(el) {
    if (!el || el.tagName !== 'INPUT') return false;
    if (hasWorkdayHardDenyAutomationId(el)) return false;
    var wrapper = el.closest ? el.closest('[data-automation-id="dateInputWrapper"]') : null;
    if (!wrapper) return false;
    if (hasWorkdayHardDenyAutomationId(wrapper)) return false;
    if (wrapper.querySelectorAll('input').length !== 1) return false;
    if (el.disabled) return false;
    if (!isVisible(wrapper)) return false;
    return true;
  }

  /**
   * The honest answer to "what does this spinbutton currently show", per S2: the `-display`
   * text (skipping its own "MM"/"DD"/"YYYY" placeholder state), then `aria-valuetext`, then
   * `aria-valuenow` — NEVER `el.value`, which keeps whatever we last wrote even when Workday's
   * own model rejected or hasn't yet caught up to it. Falls back to `el.value` ONLY when this
   * tenant's markup exposes none of the three at all (degrade gracefully rather than refuse
   * every fill outright on a page shaped slightly differently than every captured source).
   */
  function readWorkdaySpinnerDisplayValue(el) {
    if (!el) return NaN;
    var sawAnySource = false;
    var display = findWorkdayDateSectionDisplay(el);
    if (display) {
      sawAnySource = true;
      var t = cleanText(display.textContent);
      if (t && !WD_DATE_PLACEHOLDER_RE.test(t)) {
        var n1 = parseInt(t, 10);
        if (!isNaN(n1)) return n1;
      }
    }
    var valuetext = el.getAttribute && el.getAttribute('aria-valuetext');
    if (valuetext != null && valuetext !== '') {
      sawAnySource = true;
      if (!WD_DATE_PLACEHOLDER_RE.test(valuetext)) {
        var n2 = parseInt(valuetext, 10);
        if (!isNaN(n2)) return n2;
      }
    }
    var valuenow = el.getAttribute && el.getAttribute('aria-valuenow');
    if (valuenow != null && valuenow !== '') {
      sawAnySource = true;
      var n3 = parseInt(valuenow, 10);
      if (!isNaN(n3)) return n3;
    }
    if (!sawAnySource) {
      var raw = parseInt(el.value, 10);
      if (!isNaN(raw)) return raw;
    }
    return NaN;
  }

  /**
   * berellevy's technique for a Workday numeric spinbutton, made asynchronous per S2's
   * verification discipline (this is the fix for the reported "hangs on dates" bug alongside
   * the Skills fix below): setting `.value` and firing input/change is silently reverted by
   * Workday's own controlled re-render (the mock proves this — see selftest.js). Setting
   * `.value` ONE BELOW the target with NO event at all, then a real ArrowUp keydown carrying
   * `keyCode`/`which` 38 (S1), lets Workday's own handler commit the target itself. Reads back
   * through the page's OWN model (readWorkdaySpinnerDisplayValue), never our own `.value`
   * write, and only AFTER a real yield — a synchronous read-back sees only what we just wrote.
   * Some variants need the ArrowUp twice (S1); retries once.
   */
  function setWorkdaySpinnerValue(el, targetNum) {
    if (!isWorkdaySpinnerInputSafe(el)) return Promise.resolve(false);
    if (typeof targetNum !== 'number' || isNaN(targetNum)) return Promise.resolve(false);
    if (readWorkdaySpinnerDisplayValue(el) === targetNum) return Promise.resolve(true);

    function attempt() {
      if (typeof el.focus === 'function') {
        try { el.focus({ preventScroll: true }); } catch (eFocusOpts) { try { el.focus(); } catch (eFocus) { /* best effort */ } }
      }
      el.value = String(targetNum - 1);
      dispatchKeyboardEvent(el, 'keydown', 'ArrowUp', 'ArrowUp', 38);
      dispatchKeyboardEvent(el, 'keyup', 'ArrowUp', 'ArrowUp', 38);
      if (typeof el.click === 'function') el.click();
      return wdSleep(WD_DATE_SETTLE_MS).then(function () {
        return readWorkdaySpinnerDisplayValue(el) === targetNum;
      });
    }

    return attempt().then(function (ok) {
      if (ok) return true;
      return attempt(); // some variants need ArrowUp twice -- retry once more from wherever it landed
    });
  }

  /**
   * ankitsharma38's fallback: ONLY used when a dateInputWrapper holds exactly one masked text
   * input (no separate Month/Day/Year spinners at all). Types `text` character by character with
   * a genuine keydown/keypress/input/keyup sequence per character, then blurs.
   */
  function typeMaskedTextField(el, text) {
    if (!isWorkdayMaskedDateInputSafe(el)) return false;
    var setter = nativeSetterFor(el, 'value');
    function setSilently(v) { if (setter) setter.call(el, v); else el.value = v; }

    setSilently('');
    fireEvents(el, ['input']);
    var current = '';
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      dispatchKeyboardEvent(el, 'keydown', ch);
      dispatchKeyboardEvent(el, 'keypress', ch);
      current += ch;
      setSilently(current);
      fireEvents(el, ['input']);
      dispatchKeyboardEvent(el, 'keyup', ch);
    }
    fireEvents(el, ['change']);
    if (typeof el.blur === 'function') el.blur();
    else fireEvents(el, ['blur']);
    return true;
  }

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  var MM_DD_YYYY_RE = /^\s*(\d{1,2})\s*\/\s*(\d{1,2})\s*\/\s*(\d{4})\s*$/;

  /**
   * Fills each `{ el, target }` part in STRICT sequence, awaiting one before the next starts
   * (S1 — no source commits two parts back-to-back with no yield). Once every part has
   * committed, yields once more and RE-READS every part, redoing (one pass) any that drifted —
   * S4: "editing the year can clear the month". Blurs the last part, then reads the formField's
   * own error text (S2), so a date Workday itself rejected (e.g. "Must end after start date")
   * is reported as a failure rather than a false "ok".
   */
  function fillWorkdayDateParts(entry, parts) {
    function fillOne(i) {
      if (i >= parts.length) return Promise.resolve(true);
      return setWorkdaySpinnerValue(parts[i].el, parts[i].target).then(function (ok) {
        if (!ok) return false;
        return wdSleep(WD_DATE_YIELD_MS).then(function () { return fillOne(i + 1); });
      });
    }
    function recheckOne(i, accOk) {
      if (i >= parts.length) return Promise.resolve(accOk);
      var p = parts[i];
      var already = readWorkdaySpinnerDisplayValue(p.el) === p.target;
      var step = already ? Promise.resolve(true) : setWorkdaySpinnerValue(p.el, p.target);
      return step.then(function (ok) { return recheckOne(i + 1, accOk && ok); });
    }

    return fillOne(0).then(function (allOk) {
      return wdSleep(WD_DATE_YIELD_MS).then(function () {
        return recheckOne(0, allOk).then(function (finalOk) {
          var lastEl = parts[parts.length - 1].el;
          if (typeof lastEl.blur === 'function') lastEl.blur(); else fireEvents(lastEl, ['focusout', 'blur']);
          if (!finalOk) return false;
          var wrapper = entry.wrapper;
          var formField = (wrapper && findWorkdayFormField(wrapper)) || wrapper;
          var err = (formField && formField.querySelector)
            ? formField.querySelector('[data-automation-id="errorMessage"], [data-automation-id*="error"]')
            : null;
          if (err && isVisible(err) && cleanText(err.textContent)) return false; // Workday itself rejected the date
          return true;
        });
      });
    });
  }

  /**
   * Splits a service-provided date across the wrapper's parts — "MM/YYYY" for 'wd-date-my',
   * "MM/DD/YYYY" for 'wd-date-mdy' (Self-Identify and similar), "MM/YYYY" or bare "YYYY" for
   * 'wd-date-y'. Always asynchronous now (see fillWorkdayDateParts/setWorkdaySpinnerValue) —
   * `applyFill`'s wd-date-* branches below already `.then()` this like the other Workday
   * widgets.
   */
  function setWorkdayDateValue(entry, value) {
    var str = String(value == null ? '' : value).trim();
    if (!str) return Promise.resolve(false); // never guess at clearing a spinner -- report failure honestly

    if (entry.kind === 'wd-date-y') {
      var ym = MM_YYYY_RE.exec(str);
      var yr = ym ? ym[2] : (/^\d{4}$/.test(str) ? str : null);
      if (!yr) return Promise.resolve(false);
      return setWorkdaySpinnerValue(entry.yearEl, parseInt(yr, 10));
    }

    if (entry.kind === 'wd-date-mdy') {
      var mdy = MM_DD_YYYY_RE.exec(str);
      if (!mdy) return Promise.resolve(false);
      var monthNumM = parseInt(mdy[1], 10), dayNumM = parseInt(mdy[2], 10), yearNumM = parseInt(mdy[3], 10);
      if (monthNumM < 1 || monthNumM > 12 || dayNumM < 1 || dayNumM > 31) return Promise.resolve(false);
      return fillWorkdayDateParts(entry, [
        { el: entry.monthEl, target: monthNumM },
        { el: entry.dayEl, target: dayNumM },
        { el: entry.yearEl, target: yearNumM }
      ]);
    }

    if (entry.kind === 'wd-date-my') {
      var m = MM_YYYY_RE.exec(str);
      if (!m) return Promise.resolve(false);
      var monthNum = parseInt(m[1], 10);
      if (monthNum < 1 || monthNum > 12) return Promise.resolve(false);
      if (entry.maskedEl) {
        return Promise.resolve(typeMaskedTextField(entry.maskedEl, pad2(monthNum) + '/' + m[2]));
      }
      return fillWorkdayDateParts(entry, [
        { el: entry.monthEl, target: monthNum },
        { el: entry.yearEl, target: parseInt(m[2], 10) }
      ]);
    }

    return Promise.resolve(false);
  }

  function getWorkdayDateValue(entry) {
    if (entry.kind === 'wd-date-y') {
      var y = readWorkdaySpinnerDisplayValue(entry.yearEl);
      return isNaN(y) ? '' : String(y);
    }
    if (entry.kind === 'wd-date-mdy') {
      var mm = readWorkdaySpinnerDisplayValue(entry.monthEl);
      var dd = readWorkdaySpinnerDisplayValue(entry.dayEl);
      var yy = readWorkdaySpinnerDisplayValue(entry.yearEl);
      if (isNaN(mm) && isNaN(dd) && isNaN(yy)) return '';
      return (isNaN(mm) ? '' : pad2(mm)) + '/' + (isNaN(dd) ? '' : pad2(dd)) + '/' + (isNaN(yy) ? '' : String(yy));
    }
    if (entry.kind === 'wd-date-my') {
      if (entry.maskedEl) return String(entry.maskedEl.value || '').trim();
      var mv = readWorkdaySpinnerDisplayValue(entry.monthEl);
      var yv = readWorkdaySpinnerDisplayValue(entry.yearEl);
      if (isNaN(mv) && isNaN(yv)) return '';
      return (isNaN(mv) ? '' : pad2(mv)) + '/' + (isNaN(yv) ? '' : String(yv));
    }
    return '';
  }

  function getWorkdayDateHighlightTargets(entry) {
    if (entry.kind === 'wd-date-y') return [entry.yearEl];
    if (entry.maskedEl) return [entry.maskedEl];
    if (entry.kind === 'wd-date-mdy') return [entry.monthEl, entry.dayEl, entry.yearEl];
    return [entry.monthEl, entry.yearEl];
  }

  // ---------------------------------------------------------------------
  // Workday dropdown (button[aria-haspopup="listbox"], portalled listbox)
  // ---------------------------------------------------------------------

  function isWorkdayDropdownOpenerSafe(el) {
    if (!el || el.nodeType !== 1) return false;
    if (hasWorkdayHardDenyAutomationId(el)) return false;
    var tag = el.tagName;
    var role = el.getAttribute && el.getAttribute('role');
    var isButtonLike = tag === 'BUTTON' || role === 'combobox';
    if (!isButtonLike) return false;
    if ((el.getAttribute && el.getAttribute('aria-haspopup')) !== 'listbox') return false;
    if (!findWorkdayFormField(el)) return false;
    var effectiveType = String(el.type || '').toLowerCase();
    if ((effectiveType === 'submit' || effectiveType === 'image') && el.form) return false;
    var text = accessibleControlText(el);
    if (ADD_BUTTON_DENY_RE.test(text)) return false;
    if (!isVisible(el)) return false;
    if (el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  }

  // menuItem/promptLeafNode are the real Skills-popup row/leaf automation ids (see the Workday
  // prompt section below); promptOption/checkboxItem cover the option label and a legacy row
  // shape.
  var WD_OPTION_AUTOMATION_RE = /promptOption|checkboxItem|menuItem|promptLeafNode/i;

  /**
   * `el` must look like a real Workday option AND live inside `scopeEl` (the listbox the
   * opener controls, or the popup that appeared after opening) — never an arbitrary element on
   * the page, no matter how option-shaped its own attributes look.
   */
  function isWorkdayOptionSafe(el, scopeEl) {
    if (!el || el.nodeType !== 1) return false;
    if (hasWorkdayHardDenyAutomationId(el)) return false;
    var role = el.getAttribute && el.getAttribute('role');
    var auto = (el.getAttribute && el.getAttribute('data-automation-id')) || '';
    var looksLikeOption = role === 'option' || WD_OPTION_AUTOMATION_RE.test(auto);
    if (!looksLikeOption) return false;
    if (!scopeEl || !scopeEl.contains(el)) return false;
    if (el.tagName === 'BUTTON' && ADD_BUTTON_DENY_RE.test(accessibleControlText(el))) return false;
    if (!isVisible(el)) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  }

  function resolveWorkdayListbox(doc, button) {
    var controlsId = button.getAttribute && button.getAttribute('aria-controls');
    if (controlsId && doc.getElementById) {
      var byId = doc.getElementById(controlsId);
      if (byId && isVisible(byId)) return byId;
    }
    // Deliberately NO document-wide "first visible [role=listbox]" fallback: with more than one
    // dropdown on the page, that can grab a STALE listbox some earlier interaction left open
    // rather than the one this button just opened (see the project brief) — aria-controls, set
    // by Workday itself on this exact button, is the only link we trust.
    return null;
  }

  function collectWorkdayListboxOptions(listbox) {
    return Array.prototype.slice.call(
      listbox.querySelectorAll('[role="option"], [data-automation-id*="promptOption"]')
    ).filter(isVisible);
  }

  // ---- degree / country synonyms (explicit families only -- no guessing outside them) ------

  // Strips dots, whitespace AND hyphens -- the last of those matters for degree names that are
  // legitimately hyphenated ("Human-Computer Interaction"), not just abbreviations with dots
  // ("B.E.") or spaces ("Bachelor of Science"). No existing token in either family list below
  // contains a hyphen, so widening this is purely additive for values that previously matched
  // nothing at all.
  function normalizeToken(s) { return String(s == null ? '' : s).toLowerCase().replace(/[.\s-]/g, ''); }

  var WD_DEGREE_FAMILY_TOKENS = {
    doctorate: ['phd', 'doctorate', 'doctoral', 'dsc', 'edd', 'dba'],
    master: ['ms', 'msc', 'ma', 'mba', 'me', 'meng', 'mfa', 'mtech', 'master', 'masters',
      'masterofscience', 'masterofarts', 'masterofengineering', 'masterofbusinessadministration',
      // Design/creative-field synonyms (the primary user is a product/UX designer).
      'mdes', 'masterofdesign', 'masteroffinearts', 'march', 'masterofarchitecture',
      'mca', 'mhci', 'masterofhumancomputerinteraction', 'mps', 'masterofprofessionalstudies'],
    bachelor: ['bs', 'bsc', 'ba', 'be', 'beng', 'btech', 'bachelor', 'bachelors',
      'bachelorofscience', 'bachelorofarts', 'bachelorofengineering',
      // Design/creative-field synonyms (the primary user is a product/UX designer).
      'bdes', 'bachelorofdesign', 'bfa', 'bacheloroffinearts', 'barch', 'bachelorofarchitecture',
      'bca', 'bba', 'bachelorofbusinessadministration'],
    associate: ['as', 'aa', 'associate', 'associates', 'associatedegree'],
    highschool: ['hs', 'ged', 'highschool', 'secondary', 'highschoolorequivalent']
  };
  var WD_DEGREE_FAMILY_OPTION_RE = {
    doctorate: /doctor/i,
    master: /master/i,
    bachelor: /bachelor/i,
    associate: /associate/i,
    highschool: /high\s*school|secondary/i
  };

  function degreeFamilyOf(value) {
    var tok = normalizeToken(value);
    if (!tok) return null;
    for (var fam in WD_DEGREE_FAMILY_TOKENS) {
      if (WD_DEGREE_FAMILY_TOKENS[fam].indexOf(tok) !== -1) return fam;
    }
    return null;
  }

  var WD_COUNTRY_ALIASES = {
    'unitedstates': 'united states of america',
    'usa': 'united states of america',
    'us': 'united states of america'
  };

  function countryAliasOf(value) {
    return WD_COUNTRY_ALIASES[normalizeToken(value)] || null;
  }

  // ---- answer-family matching (EEO / screening yes-no-shaped questions) --------------------
  //
  // A "family" here is a known SHAPE of profile value for a decline-to-answer, gender, veteran,
  // disability, or race/ethnicity question -- distinct from the Workday-specific degree/country
  // synonyms above, which stay exactly as they were. Once a value is recognised as belonging to
  // one of these families, matching is scoped to ONLY that family's own option patterns: if none
  // of them fit, matchAnswerFamily returns -1 immediately, and matchChoiceOption() below NEVER
  // falls through to generic word-boundary containment for a family-recognised value. A wrong
  // pick on one of these questions is a false statement on a real EEO question, so "no confident
  // match" must always win over a fuzzy guess.

  /**
   * Like a "find the matching option" lookup, but AMBIGUITY-SAFE throughout: returns the
   * option's index only when EXACTLY ONE option matches `re`, and -1 both when none match and
   * when two or more do — a family match must never guess the first of several candidates any
   * more than matchChoiceOption's plain containment tier does (e.g. value "Yes" against options
   * "Yes, I am a U.S. citizen or permanent resident" / "Yes, I am authorized but will require
   * sponsorship" is a materially different legal statement depending on which "Yes" is meant,
   * so it must resolve to neither). Every existing caller below was already written as "if -1,
   * try the next fallback (or give up)", so this keeps working with zero call-site changes.
   */
  function findUniqueMatch(optionTexts, re) {
    var idx = -1;
    var count = 0;
    for (var i = 0; i < optionTexts.length; i++) {
      if (re.test(cleanText(optionTexts[i]))) {
        count++;
        if (count === 1) idx = i; else return -1;
      }
    }
    return count === 1 ? idx : -1;
  }

  var NEGATION_RE = /\bnot\b|n't/i;

  // Matches BOTH a decline-shaped VALUE ("Decline to self-identify") and a decline-shaped
  // OPTION ("I don't wish to answer", "Not Declared", "I DO NOT WISH TO SELF-IDENTIFY", ...) --
  // deliberately the SAME regex for both sides, since an option expressing "I decline to answer
  // this" is recognised by the same set of phrasings the value itself uses. No `$` anchor, so a
  // Workday-style trailing suffix ("... (United States of America)") never breaks the match.
  var DECLINE_RE = /decline|prefer not|rather not|not declared|do(n'?t| not) (wish|want)|choose not|not to (say|answer|disclose)|self[-\s]?identify/i;

  // A decline-shaped OPTION can still contain decline-ish phrasing (bare "self-identify" is
  // part of DECLINE_RE above) while actually ASSERTING a specific yes/no answer or identity
  // claim — "I am a protected veteran, but I choose not to self-identify the classifications to
  // which I belong" and "Yes, I self-identify as LGBTQ+" both matched DECLINE_RE, but neither is
  // truly a decline-to-answer option: one is a veteran assertion, the other an identity
  // disclosure. A genuine decline option never ALSO asserts one of these.
  var DECLINE_ASSERTION_RE = /^\s*(yes|no)\b|\bI\s*am\s+an?\b|\bI\s*identify\s+as\b|\bI\s*self[-\s]?identify\s+as\b|\bI\s*have\s+an?\b/i;

  /** Decline-to-answer match: reject any DECLINE_RE-matching option that also asserts a
   * yes/no/identity claim, then require EXACTLY ONE plain decline option to remain. */
  function findDeclineMatch(optionTexts) {
    var idx = -1;
    var count = 0;
    for (var i = 0; i < optionTexts.length; i++) {
      var ot = cleanText(optionTexts[i]);
      if (!DECLINE_RE.test(ot) || DECLINE_ASSERTION_RE.test(ot)) continue;
      count++;
      if (count === 1) idx = i; else return -1;
    }
    return count === 1 ? idx : -1;
  }

  var RACE_VALUES = [
    'american indian or alaska native', 'asian', 'black or african american',
    'hispanic or latino', 'native hawaiian or other pacific islander', 'white', 'two or more races'
  ];

  /**
   * `v` must already be cleanText()+lowercased (matchChoiceOption's job, so this can be called
   * directly with the same normalised value it already computed). Returns an option index, -1
   * ("recognised family, no fitting option -- stop here, never guess"), or null ("value doesn't
   * belong to any of these families -- the caller should keep looking", i.e. fall through to
   * generic word-boundary containment).
   */
  function matchAnswerFamily(v, optionTexts) {
    // -- decline to answer -----------------------------------------------------------------
    if (DECLINE_RE.test(v)) return findDeclineMatch(optionTexts);

    // -- plain yes/no -----------------------------------------------------------------------
    if (v === 'yes' || v === 'no') {
      return findUniqueMatch(optionTexts, new RegExp('^\\s*' + v + '\\b', 'i'));
    }

    // -- gender -------------------------------------------------------------------------------
    if (v === 'male') return findUniqueMatch(optionTexts, /^(male|man)\b/i);
    if (v === 'female') return findUniqueMatch(optionTexts, /^(female|woman)\b/i);
    if (/^non[-\s]?binary$/.test(v)) return findUniqueMatch(optionTexts, /non[-\s]?binary/i);

    // -- veteran status -----------------------------------------------------------------------
    // Four distinct known phrasings, each with its OWN fallback chain -- deliberately NOT a
    // single shared regex, because "not a veteran" and "not a protected veteran" mean different
    // things and conflating them is exactly the kind of guess this project refuses to make.
    if (v === 'i am not a veteran') {
      var vr = findUniqueMatch(optionTexts, /\bnot a veteran\b/i);
      if (vr === -1) vr = findUniqueMatch(optionTexts, /\bnot a protected veteran\b/i);
      if (vr === -1) vr = findUniqueMatch(optionTexts, /^\s*no\b/i);
      return vr;
    }
    if (v === 'i am a veteran, but not a protected veteran') {
      var vr2 = findUniqueMatch(optionTexts, /\bveteran\b.*\bnot a protected\b/i);
      if (vr2 === -1) vr2 = findUniqueMatch(optionTexts, /\bnot a protected veteran\b/i);
      return vr2;
    }
    if (v === 'i am a protected veteran' ||
        v === 'i identify as one or more of the classifications of protected veteran') {
      var vIdx = -1, vCount = 0;
      for (var i = 0; i < optionTexts.length; i++) {
        var ot = cleanText(optionTexts[i]);
        if (/(protected veteran|identify as one or more)/i.test(ot) && !NEGATION_RE.test(ot)) {
          vCount++;
          if (vCount === 1) vIdx = i; else return -1;
        }
      }
      return vCount === 1 ? vIdx : -1;
    }
    if (v === 'i am not a protected veteran') {
      // Legacy value, deliberately ambiguous between "not a veteran at all" and "veteran but
      // not protected" -- never guess between them.
      return findUniqueMatch(optionTexts, /\bnot a protected veteran\b/i);
    }

    // -- disability status ----------------------------------------------------------------------
    if (/disability/.test(v) && /^no\b/.test(v)) {
      var noIdx = -1, noCount = 0;
      for (var j = 0; j < optionTexts.length; j++) {
        var otD = cleanText(optionTexts[j]);
        var fitsNo = /^\s*no\b/i.test(otD) || /do(n'?t| not) have a disability/i.test(otD);
        if (fitsNo && !/wish|want|answer/i.test(otD)) {
          noCount++;
          if (noCount === 1) noIdx = j; else return -1;
        }
      }
      return noCount === 1 ? noIdx : -1;
    }
    if (/disability/.test(v) && /^yes\b/.test(v)) {
      var yesIdx = -1, yesCount = 0;
      for (var k = 0; k < optionTexts.length; k++) {
        var otY = cleanText(optionTexts[k]);
        var fitsYes = /^\s*yes\b/i.test(otY) || /\bi have a disability\b/i.test(otY) || /have had one/i.test(otY);
        if (fitsYes && !NEGATION_RE.test(otY)) {
          yesCount++;
          if (yesCount === 1) yesIdx = k; else return -1;
        }
      }
      return yesCount === 1 ? yesIdx : -1;
    }

    // -- race / ethnicity -----------------------------------------------------------------------
    if (RACE_VALUES.indexOf(v) !== -1) {
      var raceRe = new RegExp('^' + escapeRegExp(v) + '\\b', 'i');
      return findUniqueMatch(optionTexts, raceRe);
    }

    return null; // not a recognised family -- caller falls through to generic containment
  }

  /**
   * Shared choice/option matcher for native <select>s, radio-button labels, and Workday's
   * custom dropdown widget. Priority: exact (normalised) -> answer families (decline-to-answer,
   * yes/no, gender, veteran, disability, race -- see matchAnswerFamily above) -> word-boundary
   * containment (NEVER a raw substring -- see the project brief: "Male" must never match
   * "Female" just because "female".indexOf("male") !== -1). Callers keep their OWN pre-existing
   * special-case tiers (findOptionMatch's US-state cross-match and option.value match,
   * matchWorkdayDropdownOption's degree-family and country-alias logic) as steps BEFORE calling
   * this -- those are untouched and still run first.
   *
   * Returns an option index, or -1 when nothing matches confidently enough to fill blind.
   */
  function matchChoiceOption(value, optionTexts) {
    var norm = function (s) { return cleanText(s).toLowerCase(); };
    var v = norm(value);
    if (!v) return -1;
    var i;

    // 1. exact normalised match.
    for (i = 0; i < optionTexts.length; i++) {
      if (norm(optionTexts[i]) === v) return i;
    }

    // 1b. degree-level / country synonym normalisation -- e.g. a specific degree TITLE like
    // "Bachelor of Design" against a generic Greenhouse/Ashby "Degree" combobox whose options
    // are plain LEVELS ("Bachelor's Degree", "Master's Degree", ...); or "USA"/"US" against a
    // spelled-out "United States of America". Previously only matchWorkdayDropdownOption ran
    // this -- a live probe (2026-09-24) showed a Greenhouse Degree combobox fail on exactly
    // this shape ("no confident match... among filtered options" for "Bachelor of Design"
    // against ["High School Diploma", "Associate's Degree", "Bachelor's Degree", ...]), so it
    // is promoted here to benefit every matchChoiceOption caller (combobox, checkbox-group,
    // radio, native select), not only Workday's dropdown.
    var fam = degreeFamilyOf(value);
    if (fam) {
      var famRe = WD_DEGREE_FAMILY_OPTION_RE[fam];
      var famMatches = [];
      for (i = 0; i < optionTexts.length; i++) {
        if (famRe.test(optionTexts[i])) famMatches.push(i);
      }
      if (famMatches.length === 1) return famMatches[0];
    }
    var alias = countryAliasOf(value);
    if (alias) {
      for (i = 0; i < optionTexts.length; i++) {
        if (norm(optionTexts[i]) === alias) return i;
      }
    }

    // 2. answer families -- a recognised family short-circuits here, whether it finds a
    //    fitting option (returns its index) or not (returns -1) -- see matchAnswerFamily.
    //    Family values can legitimately run long ("I am a veteran, but not a protected
    //    veteran" is 9 words), so this check happens BEFORE the prose guard below.
    var familyResult = matchAnswerFamily(v, optionTexts);
    if (familyResult !== null) return familyResult;

    // A long, free-text/prose-shaped value (more than ~6 words -- an open-ended answer, not a
    // pick from a short list of choices) is NEVER matched by containment: a rambling sentence
    // that happens to contain an option word ("...I know Figma..." containing "no" inside
    // "know", or "...but I am open to it" containing a 4-letter "Open" option) is not the same
    // as the user picking that option. Only an exact match (tier 1, already tried above) can
    // resolve a value this long -- the word-boundary anchoring below guards against SHORT
    // accidental substrings, but not against a merely-long-enough option word turning up
    // somewhere in an otherwise unrelated sentence.
    if (v.split(/\s+/).filter(Boolean).length > 6) return -1;

    // 3. word-boundary containment. Forward direction (option contains value) is tried across
    //    ALL options first; only if that finds nothing at all does the reverse direction (value
    //    contains option) get tried -- and only for options of >= 4 (cleaned) characters, so a
    //    short option text ("no", "ok") can never win just by coincidentally appearing inside a
    //    longer value string. Either direction: if MORE THAN ONE option matches, the tier is
    //    ambiguous and returns -1 -- NEVER the first of several ("Engineer" must not blindly
    //    pick "Software Engineer" when "Site Engineer" also matches).
    var valueRe = new RegExp('\\b' + escapeRegExp(v) + '\\b', 'i');
    var forwardMatches = [];
    for (i = 0; i < optionTexts.length; i++) {
      var otNorm = norm(optionTexts[i]);
      if (otNorm && valueRe.test(otNorm)) forwardMatches.push(i);
    }
    if (forwardMatches.length === 1) return forwardMatches[0];
    if (forwardMatches.length > 1) return -1;

    var reverseMatches = [];
    for (i = 0; i < optionTexts.length; i++) {
      var otNorm2 = norm(optionTexts[i]);
      if (otNorm2.length < 4) continue;
      if (new RegExp('\\b' + escapeRegExp(otNorm2) + '\\b', 'i').test(v)) reverseMatches.push(i);
    }
    if (reverseMatches.length === 1) return reverseMatches[0];
    return -1;
  }

  /**
   * Match priority: exact (normalised) -> normalised synonym (degree family / country alias) ->
   * matchChoiceOption's answer families / word-boundary containment (see above -- this is the
   * ONE shared final tier, also used by findOptionMatch and setRadioValue).
   */
  function matchWorkdayDropdownOption(target, optionTexts) {
    var norm = function (s) { return cleanText(s).toLowerCase(); };
    var t = norm(target);
    if (!t) return -1;
    var i;

    for (i = 0; i < optionTexts.length; i++) {
      if (norm(optionTexts[i]) === t) return i;
    }

    var fam = degreeFamilyOf(target);
    if (fam) {
      var famRe = WD_DEGREE_FAMILY_OPTION_RE[fam];
      for (i = 0; i < optionTexts.length; i++) {
        if (famRe.test(optionTexts[i])) return i;
      }
    }

    var alias = countryAliasOf(target);
    if (alias) {
      for (i = 0; i < optionTexts.length; i++) {
        if (norm(optionTexts[i]) === alias) return i;
      }
    }

    return matchChoiceOption(target, optionTexts);
  }

  /**
   * Opens the dropdown, matches `value` against its options, and clicks the match — or, when
   * nothing matches confidently, presses Escape and leaves the field untouched. NEVER falls
   * back to the first option (see the project brief: a typeahead once blindly picked
   * "Venezuela" for "Arizona" on a real application).
   */
  /**
   * Opens the dropdown, matches `value` against its options, and clicks the match — or, when
   * nothing matches confidently, presses Escape and leaves the field untouched. NEVER falls
   * back to the first option (see the project brief: a typeahead once blindly picked
   * "Venezuela" for "Arizona" on a real application).
   *
   * When the target isn't among the currently-rendered options, scrolls the listbox (and
   * presses ArrowDown, S4) up to 15 times, re-collecting each time, and gives up after two
   * consecutive rounds that render no new option labels at all — a long/virtualized list
   * (Country, State, Source) that only renders a window of its options at a time.
   */
  function fillWorkdayDropdown(entry, value) {
    var button = entry.button;
    var doc = ownerDoc(button);
    if (!isWorkdayDropdownOpenerSafe(button)) {
      return Promise.resolve({ ok: false, reason: 'safety guard refused the dropdown opener' });
    }
    var target = String(value == null ? '' : value).trim();
    if (!target) return Promise.resolve({ ok: false, reason: 'empty value' });

    dispatchPointerClickSequence(button);

    return waitFor(function () { return resolveWorkdayListbox(doc, button); }, 2000, doc).then(function (listbox) {
      if (!listbox) return { ok: false, reason: 'dropdown popup never appeared' };

      var optionEls = collectWorkdayListboxOptions(listbox);
      var texts = optionEls.map(optionAccessibleText);
      var idx = matchWorkdayDropdownOption(target, texts);

      // Compares the RENDERED LABELS, not just how many there are: a virtualized listbox can
      // keep showing the same COUNT of options on every round (a fixed-size sliding window)
      // while the actual set behind it keeps changing -- comparing length alone would have
      // declared that "stale" after the very first round and given up long before reaching a
      // target seated deep in a long list.
      function scrollRound(roundsLeft, staleRounds, lastKey) {
        if (idx !== -1 || roundsLeft <= 0 || staleRounds >= 2) return Promise.resolve();
        if (typeof listbox.scrollBy === 'function') {
          try { listbox.scrollBy(0, listbox.clientHeight || 200); } catch (eScroll) { /* best effort */ }
        } else if (typeof listbox.scrollTop === 'number') {
          listbox.scrollTop += (listbox.clientHeight || 200);
        }
        dispatchKeyboardEvent(button, 'keydown', 'ArrowDown', 'ArrowDown', 40);
        return wdSleep(150).then(function () {
          optionEls = collectWorkdayListboxOptions(listbox);
          texts = optionEls.map(optionAccessibleText);
          idx = matchWorkdayDropdownOption(target, texts);
          var key = texts.join('␟');
          var grew = key !== lastKey;
          return scrollRound(roundsLeft - 1, grew ? 0 : staleRounds + 1, key);
        });
      }

      return scrollRound(15, 0, texts.join('␟')).then(function () {
        if (idx === -1) {
          dispatchKeyboardEvent(button, 'keydown', 'Escape', 'Escape', 27);
          return { ok: false, reason: 'no confident match for "' + target + '" among dropdown options' };
        }

        var matched = optionEls[idx];
        if (!isWorkdayOptionSafe(matched, listbox)) {
          dispatchKeyboardEvent(button, 'keydown', 'Escape', 'Escape', 27);
          return { ok: false, reason: 'matched option failed the safety guard' };
        }

        dispatchPointerClickSequence(matched);
        var matchedText = texts[idx];
        return waitFor(function () {
          var shown = cleanText(button.textContent).toLowerCase();
          return shown && shown.indexOf(cleanText(matchedText).toLowerCase()) !== -1;
        }, 1000, doc).then(function (verified) {
          if (!verified) return { ok: false, reason: 'selected option did not appear on the button afterwards' };
          return { ok: true, matchedText: matchedText };
        });
      });
    });
  }

  // ---------------------------------------------------------------------
  // Workday prompt / multi-select (multiSelectContainer) — Field of Study, School,
  // Certification, Skills, ...
  // ---------------------------------------------------------------------

  // Real anatomy (S3): a "menuItem" row carries the highlight/selection state; inside it, a
  // "promptLeafNode" carries the CHECKED state and is what a pointer sequence must land on; its
  // own child "checkboxPanel" input is the actual checkbox; the visible label lives on
  // "promptOption"'s `data-automation-label`. WD_PROMPT_RESULT_SELECTOR is kept ONLY as a
  // fallback for a tenant/mock that renders no menuItem rows at all.
  var WD_PROMPT_ROW_SELECTOR = '[data-automation-id="menuItem"]';
  var WD_PROMPT_RESULT_SELECTOR = '[data-automation-id*="promptOption"], [data-automation-id*="checkboxItem"], [role="option"]';
  var WD_PROMPT_NO_ITEMS_RE = /no (items|matches|results)\b/i;
  var WD_PROMPT_DEBOUNCE_MS = 500;
  var WD_PROMPT_RESULT_TIMEOUT_MS = 8000;
  var WD_PROMPT_ARROWDOWN_MAX = 40;
  var WD_PROMPT_ARROWDOWN_DELAY_MS = 150;
  var WD_PROMPT_MAX_TERMS = 15;
  var WD_PROMPT_TOTAL_BUDGET_MS = 60000;
  var WD_PROMPT_NO_POPUP_ABORT_STREAK = 2;

  function findWorkdayPrompts(root) {
    var containers = Array.prototype.slice.call(root.querySelectorAll('[data-automation-id="multiSelectContainer"]'));
    var out = [];
    for (var i = 0; i < containers.length; i++) {
      var c = containers[i];
      if (!isVisible(c)) continue;
      var input = c.querySelector('input');
      if (!input) continue;
      var formField = findWorkdayFormField(c) || c;
      out.push({ container: c, input: input, formField: formField, label: getWorkdayFieldLabel(formField) });
    }
    return out;
  }

  function isWorkdayPromptInputSafe(el) {
    if (!el || el.tagName !== 'INPUT') return false;
    if (hasWorkdayHardDenyAutomationId(el)) return false;
    var container = el.closest ? el.closest('[data-automation-id="multiSelectContainer"]') : null;
    if (!container) return false;
    if (hasWorkdayHardDenyAutomationId(container)) return false;
    if (!findWorkdayFormField(el)) return false;
    if (el.disabled) return false;
    if (!isVisible(el)) return false;
    return true;
  }

  function escapeRegExp(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  /**
   * Match priority: exact (case-insensitive) -> the "(ACRONYM)" form -> nothing. NEVER a
   * prefix/startsWith or substring match (the previous tiers here) — see the project brief:
   * "Java" must never match a rendered "JavaScript" while the exact "Java" row sits one
   * ArrowDown away in a virtualized list. A TIE — more than one option normalising to the same
   * exact or acronym text — fails outright rather than guessing which one was meant.
   */
  function matchWorkdayPromptOption(term, optionTexts) {
    var norm = function (s) { return cleanText(s).toLowerCase(); };
    var t = norm(term);
    if (!t) return -1;
    var i;

    var exactHits = [];
    for (i = 0; i < optionTexts.length; i++) {
      if (norm(optionTexts[i]) === t) exactHits.push(i);
    }
    if (exactHits.length === 1) return exactHits[0];
    if (exactHits.length > 1) return -1;

    var acronymRe = new RegExp('\\(' + escapeRegExp(t) + '\\)', 'i');
    var acronymHits = [];
    for (i = 0; i < optionTexts.length; i++) {
      if (acronymRe.test(optionTexts[i])) acronymHits.push(i);
    }
    if (acronymHits.length === 1) return acronymHits[0];
    return -1;
  }

  function clearWorkdayPromptInput(input) {
    setNativeValue(input, '');
  }

  function snapshotWorkdayPopups(doc) {
    return Array.prototype.slice.call(doc.querySelectorAll('[data-automation-widget="wd-popup"]'));
  }

  /**
   * Finds the popup a prompt's results render into. Every working source agrees Workday portals
   * this to <body>, away from the field — S1: "this element's id is used to identify the
   * dropdown element, since the dropdown is a popup and not a direct child of this field",
   * linked by `[data-associated-widget]` == the multiSelectContainer's own id. Falls back to the
   * input's own aria-controls/aria-owns target, then — ONLY ever restricted to a popup that
   * appeared SINCE `beforePopups` (a snapshot taken before we typed) — the single most recently
   * opened VISIBLE wd-popup. Deliberately never a bare document-wide scan of every popup on the
   * page: with two prompts' popups open at once, that could read a DIFFERENT field's results.
   */
  function resolveWorkdayPromptPopup(doc, input, beforePopups) {
    var container = input.closest ? input.closest('[data-automation-id="multiSelectContainer"]') : null;
    var id = container && container.id;
    if (id) {
      var linked = doc.querySelector('[data-associated-widget="' + cssEscape(id) + '"]');
      if (linked) {
        var popup = (linked.closest && linked.closest('[data-automation-widget="wd-popup"]')) || linked;
        if (isVisible(popup)) return popup;
      }
    }
    var controlsId = (input.getAttribute && (input.getAttribute('aria-controls') || input.getAttribute('aria-owns'))) || '';
    if (controlsId && doc.getElementById) {
      var byId = doc.getElementById(controlsId);
      if (byId && isVisible(byId)) return byId;
    }
    if (beforePopups) {
      var fresh = Array.prototype.slice.call(doc.querySelectorAll('[data-automation-widget="wd-popup"]'))
        .filter(function (p) { return beforePopups.indexOf(p) === -1 && isVisible(p); });
      if (fresh.length) return fresh[fresh.length - 1]; // the single most recently opened
    }
    return null;
  }

  function workdayPromptNoItemsShown(popup) {
    return WD_PROMPT_NO_ITEMS_RE.test(cleanText(popup.textContent));
  }

  function workdayPromptRows(popup) {
    var rows = Array.prototype.slice.call(popup.querySelectorAll(WD_PROMPT_ROW_SELECTOR)).filter(isVisible);
    if (rows.length) return rows;
    // Fallback shape only, for a tenant/mock that renders no menuItem rows at all.
    return Array.prototype.slice.call(popup.querySelectorAll(WD_PROMPT_RESULT_SELECTOR)).filter(isVisible);
  }

  function workdayPromptRowLabel(row) {
    var opt = row.querySelector && row.querySelector('[data-automation-id="promptOption"]');
    var byAttr = opt && opt.getAttribute && opt.getAttribute('data-automation-label');
    if (byAttr) return cleanText(byAttr);
    return optionAccessibleText(row);
  }

  function workdayPromptRowCheckbox(row) {
    return (row.querySelector && (row.querySelector('input[data-automation-id="checkboxPanel"]') || row.querySelector('input[type="checkbox"]'))) || null;
  }

  function workdayPromptRowIsChecked(row) {
    var leaf = (row.getAttribute && row.getAttribute('data-automation-id') === 'promptLeafNode')
      ? row
      : (row.querySelector && row.querySelector('[data-automation-id="promptLeafNode"]'));
    if (leaf && leaf.getAttribute && leaf.getAttribute('data-automation-checked') === 'Checked') return true;
    var cb = workdayPromptRowCheckbox(row);
    return !!(cb && cb.checked);
  }

  function workdayPromptClickTarget(row) {
    return (row.querySelector && row.querySelector('[data-automation-id="promptLeafNode"]')) || row;
  }

  /**
   * Skills: the row's own checkbox first — S8 says a bare click works; S3 says "React ignores a
   * bare el.click()" on some tenants and needs a full pointer sequence on the promptLeafNode
   * instead. They disagree, so this tries the cheap path, verifies via `data-automation-checked`
   * (never assumed), and falls back to the pointer sequence when the state didn't move. The
   * caller still verifies the actual pill regardless of which technique the checked-state came
   * from. Single-select (no checkbox at all): a pointer sequence on the leaf/row directly.
   */
  function clickWorkdayPromptRow(row) {
    var doc = ownerDoc(row);
    var checkbox = workdayPromptRowCheckbox(row);
    if (!checkbox) {
      dispatchPointerClickSequence(workdayPromptClickTarget(row));
      return Promise.resolve(true);
    }
    if (!isClickSafe(checkbox)) return Promise.resolve(false);
    checkbox.click();
    return waitFor(function () { return workdayPromptRowIsChecked(row) ? true : null; }, 1500, doc).then(function (checked) {
      if (!checked) dispatchPointerClickSequence(workdayPromptClickTarget(row));
      return true;
    });
  }

  function workdayPromptPillTexts(entry) {
    var formField = entry.formField;
    if (!formField || !formField.querySelectorAll) return [];
    var pills = Array.prototype.slice.call(formField.querySelectorAll(
      'ul[data-automation-id="selectedItemList"] li, [data-automation-id="selectedItem"], [data-automation-id="promptSelectedItem"]'
    ));
    return pills.map(function (li) { return cleanText(li.textContent); }).filter(Boolean);
  }

  /** Scoped to `entry.formField` ONLY — no document-wide fallback (see the project brief: that
   *  let a different field's pre-existing pill satisfy this field's own check). */
  function workdayPromptHasPill(entry, term) {
    var t = cleanText(term).toLowerCase();
    if (!t) return false;
    return workdayPromptPillTexts(entry).some(function (p) { return p.toLowerCase().indexOf(t) !== -1; });
  }

  function getWorkdayPromptCurrentValue(entry) {
    return workdayPromptPillTexts(entry).join(', ');
  }

  /** Escape on the input, then clear it, then wait (briefly, best-effort) for the popup itself to disappear. */
  function closeWorkdayPromptPopup(input, popup) {
    dispatchKeyboardEvent(input, 'keydown', 'Escape', 'Escape', 27);
    clearWorkdayPromptInput(input);
    if (!popup) return Promise.resolve();
    return waitFor(function () { return isVisible(popup) ? null : true; }, 1000, ownerDoc(input)).then(function () {});
  }

  /**
   * Given the popup's currently-rendered rows, matches `t` and clicks it — or, when the list is
   * virtualized and the exact/acronym match isn't rendered yet, walks the highlight with
   * ArrowDown (S3) up to WD_PROMPT_ARROWDOWN_MAX times, reading whichever row is currently
   * `data-automation-selected="true"` and re-collecting the plain rendered set each step (new
   * rows can also just scroll into the DOM without ever becoming "selected").
   */
  function matchWorkdayPromptRows(entry, input, doc, t, popup, rows) {
    var texts = rows.map(workdayPromptRowLabel);
    var idx = matchWorkdayPromptOption(t, texts);

    function withMatch(matchedRow, matchedText) {
      if (!isWorkdayOptionSafe(matchedRow, popup)) {
        return closeWorkdayPromptPopup(input, popup).then(function () {
          return { ok: false, reason: 'matched result failed the safety guard' };
        });
      }
      return clickWorkdayPromptRow(matchedRow).then(function (clicked) {
        if (!clicked) {
          return closeWorkdayPromptPopup(input, popup).then(function () {
            return { ok: false, reason: 'checkbox click refused by the click guard' };
          });
        }
        return waitFor(function () { return workdayPromptHasPill(entry, matchedText) ? true : null; }, 2000, doc).then(function (added) {
          return closeWorkdayPromptPopup(input, popup).then(function () {
            if (!added) return { ok: false, reason: 'click did not add "' + t + '" to the selected item list' };
            return { ok: true, matchedText: matchedText };
          });
        });
      });
    }

    if (idx !== -1) return withMatch(rows[idx], texts[idx]);

    // Stops after 2 consecutive rounds where NEITHER the highlighted row NOR the rendered set
    // actually changed (the same discipline as fillWorkdayDropdown's scrollRound) -- a prompt
    // that isn't virtualized at all (Field of Study's fixed 3-option "live search" mock, or a
    // real single-select prompt with a short, fully-rendered list) would otherwise walk the
    // FULL 40 tries x 150ms (~6s) on every single no-match term. That is worse than the
    // original 3s-per-term bug this rewrite exists to fix, just relocated to a different case.
    function arrowWalk(triesLeft, staleRounds, lastKey) {
      if (triesLeft <= 0 || staleRounds >= 2) {
        return closeWorkdayPromptPopup(input, popup).then(function () {
          return { ok: false, reason: 'no confident match among results for "' + t + '"' };
        });
      }
      dispatchKeyboardEvent(input, 'keydown', 'ArrowDown', 'ArrowDown', 40);
      return wdSleep(WD_PROMPT_ARROWDOWN_DELAY_MS).then(function () {
        var current = popup.querySelector('[data-automation-id="menuItem"][data-automation-selected="true"]');
        var currentLabel = current ? workdayPromptRowLabel(current) : '';
        if (current && cleanText(currentLabel).toLowerCase() === cleanText(t).toLowerCase()) {
          return withMatch(current, currentLabel);
        }
        var freshRows = workdayPromptRows(popup);
        var freshTexts = freshRows.map(workdayPromptRowLabel);
        var freshIdx = matchWorkdayPromptOption(t, freshTexts);
        if (freshIdx !== -1) return withMatch(freshRows[freshIdx], freshTexts[freshIdx]);
        var key = currentLabel + '␟' + freshTexts.join('␟');
        var grew = key !== lastKey;
        return arrowWalk(triesLeft - 1, grew ? 0 : staleRounds + 1, key);
      });
    }
    return arrowWalk(WD_PROMPT_ARROWDOWN_MAX, 0, texts.join('␟'));
  }

  /**
   * Types one term into a prompt/multi-select, finds Workday's own results IN THE PORTALLED
   * POPUP (see resolveWorkdayPromptPopup — this is the fix for the reported "hangs on ...
   * skills" bug: the previous code here searched only inside the field, so every term waited out
   * its full timeout and a Workday auto-commit was cleared and reported as a failure), matches
   * by strict priority, and clicks the match — or, with no confident match, clears the typed
   * text and reports failure. NEVER clicks the first result blindly.
   *
   * `opts` (optional) overrides timing for a caller/test: `resultTimeoutMs`, `debounceMs`.
   *
   * Resolves to `{ ok, reason, matchedText, noPopupAtAll }` — `noPopupAtAll` is true only when
   * NEITHER a popup NOR a matching pill ever appeared at all within budget (used by
   * fillWorkdayPromptValue to abort the rest of a Skills-shaped list early, per the project
   * brief, rather than let every remaining term hang for its own full timeout).
   */
  function fillWorkdayPromptTerm(entry, term, opts) {
    opts = opts || {};
    var resultTimeoutMs = opts.resultTimeoutMs == null ? WD_PROMPT_RESULT_TIMEOUT_MS : opts.resultTimeoutMs;
    var debounceMs = opts.debounceMs == null ? WD_PROMPT_DEBOUNCE_MS : opts.debounceMs;
    var input = entry.input;
    var doc = ownerDoc(input);
    if (!isWorkdayPromptInputSafe(input)) {
      return Promise.resolve({ ok: false, reason: 'safety guard refused the prompt input' });
    }
    var t = String(term == null ? '' : term).trim();
    if (!t) return Promise.resolve({ ok: false, reason: 'empty term' });

    if (workdayPromptHasPill(entry, t)) {
      return Promise.resolve({ ok: true, matchedText: t, alreadyPresent: true });
    }

    var beforePopups = snapshotWorkdayPopups(doc);
    setNativeValue(input, t);

    return wdSleep(debounceMs).then(function () {
      dispatchKeyboardEvent(input, 'keydown', 'Enter', 'Enter', 13);
      dispatchKeyboardEvent(input, 'keypress', 'Enter', 'Enter', 13);
      dispatchKeyboardEvent(input, 'keyup', 'Enter', 'Enter', 13);

      var start = Date.now();
      var sawPopupEver = false;

      function poll() {
        if (workdayPromptHasPill(entry, t)) return { kind: 'pill' };
        var popup = resolveWorkdayPromptPopup(doc, input, beforePopups);
        if (popup) {
          sawPopupEver = true;
          if (workdayPromptNoItemsShown(popup)) return { kind: 'empty', popup: popup };
          var rows = workdayPromptRows(popup);
          if (rows.length) return { kind: 'rows', popup: popup, rows: rows };
        }
        if (Date.now() - start >= resultTimeoutMs) return { kind: 'timeout' };
        return null;
      }

      return waitFor(poll, resultTimeoutMs, doc).then(function (outcome) {
        outcome = outcome || { kind: 'timeout' };

        if (outcome.kind === 'pill') return { ok: true, matchedText: t }; // Workday auto-committed it

        if (outcome.kind === 'empty') {
          return closeWorkdayPromptPopup(input, outcome.popup).then(function () {
            return { ok: false, reason: 'Workday reported no results for "' + t + '"' };
          });
        }
        if (outcome.kind === 'timeout') {
          clearWorkdayPromptInput(input);
          return { ok: false, reason: 'no results returned for "' + t + '"', noPopupAtAll: !sawPopupEver };
        }
        return matchWorkdayPromptRows(entry, input, doc, t, outcome.popup, outcome.rows);
      });
    });
  }

  /**
   * `value` is either a single term (Field of Study, School, Certification) or an array
   * (Skills — the service sends a list). For a list: add each in turn, skip one with no
   * confident match and continue with the rest (skip-and-continue, never stop on the first
   * failure) — capped at WD_PROMPT_MAX_TERMS terms and WD_PROMPT_TOTAL_BUDGET_MS overall. If the
   * first WD_PROMPT_NO_POPUP_ABORT_STREAK terms in a row each produce no popup and no pill AT
   * ALL, this tenant's Skills widget almost certainly isn't the shape this code expects — stop
   * the rest of the list rather than let every remaining term hang for its own full timeout,
   * which is exactly the reported bug this rewrite targets.
   */
  function fillWorkdayPromptValue(entry, value, opts) {
    if (Array.isArray(value)) {
      var terms = value.slice(0, WD_PROMPT_MAX_TERMS);
      var overCap = value.slice(WD_PROMPT_MAX_TERMS);
      var results = [];
      var noPopupStreak = 0;
      var startedAt = Date.now();

      function next(i) {
        if (i >= terms.length) return Promise.resolve(results);
        if (Date.now() - startedAt > WD_PROMPT_TOTAL_BUDGET_MS) {
          for (var j = i; j < terms.length; j++) {
            results.push({ term: terms[j], result: { ok: false, reason: 'not attempted -- overall time budget exhausted' } });
          }
          return Promise.resolve(results);
        }
        return fillWorkdayPromptTerm(entry, terms[i], opts).then(function (r) {
          results.push({ term: terms[i], result: r });
          noPopupStreak = r.noPopupAtAll ? (noPopupStreak + 1) : 0;
          if (noPopupStreak >= WD_PROMPT_NO_POPUP_ABORT_STREAK) {
            for (var k = i + 1; k < terms.length; k++) {
              results.push({ term: terms[k], result: { ok: false, reason: 'Workday skills results not found — please send a Report page' } });
            }
            return results;
          }
          return next(i + 1);
        });
      }
      return next(0).then(function (all) {
        for (var s = 0; s < overCap.length; s++) {
          all.push({ term: overCap[s], result: { ok: false, reason: 'not attempted -- over the ' + WD_PROMPT_MAX_TERMS + '-term cap' } });
        }
        var failedTerms = all.filter(function (r) { return !r.result.ok; }).map(function (r) { return r.term; });
        var anyOk = all.some(function (r) { return r.result.ok; });
        return { ok: anyOk, failedTerms: failedTerms, results: all };
      });
    }
    return fillWorkdayPromptTerm(entry, value, opts);
  }

  // ---------------------------------------------------------------------
  // Workday Self-Identify checkbox group (disabilityStatus-CheckboxGroup, CC-305) — several
  // independent <input type=checkbox> elements that together behave as ONE single-choice
  // question ("requires exactly one selection" — S5), never as N independent booleans. Scoped
  // narrowly to `fieldset[data-automation-id$="-CheckboxGroup"]`, the one concrete, cited shape
  // — deliberately NOT the broader "any formField with more than one checkbox" heuristic the
  // brief also mentions, since that would risk misreading the Skills popup's own checkboxItem
  // rows (also `input[type=checkbox]`, but a completely different widget) as a choice group.
  // ---------------------------------------------------------------------

  function findWorkdayCheckboxGroups(root) {
    var fieldsets = Array.prototype.slice.call(root.querySelectorAll('fieldset[data-automation-id$="-CheckboxGroup"]'));
    var out = [];
    for (var i = 0; i < fieldsets.length; i++) {
      var fs = fieldsets[i];
      if (!isVisible(fs)) continue;
      if (hasWorkdayHardDenyAutomationId(fs)) continue;
      var boxes = Array.prototype.slice.call(fs.querySelectorAll('input[type="checkbox"]')).filter(function (cb) {
        return !cb.disabled && !hasWorkdayHardDenyAutomationId(cb);
      });
      if (!boxes.length) continue;
      var container = findWorkdayFormField(fs) || fs;
      out.push({ fieldset: fs, boxes: boxes, container: container, label: getWorkdayCheckboxGroupLabel(fs, container) });
    }
    return out;
  }

  /** The group's own question text — its <legend> first, else the usual formField label resolution. */
  function getWorkdayCheckboxGroupLabel(fieldset, container) {
    var legend = fieldset.querySelector && fieldset.querySelector('legend');
    if (legend) {
      var t = cleanText(legend.textContent);
      if (t) return t;
    }
    return getWorkdayFieldLabel(container);
  }

  /**
   * One option's own label — S5: "each option's label is at nextElementSibling.nextElementSibling";
   * the recommended, more robust shape first (`label[for]`), then that exact sibling walk, then
   * an ancestor `div[role=cell]` (a table-laid-out group), then the shared getLabel() fallback.
   */
  function getWorkdayCheckboxGroupOptionLabel(checkbox) {
    var byFor = checkbox.id && checkbox.ownerDocument ? checkbox.ownerDocument.querySelector('label[for="' + cssEscape(checkbox.id) + '"]') : null;
    if (byFor) {
      var t1 = cleanText(byFor.textContent);
      if (t1) return t1;
    }
    var sib = checkbox.nextElementSibling && checkbox.nextElementSibling.nextElementSibling;
    if (sib) {
      var t2 = cleanText(sib.textContent);
      if (t2) return t2;
    }
    var cell = checkbox.closest ? checkbox.closest('[role="cell"]') : null;
    if (cell) {
      var t3 = cleanText(cell.textContent);
      if (t3) return t3;
    }
    return cleanText(getLabel(checkbox));
  }

  function isWorkdayCheckboxGroupOptionSafe(el, fieldset) {
    if (!el || el.tagName !== 'INPUT' || (el.type || '').toLowerCase() !== 'checkbox') return false;
    if (hasWorkdayHardDenyAutomationId(el)) return false;
    if (!fieldset || !fieldset.contains(el)) return false;
    if (hasWorkdayHardDenyAutomationId(fieldset)) return false;
    if (el.disabled) return false;
    return true;
  }

  /**
   * Matches `value` against the group's own option labels (the SAME shared matcher used for
   * native selects/radios/Workday dropdowns — the CC-305 texts are exactly the decline/yes/no
   * "answer family" shapes it already knows), clicks that one option, then unchecks every OTHER
   * currently-checked box in the group so "exactly one" always holds. Never guesses: no
   * confident match leaves the whole group untouched.
   */
  function setWorkdayCheckboxGroupValue(entry, value) {
    var options = entry.boxes.map(getWorkdayCheckboxGroupOptionLabel);
    var idx = matchChoiceOption(value, options);
    if (idx === -1) return false;
    var target = entry.boxes[idx];
    if (!isWorkdayCheckboxGroupOptionSafe(target, entry.fieldset)) return false;
    if (!target.checked && !safeClick(target)) return false;
    if (!target.checked) return false;
    for (var i = 0; i < entry.boxes.length; i++) {
      var box = entry.boxes[i];
      if (box !== target && box.checked && isWorkdayCheckboxGroupOptionSafe(box, entry.fieldset)) safeClick(box);
    }
    return true;
  }

  // ---------------------------------------------------------------------
  // Choice widgets ground truth (2026-09-24 live probe) — react-select / generic ARIA
  // comboboxes, Ashby Yes/No button groups, checkbox groups, Lever's location type-ahead.
  // See docs/research/2026-09-24-ats-widget-ground-truth.md §6 and
  // docs/research/live-captures-2026-09-24/*.json for the real markup this is built from.
  //
  // The #1 gap on Greenhouse (~50% of the user's target jobs): EVERY dropdown-shaped question
  // (Country, Location, "How did you hear about us?", work authorization, sponsorship, Gender,
  // Hispanic/Latino, Veteran, Race, Degree, ...) renders as a react-select combobox --
  // `input.select__input[role=combobox]` inside `.select__control`. Scanned as a plain text
  // input, typing into it and reading the typed text back reports "filled" while react-select
  // drops that text on blur -- nothing was actually selected. This section makes it its own
  // widget kind so it is never scanned or filled as a text field again.
  // ---------------------------------------------------------------------

  /** A single, unadorned mouse event -- ground truth: react-select's flyout toggles on
   * `mouseup`, never `click` (a bare click is preventDefault()'d by the button). */
  function dispatchMouseEvent(el, type) {
    var view = realmOf(el);
    var Ctor = (view && view.MouseEvent) || (typeof MouseEvent !== 'undefined' ? MouseEvent : null);
    if (!Ctor) return;
    var evt;
    try { evt = new Ctor(type, { bubbles: true, cancelable: true, view: view || undefined }); }
    catch (e) {
      try { evt = new Ctor(type, { bubbles: true, cancelable: true }); }
      catch (e2) { return; }
    }
    el.dispatchEvent(evt);
  }

  // ---- combobox (react-select + generic ARIA) --------------------------------------------

  function isComboboxInputSafe(el) {
    if (!el || el.tagName !== 'INPUT') return false;
    if ((el.getAttribute && el.getAttribute('role')) !== 'combobox') return false;
    if (el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    if (!isVisible(el)) return false;
    return true;
  }

  /** The react-select control widget (".select__control") standing in for `input`, if any. */
  function comboboxControlOf(input) {
    return input.closest ? input.closest('[class*="select__control" i], [class*="Select__control" i]') : null;
  }

  /** The bounded field wrapper a react-select menu/value chips render inside -- one level up
   * from ".select__control", never a document-wide search (would risk another field's menu). */
  function comboboxFieldScope(input) {
    var control = comboboxControlOf(input);
    return (control && control.parentElement) || input.parentElement || input;
  }

  /**
   * The listbox/menu belonging to THIS combobox input, or null if none is open. Resolved via
   * aria-controls/aria-owns FIRST (the generic ARIA combobox contract, and what react-select
   * itself sets once open: `#react-select-<id>-listbox`) — only when that id is missing does
   * this fall back to a `.select__menu`-shaped descendant of the input's own small field
   * wrapper. Never another field's menu, however option-shaped it looks.
   */
  function resolveComboboxMenu(entry) {
    var input = entry.input;
    var doc = ownerDoc(input);
    var controlsAttr = (input.getAttribute && (input.getAttribute('aria-controls') || input.getAttribute('aria-owns'))) || '';
    var ids = controlsAttr.split(/\s+/).filter(Boolean);
    for (var i = 0; i < ids.length; i++) {
      var byId = doc && doc.getElementById ? doc.getElementById(ids[i]) : null;
      if (byId && isVisible(byId)) return byId;
    }
    var scope = comboboxFieldScope(input);
    if (scope && scope.querySelector) {
      var menu = scope.querySelector('[class*="select__menu" i], [class*="Select__menu" i], [role="listbox"]');
      if (menu && isVisible(menu)) return menu;
    }
    return null;
  }

  /**
   * `el` must look like a real react-select/ARIA option AND live inside `menu` (the listbox
   * THIS input controls, resolved by resolveComboboxMenu — never an arbitrary element, and
   * never another field's menu) — the guard the project brief requires every new click path
   * to have, re-checked at the point of action rather than trusted from scanning/matching.
   */
  function isComboboxOptionSafe(el, menu) {
    if (!el || el.nodeType !== 1) return false;
    if (!menu || !menu.contains(el)) return false;
    var role = el.getAttribute && el.getAttribute('role');
    var cls = (el.getAttribute && el.getAttribute('class')) || '';
    var looksLikeOption = role === 'option' || /select__option|Select__option|Select-option/i.test(cls);
    if (!looksLikeOption) return false;
    var tag = el.tagName;
    if (tag === 'BUTTON' || tag === 'A') return false;
    var effectiveType = String(el.type || '').toLowerCase();
    if (effectiveType === 'submit' || effectiveType === 'image') return false;
    if (!isVisible(el)) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    if (ADD_BUTTON_DENY_RE.test(optionAccessibleText(el))) return false; // never submit/next/save-shaped
    return true;
  }

  function comboboxOptionEls(menu) {
    if (!menu || !menu.querySelectorAll) return [];
    return Array.prototype.slice.call(
      menu.querySelectorAll('[role="option"], [class*="select__option" i], [class*="Select__option" i]')
    ).filter(isVisible);
  }

  var COMBOBOX_VALUE_CHIP_SELECTOR =
    '[class*="select__single-value" i], [class*="Select__single-value" i], ' +
    '[class*="select__multi-value__label" i], [class*="Select__multi-value__label" i]';

  /** Every currently-committed chip's text (single-value: one; multi-value: each tag/pill). */
  function getComboboxChipTexts(entry) {
    var scope = comboboxFieldScope(entry.input);
    if (!scope || !scope.querySelectorAll) return [];
    var chips = Array.prototype.slice.call(scope.querySelectorAll(COMBOBOX_VALUE_CHIP_SELECTOR));
    var texts = [];
    for (var i = 0; i < chips.length; i++) {
      var t = cleanText(chips[i].textContent);
      if (t) texts.push(t);
    }
    return texts;
  }

  /** Generic ARIA combobox fallback (no react-select classes at all): aria-activedescendant
   * naming the chosen option stands in for the chip text react-select renders. */
  function getGenericComboboxValue(entry) {
    var input = entry.input;
    var doc = ownerDoc(input);
    var activeId = input.getAttribute && input.getAttribute('aria-activedescendant');
    if (activeId && doc && doc.getElementById) {
      var opt = doc.getElementById(activeId);
      if (opt) {
        var t = cleanText(opt.textContent);
        if (t) return t;
      }
    }
    return '';
  }

  /** The field's current COMMITTED value (for getCurrentValue/undo) — never the search
   * input's own leftover typed text (see the project brief: typed-but-not-selected is not a
   * value). */
  function getComboboxCommittedValue(entry) {
    var chips = getComboboxChipTexts(entry);
    if (chips.length) return chips.join(', ');
    return getGenericComboboxValue(entry);
  }

  function normEqText(a, b) { return cleanText(a).toLowerCase() === cleanText(b).toLowerCase(); }

  /** True only once `matchedText` shows up as a genuinely COMMITTED chip/aria-state AND the
   * search input itself is empty — leftover search text sitting in the input is explicitly
   * NOT a commit (react-select drops it on blur; see the project brief). */
  function verifyComboboxSelection(entry, matchedText) {
    if (cleanText(entry.input.value || '')) return false;
    var chips = getComboboxChipTexts(entry);
    if (chips.length) {
      for (var i = 0; i < chips.length; i++) { if (normEqText(chips[i], matchedText)) return true; }
      return false;
    }
    var generic = getGenericComboboxValue(entry);
    return !!generic && normEqText(generic, matchedText);
  }

  /** True once `menu` has rendered SOMETHING to read -- at least one option, or an explicit
   * "no options" notice -- not just once the menu container itself becomes visible. A live
   * probe (2026-09-24, a Greenhouse "Degree" field) showed react-select can paint the menu
   * wrapper one render pass before its option rows exist, so a wait keyed on container
   * visibility alone can resolve against a container that is visible but still genuinely
   * empty, reading zero options and mis-diagnosing a perfectly normal static list as needing
   * the async type-to-filter fallback. */
  function comboboxMenuHasContent(menu) {
    if (!menu || !menu.querySelector) return false;
    if (menu.querySelector('[role="option"], [class*="select__option" i], [class*="Select__option" i]')) return true;
    return !!menu.querySelector('[class*="no-options" i], [class*="noresults" i], [class*="menu-notice" i]');
  }

  /** Opens the menu per the ground truth: mouseup on "Toggle flyout", else ArrowDown keyup on
   * the input itself. Returns a Promise of the now-open, rendered menu, or null if it never
   * opened / never finished rendering within budget. */
  function openCombobox(entry, doc) {
    var input = entry.input;
    var scope = comboboxFieldScope(input);
    var toggle = scope && scope.querySelector ? scope.querySelector('button[aria-label="Toggle flyout" i]') : null;
    if (toggle && isVisible(toggle) && !toggle.disabled) {
      dispatchMouseEvent(toggle, 'mouseup');
    } else {
      dispatchKeyboardEvent(input, 'keyup', 'ArrowDown', 'ArrowDown');
    }
    return waitFor(function () {
      var menu = resolveComboboxMenu(entry);
      return (menu && isVisible(menu) && comboboxMenuHasContent(menu)) ? menu : null;
    }, 1500, doc);
  }

  /** For a "City, State" value, the part to type to filter an async catalog is just the city. */
  function comboboxFilterQuery(target) {
    var t = String(target || '').trim();
    if (!t) return '';
    var commaIdx = t.indexOf(',');
    if (commaIdx > 0) t = t.slice(0, commaIdx).trim();
    return t.slice(0, 60);
  }

  /**
   * Ground truth (§6.6 step 3): "type... then poll for up to 4s, ignoring 'No options' for the
   * first ~800ms" — an async fetch (Greenhouse's own debounce is ~300ms) may still be in
   * flight, so an empty/no-options render in that first window is not yet a real answer. Two
   * stages against the SAME 4s total budget rather than one flat wait, so a genuinely static
   * list's zero-match render (which settles synchronously) is not mistaken for "still loading".
   */
  function waitForComboboxFilterResults(entry, doc) {
    function poll() {
      var menu = resolveComboboxMenu(entry);
      var els = comboboxOptionEls(menu);
      return els.length ? { menu: menu, els: els } : null;
    }
    return waitFor(poll, 800, doc).then(function (found) {
      if (found) return found;
      return waitFor(poll, 3200, doc);
    });
  }

  /** Selects `els[idx]` inside `menu` through the new guard, then verifies a genuine commit. */
  function commitComboboxOption(entry, menu, els, idx, doc) {
    var matched = els[idx];
    if (!isComboboxOptionSafe(matched, menu)) {
      return { ok: false, reason: 'matched option failed the safety guard' };
    }
    var matchedText = optionAccessibleText(matched);
    // Ground truth: react-select commits on mousedown (a bare click alone runs after blur and
    // is dropped) -- mousedown, mouseup, click on the option itself.
    dispatchPointerClickSequence(matched);
    return waitFor(function () { return verifyComboboxSelection(entry, matchedText) ? true : null; }, 1000, doc)
      .then(function (verified) {
        if (!verified) {
          return { ok: false, reason: 'selection did not commit (search text may have been dropped on blur)' };
        }
        return { ok: true, matchedText: matchedText };
      });
  }

  /** Fills ONE value into a combobox: open, match the rendered options, and if nothing
   * confident is there yet (an async/filtered catalog), type to filter and re-read once. Never
   * picks the first option, and never trusts typed-but-unselected text as a fill. */
  function fillComboboxOne(entry, value, doc) {
    if (!isComboboxInputSafe(entry.input)) {
      return Promise.resolve({ ok: false, reason: 'safety guard refused the combobox input' });
    }
    var target = String(value == null ? '' : value).trim();
    if (!target) return Promise.resolve({ ok: false, reason: 'empty value' });

    var already = getComboboxChipTexts(entry);
    for (var ai = 0; ai < already.length; ai++) {
      if (matchChoiceOption(target, [already[ai]]) === 0) return Promise.resolve({ ok: true, matchedText: already[ai] });
    }

    return openCombobox(entry, doc).then(function (menu) {
      var els = comboboxOptionEls(menu);
      // A live probe (2026-09-24, a Greenhouse "Degree" field) showed react-select can
      // re-render the menu AGAIN right after the open-wait resolves (recalculating the
      // focused/virtualized row), transiently clearing the options a tick after
      // comboboxMenuHasContent() saw them and before this line runs. One short re-settle
      // before concluding "genuinely empty" avoids mistaking that gap for an async catalog.
      if (!els.length && menu) {
        return waitFor(function () {
          var els2 = comboboxOptionEls(menu);
          return els2.length ? els2 : null;
        }, 600, doc).then(function (settled) {
          return matchAndCommitOrFilter(settled || []);
        });
      }
      return matchAndCommitOrFilter(els);

      function matchAndCommitOrFilter(els) {
        var texts = els.map(optionAccessibleText);
        var idx = matchChoiceOption(target, texts);
        if (idx !== -1) return commitComboboxOption(entry, menu, els, idx, doc);

        var query = comboboxFilterQuery(target);
        if (!query) {
          dispatchKeyboardEvent(entry.input, 'keydown', 'Escape', 'Escape');
          return { ok: false, reason: 'no confident match for "' + target + '" among combobox options' };
        }
        setNativeValue(entry.input, query);
        return waitForComboboxFilterResults(entry, doc).then(function (found) {
          if (!found) {
            dispatchKeyboardEvent(entry.input, 'keydown', 'Escape', 'Escape');
            setNativeValue(entry.input, '');
            return { ok: false, reason: 'no options rendered while filtering for "' + query + '"' };
          }
          var texts2 = found.els.map(optionAccessibleText);
          var idx2 = matchChoiceOption(target, texts2);
          if (idx2 === -1) {
            dispatchKeyboardEvent(entry.input, 'keydown', 'Escape', 'Escape');
            setNativeValue(entry.input, '');
            return { ok: false, reason: 'no confident match for "' + target + '" among filtered options' };
          }
          return commitComboboxOption(entry, found.menu, found.els, idx2, doc);
        });
      }
    });
  }

  /** `value` may be a single term or an array (a multi-select combobox) — added one at a time,
   * same discipline as fillWorkdayPromptValue: one failed/unmatched term never blocks the rest. */
  function fillComboboxValue(entry, value, doc) {
    if (Array.isArray(value)) {
      var results = [];
      function next(i) {
        if (i >= value.length) return Promise.resolve(results);
        return fillComboboxOne(entry, value[i], doc).then(function (r) {
          results.push({ term: value[i], result: r });
          return next(i + 1);
        });
      }
      return next(0).then(function (all) {
        var failedTerms = all.filter(function (r) { return !r.result.ok; }).map(function (r) { return r.term; });
        var anyOk = all.some(function (r) { return r.result.ok; });
        return { ok: anyOk, failedTerms: failedTerms };
      });
    }
    return fillComboboxOne(entry, value, doc);
  }

  // ---- Ashby-style Yes/No (and other short) button groups ----------------------------------

  // The confirmed, real Ashby marker (docs/research/live-captures-2026-09-24/structure-06/07):
  // `<button class="..._option_1svni_32  ashby-application-form-input-yesno-option">Yes</button>`.
  // Generalised to the "-option" class family Ashby uses for the whole input-widget line
  // (yesno-option, checkbox-group-option, ...) in case a future Ashby release renders a
  // same-shaped button group with more than two options.
  var BUTTON_GROUP_OPTION_RE = /ashby-application-form-input-[a-z-]*option\b/i;

  function findButtonGroupQuestionContainer(anyButton) {
    var entry = anyButton.closest ? anyButton.closest('[data-field-path], [class*="field-entry" i]') : null;
    if (entry) return entry;
    var node = anyButton.parentElement;
    for (var depth = 0; depth < 4 && node; depth++) {
      if (node.tagName === 'FORM' || node.tagName === 'BODY') break;
      node = node.parentElement;
    }
    return node || anyButton.parentElement;
  }

  function getButtonGroupLabel(group, container) {
    var lbl = getPrecedingText(group[0]);
    if (lbl) return stripRequiredMarker(lbl);
    if (container) {
      var direct = getLabel(container);
      if (direct) return stripRequiredMarker(direct);
    }
    return '';
  }

  /** Finds every Ashby-style option button GROUP (2+ buttons sharing one immediate parent) in
   * `root`. A lone button carrying the option class is left alone (not a choice between
   * anything). */
  function findButtonGroups(root) {
    var buttons = Array.prototype.slice.call(root.querySelectorAll('button')).filter(function (b) {
      var cls = (b.getAttribute && b.getAttribute('class')) || '';
      return BUTTON_GROUP_OPTION_RE.test(cls) && isVisible(b) && !b.disabled;
    });
    var parents = [];
    var byParent = [];
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var parent = btn.parentElement;
      var idx = parents.indexOf(parent);
      if (idx === -1) { parents.push(parent); byParent.push([btn]); }
      else byParent[idx].push(btn);
    }
    var out = [];
    for (var g = 0; g < byParent.length; g++) {
      var group = byParent[g];
      if (group.length < 2) continue;
      var container = findButtonGroupQuestionContainer(group[0]);
      out.push({ buttons: group, container: container, label: getButtonGroupLabel(group, container) });
    }
    return out;
  }

  /**
   * A NEW, independent click path (alongside isClickSafe()/safeClick() and
   * isAddAnotherButtonSafe()/safeClickAddButton() — a bug in one guard must never widen what
   * another allows). `el` must be a genuine `type=button` control (a bare no-type <button>
   * defaults to type=submit — the SAME HTML trap isAddAnotherButtonSafe already guards
   * against) INSIDE this question's own container, never nav/header/footer chrome, and its
   * text must not read as a submit/next/save-shaped action — a decoy submit button sitting
   * right next to the group must never be clicked.
   */
  /**
   * `optionTexts` (this group's own known option texts, e.g. ["Yes", "No"]) is required ONLY
   * to unlock the type=submit relaxation below; the type=button path never needs it, so
   * existing callers that only care about that path may omit it.
   */
  function isChoiceButtonSafe(el, container, optionTexts) {
    if (!el || el.nodeType !== 1 || el.tagName !== 'BUTTON') return false;
    if (!container || !container.contains(el)) return false;
    if (el.closest && el.closest('nav, header, footer')) return false;
    if (!isVisible(el)) return false;
    if (el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    var text = accessibleControlText(el);
    if (ADD_BUTTON_DENY_RE.test(text)) return false;

    var effectiveType = String(el.type || '').toLowerCase();
    if (effectiveType === 'button') return true;
    // Real Ashby markup (live capture, 2026-09-24): every option button, including its OWN
    // "Submit Application" button, carries NO type="" attribute at all -- which the DOM
    // reports as the default type "submit" -- but Ashby's whole application has no <form>
    // element whatsoever, so a click on such a button cannot submit anything natively (there
    // is nothing for it to submit). Relaxed ONLY for that specific, structurally safe shape:
    // no form owner, AND the button's own text is EXACTLY one of THIS group's known option
    // texts (re-checked here independently of whatever the caller already matched, so a bug
    // upstream still cannot turn this into "click any type-less button in the container") --
    // a decoy "Submit Application" button never has a matching option text, so it is refused
    // by this same check regardless of type or form ownership.
    if (effectiveType === 'submit' && !el.form) {
      if (!optionTexts || !optionTexts.length) return false;
      var norm = cleanText(text).toLowerCase();
      for (var i = 0; i < optionTexts.length; i++) {
        if (cleanText(optionTexts[i]).toLowerCase() === norm) return true;
      }
      return false;
    }
    return false;
  }

  /** Selected-state per the ground truth: aria-pressed/aria-checked, a data-state/class
   * change (Ashby's real classes are hashed CSS-modules names, e.g. "_option_1svni_32", so
   * this matches "selected"/"active" etc. as a bare substring rather than a whole word), or
   * (Ashby's own real markup) a hidden checkbox mirror inside the same container. */
  function isChoiceButtonSelected(button) {
    if (!button) return false;
    var ariaPressed = ((button.getAttribute && button.getAttribute('aria-pressed')) || '').toLowerCase();
    if (ariaPressed === 'true') return true;
    var ariaChecked = ((button.getAttribute && button.getAttribute('aria-checked')) || '').toLowerCase();
    if (ariaChecked === 'true') return true;
    var dataState = ((button.getAttribute && button.getAttribute('data-state')) || '').toLowerCase();
    if (dataState === 'checked' || dataState === 'selected' || dataState === 'active' || dataState === 'on') return true;
    var cls = String(button.className || '').toLowerCase();
    if (/active|selected|is-checked|is-selected/.test(cls)) return true;
    return false;
  }

  function verifyChoiceButtonSelected(button, container) {
    if (isChoiceButtonSelected(button)) return true;
    var hidden = container && container.querySelector ? container.querySelector('input[type="checkbox"]') : null;
    return !!(hidden && hidden.checked);
  }

  /** Matches `value` against the group's own button texts (matchChoiceOption — never the
   * first of several) and clicks the match through isChoiceButtonSafe. A decoy submit button
   * beside the group is never even considered a candidate: it is not one of `entry.buttons`,
   * and even if it were, its text would not equal a known option (see isChoiceButtonSafe). */
  function fillButtonGroup(entry, value, doc) {
    var target = String(value == null ? '' : value).trim();
    if (!target) return Promise.resolve({ ok: false, reason: 'empty value' });
    var texts = entry.buttons.map(accessibleControlText);
    var idx = matchChoiceOption(target, texts);
    if (idx === -1) {
      return Promise.resolve({ ok: false, reason: 'no confident match for "' + target + '" among button options' });
    }
    var button = entry.buttons[idx];
    if (!isChoiceButtonSafe(button, entry.container, texts)) {
      return Promise.resolve({ ok: false, reason: 'matched button failed the safety guard' });
    }
    button.click();
    return waitFor(function () { return verifyChoiceButtonSelected(button, entry.container) ? true : null; }, 800, doc)
      .then(function (verified) {
        if (!verified) return { ok: false, reason: 'button click did not register as selected' };
        return { ok: true, matchedText: texts[idx] };
      });
  }

  // ---- checkbox groups (several checkboxes = one question) ---------------------------------

  function checkboxGroupKey(el, doc) {
    var name = el.name || '';
    if (!name) return null;
    var form = el.closest ? el.closest('form') : null;
    var formIndex = 'noform';
    if (form && doc && doc.forms) {
      var idx = Array.prototype.indexOf.call(doc.forms, form);
      if (idx !== -1) formIndex = String(idx);
    }
    return 'checkbox:' + formIndex + ':' + name;
  }

  /**
   * Finds every checkbox GROUP in `root` (2+ checkboxes that are really ONE question), by two
   * independent structural signals -- never by label text similarity, which is exactly how a
   * checkbox group could get confused with an unrelated standalone checkbox sharing a word:
   *   1. a shared, non-empty `name` (Lever's pronouns / "cards[<uuid>][fieldN]"; Greenhouse's
   *      "question_<id>[]" language-fluency / EEO "check all that apply" checkboxes);
   *   2. failing that, a shared nearest-enclosing <fieldset> (Ashby's checkbox-group questions,
   *      whose individual options each carry their OWN unique name, e.g. name="LinkedIn").
   * A single stand-alone checkbox (no name-mate, no fieldset-mate) is left alone entirely --
   * scanFields() then scans it exactly as it does today.
   */
  function findCheckboxGroups(root, doc, excludeEls) {
    var exclusions = excludeEls || [];
    var checkboxes = Array.prototype.slice.call(root.querySelectorAll('input[type="checkbox"]')).filter(function (cb) {
      // Never re-group a checkbox Workday's OWN fieldset[data-automation-id$="-CheckboxGroup"]
      // detector (findWorkdayCheckboxGroups) already claimed -- this generic fieldset-based
      // tier below has no automation-id awareness of its own and would otherwise double-
      // register the SAME Workday disability/self-identify group under a second, competing
      // field id.
      if (exclusions.indexOf(cb) !== -1) return false;
      return isVisible(cb) && !cb.disabled;
    });
    var used = [];
    var out = [];

    var byName = {};
    var nameOrder = [];
    for (var i = 0; i < checkboxes.length; i++) {
      var cb = checkboxes[i];
      var key = checkboxGroupKey(cb, doc);
      if (!key) continue;
      if (!byName[key]) { byName[key] = []; nameOrder.push(key); }
      byName[key].push(cb);
    }
    for (var o = 0; o < nameOrder.length; o++) {
      var group = byName[nameOrder[o]];
      if (group.length < 2) continue;
      used = used.concat(group);
      out.push({ elements: group, label: getGroupLabel(group) });
    }

    var fieldsets = [];
    var byFieldset = [];
    for (var j = 0; j < checkboxes.length; j++) {
      var cbj = checkboxes[j];
      if (used.indexOf(cbj) !== -1) continue;
      var fs = cbj.closest ? cbj.closest('fieldset') : null;
      if (!fs) continue;
      var fidx = fieldsets.indexOf(fs);
      if (fidx === -1) { fieldsets.push(fs); byFieldset.push([cbj]); }
      else byFieldset[fidx].push(cbj);
    }
    for (var k = 0; k < byFieldset.length; k++) {
      var group2 = byFieldset[k];
      if (group2.length < 2) continue;
      used = used.concat(group2);
      out.push({ elements: group2, label: getGroupLabel(group2) });
    }

    return { groups: out, used: used };
  }

  /** Ticks the ONE option matching `target` (matchChoiceOption — decline-family, exact, or
   * unambiguous containment; never a guess). Never unticks a box the user already ticked. */
  function setCheckboxGroupOne(elements, target) {
    var labels = elements.map(function (e) { return getLabel(e); });
    var idx = matchChoiceOption(target, labels);
    if (idx === -1) return false;
    var el = elements[idx];
    if (el.checked) return true;
    return safeClick(el); // the SAME click guard already used for every other checkbox
  }

  /** `value` is a single term, or an array (several boxes to tick). One failed/unmatched term
   * never blocks the rest; overall success is "at least one applied", same as Workday's prompt
   * multi-value rule. */
  function applyCheckboxGroupValue(entry, value) {
    var elements = entry.elements;
    if (Array.isArray(value)) {
      var anyOk = false;
      var failedTerms = [];
      for (var i = 0; i < value.length; i++) {
        if (setCheckboxGroupOne(elements, value[i])) anyOk = true; else failedTerms.push(value[i]);
      }
      entry._lastReason = failedTerms.length ? ('no confident match for: ' + failedTerms.join(', ')) : '';
      return anyOk;
    }
    var single = String(value == null ? '' : value).trim();
    if (!single) { entry._lastReason = 'empty value'; return false; }
    var ok = setCheckboxGroupOne(elements, single);
    entry._lastReason = ok ? '' : ('no confident match for "' + single + '" among checkbox options');
    return ok;
  }

  // ---- Lever location type-ahead --------------------------------------------------------

  // Ground truth (docs/research/live-captures-2026-09-24/structure-08/09-lever-f0.json):
  //   <input type="text" id="location-input" name="location" class="location-input">
  //   <input type="hidden" id="selected-location" name="selectedLocation">
  // both wrapped in ONE <label> that ALSO contains the results dropdown / "No location found"
  // status text -- a naive getLabel() on the input picks up all of that concatenated together.
  var LEVER_STOP_TAGS = { UL: 1, OL: 1, DIV: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, SCRIPT: 1, STYLE: 1 };

  /** The wrapping <label>'s own leading text/inline content ONLY, stopping at the first
   * block-level/list/results container -- never the dropdown suggestions or status text Lever
   * renders inside that same <label>. */
  function getLeverLocationLabel(input) {
    var wrap = input.closest ? input.closest('label') : null;
    if (!wrap) return getLabel(input);
    var parts = [];
    for (var node = wrap.firstChild; node; node = node.nextSibling) {
      if (node.nodeType === 3) {
        var t = cleanText(node.textContent);
        if (t) parts.push(t);
        continue;
      }
      if (node.nodeType === 1) {
        if (LEVER_STOP_TAGS[node.tagName]) break;
        var t2 = cleanText(node.textContent);
        if (t2) parts.push(t2);
      }
    }
    var joined = stripRequiredMarker(parts.join(' '));
    return joined || getLabel(input);
  }

  function findLeverLocationHidden(input) {
    var node = input;
    for (var depth = 0; depth < 5 && node; depth++) {
      var parent = node.parentElement;
      if (!parent) break;
      var hidden = parent.querySelector ? parent.querySelector('input[type="hidden"][name="selectedLocation"]') : null;
      if (hidden) return hidden;
      node = parent;
    }
    return null;
  }

  /** Finds Lever's "Current location" type-ahead: the visible text input paired with its own
   * `input[name=selectedLocation]` commit target -- that pairing is the real signature of this
   * specific widget (a plain "location" text field elsewhere has no such hidden partner). */
  function findLeverLocationFields(root) {
    var out = [];
    var inputs = Array.prototype.slice.call(root.querySelectorAll('input.location-input'));
    for (var i = 0; i < inputs.length; i++) {
      var input = inputs[i];
      if (!isVisible(input) || input.disabled) continue;
      var hidden = findLeverLocationHidden(input);
      if (!hidden) continue;
      out.push({ input: input, hidden: hidden, label: getLeverLocationLabel(input) });
    }
    return out;
  }

  function splitCityState(value) {
    var str = String(value == null ? '' : value).trim();
    var commaIdx = str.indexOf(',');
    if (commaIdx === -1) return { city: str, state: '' };
    return { city: str.slice(0, commaIdx).trim(), state: str.slice(commaIdx + 1).trim() };
  }

  function leverStateVariants(state) {
    var s = cleanText(state).toLowerCase();
    if (!s) return [];
    var out = [s];
    var code = US_STATE_NAME_TO_CODE[s];
    var name = US_STATE_CODE_TO_NAME[s];
    if (code) out.push(code);
    if (name) out.push(name);
    return out;
  }

  function isLeverLocationRowSafe(el, resultsContainer) {
    if (!el || el.nodeType !== 1) return false;
    if (!resultsContainer || !resultsContainer.contains(el)) return false;
    var cls = (el.getAttribute && el.getAttribute('class')) || '';
    if (!/dropdown-location/i.test(cls)) return false;
    if (!isVisible(el)) return false;
    if (ADD_BUTTON_DENY_RE.test(optionAccessibleText(el))) return false;
    return true;
  }

  /**
   * Types the city, waits for `.dropdown-location` suggestion rows, and picks the ONE row
   * whose text contains BOTH the city AND the state (full name or 2-letter code, via the
   * existing US state table) -- never a blind first/only-city match. Commits with `mousedown`
   * (ground truth: Lever's widget clears the input on blur unless a row was chosen with
   * mousedown) and verifies via the paired `input[name=selectedLocation]` becoming non-empty,
   * never the search input's own typed text.
   */
  function fillLeverLocation(entry, value, doc) {
    if (!isVisible(entry.input) || entry.input.disabled) {
      return Promise.resolve({ ok: false, reason: 'safety guard refused the location input' });
    }
    var parts = splitCityState(value);
    if (!parts.city) return Promise.resolve({ ok: false, reason: 'empty value' });

    setNativeValue(entry.input, parts.city);
    dispatchKeyboardEvent(entry.input, 'keydown', parts.city.slice(-1) || 'a');

    return waitFor(function () {
      var rows = Array.prototype.slice.call(doc.querySelectorAll('[class*="dropdown-location" i]')).filter(isVisible);
      return rows.length ? rows : null;
    }, 3500, doc).then(function (rows) {
      if (!rows) return { ok: false, reason: 'no location suggestions appeared for "' + parts.city + '"' };

      var cityRe = new RegExp('\\b' + escapeRegExp(parts.city.toLowerCase()) + '\\b', 'i');
      var variants = leverStateVariants(parts.state);
      var matches = [];
      for (var i = 0; i < rows.length; i++) {
        var text = cleanText(rows[i].textContent).toLowerCase();
        if (!cityRe.test(text)) continue;
        if (variants.length) {
          var stateOk = false;
          for (var v = 0; v < variants.length; v++) {
            if (new RegExp('\\b' + escapeRegExp(variants[v]) + '\\b', 'i').test(text)) { stateOk = true; break; }
          }
          if (!stateOk) continue;
        }
        matches.push(rows[i]);
      }
      if (matches.length !== 1) {
        return {
          ok: false,
          reason: matches.length === 0
            ? ('no suggestion matched city and state for "' + value + '"')
            : ('ambiguous: ' + matches.length + ' suggestions matched "' + value + '", never guessing')
        };
      }

      var row = matches[0];
      var resultsContainer = rows[0].parentElement || doc;
      if (!isLeverLocationRowSafe(row, resultsContainer)) {
        return { ok: false, reason: 'matched suggestion failed the safety guard' };
      }
      dispatchMouseEvent(row, 'mousedown');

      return waitFor(function () {
        return cleanText(entry.hidden.value || '') ? true : null;
      }, 1000, doc).then(function (committed) {
        if (!committed) return { ok: false, reason: 'selectedLocation was never committed after choosing a suggestion' };
        return { ok: true, matchedText: cleanText(row.textContent) };
      });
    });
  }

  // ---------------------------------------------------------------------
  // scanning
  // ---------------------------------------------------------------------

  function isEligible(el) {
    var tag = el.tagName;
    if (tag === 'INPUT') {
      var type = (el.type || 'text').toLowerCase();
      return EXCLUDED_INPUT_TYPES.indexOf(type) === -1;
    }
    return tag === 'SELECT' || tag === 'TEXTAREA';
  }

  function getSelectOptions(el) {
    var out = [];
    for (var i = 0; i < el.options.length; i++) {
      var t = cleanText(el.options[i].textContent);
      if (t) out.push(t);
    }
    return out;
  }

  function radioGroupKey(el, doc) {
    var name = el.name || '';
    if (!name) return null;
    var form = el.closest ? el.closest('form') : null;
    var formIndex = 'noform';
    if (form && doc && doc.forms) {
      var idx = Array.prototype.indexOf.call(doc.forms, form);
      if (idx !== -1) formIndex = String(idx);
    }
    return 'radio:' + formIndex + ':' + name;
  }

  /**
   * Scans `root` (a Document, or any element for a narrower scan — e.g. a same-origin
   * iframe's document) for fillable fields.
   *
   * Returns { fields, registry } where `fields` is a plain-JSON-serializable array of
   * FieldDescriptors (matching the /resolve request contract) and `registry` maps each
   * field id to the live element(s) it refers to, for later filling. `registry` is NOT
   * serializable and must stay in the same JS context that did the scan.
   */
  function scanFields(root, opts) {
    root = root || (typeof document !== 'undefined' ? document : null);
    if (!root) return { fields: [], registry: {} };
    opts = opts || {};
    var startId = opts.startId || 0;
    var doc = root.nodeType === 9 ? root : ownerDoc(root); // Document or Element

    var candidates = Array.prototype.slice.call(root.querySelectorAll('input, select, textarea')).filter(isEligible);
    var fields = [];
    var registry = {};
    var seenRadioGroups = {};
    var counter = startId;

    // Split month/year date pairs (see the section above) are detected FIRST and their two
    // elements pulled out of the ordinary per-element loop below, so each half is scanned
    // exactly once — merged into a single field — never twice as two unrelated fields.
    var datePairs = findDatePartPairs(root);
    var consumedByDatePair = [];
    for (var dpc = 0; dpc < datePairs.length; dpc++) {
      consumedByDatePair.push(datePairs[dpc].monthEl, datePairs[dpc].yearEl);
    }

    // Workday widgets (see the section above) are ALSO detected first and their elements
    // pulled out of the ordinary loop: a dateInputWrapper's Month/Year spinners and a
    // multiSelectContainer's inner <input> would otherwise be scanned (and filled with the
    // wrong technique) as ordinary text inputs; a dropdown's <button> was never in `candidates`
    // to begin with (only input/select/textarea are), so it is scanned separately below.
    var wdDateWrappers = findWorkdayDateWrappers(root);
    var wdPrompts = findWorkdayPrompts(root);
    var wdCheckboxGroups = findWorkdayCheckboxGroups(root);
    var wdDropdowns = (function () {
      var buttons = Array.prototype.slice.call(root.querySelectorAll('button[aria-haspopup="listbox"]'));
      var out = [];
      for (var b = 0; b < buttons.length; b++) {
        var btn = buttons[b];
        if (!isVisible(btn)) continue;
        var container = findWorkdayFormField(btn);
        if (!container) continue; // only a listbox opener inside a formField-* wrapper counts
        out.push({ button: btn, container: container, label: getWorkdayFieldLabel(container) });
      }
      return out;
    })();
    var consumedByWorkday = [];
    for (var wd = 0; wd < wdDateWrappers.length; wd++) {
      var wdw = wdDateWrappers[wd];
      if (wdw.monthEl) consumedByWorkday.push(wdw.monthEl);
      if (wdw.dayEl) consumedByWorkday.push(wdw.dayEl);
      if (wdw.yearEl) consumedByWorkday.push(wdw.yearEl);
      if (wdw.maskedEl) consumedByWorkday.push(wdw.maskedEl);
    }
    for (var wp = 0; wp < wdPrompts.length; wp++) consumedByWorkday.push(wdPrompts[wp].input);
    for (var wcg = 0; wcg < wdCheckboxGroups.length; wcg++) {
      var wcgBoxes = wdCheckboxGroups[wcg].boxes;
      for (var wcgb = 0; wcgb < wcgBoxes.length; wcgb++) consumedByWorkday.push(wcgBoxes[wcgb]);
    }
    // A hard-deny element (agreementCheckbox/terms consent, chiefly) must never be offered as a
    // fillable field AT ALL, not merely refused at fill time -- the generic checkbox fill path
    // (setCheckboxValue) has no automation-id awareness of its own to fall back on, unlike every
    // Workday-specific widget above, each of which re-checks this at the point of action. Scoped
    // to inputs inside a formField-* container, so this can never reach outside Workday's own
    // markup.
    var wdHardDenyInputs = Array.prototype.slice.call(root.querySelectorAll(WD_FORM_FIELD_SELECTOR + ' input'))
      .filter(hasWorkdayHardDenyAutomationId);
    for (var hd = 0; hd < wdHardDenyInputs.length; hd++) consumedByWorkday.push(wdHardDenyInputs[hd]);

    // Choice widgets (see the section above) — ALSO detected first and their elements pulled
    // out of the ordinary loop, same discipline as the date-pair/Workday detection above: a
    // react-select/generic-ARIA combobox's inner <input role=combobox>, a checkbox that
    // belongs to a checkbox GROUP, and Lever's location type-ahead input would otherwise be
    // scanned (and filled with the wrong, plain-text technique) individually.
    var checkboxGroupScan = findCheckboxGroups(root, doc, consumedByWorkday);
    var checkboxGroups = checkboxGroupScan.groups;
    var consumedByCheckboxGroup = checkboxGroupScan.used;

    var leverLocationFields = findLeverLocationFields(root);
    var consumedByLeverLocation = leverLocationFields.map(function (lf) { return lf.input; });

    var comboboxExclusions = consumedByWorkday.concat(consumedByLeverLocation);
    var comboboxWidgets = (function () {
      var inputEls = Array.prototype.slice.call(root.querySelectorAll('input[role="combobox"]'));
      var out = [];
      for (var ci = 0; ci < inputEls.length; ci++) {
        var cinput = inputEls[ci];
        if (comboboxExclusions.indexOf(cinput) !== -1) continue;
        if (cinput.closest && (cinput.closest('[data-automation-id="multiSelectContainer"]') ||
            cinput.closest('[data-automation-id="dateInputWrapper"]'))) continue;
        if (cinput.disabled) continue;
        if (!isVisible(cinput)) continue;
        out.push({ input: cinput, label: getLabel(cinput) });
      }
      return out;
    })();
    var consumedByCombobox = comboboxWidgets.map(function (cw) { return cw.input; });

    // Ashby-style option button GROUPS are <button>s, never in `candidates` (only
    // input/select/textarea are queried above) — same as Workday's dropdown buttons, they need
    // no exclusion from the ordinary per-element loop below.
    var buttonGroups = findButtonGroups(root);

    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (el.disabled) continue;
      if (consumedByDatePair.indexOf(el) !== -1) continue;
      if (consumedByWorkday.indexOf(el) !== -1) continue;
      if (consumedByCheckboxGroup.indexOf(el) !== -1) continue;
      if (consumedByCombobox.indexOf(el) !== -1) continue;
      if (consumedByLeverLocation.indexOf(el) !== -1) continue;

      var tagLower = el.tagName.toLowerCase();

      // Normally an invisible field is dropped outright. The one exception:
      // a hidden native <select> that a custom widget (select2/Chosen/...)
      // has taken over visually — still scan it, but only when we can find
      // that visible replacement (see findPairedWidget's file-level comment).
      var pairedWidget = null;
      if (!isVisible(el)) {
        if (tagLower === 'select') pairedWidget = findPairedWidget(el);
        if (!pairedWidget) continue;
      }

      if (tagLower === 'input' && (el.type || 'text').toLowerCase() === 'radio') {
        var key = radioGroupKey(el, doc) || ('radio:noname:' + counter);
        if (seenRadioGroups[key]) continue;
        seenRadioGroups[key] = true;

        var group;
        if (el.name) {
          try {
            group = Array.prototype.slice.call(
              root.querySelectorAll('input[type="radio"][name="' + cssEscape(el.name) + '"]')
            ).filter(isVisible);
          } catch (e) {
            group = [el];
          }
        } else {
          group = [el];
        }
        if (!group.length) group = [el];

        var id = 'f' + (counter++);
        var options = [];
        for (var g = 0; g < group.length; g++) {
          var lbl = cleanText(getLabel(group[g]));
          if (lbl) options.push(lbl);
        }
        var groupSelector = el.name
          ? 'input[type="radio"][name="' + cssEscape(el.name) + '"]'
          : buildSelector(el);

        var radioSection = getSectionContext(el);
        registry[id] = { kind: 'radio-group', elements: group };
        fields.push({
          id: id,
          selector: groupSelector,
          tag: 'input',
          type: 'radio',
          name: el.name || '',
          autocomplete: el.getAttribute('autocomplete') || '',
          label: getGroupLabel(group),
          placeholder: '',
          required: group.some(function (r) { return r.required; }),
          options: options,
          section: radioSection.section,
          section_index: radioSection.section_index,
          widget: ''
        });
        continue;
      }

      var type = tagLower === 'input'
        ? (el.type || 'text').toLowerCase()
        : (tagLower === 'textarea' ? 'textarea' : (el.multiple ? 'select-multiple' : 'select-one'));

      var fid = 'f' + (counter++);
      var selector = buildSelector(el);
      var fieldSection = getSectionContext(el);
      registry[fid] = pairedWidget ? { kind: 'element', el: el, highlightEl: pairedWidget } : { kind: 'element', el: el };
      fields.push({
        id: fid,
        selector: selector,
        tag: tagLower,
        type: type,
        name: el.name || '',
        autocomplete: el.getAttribute('autocomplete') || '',
        label: pairedWidget ? getFieldLabel(el, pairedWidget) : getLabel(el),
        placeholder: el.placeholder || '',
        required: !!el.required,
        options: tagLower === 'select' ? getSelectOptions(el) : [],
        section: fieldSection.section,
        section_index: fieldSection.section_index,
        widget: ''
      });
    }

    // Emit each detected month/year pair as ONE merged field — "type" is deliberately
    // NOT "date" (that would tell the service's structured tier to reformat the value as
    // ISO YYYY-MM-DD for a native date picker; this is a plain MM/YYYY value that gets
    // split back out on fill, see setDatePartsValue()).
    for (var dp = 0; dp < datePairs.length; dp++) {
      var pair = datePairs[dp];
      if (pair.monthEl.disabled || pair.yearEl.disabled) continue;
      var did = 'f' + (counter++);
      var dateSection = getSectionContext(pair.monthEl);
      registry[did] = { kind: 'date-parts', monthEl: pair.monthEl, yearEl: pair.yearEl };
      fields.push({
        id: did,
        selector: buildSelector(pair.monthEl),
        tag: pair.monthEl.tagName.toLowerCase(),
        type: 'text',
        name: pair.monthEl.name || '',
        autocomplete: '',
        label: pair.label || '',
        placeholder: 'MM/YYYY',
        required: !!(pair.monthEl.required || pair.yearEl.required),
        options: [],
        section: dateSection.section,
        section_index: dateSection.section_index,
        widget: ''
      });
    }

    // Workday spinner dates -- one field per dateInputWrapper: "wd-date-my" (month+year, or the
    // single masked-input fallback), "wd-date-y" (year-only, education "From"/"To"), or
    // "wd-date-mdy" (month+day+year -- Self-Identify "Date" and similar). The service may send
    // "MM/YYYY" even for a year-only field; the resolver is told to use only the year part (see
    // FieldDescriptor.widget on the /resolve contract).
    for (var wd2 = 0; wd2 < wdDateWrappers.length; wd2++) {
      var wdw2 = wdDateWrappers[wd2];
      var wdAnchorEl = wdw2.maskedEl || wdw2.monthEl || wdw2.dayEl || wdw2.yearEl;
      if (wdAnchorEl.disabled || (wdw2.yearEl && wdw2.yearEl.disabled)) continue;
      var wdKind = wdw2.shape === 'y' ? 'wd-date-y' : (wdw2.shape === 'mdy' ? 'wd-date-mdy' : 'wd-date-my');
      var wdId = 'f' + (counter++);
      var wdSection = getSectionContext(wdw2.wrapper);
      registry[wdId] = wdKind === 'wd-date-y'
        ? { kind: 'wd-date-y', yearEl: wdw2.yearEl, wrapper: wdw2.wrapper }
        : wdKind === 'wd-date-mdy'
          ? { kind: 'wd-date-mdy', monthEl: wdw2.monthEl, dayEl: wdw2.dayEl, yearEl: wdw2.yearEl, wrapper: wdw2.wrapper }
          : { kind: 'wd-date-my', monthEl: wdw2.monthEl, yearEl: wdw2.yearEl, maskedEl: wdw2.maskedEl, wrapper: wdw2.wrapper };
      var wdPlaceholder = wdKind === 'wd-date-y' ? 'YYYY' : (wdKind === 'wd-date-mdy' ? 'MM/DD/YYYY' : 'MM/YYYY');
      fields.push({
        id: wdId,
        selector: buildSelector(wdAnchorEl),
        tag: 'input',
        type: 'text',
        name: wdAnchorEl.name || '',
        autocomplete: '',
        label: wdw2.label || '',
        placeholder: wdPlaceholder,
        required: !!wdAnchorEl.required,
        options: [],
        section: wdSection.section,
        section_index: wdSection.section_index,
        widget: wdKind
      });
    }

    // Workday dropdown -- button[aria-haspopup=listbox] inside a formField-* container
    // (Degree, Country, State, ...). The options live in a portalled listbox found only at
    // fill time (see resolveWorkdayListbox), never eagerly during scanning.
    for (var wdd = 0; wdd < wdDropdowns.length; wdd++) {
      var dd = wdDropdowns[wdd];
      if (dd.button.disabled) continue;
      var ddId = 'f' + (counter++);
      var ddSection = getSectionContext(dd.container);
      registry[ddId] = { kind: 'wd-dropdown', button: dd.button, container: dd.container };
      fields.push({
        id: ddId,
        selector: buildSelector(dd.button),
        tag: 'button',
        type: 'select-one',
        name: dd.button.name || '',
        autocomplete: '',
        label: dd.label || '',
        placeholder: '',
        required: false,
        options: [],
        section: ddSection.section,
        section_index: ddSection.section_index,
        widget: 'wd-dropdown'
      });
    }

    // Workday prompt / multi-select -- Field of Study, School, Certification, Skills, ...
    for (var wpp = 0; wpp < wdPrompts.length; wpp++) {
      var pr = wdPrompts[wpp];
      if (pr.input.disabled) continue;
      var prId = 'f' + (counter++);
      var prSection = getSectionContext(pr.formField);
      registry[prId] = { kind: 'wd-prompt', input: pr.input, container: pr.container, formField: pr.formField };
      fields.push({
        id: prId,
        selector: buildSelector(pr.input),
        tag: 'input',
        type: 'text',
        name: pr.input.name || '',
        autocomplete: '',
        label: pr.label || '',
        placeholder: pr.input.placeholder || '',
        required: !!pr.input.required,
        options: [],
        section: prSection.section,
        section_index: prSection.section_index,
        widget: 'wd-prompt'
      });
    }

    // Checkbox groups -- several checkboxes under one question become ONE field (Greenhouse
    // "check all that apply" / language fluency, Ashby location / "how did you hear", Lever
    // pronouns / cards[..][fieldN]).
    for (var cg = 0; cg < checkboxGroups.length; cg++) {
      var cgGroup = checkboxGroups[cg];
      var cgId = 'f' + (counter++);
      var cgOptions = [];
      for (var cgi = 0; cgi < cgGroup.elements.length; cgi++) {
        var cgLbl = cleanText(getLabel(cgGroup.elements[cgi]));
        if (cgLbl) cgOptions.push(cgLbl);
      }
      var cgSection = getSectionContext(cgGroup.elements[0]);
      registry[cgId] = { kind: 'checkbox-group', elements: cgGroup.elements };
      fields.push({
        id: cgId,
        selector: buildSelector(cgGroup.elements[0]),
        tag: 'input',
        type: 'checkbox-group',
        name: cgGroup.elements[0].name || '',
        autocomplete: '',
        label: cgGroup.label,
        placeholder: '',
        required: cgGroup.elements.some(function (e) { return e.required; }),
        options: cgOptions,
        section: cgSection.section,
        section_index: cgSection.section_index,
        widget: ''
      });
    }

    // Ashby-style Yes/No (or other short) option button groups.
    for (var bg = 0; bg < buttonGroups.length; bg++) {
      var bgGroup = buttonGroups[bg];
      var bgId = 'f' + (counter++);
      var bgSection = getSectionContext(bgGroup.container || bgGroup.buttons[0]);
      registry[bgId] = { kind: 'button-group', buttons: bgGroup.buttons, container: bgGroup.container };
      fields.push({
        id: bgId,
        selector: buildSelector(bgGroup.buttons[0]),
        tag: 'button',
        type: 'text',
        name: '',
        autocomplete: '',
        label: bgGroup.label,
        placeholder: '',
        required: false,
        options: bgGroup.buttons.map(accessibleControlText),
        section: bgSection.section,
        section_index: bgSection.section_index,
        widget: 'button-group'
      });
    }

    // Combobox widgets (react-select / generic ARIA) -- options unknown at scan time, the
    // menu only renders once opened.
    for (var cb2 = 0; cb2 < comboboxWidgets.length; cb2++) {
      var cbw = comboboxWidgets[cb2];
      var cbId = 'f' + (counter++);
      var cbSection = getSectionContext(cbw.input);
      registry[cbId] = { kind: 'combobox', input: cbw.input };
      fields.push({
        id: cbId,
        selector: buildSelector(cbw.input),
        tag: 'input',
        type: 'text',
        name: cbw.input.name || '',
        autocomplete: '',
        label: cbw.label || '',
        placeholder: cbw.input.placeholder || '',
        required: !!cbw.input.required,
        options: [],
        section: cbSection.section,
        section_index: cbSection.section_index,
        widget: 'combobox'
      });
    }

    // Lever location type-ahead -- verified via the paired hidden input[name=selectedLocation]
    // the widget itself commits to, never the search input's own typed text.
    for (var ll = 0; ll < leverLocationFields.length; ll++) {
      var llf = leverLocationFields[ll];
      var llId = 'f' + (counter++);
      var llSection = getSectionContext(llf.input);
      registry[llId] = { kind: 'lever-location', input: llf.input, hidden: llf.hidden };
      fields.push({
        id: llId,
        selector: buildSelector(llf.input),
        tag: 'input',
        type: 'text',
        name: llf.input.name || '',
        autocomplete: '',
        label: llf.label || '',
        placeholder: llf.input.placeholder || '',
        required: !!llf.input.required,
        options: [],
        section: llSection.section,
        section_index: llSection.section_index,
        widget: 'combobox'
      });
    }

    // Workday Self-Identify checkbox group -- disabilityStatus-CheckboxGroup and similar. Wire
    // type "checkbox-group" matches the resolver's existing single-choice handling for it (see
    // src/applypilot/extension/matcher.py) -- the SAME contract a radio group uses, just backed
    // by independent <input type=checkbox> elements instead of a native radio group. Uses its
    // OWN registry kind ("wd-checkbox-group", not "checkbox-group") so the two independently-
    // built detectors below can never double-register the same Workday fieldset.
    for (var wcgi = 0; wcgi < wdCheckboxGroups.length; wcgi++) {
      var cg = wdCheckboxGroups[wcgi];
      var wcgId = 'f' + (counter++);
      var wcgSection = getSectionContext(cg.fieldset);
      var wcgOptions = cg.boxes.map(getWorkdayCheckboxGroupOptionLabel);
      registry[wcgId] = { kind: 'wd-checkbox-group', boxes: cg.boxes, fieldset: cg.fieldset, container: cg.container, label: cg.label };
      fields.push({
        id: wcgId,
        selector: buildSelector(cg.fieldset),
        tag: 'input',
        type: 'checkbox-group',
        name: cg.boxes[0].name || '',
        autocomplete: '',
        label: cg.label || '',
        placeholder: '',
        required: true,
        options: wcgOptions,
        section: wcgSection.section,
        section_index: wcgSection.section_index,
        widget: 'wd-checkbox-group'
      });
    }

    return { fields: fields, registry: registry, nextId: counter };
  }

  /**
   * Scans the main document plus any same-origin iframes it can reach directly.
   * Cross-origin iframes cannot be introspected by a normal content script and are
   * reported back separately so the caller can tell the user fields were skipped there.
   */
  function scanAll(doc) {
    doc = doc || (typeof document !== 'undefined' ? document : null);
    var result = scanFields(doc);
    var fields = result.fields.slice();
    var registry = result.registry;
    var nextId = result.nextId;
    var skippedFrames = 0;

    var iframes = doc ? Array.prototype.slice.call(doc.querySelectorAll('iframe')) : [];
    for (var i = 0; i < iframes.length; i++) {
      var frame = iframes[i];
      var frameDoc = null;
      try {
        frameDoc = frame.contentDocument;
      } catch (e) {
        frameDoc = null;
      }
      if (!frameDoc || !frameDoc.body) {
        skippedFrames++;
        continue;
      }
      var sub = scanFields(frameDoc, { startId: nextId });
      nextId = sub.nextId;
      for (var k = 0; k < sub.fields.length; k++) {
        sub.fields[k].frame = 'same-origin-iframe';
      }
      fields = fields.concat(sub.fields);
      for (var key in sub.registry) {
        if (Object.prototype.hasOwnProperty.call(sub.registry, key)) registry[key] = sub.registry[key];
      }
    }

    return { fields: fields, registry: registry, skippedFrames: skippedFrames };
  }

  // ---------------------------------------------------------------------
  // filling — must notify React/Angular-style controlled inputs, not just set .value
  // ---------------------------------------------------------------------

  function nativeSetterFor(el, propName) {
    var view = realmOf(el);
    var proto = null;
    if (el.tagName === 'TEXTAREA' && view && view.HTMLTextAreaElement) proto = view.HTMLTextAreaElement.prototype;
    else if (el.tagName === 'SELECT' && view && view.HTMLSelectElement) proto = view.HTMLSelectElement.prototype;
    else if (view && view.HTMLInputElement) proto = view.HTMLInputElement.prototype;
    if (!proto) return null;
    var desc = Object.getOwnPropertyDescriptor(proto, propName);
    return desc && desc.set ? desc.set : null;
  }

  function fireEvents(el, types) {
    var view = realmOf(el);
    var EventCtor = (view && view.Event) || (typeof Event !== 'undefined' ? Event : null);
    if (!EventCtor) return;
    for (var i = 0; i < types.length; i++) {
      el.dispatchEvent(new EventCtor(types[i], { bubbles: true }));
    }
  }

  function setNativeValue(el, value) {
    var setter = nativeSetterFor(el, 'value');
    if (setter) setter.call(el, value);
    else el.value = value;
    fireEvents(el, ['input', 'change']);
  }

  // US state name <-> 2-letter code table. The profile stores a full name
  // (e.g. "Arizona"); a page's <select> may list the same state as
  // "Arizona", "AZ", or "US-AZ" — findOptionMatch() below tries all three.
  var US_STATE_TABLE = [
    ['Alabama', 'AL'], ['Alaska', 'AK'], ['Arizona', 'AZ'], ['Arkansas', 'AR'],
    ['California', 'CA'], ['Colorado', 'CO'], ['Connecticut', 'CT'], ['Delaware', 'DE'],
    ['Florida', 'FL'], ['Georgia', 'GA'], ['Hawaii', 'HI'], ['Idaho', 'ID'],
    ['Illinois', 'IL'], ['Indiana', 'IN'], ['Iowa', 'IA'], ['Kansas', 'KS'],
    ['Kentucky', 'KY'], ['Louisiana', 'LA'], ['Maine', 'ME'], ['Maryland', 'MD'],
    ['Massachusetts', 'MA'], ['Michigan', 'MI'], ['Minnesota', 'MN'], ['Mississippi', 'MS'],
    ['Missouri', 'MO'], ['Montana', 'MT'], ['Nebraska', 'NE'], ['Nevada', 'NV'],
    ['New Hampshire', 'NH'], ['New Jersey', 'NJ'], ['New Mexico', 'NM'], ['New York', 'NY'],
    ['North Carolina', 'NC'], ['North Dakota', 'ND'], ['Ohio', 'OH'], ['Oklahoma', 'OK'],
    ['Oregon', 'OR'], ['Pennsylvania', 'PA'], ['Rhode Island', 'RI'], ['South Carolina', 'SC'],
    ['South Dakota', 'SD'], ['Tennessee', 'TN'], ['Texas', 'TX'], ['Utah', 'UT'],
    ['Vermont', 'VT'], ['Virginia', 'VA'], ['Washington', 'WA'], ['West Virginia', 'WV'],
    ['Wisconsin', 'WI'], ['Wyoming', 'WY'],
    ['District of Columbia', 'DC'], ['Puerto Rico', 'PR'], ['American Samoa', 'AS'],
    ['Guam', 'GU'], ['Northern Mariana Islands', 'MP'], ['U.S. Virgin Islands', 'VI']
  ];
  var US_STATE_NAME_TO_CODE = {};
  var US_STATE_CODE_TO_NAME = {};
  (function () {
    for (var s = 0; s < US_STATE_TABLE.length; s++) {
      var name = US_STATE_TABLE[s][0].toLowerCase();
      var code = US_STATE_TABLE[s][1].toLowerCase();
      US_STATE_NAME_TO_CODE[name] = code;
      US_STATE_CODE_TO_NAME[code] = name;
    }
  })();

  /**
   * Finds the index of the <select> option matching `text`, trying (in order): exact text/value
   * match, US state name<->code cross-match, then matchChoiceOption's answer families /
   * word-boundary containment (shared with setRadioValue and matchWorkdayDropdownOption -- see
   * that function's doc comment). Returns -1 when nothing matches. Shared by setSelectValue()
   * and the read-back check inside it.
   */
  function findOptionMatch(el, text) {
    var target = String(text == null ? '' : text).trim().toLowerCase();
    var i;

    // 1. exact text or value match (also covers matching a blank placeholder
    //    option like "-- Select --" when target is '').
    for (i = 0; i < el.options.length; i++) {
      var optText = cleanText(el.options[i].textContent).toLowerCase();
      var optValue = String(el.options[i].value).toLowerCase();
      if (optText === target || optValue === target) return i;
    }
    if (!target) return -1;

    // 2. US state name <-> 2-letter code cross-match.
    var code = US_STATE_NAME_TO_CODE[target];
    var name = US_STATE_CODE_TO_NAME[target];
    if (code || name) {
      for (i = 0; i < el.options.length; i++) {
        var ot = cleanText(el.options[i].textContent).toLowerCase();
        var ov = String(el.options[i].value).toLowerCase();
        var otNoPrefix = ot.replace(/^us[\s-]?/, '');
        var ovNoPrefix = ov.replace(/^us[\s-]?/, '');
        if (code && (ot === code || ov === code || otNoPrefix === code || ovNoPrefix === code)) return i;
        if (name && (ot === name || ov === name)) return i;
      }
    }

    // 3. shared matcher: answer families, then word-boundary containment (never raw substring).
    var optionTexts = [];
    for (i = 0; i < el.options.length; i++) optionTexts.push(el.options[i].textContent);
    return matchChoiceOption(text, optionTexts);
  }

  function setSelectValue(el, text) {
    var matchIndex = findOptionMatch(el, text);
    if (matchIndex === -1) return false;
    var setter = nativeSetterFor(el, 'selectedIndex');
    if (setter) setter.call(el, matchIndex);
    else el.selectedIndex = matchIndex;
    fireEvents(el, ['input', 'change']);
    // Read back rather than trust the write: a custom-select widget's own
    // change handler could in principle revert or ignore this — report
    // failure honestly instead of claiming a fill the DOM disagrees with.
    return el.selectedIndex === matchIndex;
  }

  function clearRadioGroup(elements) {
    for (var i = 0; i < elements.length; i++) {
      var r = elements[i];
      if (r.checked) {
        var setter = nativeSetterFor(r, 'checked');
        if (setter) setter.call(r, false);
        else r.checked = false;
        fireEvents(r, ['input', 'change']);
      }
    }
  }

  // The ONE invariant this extension must never violate is that it does not submit
  // the form. Filling a radio or checkbox legitimately needs a native .click(), so
  // the click target is re-checked here at the point of action rather than trusting
  // that scanning filtered correctly upstream. A bug in the scanner, a spoofed
  // descriptor, or a future refactor then still cannot turn a fill into a submit.
  function isClickSafe(el) {
    if (!el || el.tagName !== 'INPUT') return false;
    var t = String(el.type || '').toLowerCase();
    return t === 'radio' || t === 'checkbox';
  }

  function safeClick(el) {
    if (!isClickSafe(el)) return false;
    el.click();
    return true;
  }

  // ---------------------------------------------------------------------
  // repeating-section expansion — "Add Another" (Workday "My Experience" step)
  // ---------------------------------------------------------------------
  //
  // A SECOND, INDEPENDENT click path alongside isClickSafe()/safeClick() above.
  // Deliberately not merged with it and deliberately not loosening it — a bug
  // in one guard must never widen what the other allows, so each stays
  // independently auditable. This one exists to click an "Add Another" /
  // "+ Add Position" / "Add Education" control that expands a repeating
  // section, driven ONLY from an explicit, user-initiated "Fill this page"
  // (see content.js's expandSections()), never on page load.
  //
  // The HTML subtlety this guard exists to catch: a <button> with no `type`
  // attribute reports type "submit" via the DOM, and a submit/image control
  // that has a form owner SUBMITS THAT FORM when clicked. Workday/React "Add
  // Another" controls are always type="button", or a non-<button> element
  // (a styled <div>/<span role="button">) with no `.type` at all — both pass.
  // A bare, unstyled <button>Add Another</button> sitting inside a <form>
  // (no type="" attribute at all) is exactly the trap this refuses.
  var ADD_BUTTON_TEXT_RE = /^\s*\+?\s*add\b.{0,30}$/i;
  var ADD_BUTTON_DENY_RE = /\b(submit|apply|send|next|continue|save|review|finish|done|sign|confirm|proceed|upload|delete|remove)\b/i;

  function accessibleControlText(el) {
    var t = cleanText(el.textContent);
    if (t) return t;
    var aria = el.getAttribute && el.getAttribute('aria-label');
    return aria ? cleanText(aria) : '';
  }

  /**
   * EVERY condition below must hold for `el` to be a safe "Add Another" target:
   *   1. structurally button-ish: a <button>, an <a> with no navigating href
   *      (none, "#", or "javascript:void(0)"), or anything with role="button".
   *   2. its accessible text (textContent, else aria-label) looks like an ADD
   *      action ("Add Another", "+ Add Position", "Add Education", ...).
   *   3. that same text does NOT also look like a submit/destructive action
   *      (submit, apply, send, next, continue, save, review, finish, done,
   *      sign, confirm, proceed, upload, delete, remove).
   *   4. refuses any element whose EFFECTIVE type is "submit"/"image" AND has
   *      a form owner (the trap above), and refuses input[type=submit|image]
   *      outright, regardless of form ownership or text.
   *   5. visible and enabled.
   */
  function isAddAnotherButtonSafe(el) {
    if (!el || el.nodeType !== 1) return false;

    var tag = el.tagName;

    // input[type=submit|image] is refused outright, before anything else —
    // no role or text can rescue it.
    if (tag === 'INPUT') {
      var inputType = String(el.type || '').toLowerCase();
      if (inputType === 'submit' || inputType === 'image') return false;
    }

    var role = el.getAttribute && el.getAttribute('role');
    var isButtonTag = tag === 'BUTTON';
    var isSafeAnchor = false;
    if (tag === 'A') {
      var href = el.getAttribute('href');
      var hrefTrim = (href === null || href === undefined) ? '' : String(href).trim();
      isSafeAnchor = hrefTrim === '' || hrefTrim === '#' || /^javascript:void\(0\)\s*;?$/i.test(hrefTrim);
    }
    var isRoleButton = role === 'button';
    if (!isButtonTag && !isSafeAnchor && !isRoleButton) return false;

    var text = accessibleControlText(el);
    if (!ADD_BUTTON_TEXT_RE.test(text)) return false;
    if (ADD_BUTTON_DENY_RE.test(text)) return false;

    // The critical subtlety: a <button> (or <input>, already refused above
    // regardless of form ownership) with no explicit type="" attribute
    // reports type "submit" via the DOM. role="button" divs/spans have no
    // `.type` property at all, so this branch never fires for them.
    var effectiveType = String(el.type || '').toLowerCase();
    if ((effectiveType === 'submit' || effectiveType === 'image') && el.form) return false;

    if (!isVisible(el)) return false;
    if (el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;

    return true;
  }

  /** Re-checks the guard at the point of click — never trust that a caller filtered correctly. */
  function safeClickAddButton(el) {
    if (!isAddAnotherButtonSafe(el)) return false;
    el.click();
    return true;
  }

  // Belt and braces: installed for the duration of expansion clicks only (see
  // content.js's expandSections()). Even if isAddAnotherButtonSafe() somehow
  // let through something that submits a form, this capturing document-level
  // listener stops the submit event before it can do anything — so a
  // mis-detected button still cannot submit an application. Returns a
  // `remove()` function; the caller MUST call it in a `finally` block. Calls
  // `onBlocked()` if the shield ever actually fires — that means the guard
  // let through something it should not have, and it must be surfaced, not
  // swallowed.
  function installSubmitShield(doc, onBlocked) {
    doc = doc || (typeof document !== 'undefined' ? document : null);
    if (!doc) return function () {};
    function blockSubmit(e) {
      e.preventDefault();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      else e.stopPropagation();
      if (typeof onBlocked === 'function') {
        try { onBlocked(e); } catch (err) { /* never let a reporting bug re-throw into the shield */ }
      }
    }
    doc.addEventListener('submit', blockSubmit, true);
    return function removeSubmitShield() {
      doc.removeEventListener('submit', blockSubmit, true);
    };
  }

  // Which section-heading keywords belong to which repeating-section "kind" —
  // deliberately a SUBSET of SECTION_KEYWORD_SRC's full list, split so a
  // block can be classified as work-history vs education rather than just
  // "some section". Bare "roles?" is treated as work-history (a "Prior
  // Roles" heading), matching real-world Workday copy.
  var WORK_KIND_SECTION_RE = new RegExp('(' + [
    'work\\s*experience', 'work\\s*history', 'employment\\s*history',
    'career\\s*history', 'employment', 'employers?',
    'prior\\s*roles?', 'previous\\s*roles?', 'roles?', 'positions?', 'jobs?'
  ].join('|') + ')', 'i');
  var EDUCATION_KIND_SECTION_RE = new RegExp('(' + [
    'education', 'schools?', 'universit(?:y|ies)', 'colleges?', 'degrees?'
  ].join('|') + ')', 'i');

  var SECTION_KIND_RE = { work_history: WORK_KIND_SECTION_RE, education: EDUCATION_KIND_SECTION_RE };

  /**
   * Counts the repeating blocks of `kind` ("work_history" | "education")
   * currently in `root`'s DOM, using the SAME section/section_index
   * resolution the scanner already relies on (getSectionContext) — never a
   * new, separately-fallible way of finding blocks. A block with no visible
   * index at all (a lone, unnumbered "Work Experience" heading) still counts
   * as one block. Returns 0 when no field of that kind exists at all.
   */
  function countSectionBlocks(root, kind) {
    var re = SECTION_KIND_RE[kind];
    if (!re) return 0;
    var scanned = scanFields(root);
    var any = false;
    var maxIndex = 0;
    for (var i = 0; i < scanned.fields.length; i++) {
      var f = scanned.fields[i];
      if (!f.section || !re.test(f.section)) continue;
      any = true;
      if (typeof f.section_index === 'number' && f.section_index > maxIndex) maxIndex = f.section_index;
    }
    if (!any) return 0;
    return Math.max(maxIndex, 1);
  }

  function elementDocPosition(a, b) {
    if (a === b) return 0;
    var pos = a.compareDocumentPosition(b);
    if (pos & 4 /* DOCUMENT_POSITION_FOLLOWING */) return -1; // a comes before b
    if (pos & 2 /* DOCUMENT_POSITION_PRECEDING */) return 1; // a comes after b
    return 0;
  }

  /**
   * Finds the "Add Another" button that belongs to `kind`'s LAST block:
   * the nearest isAddAnotherButtonSafe() candidate that follows the last
   * field of `kind` in document order, with no field belonging to the
   * OTHER kind in between. Returns null rather than guessing when no such
   * button exists (a page with no repeating section of this kind at all, or
   * one where the add control cannot be told apart safely) — the caller
   * must then skip expansion for that kind rather than click something it
   * cannot vouch for.
   */
  function findAddButtonForKind(root, kind) {
    var doc = root.nodeType === 9 ? root : ownerDoc(root);
    var re = SECTION_KIND_RE[kind];
    var otherKindKey = kind === 'work_history' ? 'education' : 'work_history';
    var otherRe = SECTION_KIND_RE[otherKindKey];
    if (!re || !doc) return null;

    var scanned = scanFields(root);
    var lastFieldEl = null;
    for (var i = 0; i < scanned.fields.length; i++) {
      var f = scanned.fields[i];
      if (!f.section || !re.test(f.section)) continue;
      var entry = scanned.registry[f.id];
      var els = entry.kind === 'radio-group' ? entry.elements :
        (entry.kind === 'date-parts' ? [entry.monthEl, entry.yearEl] : [entry.el]);
      for (var e = 0; e < els.length; e++) {
        if (!lastFieldEl || elementDocPosition(lastFieldEl, els[e]) < 0) lastFieldEl = els[e];
      }
    }
    if (!lastFieldEl) return null;

    // Walk forward in document order from the last matching field, bounded to the SAME
    // `root` the caller scoped this search to (a scoped call — e.g. one narrow container —
    // must never reach past its own boundary into unrelated parts of a bigger page). The
    // first thing encountered that is EITHER a safe add-button candidate OR a field
    // belonging to the other kind decides the outcome — whichever comes first wins, so a
    // field from a different section always blocks picking a button that lives beyond it.
    var walkRoot = root.nodeType === 9 ? (root.body || root.documentElement) : root;
    if (!walkRoot) return null;
    var walker = doc.createTreeWalker(walkRoot, 1 /* NodeFilter.SHOW_ELEMENT */, null, false);
    walker.currentNode = lastFieldEl;
    var node;
    while ((node = walker.nextNode())) {
      if (isAddAnotherButtonSafe(node)) {
        // Extra safety: a button whose OWN text names the other kind
        // ("Add Education" while looking for work_history) is never ours,
        // even if it is the nearest candidate — keep walking past it.
        var btnText = accessibleControlText(node);
        if (otherRe.test(btnText) && !re.test(btnText)) continue;
        return node;
      }
      if (isEligible(node) && !node.disabled) {
        var ctx = getSectionContext(node);
        if (ctx.section && otherRe.test(ctx.section)) return null; // boundary: a different section's field comes first
      }
    }
    return null;
  }

  function setRadioValue(elements, text) {
    var target = String(text == null ? '' : text).trim().toLowerCase();
    if (!target) { clearRadioGroup(elements); return true; }
    var match = elements.filter(function (r) { return cleanText(getLabel(r)).toLowerCase() === target; })[0];
    if (!match) match = elements.filter(function (r) { return String(r.value).toLowerCase() === target; })[0];
    if (!match) {
      // Shared matcher: answer families, then word-boundary containment (never raw substring) --
      // see matchChoiceOption's doc comment. Radio labels are the "option texts" here.
      var labels = elements.map(function (r) { return getLabel(r); });
      var idx = matchChoiceOption(text, labels);
      if (idx !== -1) match = elements[idx];
    }
    if (!match) return false;
    // A native click is the most faithful simulation of a real user selecting a radio
    // button: it flips `checked`, unchecks its siblings, and fires click/input/change —
    // exactly what React's onChange handlers listen for. Read back afterwards rather than
    // trust the click blindly — a disabled control, or a page's own handler reverting the
    // selection, must be reported honestly instead of as a fake success.
    if (!safeClick(match)) return false;
    return match.checked === true;
  }

  function setCheckboxValue(el, boolLike) {
    var want = boolLike === true || /^(true|yes|1|on)$/i.test(String(boolLike));
    if (el.checked === want) return true;
    if (!safeClick(el)) return false;
    // Read back rather than trust the click blindly -- see setRadioValue's identical reasoning.
    return el.checked === want;
  }

  /** Reads the field's current value, in the same shape applyFill expects to receive it back. */
  function getCurrentValue(entry) {
    if (entry.kind === 'radio-group') {
      var checked = entry.elements.filter(function (r) { return r.checked; })[0];
      return checked ? checked.value : '';
    }
    if (entry.kind === 'date-parts') return getDatePartsValue(entry);
    if (entry.kind === 'wd-date-my' || entry.kind === 'wd-date-y' || entry.kind === 'wd-date-mdy') return getWorkdayDateValue(entry);
    if (entry.kind === 'wd-dropdown') return cleanText(entry.button.textContent);
    if (entry.kind === 'wd-prompt') return getWorkdayPromptCurrentValue(entry);
    if (entry.kind === 'checkbox-group') {
      return entry.elements.filter(function (e) { return e.checked; }).map(function (e) { return getLabel(e); }).join(', ');
    }
    if (entry.kind === 'combobox') return getComboboxCommittedValue(entry);
    if (entry.kind === 'button-group') {
      var selected = entry.buttons.filter(isChoiceButtonSelected)[0];
      return selected ? accessibleControlText(selected) : '';
    }
    if (entry.kind === 'lever-location') return cleanText((entry.hidden && entry.hidden.value) || '');
    if (entry.kind === 'wd-checkbox-group') {
      var checkedBox = entry.boxes.filter(function (b) { return b.checked; })[0];
      return checkedBox ? getWorkdayCheckboxGroupOptionLabel(checkedBox) : '';
    }
    var el = entry.el;
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'checkbox') return el.checked;
    return el.value;
  }

  /**
   * Applies `value` to the field described by `entry` (an ApplyPilotScanner registry entry).
   * Returns a plain boolean for every pre-existing entry kind (unchanged, synchronous — undo()
   * and every existing caller/test relies on that). The two new Workday widgets that need to
   * open a popup and wait for it (`wd-dropdown`, `wd-prompt`) return a Promise<boolean> instead
   * — callers must `Promise.resolve()` the result rather than assume it is always synchronous.
   * A human-readable reason for a `false`/failed result is stashed on `entry._lastReason` for
   * these two kinds (see content.js), since a bare boolean can't carry "no confident match".
   */
  function applyFill(entry, value) {
    if (entry.kind === 'radio-group') {
      var rgOk = setRadioValue(entry.elements, value);
      if (!rgOk) entry._lastReason = 'no confident match for "' + value + '" among the radio options, or the selection did not stick';
      return rgOk;
    }
    if (entry.kind === 'date-parts') return setDatePartsValue(entry, value);
    if (entry.kind === 'wd-date-my' || entry.kind === 'wd-date-y' || entry.kind === 'wd-date-mdy') {
      return setWorkdayDateValue(entry, value);
    }
    if (entry.kind === 'wd-dropdown') {
      return fillWorkdayDropdown(entry, value).then(function (r) {
        entry._lastReason = r.reason || '';
        return !!r.ok;
      });
    }
    if (entry.kind === 'wd-prompt') {
      return fillWorkdayPromptValue(entry, value).then(function (r) {
        entry._lastReason = r.reason ||
          (r.failedTerms && r.failedTerms.length ? ('no confident match for: ' + r.failedTerms.join(', ')) : '');
        return !!r.ok;
      });
    }
    if (entry.kind === 'checkbox-group') return applyCheckboxGroupValue(entry, value);
    if (entry.kind === 'combobox') {
      return fillComboboxValue(entry, value, ownerDoc(entry.input)).then(function (r) {
        entry._lastReason = r.reason ||
          (r.failedTerms && r.failedTerms.length ? ('no confident match for: ' + r.failedTerms.join(', ')) : '');
        return !!r.ok;
      });
    }
    if (entry.kind === 'button-group') {
      return fillButtonGroup(entry, value, ownerDoc(entry.buttons[0])).then(function (r) {
        entry._lastReason = r.reason || '';
        return !!r.ok;
      });
    }
    if (entry.kind === 'lever-location') {
      return fillLeverLocation(entry, value, ownerDoc(entry.input)).then(function (r) {
        entry._lastReason = r.reason || '';
        return !!r.ok;
      });
    }
    if (entry.kind === 'wd-checkbox-group') {
      var cgResult = setWorkdayCheckboxGroupValue(entry, value);
      entry._lastReason = cgResult ? '' : ('no confident match for "' + value + '" among ' + entry.label);
      return cgResult;
    }
    var el = entry.el;
    if (el.tagName === 'SELECT') return setSelectValue(el, value);
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'checkbox') {
      var cbOk = setCheckboxValue(el, value);
      if (!cbOk) entry._lastReason = 'checkbox did not reach the intended checked state';
      return cbOk;
    }
    setNativeValue(el, value);
    // Read back rather than trust the write blind (a controlled/validated input, or a
    // type=number input given a non-numeric string, can silently discard it — see the
    // project brief: "120000 USD" into input[type=number] must NOT report success just
    // because setNativeValue() was called).
    var target = String(value == null ? '' : value);
    var stuck;
    var elType = (el.type || '').toLowerCase();
    if (el.tagName === 'INPUT' && elType === 'number') {
      var actualNum = parseFloat(el.value);
      var targetNum = parseFloat(target);
      stuck = !isNaN(actualNum) && !isNaN(targetNum) && actualNum === targetNum;
    } else if (el.tagName === 'INPUT' && elType === 'tel') {
      // A live probe (2026-09-24) showed Greenhouse's intl-tel-input widget reformat
      // "+1 206 555 0147" to "+1 206-555-0147" on commit -- a cosmetic re-punctuation, not a
      // dropped value. Compare by DIGITS ONLY, and accept either string ending in the other's
      // digits (>= 7 digits, a full local number) so a widget that also drops/adds a leading
      // country code still reads back as stuck, without ever accepting a trivial short match.
      var actualDigits = String(el.value || '').replace(/\D/g, '');
      var targetDigits = target.replace(/\D/g, '');
      stuck = !!actualDigits && !!targetDigits && (
        actualDigits === targetDigits ||
        (actualDigits.length >= 7 && targetDigits.slice(-actualDigits.length) === actualDigits) ||
        (targetDigits.length >= 7 && actualDigits.slice(-targetDigits.length) === targetDigits)
      );
    } else {
      stuck = cleanText(el.value).toLowerCase() === cleanText(target).toLowerCase();
    }
    if (!stuck) {
      entry._lastReason = 'value did not stick after being set (read back ' + JSON.stringify(String(el.value)) + ')';
      return false;
    }
    return true;
  }

  /** The element(s) that should get the visual highlight for this field. */
  function getHighlightTargets(entry) {
    if (entry.kind === 'radio-group') return entry.elements.slice();
    if (entry.kind === 'date-parts') return [entry.monthEl, entry.yearEl];
    if (entry.kind === 'wd-date-my' || entry.kind === 'wd-date-y' || entry.kind === 'wd-date-mdy') {
      return getWorkdayDateHighlightTargets(entry);
    }
    if (entry.kind === 'wd-dropdown') return [entry.button];
    if (entry.kind === 'wd-prompt') return [entry.input];
    if (entry.kind === 'checkbox-group') return entry.elements.slice();
    if (entry.kind === 'combobox') return [entry.input];
    if (entry.kind === 'button-group') return entry.buttons.slice();
    if (entry.kind === 'lever-location') return [entry.input];
    if (entry.kind === 'wd-checkbox-group') return entry.boxes.slice();
    // A hidden native <select> paired with a custom widget (see
    // findPairedWidget) highlights the visible widget, never the hidden
    // select the operator can't see.
    return [entry.highlightEl || entry.el];
  }

  // ---------------------------------------------------------------------
  // résumé attachment — picking the target, then assigning the file
  // ---------------------------------------------------------------------
  //
  // Selection mirrors the REASONING of src/applypilot/apply/v2/resolver.py's
  // _pick_resume_file_field / _is_cover_letter_file (read for the rules, not
  // imported — this file has zero Python/Node dependencies by design, see the
  // file header):
  //   1. an explicit résumé/CV-labeled file input wins;
  //   2. else the first file input that is NOT cover-letter-labeled (a plain
  //      "Attach" dropzone with no résumé-ish label still binds the résumé —
  //      it's usually the required blocker, same as Greenhouse's ambiguous
  //      "Attach" widgets);
  //   3. a cover-letter-labeled file input is NEVER the résumé target.

  var COVER_LETTER_RE = /cover[\s_-]?letter/i;
  var RESUME_LABEL_RE = /\b(r[ée]sum[ée]|curriculum\s*vitae|\bcv\b)\b/i;
  var DROPZONE_RE = /drag\s*(?:and|&|'?n'?)?\s*drop/i;

  // Gathers label + name/id + a little nearby container copy so a Greenhouse-style
  // dropzone ("Upload File / or drag and drop here", with the real <input> hidden
  // and unlabeled) is classified correctly even though none of that text sits in
  // a real <label for>/aria-label on the input itself.
  function fileInputContextText(el) {
    var parts = [getLabel(el), el.name || '', el.id || ''];
    var node = el.parentElement;
    for (var depth = 0; depth < 4 && node; depth++) {
      if (node.tagName === 'FORM' || node.tagName === 'BODY') break;
      var t = cleanText(node.textContent);
      if (t && t.length < 300) parts.push(t);
      node = node.parentElement;
    }
    return cleanText(parts.join(' '));
  }

  /**
   * Picks the single file input that should receive the résumé. Returns
   * { el, contextText, isDropzone } or null when no eligible input exists on
   * the page (no file inputs at all, or every file input is cover-letter-labeled).
   */
  // Matches an accept="" attribute that is document-shaped (pdf/doc/docx),
  // as opposed to e.g. accept="image/*" on a photo-upload field. Used only
  // as a tie-breaker below, never to exclude a candidate outright — plenty
  // of real ATS file inputs carry no accept attribute at all.
  var DOC_ACCEPT_RE = /pdf|msword|wordprocessingml|\.docx?\b/i;

  /**
   * Picks the single file input that should receive the résumé. Returns
   * { el, contextText, isDropzone } or null when no eligible input exists on
   * the page (no file inputs at all, or every file input is cover-letter-labeled).
   *
   * Considers hidden inputs too (deliberately no isVisible() filter below) —
   * custom upload widgets routinely keep the real <input type=file> hidden
   * (display:none) and drive it from a styled dropzone/button instead, the
   * same pattern findPairedWidget() handles for <select>.
   */
  function findResumeFileTarget(doc) {
    doc = doc || (typeof document !== 'undefined' ? document : null);
    if (!doc) return null;
    var inputs = Array.prototype.slice.call(doc.querySelectorAll('input[type="file"]'))
      .filter(function (el) { return el.isConnected && !el.disabled; });

    var candidates = [];
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      var ctx = fileInputContextText(el);
      if (COVER_LETTER_RE.test(ctx)) continue; // rule 3: never the résumé target
      candidates.push({ el: el, contextText: ctx, isDropzone: DROPZONE_RE.test(ctx) });
    }
    if (!candidates.length) return null;

    for (var c = 0; c < candidates.length; c++) {
      if (RESUME_LABEL_RE.test(candidates[c].contextText)) return candidates[c]; // rule 1
    }

    // rule 1.5: no explicit résumé label anywhere — prefer a candidate whose
    // accept attribute is document-shaped (pdf/doc/docx) over one that isn't
    // (e.g. a photo-upload field with accept="image/*"), before falling back
    // to plain DOM order among the rest.
    for (var d = 0; d < candidates.length; d++) {
      var accept = (candidates[d].el.getAttribute('accept') || '');
      if (DOC_ACCEPT_RE.test(accept)) return candidates[d];
    }

    return candidates[0]; // rule 2: first non-cover-letter file input, DOM order
  }

  // Nearest ancestor whose own text mentions drag-and-drop, so the synthetic `drop`
  // lands on the widget's actual drop target rather than always the input's direct
  // parent (Greenhouse-style markup often wraps the hidden input several levels
  // below the clickable/droppable area).
  function findDropzoneContainer(el) {
    var node = el.parentElement;
    var fallback = node || el;
    for (var depth = 0; depth < 4 && node; depth++) {
      var t = cleanText(node.textContent);
      if (t && DROPZONE_RE.test(t)) return node;
      node = node.parentElement;
    }
    return fallback;
  }

  /**
   * The first file already shown by a Workday-style upload widget, if any (its item name
   * lives at `[data-automation-id="file-upload-item-name"]`, independent of whatever the
   * underlying `<input>`'s `.files` currently holds — Workday empties that after consuming
   * it). Exported for direct testing; also used by attachResumeFile()'s duplicate check.
   */
  function findWorkdayUploadedFilename(doc) {
    doc = doc || (typeof document !== 'undefined' ? document : null);
    if (!doc || !doc.querySelectorAll) return '';
    var nameEls = doc.querySelectorAll('[data-automation-id="file-upload-item-name"]');
    for (var i = 0; i < nameEls.length; i++) {
      var t = cleanText(nameEls[i].textContent);
      if (t) return t;
    }
    // S4 also accepts a bare file-upload-item (no -item-name rendered yet -- e.g. mid-upload)
    // as evidence something is already there; the old check missed this window entirely.
    var items = doc.querySelectorAll('[data-automation-id="file-upload-item"]');
    for (var j = 0; j < items.length; j++) {
      var it = cleanText(items[j].textContent);
      if (it) return it;
    }
    return items.length ? '(attachment present)' : '';
  }

  /**
   * Whether Workday's own UI now shows the upload as done: either its explicit
   * `file-upload-successful` marker, or a `file-upload-item-name` whose text contains our
   * filename (a plain `contains`, not equality — Workday may append a size/status suffix).
   */
  function workdayUploadIndicatesSuccess(doc, filename) {
    if (doc.querySelector && doc.querySelector('[data-automation-id="file-upload-successful"]')) return true;
    var name = findWorkdayUploadedFilename(doc);
    if (!name) return false;
    if (!filename) return true;
    return name.toLowerCase().indexOf(String(filename).toLowerCase()) !== -1;
  }

  /** Polls up to `timeoutMs` (default ~5s) for workdayUploadIndicatesSuccess() to go true. */
  function waitForWorkdayUploadSuccess(doc, filename, timeoutMs) {
    return waitFor(function () { return workdayUploadIndicatesSuccess(doc, filename) ? true : false; }, timeoutMs == null ? 5000 : timeoutMs, doc);
  }

  /**
   * Attaches `file` to the page's résumé target and VERIFIES it stuck before reporting success
   * — a silent failure here is worse than a skip (the operator would submit with no résumé
   * attached). Never throws: assigning to a file input's `.files` is the one DOM write in this
   * whole module a site's own JS can reject outright, so every failure mode reports rather than
   * propagates.
   *
   * Never opens a native file picker and never calls .click() on anything (the resume file
   * upload success verification below only ever READS the DOM) — the file is assigned
   * programmatically via DataTransfer, full stop. That keeps this function outside
   * isClickSafe()'s guard entirely, on purpose: there is no click path here to protect against
   * turning into a submit.
   *
   * Duplicate prevention: if a Workday-style upload widget already shows an attached file
   * BEFORE this call touches anything, this returns immediately without assigning the new file
   * at all — uploading twice is worse than not uploading (see the project brief).
   *
   * Always returns a Promise (previously synchronous): Workday consumes the File and empties
   * `input.files` right after accepting it, so the ONLY honest way to verify success on
   * Workday is to wait briefly for its own success markers to appear — see
   * waitForWorkdayUploadSuccess() above. Non-Workday sites still get the fast, synchronous-in-
   * effect path (input.files[0] check) with no added latency; this only waits when that fast
   * check comes back empty.
   *
   * Returns (all wrapped in a resolved Promise):
   *   { attempted: false }                                          — no résumé-eligible
   *                                                                     file input on the page
   *   { attempted: true, attached: false, alreadyAttached: true, filename, reason }
   *                                                                  — duplicate prevention
   *   { attempted: true, attached: true, filename }                 — verified via read-back
   *                                                                     OR a Workday success marker
   *   { attempted: true, attached: false, reason }                  — tried and failed, or
   *                                                                     could not verify
   */
  function attachResumeFile(doc, file) {
    var target = findResumeFileTarget(doc);
    if (!target) return Promise.resolve({ attempted: false });

    var already = findWorkdayUploadedFilename(doc);
    if (already) {
      return Promise.resolve({
        attempted: true,
        attached: false,
        alreadyAttached: true,
        filename: already,
        reason: 'a résumé is already attached ("' + already + '")'
      });
    }

    var el = target.el;
    var view = realmOf(el);
    var DataTransferCtor = view && view.DataTransfer;
    if (!DataTransferCtor) {
      // Real Chrome always has this. Only a test/runtime environment without a
      // full DOM (e.g. jsdom, which has no DataTransfer/DragEvent) lands here —
      // fail soft, never throw. See selftest.js for what that guard exercises.
      return Promise.resolve({ attempted: true, attached: false, reason: 'DataTransfer is not available in this browsing context.' });
    }

    try {
      var dt = new DataTransferCtor();
      dt.items.add(file);

      try {
        el.files = dt.files;
      } catch (eAssign) {
        return Promise.resolve({ attempted: true, attached: false, reason: 'Site rejected programmatic file assignment: ' + (eAssign && eAssign.message ? eAssign.message : eAssign) });
      }
      fireEvents(el, ['input', 'change']);

      var attached = el.files && el.files[0];
      if (attached && attached.name === file.name) {
        return Promise.resolve({ attempted: true, attached: true, filename: attached.name });
      }

      // ONE upload channel, gated correctly. `el.files` coming back empty here is Workday's
      // NORMAL, SUCCESSFUL outcome (it consumes the File as part of accepting it via `change` --
      // see the module comment above) -- it is NOT itself evidence that `change` failed, so it
      // must never be used to decide whether to also try `drop`. The project brief's "two
      // upload channels" bug was exactly that mistake: firing a synthetic `drop` whenever
      // `input.files` looked empty re-delivered the SAME file a second time on every successful
      // Workday upload. The correct signal (S4's own recipe) is whether ANYTHING indicating an
      // upload appeared at all within a few seconds of `change` alone -- only if genuinely
      // NOTHING did, try `drop` once, as a fallback for a widget that only reads
      // e.dataTransfer.files from the drop event itself rather than the input's change event.
      return waitFor(function () {
        return (doc.querySelector && (
          doc.querySelector('[data-automation-id="file-upload-item"]') ||
          doc.querySelector('[data-automation-id="file-upload-successful"]')
        )) ? true : false;
      }, 3000, doc).then(function (sawUploadIndicator) {
        if (!sawUploadIndicator) {
          try {
            var dzEl = target.isDropzone ? findDropzoneContainer(el) : null;
            var wdDropzone = doc.querySelector && doc.querySelector('[data-automation-id="file-upload-drop-zone"]');
            var dropTarget = dzEl || wdDropzone;
            if (dropTarget) {
              var EventCtor2 = (view && (view.DragEvent || view.Event)) || (typeof Event !== 'undefined' ? Event : null);
              if (EventCtor2) {
                var dropEvt2 = new EventCtor2('drop', { bubbles: true, cancelable: true });
                try { Object.defineProperty(dropEvt2, 'dataTransfer', { value: dt }); } catch (eDef2) { /* best effort */ }
                dropTarget.dispatchEvent(dropEvt2);
              }
            }
          } catch (eWdDrop) {
            // best effort only
          }
        }

        // A further wait of up to ~20s for the actual success marker (S4 polls for ~21s; S9
        // sleeps 10s) -- on top of the up-to-3s wait above, per the brief's own steps (wait for
        // ANY indicator, maybe try drop, THEN wait for success specifically). Longer than any
        // other wait in this module on purpose: Workday's own upload confirmation is genuinely
        // the slowest thing this extension ever waits on, and the previous 5s budget was
        // shorter than every source examined. This resolves immediately if `sawUploadIndicator`
        // already WAS the success marker itself.
        return waitForWorkdayUploadSuccess(doc, file.name, 20000).then(function (found) {
          if (found) {
            return { attempted: true, attached: true, filename: findWorkdayUploadedFilename(doc) || file.name };
          }
          var again = el.files && el.files[0];
          if (again && again.name === file.name) {
            return { attempted: true, attached: true, filename: again.name };
          }
          return {
            attempted: true,
            attached: false,
            reason: 'Assigned the file but could not confirm it stuck (input.files was empty and no upload confirmation appeared).'
          };
        });
      });
    } catch (e) {
      return Promise.resolve({ attempted: true, attached: false, reason: 'Error while attaching résumé: ' + (e && e.message ? e.message : String(e)) });
    }
  }

  return {
    VERSION: '0.1.0',
    EXCLUDED_INPUT_TYPES: EXCLUDED_INPUT_TYPES,
    cleanText: cleanText,
    cssEscape: cssEscape,
    isVisible: isVisible,
    getLabel: getLabel,
    getFieldLabel: getFieldLabel,
    getGroupLabel: getGroupLabel,
    getSectionContext: getSectionContext,
    extractSectionIndex: extractSectionIndex,
    buildSelector: buildSelector,
    findPairedWidget: findPairedWidget,
    findOptionMatch: findOptionMatch,
    scanFields: scanFields,
    scanAll: scanAll,
    setNativeValue: setNativeValue,
    setSelectValue: setSelectValue,
    setRadioValue: setRadioValue,
    setCheckboxValue: setCheckboxValue,
    getCurrentValue: getCurrentValue,
    applyFill: applyFill,
    getHighlightTargets: getHighlightTargets,
    isClickSafe: isClickSafe,
    isAddAnotherButtonSafe: isAddAnotherButtonSafe,
    safeClickAddButton: safeClickAddButton,
    installSubmitShield: installSubmitShield,
    countSectionBlocks: countSectionBlocks,
    findAddButtonForKind: findAddButtonForKind,
    findDatePartPairs: findDatePartPairs,
    setDatePartsValue: setDatePartsValue,
    getDatePartsValue: getDatePartsValue,
    findResumeFileTarget: findResumeFileTarget,
    attachResumeFile: attachResumeFile,
    findWorkdayUploadedFilename: findWorkdayUploadedFilename,
    waitForWorkdayUploadSuccess: waitForWorkdayUploadSuccess,
    // Workday widgets — exported primarily for selftest.js; content.js only ever goes through
    // scanFields()/applyFill()/getCurrentValue()/getHighlightTargets() above.
    findWorkdayDateWrappers: findWorkdayDateWrappers,
    findWorkdayDatePart: findWorkdayDatePart,
    findWorkdayDateSectionDisplay: findWorkdayDateSectionDisplay,
    readWorkdaySpinnerDisplayValue: readWorkdaySpinnerDisplayValue,
    isWorkdaySpinnerInputSafe: isWorkdaySpinnerInputSafe,
    isWorkdayMaskedDateInputSafe: isWorkdayMaskedDateInputSafe,
    setWorkdaySpinnerValue: setWorkdaySpinnerValue,
    typeMaskedTextField: typeMaskedTextField,
    setWorkdayDateValue: setWorkdayDateValue,
    getWorkdayDateValue: getWorkdayDateValue,
    isWorkdayDropdownOpenerSafe: isWorkdayDropdownOpenerSafe,
    isWorkdayOptionSafe: isWorkdayOptionSafe,
    resolveWorkdayListbox: resolveWorkdayListbox,
    collectWorkdayListboxOptions: collectWorkdayListboxOptions,
    matchWorkdayDropdownOption: matchWorkdayDropdownOption,
    matchChoiceOption: matchChoiceOption,
    fillWorkdayDropdown: fillWorkdayDropdown,
    degreeFamilyOf: degreeFamilyOf,
    countryAliasOf: countryAliasOf,
    findWorkdayPrompts: findWorkdayPrompts,
    isWorkdayPromptInputSafe: isWorkdayPromptInputSafe,
    matchWorkdayPromptOption: matchWorkdayPromptOption,
    resolveWorkdayPromptPopup: resolveWorkdayPromptPopup,
    snapshotWorkdayPopups: snapshotWorkdayPopups,
    workdayPromptRows: workdayPromptRows,
    workdayPromptRowLabel: workdayPromptRowLabel,
    workdayPromptHasPill: workdayPromptHasPill,
    fillWorkdayPromptTerm: fillWorkdayPromptTerm,
    // Workday Self-Identify checkbox group (disabilityStatus-CheckboxGroup, CC-305).
    findWorkdayCheckboxGroups: findWorkdayCheckboxGroups,
    getWorkdayCheckboxGroupOptionLabel: getWorkdayCheckboxGroupOptionLabel,
    isWorkdayCheckboxGroupOptionSafe: isWorkdayCheckboxGroupOptionSafe,
    setWorkdayCheckboxGroupValue: setWorkdayCheckboxGroupValue,
    fillWorkdayPromptValue: fillWorkdayPromptValue,
    hasWorkdayHardDenyAutomationId: hasWorkdayHardDenyAutomationId,
    // Choice widgets (combobox / button-group / checkbox-group / Lever location) — exported
    // primarily for selftest.js; content.js only ever goes through
    // scanFields()/applyFill()/getCurrentValue()/getHighlightTargets() above.
    stripRequiredMarker: stripRequiredMarker,
    isComboboxInputSafe: isComboboxInputSafe,
    resolveComboboxMenu: resolveComboboxMenu,
    isComboboxOptionSafe: isComboboxOptionSafe,
    getComboboxCommittedValue: getComboboxCommittedValue,
    verifyComboboxSelection: verifyComboboxSelection,
    fillComboboxValue: fillComboboxValue,
    findButtonGroups: findButtonGroups,
    isChoiceButtonSafe: isChoiceButtonSafe,
    isChoiceButtonSelected: isChoiceButtonSelected,
    fillButtonGroup: fillButtonGroup,
    findCheckboxGroups: findCheckboxGroups,
    applyCheckboxGroupValue: applyCheckboxGroupValue,
    findLeverLocationFields: findLeverLocationFields,
    getLeverLocationLabel: getLeverLocationLabel,
    fillLeverLocation: fillLeverLocation
  };
});
