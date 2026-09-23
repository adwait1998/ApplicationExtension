/**
 * ApplyPilot Copilot — content script.
 *
 * Injected on demand (via chrome.scripting.executeScript, triggered by the popup) into the
 * active tab only — never registered as an always-on content script, so the extension never
 * touches a page the user hasn't explicitly invoked it on.
 *
 * Loaded AFTER scanner.js in the same isolated world, so `ApplyPilotScanner` is already a
 * global here. Nothing in this file ever calls form.submit(), clicks a submit button, or
 * navigates — see README.md "Safety invariants".
 *
 * Re-running this file on a page that already has it (e.g. the popup re-injecting) is safe:
 * everything below is guarded so a second injection reuses the existing state instead of
 * creating a second listener.
 */
(function () {
  'use strict';

  if (window.__applyPilotContentLoaded) {
    return; // already injected in this page; avoid double message listeners
  }
  window.__applyPilotContentLoaded = true;

  var HIGHLIGHT_ATTR = 'data-applypilot-highlighted';
  var ORIG_TITLE_ATTR = 'data-applypilot-orig-title';

  /** @type {Object<string, {kind:string, el?:Element, elements?:Element[]}>} */
  var registry = {};
  /** @type {Object<string, *>} field id -> value captured immediately before we filled it */
  var priorValues = {};
  /** @type {Element[]} everything we've highlighted, so undo/clear can find it without a registry lookup */
  var highlightedElements = [];

  function detect() {
    var scan = ApplyPilotScanner.scanAll(document);
    var hasForm = document.forms.length > 0;
    var looksLikeForm = scan.fields.length >= 3 || (hasForm && scan.fields.length >= 1);
    return {
      fieldCount: scan.fields.length,
      looksLikeForm: looksLikeForm,
      skippedFrames: scan.skippedFrames
    };
  }

  function scan() {
    var result = ApplyPilotScanner.scanAll(document);
    registry = result.registry;
    // Strip the live-element registry out of what we send back to the popup — only the
    // plain-JSON FieldDescriptors leave this context.
    return { fields: result.fields, skippedFrames: result.skippedFrames };
  }

  // 'filled' (green, solid) = a fact from the profile. 'draft' (blue, solid) = generated
  // text the operator must review before submitting — never let it look like a plain fill.
  // 'skipped' (amber, dashed) = left for the human.
  function highlight(el, kind, reason) {
    if (!el) return;
    if (!el.hasAttribute(HIGHLIGHT_ATTR)) {
      el.setAttribute(ORIG_TITLE_ATTR, el.getAttribute('title') || '');
      highlightedElements.push(el);
    }
    el.setAttribute(HIGHLIGHT_ATTR, kind);
    var outlineColor = kind === 'filled' ? '#22c55e' : (kind === 'draft' ? '#2563eb' : '#f59e0b');
    var outlineStyle = kind === 'skipped' ? 'dashed' : 'solid';
    var bgColor = kind === 'filled' ? 'rgba(34,197,94,0.08)' : (kind === 'draft' ? 'rgba(37,99,235,0.08)' : 'rgba(245,158,11,0.08)');
    el.style.setProperty('outline', '2px ' + outlineStyle + ' ' + outlineColor, 'important');
    el.style.setProperty('outline-offset', '1px', 'important');
    el.style.setProperty('background-color', bgColor, 'important');
    if (reason) el.setAttribute('title', reason);
  }

  function clearHighlight(el) {
    if (!el) return;
    var orig = el.getAttribute(ORIG_TITLE_ATTR);
    if (orig) el.setAttribute('title', orig);
    else el.removeAttribute('title');
    el.removeAttribute(ORIG_TITLE_ATTR);
    el.removeAttribute(HIGHLIGHT_ATTR);
    el.style.removeProperty('outline');
    el.style.removeProperty('outline-offset');
    el.style.removeProperty('background-color');
  }

  /**
   * Applies the /resolve response. Only entries with auto_fill:true are ever written to the
   * DOM. Everything else (skipped fields, and any fill the service marked auto_fill:false)
   * is highlighted amber for the human's attention but left untouched.
   */
  function applyFills(fills, skipped) {
    fills = fills || [];
    skipped = skipped || [];
    var applied = [];
    var failed = [];

    for (var i = 0; i < fills.length; i++) {
      var fill = fills[i];
      var entry = registry[fill.id];
      if (!entry) {
        failed.push({ id: fill.id, reason: 'Field no longer found on the page (did the page change after scanning?)' });
        continue;
      }
      if (!fill.auto_fill) {
        // The service found a candidate but didn't clear it for auto-fill (e.g. low
        // confidence). Treat exactly like a skip: highlight, never write.
        var targets = ApplyPilotScanner.getHighlightTargets(entry);
        for (var t = 0; t < targets.length; t++) highlight(targets[t], 'skipped', fill.reason || 'Not confident enough to auto-fill');
        continue;
      }
      try {
        priorValues[fill.id] = ApplyPilotScanner.getCurrentValue(entry);
        var ok = ApplyPilotScanner.applyFill(entry, fill.value);
        var hlTargets = ApplyPilotScanner.getHighlightTargets(entry);
        var isDraft = !!fill.draft || fill.source === 'draft';
        if (ok) {
          var reasonText = (fill.reason || 'Filled') + (fill.profile_key ? ' [' + fill.profile_key + ']' : '');
          if (isDraft) reasonText += ' — drafted — review before submitting';
          for (var h = 0; h < hlTargets.length; h++) highlight(hlTargets[h], isDraft ? 'draft' : 'filled', reasonText);
          applied.push({ id: fill.id, value: fill.value, reason: fill.reason, profile_key: fill.profile_key, source: fill.source, draft: isDraft });
        } else {
          for (var h2 = 0; h2 < hlTargets.length; h2++) highlight(hlTargets[h2], 'skipped', 'Could not match "' + fill.value + '" to an option');
          failed.push({ id: fill.id, reason: 'Could not match value "' + fill.value + '" to an option on the page' });
        }
      } catch (e) {
        failed.push({ id: fill.id, reason: 'Error while filling: ' + (e && e.message ? e.message : String(e)) });
      }
    }

    for (var s = 0; s < skipped.length; s++) {
      var skip = skipped[s];
      var sEntry = registry[skip.id];
      if (!sEntry) continue;
      var sTargets = ApplyPilotScanner.getHighlightTargets(sEntry);
      for (var st = 0; st < sTargets.length; st++) highlight(sTargets[st], 'skipped', skip.reason || 'Skipped — please answer this yourself');
    }

    return { applied: applied, failed: failed, skippedCount: skipped.length };
  }

  function undo() {
    var restored = 0;
    for (var id in priorValues) {
      if (!Object.prototype.hasOwnProperty.call(priorValues, id)) continue;
      var entry = registry[id];
      if (!entry) continue;
      try {
        ApplyPilotScanner.applyFill(entry, priorValues[id]);
        restored++;
      } catch (e) {
        // best-effort — still clear highlights below
      }
    }
    priorValues = {};
    for (var i = 0; i < highlightedElements.length; i++) clearHighlight(highlightedElements[i]);
    highlightedElements = [];
    return { restored: restored };
  }

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (!msg || typeof msg !== 'object') return false;
    try {
      if (msg.type === 'DETECT') {
        sendResponse(detect());
        return false;
      }
      if (msg.type === 'SCAN') {
        sendResponse(scan());
        return false;
      }
      if (msg.type === 'APPLY_FILLS') {
        sendResponse(applyFills(msg.fills, msg.skipped));
        return false;
      }
      if (msg.type === 'UNDO') {
        sendResponse(undo());
        return false;
      }
    } catch (e) {
      sendResponse({ error: String(e && e.message ? e.message : e) });
      return false;
    }
    return false;
  });
})();
