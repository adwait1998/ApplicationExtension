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

    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (el.disabled) continue;
      if (!isVisible(el)) continue;

      var tagLower = el.tagName.toLowerCase();

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
      registry[fid] = { kind: 'element', el: el };
      fields.push({
        id: fid,
        selector: selector,
        tag: tagLower,
        type: type,
        name: el.name || '',
        autocomplete: el.getAttribute('autocomplete') || '',
        label: getLabel(el),
        placeholder: el.placeholder || '',
        required: !!el.required,
        options: tagLower === 'select' ? getSelectOptions(el) : [],
        section: fieldSection.section,
        section_index: fieldSection.section_index
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

  function setSelectValue(el, text) {
    var target = String(text == null ? '' : text).trim().toLowerCase();
    var matchIndex = -1;
    var i;
    for (i = 0; i < el.options.length; i++) {
      var optText = cleanText(el.options[i].textContent).toLowerCase();
      var optValue = String(el.options[i].value).toLowerCase();
      if (optText === target || optValue === target) { matchIndex = i; break; }
    }
    if (matchIndex === -1 && target) {
      for (i = 0; i < el.options.length; i++) {
        var optText2 = cleanText(el.options[i].textContent).toLowerCase();
        if (optText2 && (optText2.indexOf(target) !== -1 || target.indexOf(optText2) !== -1)) {
          matchIndex = i;
          break;
        }
      }
    }
    if (matchIndex === -1) return false;
    var setter = nativeSetterFor(el, 'selectedIndex');
    if (setter) setter.call(el, matchIndex);
    else el.selectedIndex = matchIndex;
    fireEvents(el, ['input', 'change']);
    return true;
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
    var el = entry.el;
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'checkbox') return el.checked;
    return el.value;
  }

  /** Applies `value` to the field described by `entry` (an ApplyPilotScanner registry entry). */
  function applyFill(entry, value) {
    if (entry.kind === 'radio-group') {
      return setRadioValue(entry.elements, value);
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
    return [entry.el];
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
    getGroupLabel: getGroupLabel,
    getSectionContext: getSectionContext,
    extractSectionIndex: extractSectionIndex,
    buildSelector: buildSelector,
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
    findResumeFileTarget: findResumeFileTarget,
    attachResumeFile: attachResumeFile
  };
});
