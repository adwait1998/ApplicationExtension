/**
 * ApplyPilot Copilot — "Report this page" structure capture.
 *
 * Why this exists: the extension's handling of Workday's custom widgets was
 * built twice from guesses about Workday's markup. A mock page built from those
 * guesses passed every test while the real form still failed. Real markup beats
 * a third guess, so this exports the STRUCTURE of the form — tags, roles, ARIA
 * attributes, data-automation-id, labels, option lists — for the operator to
 * send back when a page fills badly.
 *
 * PRIVACY — the invariant this file exists to keep: it NEVER reads a control's
 * current value. No `.value`, no `.checked`, no `.files`, no textContent of an
 * editable control. Everything captured is the page's own furniture (labels,
 * button text, option lists, attribute names), not what the applicant typed or
 * what the extension filled. Emails, phone numbers and long digit runs are also
 * scrubbed from any captured text, in case a site echoes a value into a label.
 *
 * Runs in the content-script world like scanner.js. Exposes
 * `ApplyPilotCapture.captureStructure(document)`.
 */
(function (root) {
  'use strict';

  var MAX_NODES = 2500;
  var MAX_TEXT = 120;
  var ANCESTOR_DEPTH = 6;

  var SELECTOR = [
    'input', 'select', 'textarea', 'button', 'label', 'legend', 'fieldset',
    '[role]', '[data-automation-id]', '[aria-haspopup]', '[contenteditable="true"]',
    'h1', 'h2', 'h3', 'h4',
  ].join(',');

  var ARIA_ATTRS = ['aria-label', 'aria-labelledby', 'aria-describedby', 'aria-haspopup',
    'aria-expanded', 'aria-controls', 'aria-owns', 'aria-autocomplete', 'aria-required',
    'aria-invalid', 'aria-hidden', 'aria-disabled', 'aria-selected', 'aria-activedescendant'];

  // Values the page might echo into a label or button; never let one leave.
  var EMAIL_RE = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi;
  var DIGITS_RE = /\+?\d[\d\s().-]{6,}\d/g;

  function scrub(s) {
    if (!s) return '';
    s = String(s).replace(/\s+/g, ' ').trim();
    s = s.replace(EMAIL_RE, '<email>').replace(DIGITS_RE, '<digits>');
    return s.length > MAX_TEXT ? s.slice(0, MAX_TEXT) + '…' : s;
  }

  function isEditable(el) {
    var t = el.tagName;
    return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' ||
      el.getAttribute('contenteditable') === 'true';
  }

  // Text of NON-editable furniture only (buttons, options, headings, labels).
  function furnitureText(el) {
    if (isEditable(el)) return '';
    var t = el.tagName;
    var role = el.getAttribute('role');
    var worthReading = t === 'BUTTON' || t === 'LABEL' || t === 'LEGEND' || /^H[1-4]$/.test(t) ||
      role === 'option' || role === 'button' || role === 'heading' || role === 'tab' ||
      role === 'menuitem' || role === 'listbox' || el.hasAttribute('data-automation-id');
    if (!worthReading) return '';
    // Skip containers whose text would sweep up a whole form section.
    if (el.querySelector && el.querySelector('input, textarea, select')) return '';
    return scrub(el.textContent);
  }

  function describe(el) {
    var d = { tag: el.tagName.toLowerCase() };
    var type = el.getAttribute('type');
    if (type) d.type = type;
    var role = el.getAttribute('role');
    if (role) d.role = role;
    var auto = el.getAttribute('data-automation-id');
    if (auto) d.automationId = auto;
    if (el.id) d.id = el.id;
    var name = el.getAttribute('name');
    if (name) d.name = name;
    if (el.className && typeof el.className === 'string') d.cls = el.className.slice(0, 80);
    var ph = el.getAttribute('placeholder');
    if (ph) d.placeholder = scrub(ph);
    var aria = {};
    for (var i = 0; i < ARIA_ATTRS.length; i++) {
      var v = el.getAttribute(ARIA_ATTRS[i]);
      if (v !== null) aria[ARIA_ATTRS[i].replace('aria-', '')] = scrub(v);
    }
    if (Object.keys(aria).length) d.aria = aria;
    return d;
  }

  function ancestorPath(el) {
    var path = [];
    var node = el.parentElement;
    for (var i = 0; i < ANCESTOR_DEPTH && node && node.tagName !== 'BODY'; i++) {
      var bit = node.tagName.toLowerCase();
      var auto = node.getAttribute('data-automation-id');
      var role = node.getAttribute('role');
      if (auto) bit += '[data-automation-id=' + auto + ']';
      if (role) bit += '[role=' + role + ']';
      path.push(bit);
      node = node.parentElement;
    }
    return path;
  }

  function captureStructure(doc) {
    doc = doc || document;
    var scanner = root.ApplyPilotScanner;
    var nodes = [];
    var els = doc.querySelectorAll(SELECTOR);
    var truncated = els.length > MAX_NODES;
    for (var i = 0; i < els.length && i < MAX_NODES; i++) {
      var el = els[i];
      var d = describe(el);
      var text = furnitureText(el);
      if (text) d.text = text;
      if (isEditable(el) && scanner && scanner.getLabel) {
        try { d.label = scrub(scanner.getLabel(el)); } catch (e) { /* best-effort */ }
      }
      if (el.tagName === 'SELECT') {
        // The option LIST is the form's own; the SELECTED option is the user's.
        d.options = Array.prototype.slice.call(el.options, 0, 60).map(function (o) {
          return scrub(o.text);
        });
      }
      try {
        d.visible = scanner && scanner.isVisible ? scanner.isVisible(el) : undefined;
      } catch (e) { /* best-effort */ }
      d.path = ancestorPath(el);
      nodes.push(d);
    }
    return {
      kind: 'applypilot-page-structure',
      version: 1,
      capturedAt: new Date().toISOString(),
      host: doc.location ? doc.location.host : '',
      path: doc.location ? doc.location.pathname : '',
      title: scrub(doc.title),
      nodeCount: nodes.length,
      truncated: truncated,
      note: 'Structure only: no field values, checked states or files are captured.',
      nodes: nodes,
    };
  }

  root.ApplyPilotCapture = { captureStructure: captureStructure, scrub: scrub };
})(typeof self !== 'undefined' ? self : this);
