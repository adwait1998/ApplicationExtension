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

    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (el.disabled) continue;
      if (consumedByDatePair.indexOf(el) !== -1) continue;

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
          section_index: radioSection.section_index
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
        section_index: fieldSection.section_index
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
        section_index: dateSection.section_index
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
    var el = entry.el;
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'checkbox') return el.checked;
    return el.value;
  }

  /** Applies `value` to the field described by `entry` (an ApplyPilotScanner registry entry). */
  function applyFill(entry, value) {
    if (entry.kind === 'radio-group') {
      return setRadioValue(entry.elements, value);
    }
    if (entry.kind === 'date-parts') return setDatePartsValue(entry, value);
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
   * Attaches `file` to the page's résumé target and VERIFIES it stuck before
   * reporting success — a silent failure here is worse than a skip (the
   * operator would submit with no résumé attached). Never throws: assigning
   * to a file input's `.files` is the one DOM write in this whole module a
   * site's own JS can reject outright, so every failure mode reports rather
   * than propagates.
   *
   * Never opens a native file picker and never calls .click() on anything —
   * the file is assigned programmatically via DataTransfer, full stop. That
   * keeps this function outside isClickSafe()'s guard entirely, on purpose:
   * there is no click path here to protect against turning into a submit.
   *
   * Returns:
   *   { attempted: false }                                 — no résumé-eligible
   *                                                            file input on the page
   *   { attempted: true, attached: true, filename }         — verified via read-back
   *   { attempted: true, attached: false, reason }          — tried and failed, or
   *                                                            could not verify
   */
  function attachResumeFile(doc, file) {
    var target = findResumeFileTarget(doc);
    if (!target) return { attempted: false };

    var el = target.el;
    var view = realmOf(el);
    var DataTransferCtor = view && view.DataTransfer;
    if (!DataTransferCtor) {
      // Real Chrome always has this. Only a test/runtime environment without a
      // full DOM (e.g. jsdom, which has no DataTransfer/DragEvent) lands here —
      // fail soft, never throw. See selftest.js for what that guard exercises.
      return { attempted: true, attached: false, reason: 'DataTransfer is not available in this browsing context.' };
    }

    try {
      var dt = new DataTransferCtor();
      dt.items.add(file);

      try {
        el.files = dt.files;
      } catch (eAssign) {
        return { attempted: true, attached: false, reason: 'Site rejected programmatic file assignment: ' + (eAssign && eAssign.message ? eAssign.message : eAssign) };
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
        return { attempted: true, attached: true, filename: attached.name };
      }
      return {
        attempted: true,
        attached: false,
        reason: 'Assigned the file but could not confirm it stuck (input.files[0] was ' +
          (attached ? ('"' + attached.name + '"') : 'empty') + ').'
      };
    } catch (e) {
      return { attempted: true, attached: false, reason: 'Error while attaching résumé: ' + (e && e.message ? e.message : String(e)) };
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
    attachResumeFile: attachResumeFile
  };
});
