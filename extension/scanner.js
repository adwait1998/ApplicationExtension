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
        if (t) return t;
      }
    }
    var container = (first.closest && (first.closest('[role="radiogroup"]') || first.closest('[role="group"]'))) || null;
    if (container) {
      var ariaLabel = container.getAttribute('aria-label');
      if (ariaLabel) return cleanText(ariaLabel);
      var labelledBy = container.getAttribute('aria-labelledby');
      var doc = ownerDoc(first);
      if (labelledBy && doc) {
        var ref = doc.getElementById(labelledBy.split(/\s+/)[0]);
        if (ref) return cleanText(ref.textContent);
      }
    }
    return getPrecedingText(first) || '';
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
  // refused outright, no matter how much it otherwise looks like a safe target.
  var WD_HARD_DENY_AUTOMATION_RE = /bottom-navigation|submit|next|save/i;

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

  function dispatchKeyboardEvent(el, type, key, code) {
    var view = realmOf(el);
    var Ctor = (view && view.KeyboardEvent) || (typeof KeyboardEvent !== 'undefined' ? KeyboardEvent : null);
    if (!Ctor) return;
    el.dispatchEvent(new Ctor(type, { key: key, code: code || key, bubbles: true, cancelable: true }));
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
  // Workday spinner dates (dateInputWrapper) — ground truth structure:
  //   div[data-automation-id^="formField-"] -> div[data-automation-id="dateInputWrapper"]
  //     -> input[aria-label="Month"] + input[aria-label="Year"]      (month/year pair)
  //     -> input[aria-label="Year"] only                              (year-only, education)
  //     -> exactly ONE plain <input>, no aria-label Month/Year at all (masked MM/YYYY fallback)
  //
  // The data-automation-id "dateSectionMonth-input"/"dateSectionYear-input" this extension
  // used to look for appears in NEITHER working source it was rebuilt from — that guess is why
  // real Workday dates silently failed twice. Detection here keys ONLY on the structure above.
  // ---------------------------------------------------------------------

  function findWorkdayDateWrappers(root) {
    var wrappers = Array.prototype.slice.call(root.querySelectorAll('[data-automation-id="dateInputWrapper"]'));
    var out = [];
    for (var i = 0; i < wrappers.length; i++) {
      var w = wrappers[i];
      if (!isVisible(w)) continue;
      var monthEl = w.querySelector('input[aria-label="Month"]');
      var yearEl = w.querySelector('input[aria-label="Year"]');
      var container = findWorkdayFormField(w) || w;
      var label = getWorkdayFieldLabel(container);
      if (yearEl) {
        out.push({ shape: monthEl ? 'my' : 'y', wrapper: w, monthEl: monthEl || null, yearEl: yearEl, maskedEl: null, container: container, label: label });
        continue;
      }
      // No aria-label Month/Year input at all -- the masked single-input fallback shape
      // (ankitsharma38), ONLY when the wrapper holds exactly one <input>.
      var allInputs = w.querySelectorAll('input');
      if (allInputs.length === 1) {
        out.push({ shape: 'masked', wrapper: w, monthEl: null, yearEl: null, maskedEl: allInputs[0], container: container, label: label });
      }
    }
    return out;
  }

  function isWorkdaySpinnerInputSafe(el) {
    if (!el || el.tagName !== 'INPUT') return false;
    if (hasWorkdayHardDenyAutomationId(el)) return false;
    var aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
    if (aria !== 'Month' && aria !== 'Year') return false;
    var wrapper = el.closest ? el.closest('[data-automation-id="dateInputWrapper"]') : null;
    if (!wrapper) return false;
    if (hasWorkdayHardDenyAutomationId(wrapper)) return false;
    if (el.disabled) return false;
    if (!isVisible(el)) return false;
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
    if (!isVisible(el)) return false;
    return true;
  }

  /**
   * berellevy's technique for a Workday numeric spinbutton: setting `.value` and firing
   * input/change is silently reverted by Workday's own controlled re-render. Setting `.value`
   * ONE BELOW the target with no event at all, then a real ArrowUp keydown, lets Workday's own
   * handler commit the target itself. Some variants need the ArrowUp twice; read back and
   * retry once before giving up.
   */
  function setWorkdaySpinnerValue(el, targetNum) {
    if (!isWorkdaySpinnerInputSafe(el)) return false;
    if (typeof targetNum !== 'number' || isNaN(targetNum)) return false;

    el.value = String(targetNum - 1);
    dispatchKeyboardEvent(el, 'keydown', 'ArrowUp', 'ArrowUp');
    if (typeof el.click === 'function') el.click();
    if (parseInt(el.value, 10) === targetNum) return true;

    // Some variants need ArrowUp twice -- read back and retry once, from wherever it landed.
    dispatchKeyboardEvent(el, 'keydown', 'ArrowUp', 'ArrowUp');
    if (typeof el.click === 'function') el.click();
    return parseInt(el.value, 10) === targetNum;
  }

  /**
   * ankitsharma38's fallback: ONLY used when a dateInputWrapper holds exactly one masked text
   * input (no separate Month/Year spinners at all). Types `text` character by character with a
   * genuine keydown/keypress/input/keyup sequence per character, then blurs.
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

  /** Splits a service-provided "MM/YYYY" (or, for a year-only field, "MM/YYYY" or bare "YYYY") value. */
  function setWorkdayDateValue(entry, value) {
    var str = String(value == null ? '' : value).trim();
    if (!str) return false; // never guess at clearing a spinner -- report failure honestly

    if (entry.kind === 'wd-date-y') {
      var ym = MM_YYYY_RE.exec(str);
      var yr = ym ? ym[2] : (/^\d{4}$/.test(str) ? str : null);
      if (!yr) return false;
      return setWorkdaySpinnerValue(entry.yearEl, parseInt(yr, 10));
    }

    if (entry.kind === 'wd-date-my') {
      var m = MM_YYYY_RE.exec(str);
      if (!m) return false;
      var monthNum = parseInt(m[1], 10);
      if (monthNum < 1 || monthNum > 12) return false;
      if (entry.maskedEl) {
        return typeMaskedTextField(entry.maskedEl, pad2(monthNum) + '/' + m[2]);
      }
      var okMonth = setWorkdaySpinnerValue(entry.monthEl, monthNum);
      var okYear = setWorkdaySpinnerValue(entry.yearEl, parseInt(m[2], 10));
      return okMonth && okYear;
    }

    return false;
  }

  function getWorkdayDateValue(entry) {
    if (entry.kind === 'wd-date-y') return String((entry.yearEl && entry.yearEl.value) || '').trim();
    if (entry.kind === 'wd-date-my') {
      if (entry.maskedEl) return String(entry.maskedEl.value || '').trim();
      var mv = parseInt(entry.monthEl && entry.monthEl.value, 10);
      var yv = String((entry.yearEl && entry.yearEl.value) || '').trim();
      if (isNaN(mv) && !yv) return '';
      return (isNaN(mv) ? '' : pad2(mv)) + '/' + yv;
    }
    return '';
  }

  function getWorkdayDateHighlightTargets(entry) {
    if (entry.kind === 'wd-date-y') return [entry.yearEl];
    if (entry.maskedEl) return [entry.maskedEl];
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

  var WD_OPTION_AUTOMATION_RE = /promptOption|checkboxItem/i;

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
    // Fallback: any [role=listbox] that has become visible somewhere in the document --
    // Workday portals the popup to <body>, far from the opener button.
    var candidates = Array.prototype.slice.call(doc.querySelectorAll('[role="listbox"]'));
    for (var i = 0; i < candidates.length; i++) {
      if (isVisible(candidates[i])) return candidates[i];
    }
    return null;
  }

  // ---- degree / country synonyms (explicit families only -- no guessing outside them) ------

  function normalizeToken(s) { return String(s == null ? '' : s).toLowerCase().replace(/[.\s]/g, ''); }

  var WD_DEGREE_FAMILY_TOKENS = {
    doctorate: ['phd', 'doctorate', 'doctoral', 'dsc', 'edd', 'dba'],
    master: ['ms', 'msc', 'ma', 'mba', 'me', 'meng', 'mfa', 'mtech', 'master', 'masters',
      'masterofscience', 'masterofarts', 'masterofengineering', 'masterofbusinessadministration'],
    bachelor: ['bs', 'bsc', 'ba', 'be', 'beng', 'btech', 'bachelor', 'bachelors',
      'bachelorofscience', 'bachelorofarts', 'bachelorofengineering'],
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

  /** Match priority: exact (normalised) -> normalised synonym (degree family / country alias) -> one-way contains. */
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

    for (i = 0; i < optionTexts.length; i++) {
      var ot = norm(optionTexts[i]);
      if (ot && ot.indexOf(t) !== -1) return i;
    }
    return -1;
  }

  /**
   * Opens the dropdown, matches `value` against its options, and clicks the match — or, when
   * nothing matches confidently, presses Escape and leaves the field untouched. NEVER falls
   * back to the first option (see the project brief: a typeahead once blindly picked
   * "Venezuela" for "Arizona" on a real application).
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

      var optionEls = Array.prototype.slice.call(
        listbox.querySelectorAll('[role="option"], [data-automation-id*="promptOption"]')
      ).filter(isVisible);
      var texts = optionEls.map(optionAccessibleText);
      var idx = matchWorkdayDropdownOption(target, texts);

      if (idx === -1) {
        dispatchKeyboardEvent(button, 'keydown', 'Escape', 'Escape');
        return { ok: false, reason: 'no confident match for "' + target + '" among dropdown options' };
      }

      var matched = optionEls[idx];
      if (!isWorkdayOptionSafe(matched, listbox)) {
        dispatchKeyboardEvent(button, 'keydown', 'Escape', 'Escape');
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
  }

  // ---------------------------------------------------------------------
  // Workday prompt / multi-select (multiSelectContainer) — Field of Study, School,
  // Certification, Skills, ...
  // ---------------------------------------------------------------------

  var WD_PROMPT_RESULT_SELECTOR = '[data-automation-id*="promptOption"], [data-automation-id*="checkboxItem"], [role="option"]';

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

  /** Match priority: exact -> acronym form ("(SQL)") -> word-boundary startsWith -> substring. */
  function matchWorkdayPromptOption(term, optionTexts) {
    var norm = function (s) { return cleanText(s).toLowerCase(); };
    var t = norm(term);
    if (!t) return -1;
    var i;

    for (i = 0; i < optionTexts.length; i++) {
      if (norm(optionTexts[i]) === t) return i;
    }

    var acronymRe = new RegExp('\\(' + escapeRegExp(t) + '\\)', 'i');
    for (i = 0; i < optionTexts.length; i++) {
      if (acronymRe.test(optionTexts[i])) return i;
    }

    var startRe = new RegExp('\\b' + escapeRegExp(t), 'i');
    for (i = 0; i < optionTexts.length; i++) {
      if (startRe.test(optionTexts[i])) return i;
    }

    for (i = 0; i < optionTexts.length; i++) {
      if (norm(optionTexts[i]).indexOf(t) !== -1) return i;
    }
    return -1;
  }

  function clearWorkdayPromptInput(input) {
    setNativeValue(input, '');
  }

  /**
   * Types one term into a prompt/multi-select, waits for Workday's own results to appear,
   * matches by strict priority, and clicks the match -- or, with no confident match, clears the
   * typed text and reports failure. NEVER clicks the first result blindly (same rule as the
   * dropdown above, and for the same reason: a wrong pick here is a false statement on a real
   * application).
   */
  function fillWorkdayPromptTerm(entry, term) {
    var input = entry.input;
    var doc = ownerDoc(input);
    if (!isWorkdayPromptInputSafe(input)) {
      return Promise.resolve({ ok: false, reason: 'safety guard refused the prompt input' });
    }
    var t = String(term == null ? '' : term).trim();
    if (!t) return Promise.resolve({ ok: false, reason: 'empty term' });

    setNativeValue(input, t);
    dispatchKeyboardEvent(input, 'keydown', 'Enter', 'Enter');

    // Searched narrowest-first: the multiSelectContainer itself (some implementations render
    // results inline inside it), then the whole formField-* wrapper (covers a results panel
    // rendered as a sibling of the container, under the same field — the common shape).
    // Deliberately NEVER falls back to a whole-document search the way the dropdown's listbox
    // resolution does: unlike the dropdown (explicitly portalled per the project brief, found
    // only via its own aria-controls id), nothing in the ground truth says a prompt's results
    // are portalled away from their own field, and a document-wide fallback here would risk
    // binding to a DIFFERENT field's results left open on the page (e.g. an earlier prompt
    // whose typed term had no confident match, so its own results panel was correctly left
    // open rather than guessed at) — exactly the kind of cross-field mix-up this extension
    // exists to prevent.
    return waitFor(function () {
      var roots = [entry.container, entry.formField];
      for (var r = 0; r < roots.length; r++) {
        var root = roots[r];
        if (!root || !root.querySelectorAll) continue;
        var els = Array.prototype.slice.call(root.querySelectorAll(WD_PROMPT_RESULT_SELECTOR)).filter(isVisible);
        if (els.length) return { root: root, els: els };
      }
      return null;
    }, 3000, doc).then(function (found) {
      if (!found) {
        clearWorkdayPromptInput(input);
        return { ok: false, reason: 'no results returned for "' + t + '"' };
      }
      var resultEls = found.els;
      var texts = resultEls.map(optionAccessibleText);
      var idx = matchWorkdayPromptOption(t, texts);
      if (idx === -1) {
        clearWorkdayPromptInput(input);
        return { ok: false, reason: 'no confident match among results for "' + t + '"' };
      }
      var matched = resultEls[idx];
      if (!isWorkdayOptionSafe(matched, found.root)) {
        clearWorkdayPromptInput(input);
        return { ok: false, reason: 'matched result failed the safety guard' };
      }

      var checkbox = matched.querySelector ? matched.querySelector('input[type="checkbox"]') : null;
      if (checkbox) {
        if (!safeClick(checkbox)) return { ok: false, reason: 'checkbox click refused by the click guard' };
      } else {
        dispatchPointerClickSequence(matched);
      }

      var matchedText = texts[idx];
      return waitFor(function () {
        var list = (entry.formField && entry.formField.querySelector && entry.formField.querySelector('ul[data-automation-id="selectedItemList"]'))
          || doc.querySelector('ul[data-automation-id="selectedItemList"]');
        if (!list) return false;
        var items = Array.prototype.slice.call(list.querySelectorAll('li'));
        return items.some(function (li) { return cleanText(li.textContent).toLowerCase().indexOf(t.toLowerCase()) !== -1; });
      }, 2000, doc).then(function (added) {
        if (!added) return { ok: false, reason: 'click did not add "' + t + '" to the selected item list' };
        return { ok: true, matchedText: matchedText };
      });
    });
  }

  /**
   * `value` is either a single term (Field of Study, School, Certification) or an array
   * (Skills — the service sends a list). For a list: add each in turn, skip one with no
   * confident match and continue with the rest, capped at the list's own length.
   */
  function fillWorkdayPromptValue(entry, value) {
    if (Array.isArray(value)) {
      var results = [];
      function next(i) {
        if (i >= value.length) return Promise.resolve(results);
        return fillWorkdayPromptTerm(entry, value[i]).then(function (r) {
          results.push({ term: value[i], result: r });
          return next(i + 1);
        });
      }
      return next(0).then(function (all) {
        var failedTerms = all.filter(function (r) { return !r.result.ok; }).map(function (r) { return r.term; });
        var anyOk = all.some(function (r) { return r.result.ok; });
        return { ok: anyOk, failedTerms: failedTerms, results: all };
      });
    }
    return fillWorkdayPromptTerm(entry, value);
  }

  function getWorkdayPromptCurrentValue(entry) {
    var doc = ownerDoc(entry.input);
    var list = (entry.formField && entry.formField.querySelector && entry.formField.querySelector('ul[data-automation-id="selectedItemList"]'))
      || (doc && doc.querySelector('ul[data-automation-id="selectedItemList"]'));
    if (!list) return '';
    var items = Array.prototype.slice.call(list.querySelectorAll('li')).map(function (li) { return cleanText(li.textContent); }).filter(Boolean);
    return items.join(', ');
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
      if (wdw.yearEl) consumedByWorkday.push(wdw.yearEl);
      if (wdw.maskedEl) consumedByWorkday.push(wdw.maskedEl);
    }
    for (var wp = 0; wp < wdPrompts.length; wp++) consumedByWorkday.push(wdPrompts[wp].input);

    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (el.disabled) continue;
      if (consumedByDatePair.indexOf(el) !== -1) continue;
      if (consumedByWorkday.indexOf(el) !== -1) continue;

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

    // Workday spinner dates -- one field per dateInputWrapper, "wd-date-my" (month+year, or the
    // single masked-input fallback) or "wd-date-y" (year-only, education "From"/"To"). The
    // service may send "MM/YYYY" even for a year-only field; the resolver is told to use only
    // the year part (see FieldDescriptor.widget on the /resolve contract).
    for (var wd2 = 0; wd2 < wdDateWrappers.length; wd2++) {
      var wdw2 = wdDateWrappers[wd2];
      var wdAnchorEl = wdw2.maskedEl || wdw2.monthEl || wdw2.yearEl;
      if (wdAnchorEl.disabled || (wdw2.yearEl && wdw2.yearEl.disabled)) continue;
      var wdKind = wdw2.shape === 'y' ? 'wd-date-y' : 'wd-date-my';
      var wdId = 'f' + (counter++);
      var wdSection = getSectionContext(wdw2.wrapper);
      registry[wdId] = wdw2.shape === 'y'
        ? { kind: 'wd-date-y', yearEl: wdw2.yearEl, wrapper: wdw2.wrapper }
        : { kind: 'wd-date-my', monthEl: wdw2.monthEl, yearEl: wdw2.yearEl, maskedEl: wdw2.maskedEl, wrapper: wdw2.wrapper };
      fields.push({
        id: wdId,
        selector: buildSelector(wdAnchorEl),
        tag: 'input',
        type: 'text',
        name: wdAnchorEl.name || '',
        autocomplete: '',
        label: wdw2.label || '',
        placeholder: wdKind === 'wd-date-y' ? 'YYYY' : 'MM/YYYY',
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
   * Finds the index of the <select> option matching `text`, trying (in
   * order): exact text/value match, US state name<->code cross-match, then
   * a loose case-insensitive substring match. Returns -1 when nothing
   * matches. Shared by setSelectValue() and the read-back check inside it.
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

    // 3. case-insensitive contains (either direction).
    for (i = 0; i < el.options.length; i++) {
      var optText2 = cleanText(el.options[i].textContent).toLowerCase();
      if (optText2 && (optText2.indexOf(target) !== -1 || target.indexOf(optText2) !== -1)) return i;
    }
    return -1;
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
      match = elements.filter(function (r) {
        var l = cleanText(getLabel(r)).toLowerCase();
        return l && (l.indexOf(target) !== -1 || target.indexOf(l) !== -1);
      })[0];
    }
    if (!match) return false;
    // A native click is the most faithful simulation of a real user selecting a radio
    // button: it flips `checked`, unchecks its siblings, and fires click/input/change —
    // exactly what React's onChange handlers listen for.
    return safeClick(match);
  }

  function setCheckboxValue(el, boolLike) {
    var want = boolLike === true || /^(true|yes|1|on)$/i.test(String(boolLike));
    if (el.checked === want) return true;
    return safeClick(el);
  }

  /** Reads the field's current value, in the same shape applyFill expects to receive it back. */
  function getCurrentValue(entry) {
    if (entry.kind === 'radio-group') {
      var checked = entry.elements.filter(function (r) { return r.checked; })[0];
      return checked ? checked.value : '';
    }
    if (entry.kind === 'date-parts') return getDatePartsValue(entry);
    if (entry.kind === 'wd-date-my' || entry.kind === 'wd-date-y') return getWorkdayDateValue(entry);
    if (entry.kind === 'wd-dropdown') return cleanText(entry.button.textContent);
    if (entry.kind === 'wd-prompt') return getWorkdayPromptCurrentValue(entry);
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
      return setRadioValue(entry.elements, value);
    }
    if (entry.kind === 'date-parts') return setDatePartsValue(entry, value);
    if (entry.kind === 'wd-date-my' || entry.kind === 'wd-date-y') return setWorkdayDateValue(entry, value);
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
    var el = entry.el;
    if (el.tagName === 'SELECT') return setSelectValue(el, value);
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'checkbox') return setCheckboxValue(el, value);
    setNativeValue(el, value);
    return true;
  }

  /** The element(s) that should get the visual highlight for this field. */
  function getHighlightTargets(entry) {
    if (entry.kind === 'radio-group') return entry.elements.slice();
    if (entry.kind === 'date-parts') return [entry.monthEl, entry.yearEl];
    if (entry.kind === 'wd-date-my' || entry.kind === 'wd-date-y') return getWorkdayDateHighlightTargets(entry);
    if (entry.kind === 'wd-dropdown') return [entry.button];
    if (entry.kind === 'wd-prompt') return [entry.input];
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
    return '';
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

      if (target.isDropzone) {
        // Best-effort only: some dropzone widgets (react-dropzone and similar)
        // read e.dataTransfer.files from the drop event itself rather than
        // from the underlying input's change event. This can never downgrade
        // the verified result below, and any failure here is swallowed.
        try {
          var dzEl = findDropzoneContainer(el);
          var EventCtor = (view && (view.DragEvent || view.Event)) || (typeof Event !== 'undefined' ? Event : null);
          if (EventCtor && dzEl) {
            var dropEvt = new EventCtor('drop', { bubbles: true, cancelable: true });
            try { Object.defineProperty(dropEvt, 'dataTransfer', { value: dt }); } catch (eDef) { /* best effort */ }
            dzEl.dispatchEvent(dropEvt);
          }
        } catch (eDrop) {
          // best effort only — never affects the verified result below
        }
      }

      var attached = el.files && el.files[0];
      if (attached && attached.name === file.name) {
        return Promise.resolve({ attempted: true, attached: true, filename: attached.name });
      }

      // input.files came back empty — a plain dropzone widget may still need the drop
      // event too (independent of target.isDropzone above: a plain input[type=file] can sit
      // right next to a Workday-style dropzone), and Workday itself always ends up here
      // (it consumes the File and clears the input). Try the Workday drop zone, then fall
      // back to waiting briefly for Workday's own success markers before giving up.
      try {
        var wdDropzone = doc.querySelector && doc.querySelector('[data-automation-id="file-upload-drop-zone"]');
        if (wdDropzone) {
          var EventCtor2 = (view && (view.DragEvent || view.Event)) || (typeof Event !== 'undefined' ? Event : null);
          if (EventCtor2) {
            var dropEvt2 = new EventCtor2('drop', { bubbles: true, cancelable: true });
            try { Object.defineProperty(dropEvt2, 'dataTransfer', { value: dt }); } catch (eDef2) { /* best effort */ }
            wdDropzone.dispatchEvent(dropEvt2);
          }
        }
      } catch (eWdDrop) {
        // best effort only
      }

      return waitForWorkdayUploadSuccess(doc, file.name, 5000).then(function (found) {
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
    isWorkdaySpinnerInputSafe: isWorkdaySpinnerInputSafe,
    isWorkdayMaskedDateInputSafe: isWorkdayMaskedDateInputSafe,
    setWorkdaySpinnerValue: setWorkdaySpinnerValue,
    typeMaskedTextField: typeMaskedTextField,
    setWorkdayDateValue: setWorkdayDateValue,
    getWorkdayDateValue: getWorkdayDateValue,
    isWorkdayDropdownOpenerSafe: isWorkdayDropdownOpenerSafe,
    isWorkdayOptionSafe: isWorkdayOptionSafe,
    resolveWorkdayListbox: resolveWorkdayListbox,
    matchWorkdayDropdownOption: matchWorkdayDropdownOption,
    fillWorkdayDropdown: fillWorkdayDropdown,
    degreeFamilyOf: degreeFamilyOf,
    countryAliasOf: countryAliasOf,
    findWorkdayPrompts: findWorkdayPrompts,
    isWorkdayPromptInputSafe: isWorkdayPromptInputSafe,
    matchWorkdayPromptOption: matchWorkdayPromptOption,
    fillWorkdayPromptTerm: fillWorkdayPromptTerm,
    fillWorkdayPromptValue: fillWorkdayPromptValue,
    hasWorkdayHardDenyAutomationId: hasWorkdayHardDenyAutomationId
  };
});
