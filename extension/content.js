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

  // ---------------------------------------------------------------------
  // repeating-section expansion — "Add Another" (Workday "My Experience" step)
  // ---------------------------------------------------------------------
  //
  // Runs ONLY as the first step of an explicit, user-initiated "Fill this page" (see scan()
  // below) — never on page load, never on DETECT. Asks the local service how many
  // work_history/education entries the profile actually has, then clicks "Add Another" just
  // enough times to make room for them before scanning, so the structured tier's existing
  // section_index -> work_history[index-1]/education[index-1] mapping has somewhere to write
  // block 2, 3, ... into. All the actual DOM/safety work (the guard, the click, counting
  // blocks, finding the right button) lives in scanner.js so it can be exercised offline in
  // jsdom (see selftest.js) exactly like every other scanning concern in this extension.

  var MAX_EXPANSION_CLICKS_TOTAL = 10; // hard cap across BOTH kinds combined, per fill
  var EXPANSION_WAIT_TIMEOUT_MS = 3000;

  /** Resolves true once `kind`'s block count exceeds `priorCount`, or false after the timeout. */
  function waitForBlockCountIncrease(kind, priorCount) {
    return new Promise(function (resolve) {
      var settled = false;
      var timer = null;
      var observer = null;
      function check() {
        return ApplyPilotScanner.countSectionBlocks(document, kind) > priorCount;
      }
      function finish(grew) {
        if (settled) return;
        settled = true;
        if (observer) observer.disconnect();
        if (timer) clearTimeout(timer);
        resolve(grew);
      }
      if (check()) { finish(true); return; }
      if (typeof MutationObserver !== 'undefined') {
        observer = new MutationObserver(function () { if (check()) finish(true); });
        observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
      }
      timer = setTimeout(function () { finish(check()); }, EXPANSION_WAIT_TIMEOUT_MS);
    });
  }

  /**
   * Clicks `kind`'s "Add Another" button, one block at a time, until `target` blocks exist,
   * the click budget runs out, or a click fails to add a block — in which case it stops
   * immediately rather than retrying blindly (see the spec's safety requirement).
   */
  function expandKindTo(kind, target, budget, startHref, log) {
    var current = ApplyPilotScanner.countSectionBlocks(document, kind);
    if (current >= target) return Promise.resolve();
    if (budget.remaining <= 0) {
      log.push(kind + ': stopped — reached the max expansion clicks for this fill');
      return Promise.resolve();
    }
    var btn = ApplyPilotScanner.findAddButtonForKind(document, kind);
    if (!btn) {
      log.push(kind + ': no safe "Add Another" button found (have ' + current + ', need ' + target + ')');
      return Promise.resolve();
    }
    budget.remaining--;
    budget.totalClicks++;
    var clicked = ApplyPilotScanner.safeClickAddButton(btn);
    if (!clicked) {
      log.push(kind + ': the safety guard refused the add button at the point of click');
      return Promise.resolve();
    }
    if (location.href !== startHref) {
      log.push(kind + ': stopped — navigation detected right after clicking the add button');
      return Promise.resolve();
    }
    return waitForBlockCountIncrease(kind, current).then(function (grew) {
      if (!grew) {
        log.push(kind + ': stopped — the click did not add a new block (never retried blindly)');
        return;
      }
      log.push(kind + ': expanded to ' + ApplyPilotScanner.countSectionBlocks(document, kind) + ' block(s)');
      return expandKindTo(kind, target, budget, startHref, log);
    });
  }

  function expandSections() {
    return chrome.runtime.sendMessage({ type: 'PROFILE_COUNTS' }).then(function (resp) {
      if (!resp || !resp.ok) {
        // Includes a 404 (service hasn't been upgraded yet), no token, or unreachable —
        // every case is treated the same: skip expansion entirely, fill as today.
        return { attempted: false, reason: (resp && resp.message) || 'profile counts unavailable' };
      }
      var counts = resp.data || {};
      var budget = { remaining: MAX_EXPANSION_CLICKS_TOTAL, totalClicks: 0 };
      var shieldFired = false;
      var log = [];
      var startHref = location.href;
      // Belt and braces for the whole expansion pass: even if the guard somehow let
      // something through, this stops the submit before it can do anything, and flags it.
      var removeShield = ApplyPilotScanner.installSubmitShield(document, function () { shieldFired = true; });

      function runKind(kinds, i) {
        if (i >= kinds.length) return Promise.resolve();
        var kind = kinds[i];
        var target = counts[kind];
        if (typeof target !== 'number' || target <= 0) return runKind(kinds, i + 1);
        return expandKindTo(kind, target, budget, startHref, log).then(function () { return runKind(kinds, i + 1); });
      }

      return runKind(['work_history', 'education'], 0).then(function () {
        removeShield();
        return { attempted: true, clicks: budget.totalClicks, shieldFired: shieldFired, log: log };
      }, function (e) {
        removeShield();
        return { attempted: true, clicks: budget.totalClicks, shieldFired: shieldFired, log: log, error: String(e && e.message ? e.message : e) };
      });
    }, function (e) {
      return { attempted: false, reason: 'Could not reach the background worker for profile counts: ' + (e && e.message ? e.message : e) };
    });
  }

  function scan() {
    return expandSections().then(function (expansion) {
      var result = ApplyPilotScanner.scanAll(document);
      registry = result.registry;
      // Strip the live-element registry out of what we send back to the popup — only the
      // plain-JSON FieldDescriptors (plus the expansion report) leave this context.
      return { fields: result.fields, skippedFrames: result.skippedFrames, expansion: expansion };
    });
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
   *
   * Fills are applied ONE AT A TIME, in order, awaiting each before starting the next — never
   * concurrently. Most widgets are synchronous (ApplyPilotScanner.applyFill() returns a plain
   * boolean for them, unchanged), but the two Workday popup widgets (wd-dropdown, wd-prompt)
   * return a Promise instead, since each has to open a popup and wait for Workday's own UI to
   * catch up before the next field is safe to touch (see scanner.js's applyFill()). Wrapping
   * every result in Promise.resolve() lets this loop treat both shapes identically.
   */
  function applyFills(fills, skipped) {
    fills = fills || [];
    skipped = skipped || [];
    var applied = [];
    var failed = [];

    function step(i) {
      if (i >= fills.length) return Promise.resolve();
      var fill = fills[i];
      var entry = registry[fill.id];
      if (!entry) {
        failed.push({ id: fill.id, reason: 'Field no longer found on the page (did the page change after scanning?)' });
        return step(i + 1);
      }
      if (!fill.auto_fill) {
        // The service found a candidate but didn't clear it for auto-fill (e.g. low
        // confidence). Treat exactly like a skip: highlight, never write.
        var targets = ApplyPilotScanner.getHighlightTargets(entry);
        for (var t = 0; t < targets.length; t++) highlight(targets[t], 'skipped', fill.reason || 'Not confident enough to auto-fill');
        return step(i + 1);
      }

      try {
        priorValues[fill.id] = ApplyPilotScanner.getCurrentValue(entry);
      } catch (e) {
        priorValues[fill.id] = undefined;
      }

      // Multi-value fields (currently just Skills) carry the individual values to add one at a
      // time in `fill.values` (a non-empty array) alongside a comma-joined `fill.value` for any
      // caller that only understands a single string — see schema.FillResult.values on the
      // service side. Prefer `values` whenever the service sent it.
      var fillValue = (Array.isArray(fill.values) && fill.values.length) ? fill.values : fill.value;

      return Promise.resolve()
        .then(function () { return ApplyPilotScanner.applyFill(entry, fillValue); })
        .then(function (ok) {
          var hlTargets = ApplyPilotScanner.getHighlightTargets(entry);
          var isDraft = !!fill.draft || fill.source === 'draft';
          if (ok) {
            var reasonText = (fill.reason || 'Filled') + (fill.profile_key ? ' [' + fill.profile_key + ']' : '');
            if (isDraft) reasonText += ' — drafted — review before submitting';
            for (var h = 0; h < hlTargets.length; h++) highlight(hlTargets[h], isDraft ? 'draft' : 'filled', reasonText);
            applied.push({ id: fill.id, value: fill.value, reason: fill.reason, profile_key: fill.profile_key, source: fill.source, draft: isDraft });
          } else {
            // entry._lastReason is set by scanner.js's applyFill() for the Workday popup
            // widgets (e.g. "no confident match for ... among dropdown options") — surface it
            // when present rather than only the generic message.
            var extra = entry._lastReason ? (' — ' + entry._lastReason) : '';
            for (var h2 = 0; h2 < hlTargets.length; h2++) highlight(hlTargets[h2], 'skipped', 'Could not match "' + fill.value + '" to an option' + extra);
            failed.push({ id: fill.id, reason: 'Could not match value "' + fill.value + '" to an option on the page' + extra });
          }
        }, function (e) {
          failed.push({ id: fill.id, reason: 'Error while filling: ' + (e && e.message ? e.message : String(e)) });
        })
        .then(function () { return step(i + 1); });
    }

    return step(0).then(function () {
      for (var s = 0; s < skipped.length; s++) {
        var skip = skipped[s];
        var sEntry = registry[skip.id];
        if (!sEntry) continue;
        var sTargets = ApplyPilotScanner.getHighlightTargets(sEntry);
        for (var st = 0; st < sTargets.length; st++) highlight(sTargets[st], 'skipped', skip.reason || 'Skipped — please answer this yourself');
      }
      return { applied: applied, failed: failed, skippedCount: skipped.length };
    });
  }

  function base64ToUint8Array(base64) {
    var binary = atob(base64);
    var len = binary.length;
    var bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  /**
   * Best-effort résumé attachment, run as part of applyFills() — i.e. only ever
   * as a step inside an explicit, user-initiated "fill this page" action, never
   * on page load (see file header). Fails soft at every step: no résumé-shaped
   * field on the page, no service reachable, no résumé stored, or a widget that
   * rejects programmatic assignment all resolve to a reported (never thrown)
   * failure. The target gets the same highlight() treatment as a normal
   * fill/skip so it's visible on the page, not just in the popup's summary.
   */
  function maybeAttachResume() {
    // Cheap local check first — skip the round-trip to the background worker
    // (and the local service) entirely when there's nowhere on this page to
    // put a résumé.
    var target = ApplyPilotScanner.findResumeFileTarget(document);
    if (!target) return Promise.resolve({ attempted: false });

    // `errorCode` mirrors background.js's callResume() error codes (e.g.
    // "no-resume") so popup.js can tell "nothing is stored yet — go add one"
    // apart from a generic connectivity/attachment failure, without the two
    // of them having to agree on parsing reason text.
    function fail(reason, errorCode) {
      highlight(target.el, 'skipped', 'Résumé not attached — ' + reason);
      return { attempted: true, attached: false, reason: reason, errorCode: errorCode || '' };
    }

    return chrome.runtime.sendMessage({ type: 'GET_RESUME' }).then(function (resp) {
      if (!resp || !resp.ok) {
        return fail((resp && resp.message) || 'Could not reach the local service for the résumé file.', resp && resp.error);
      }
      var d = resp.data || {};
      var file;
      try {
        var bytes = base64ToUint8Array(d.base64 || '');
        file = new File([bytes], d.filename || 'resume.pdf', { type: d.contentType || 'application/octet-stream' });
      } catch (e) {
        return fail('Could not decode the résumé file from the service response.');
      }

      // attachResumeFile() now always returns a Promise: on a real Workday page it consumes
      // the File and empties input.files right away, so the only honest way to verify success
      // is a short wait for Workday's own upload-confirmation markers (see scanner.js) —
      // that's async, so this whole path has to be too.
      return ApplyPilotScanner.attachResumeFile(document, file).then(function (result) {
        if (result.attached) {
          highlight(target.el, 'filled', 'Résumé attached: ' + result.filename);
        } else if (result.alreadyAttached) {
          // Duplicate prevention: a résumé was already shown as attached, so nothing was
          // touched — this is a success state, not a failure, and must not be re-uploaded.
          highlight(target.el, 'filled', 'Résumé already attached: ' + (result.filename || ''));
        } else if (result.attempted) {
          highlight(target.el, 'skipped', 'Résumé not attached — ' + (result.reason || 'unknown error'));
        }
        return result;
      });
    }, function (e) {
      return fail('Could not reach the background worker for the résumé file: ' + (e && e.message ? e.message : e));
    });
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

    if (msg.type === 'APPLY_FILLS') {
      // Belt and braces for the WHOLE fill, not just section expansion: every one of the new
      // Workday interaction paths (dropdown opener, option, prompt result, date spinner) is a
      // dispatched event that could, if some future bug mis-targeted it, land on a submit
      // control. Installed before the first field is touched and removed in a `finally`
      // (the .then(...).then(...) pair below, run unconditionally) so it spans every fill,
      // the résumé attachment, and any error path. Reuses the SAME shieldFired channel the
      // popup already surfaces for section expansion (see popup.js).
      var shieldFired = false;
      var removeShield = ApplyPilotScanner.installSubmitShield(document, function () { shieldFired = true; });

      // Async: applying the field fills themselves is mostly synchronous, but the new Workday
      // popup widgets (dropdown/prompt) each wait for their own popup, and the résumé step
      // needs a round-trip to the background worker for the file bytes (see
      // maybeAttachResume). Returning true below keeps the message channel open for this
      // promise chain.
      Promise.resolve()
        .then(function () { return applyFills(msg.fills, msg.skipped); })
        .then(function (result) {
          return maybeAttachResume().then(function (resume) {
            result.resume = resume;
            return result;
          });
        })
        .then(function (result) {
          result.shieldFired = shieldFired;
          return result;
        })
        .catch(function (e) {
          return { error: String(e && e.message ? e.message : e), shieldFired: shieldFired };
        })
        .then(function (result) {
          removeShield();
          sendResponse(result);
        });
      return true;
    }

    if (msg.type === 'SCAN') {
      // Async: scan() first runs expandSections() (a round-trip to the background worker
      // for /profile/counts, plus however many click-and-wait cycles it takes), THEN scans.
      // Returning true below keeps the message channel open for this promise chain.
      scan().then(sendResponse, function (e) {
        sendResponse({ error: String(e && e.message ? e.message : e) });
      });
      return true;
    }

    try {
      if (msg.type === 'DETECT') {
        sendResponse(detect());
        return false;
      }
      if (msg.type === 'UNDO') {
        sendResponse(undo());
        return false;
      }
      if (msg.type === 'CAPTURE') {
        // Structure only — see capture.js for the no-values invariant.
        var cap = self.ApplyPilotCapture;
        sendResponse(cap ? { ok: true, structure: cap.captureStructure(document) }
                         : { ok: false, error: 'capture.js not loaded' });
        return false;
      }
    } catch (e) {
      sendResponse({ error: String(e && e.message ? e.message : e) });
      return false;
    }
    return false;
  });
})();
