/**
 * ApplyPilot Copilot — content script.
 *
 * Injected on demand (via chrome.scripting.executeScript, triggered by the side panel) into
 * every frame the operator has granted host permission for — never registered as an always-on
 * content script, so the extension never touches a page the user hasn't explicitly invoked it
 * on (injection happens only when the operator clicks "Fill this page" or "Report page" in the
 * panel — never on tab switch, never on panel open).
 *
 * Loaded AFTER scanner.js in the same isolated world, so `ApplyPilotScanner` is already a
 * global here. Nothing in this file ever calls form.submit(), clicks a submit button, or
 * navigates — see README.md "Safety invariants".
 *
 * Re-running this file on a page that already has it (e.g. the panel re-injecting) is safe:
 * everything below is guarded so a second injection reuses the existing state instead of
 * creating a second listener.
 *
 * FILL EVERY FRAME: this same file is injected into EVERY frame the operator granted (the
 * tab's own top-level document plus every same- or cross-origin iframe background.js could
 * reach), one independent instance per frame, each with its own registry/priorValues/shield.
 * background.js is the cross-frame coordinator (only an extension page — never a content
 * script — can chrome.tabs.sendMessage a specific frameId), and it is what makes this work
 * feel like ONE fill instead of N: it asks every frame to PREPARE_AND_SCAN itself, merges all
 * of their fields into a single /resolve call, then hands each frame back only its own slice
 * of the answer via APPLY_FILLS. See background.js's file header for the full sequence.
 *
 * DETACHED FILL / PANEL-INDEPENDENCE: the side panel can be closed, its window can lose focus,
 * or the user can switch tabs at any moment — none of that may interrupt a fill in progress.
 * So each frame's own scan -> expand -> résumé -> apply pipeline lives entirely in THIS file
 * and is driven by messages FROM background.js (never awaited by the panel). Progress and the
 * final result are reported by messaging the background worker (chrome.runtime.sendMessage —
 * this works whether or not any panel/popup is open, because it targets the extension's own
 * service worker, not a UI page), which merges every frame's report into one per-tab result
 * and persists it into chrome.storage.session keyed by this tab's id. The panel only ever
 * READS that storage (plus chrome.storage.onChanged for live updates); it never depends on
 * staying open, and it never writes the fill-result state itself.
 *
 * RÉSUMÉ FIRST: each frame attaches its own résumé (if it has a résumé-shaped file input at
 * all — most frames won't) and waits for the page to settle BEFORE scanning, not after filling
 * everything else. Some ATSs (Workday, Lever) parse an uploaded résumé and repopulate/overwrite
 * form fields shortly after upload; scanning and filling first would mean those fields get
 * silently clobbered the moment the résumé parse lands. See prepareAndScan() below.
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

  // A fill "never depends on the panel staying open", but exactly one fill runs at a time per
  // FRAME. `activeRun` is non-null from the moment PREPARE_AND_SCAN starts until this frame's
  // participation in the fill is fully finished (see finishRun()); CANCEL_FILL flips its
  // `cancelled` flag, which every loop below (expansion, résumé settle, the apply loop) checks
  // between steps/fields/items.
  var activeRun = null;
  var runCounter = 0;

  // Bridges PREPARE_AND_SCAN -> the later APPLY_FILLS message for this frame's current run:
  // { state, run, removeShield, startedAt, fieldsById }. Non-null for exactly as long as this
  // frame has told background.js "I scanned N fields" and is waiting to be told what to do
  // with them (or has since been told and is actively applying them).
  var currentPrepare = null;

  // Defaults per the spec: no single field may stall the fill for more than ~12s, and the
  // whole fill gives up on remaining (not-yet-attempted) fields after ~120s. Both are
  // overridable ONLY via an explicit message field — used exclusively by
  // scripts/chrome_panel_test.py so it can prove the timeout/budget paths in well under Chrome
  // launch time, and documented as test-only in extension/README.md. Absent, behavior is
  // exactly the production default.
  var DEFAULT_FIELD_TIMEOUT_MS = 12000;
  var DEFAULT_BUDGET_MS = 120000;

  // How long to wait for the page to go quiet after a résumé attach before scanning (see
  // "RÉSUMÉ FIRST" above) when there is no more specific upload-confirmation signal available
  // (attachResumeFile() already waits on Workday's own confirmation marker internally — this is
  // the generic fallback for everything else, e.g. Lever/Greenhouse résumé parsing that
  // repopulates name/email/phone a moment after upload).
  var DOM_QUIET_MS = 1200;
  var DOM_QUIET_MAX_MS = 3000;

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
  // Runs ONLY as the first step of this frame's participation in an explicit, user-initiated
  // fill (see prepareAndScan() below) — never on page load, never on DETECT. Asks the local
  // service how many work_history/education entries the profile actually has, then clicks "Add
  // Another" just enough times to make room for them before scanning, so the structured tier's
  // existing section_index -> work_history[index-1]/education[index-1] mapping has somewhere to
  // write block 2, 3, ... into. All the actual DOM/safety work (the guard, the click, counting
  // blocks, finding the right button) lives in scanner.js so it can be exercised offline in
  // jsdom (see selftest.js) exactly like every other scanning concern in this extension.

  var MAX_EXPANSION_CLICKS_TOTAL = 10; // hard cap across BOTH kinds combined, per fill, per frame
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
      var log = [];
      var startHref = location.href;

      function runKind(kinds, i) {
        if (i >= kinds.length) return Promise.resolve();
        var kind = kinds[i];
        var target = counts[kind];
        if (typeof target !== 'number' || target <= 0) return runKind(kinds, i + 1);
        return expandKindTo(kind, target, budget, startHref, log).then(function () { return runKind(kinds, i + 1); });
      }

      return runKind(['work_history', 'education'], 0).then(function () {
        return { attempted: true, clicks: budget.totalClicks, log: log };
      }, function (e) {
        return { attempted: true, clicks: budget.totalClicks, log: log, error: String(e && e.message ? e.message : e) };
      });
    }, function (e) {
      return { attempted: false, reason: 'Could not reach the background worker for profile counts: ' + (e && e.message ? e.message : e) };
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

  // ---------------------------------------------------------------------
  // timeouts, cancellation
  // ---------------------------------------------------------------------

  /**
   * Races `promise` against a timer. On timeout, rejects with an Error whose `isTimeout` is
   * true — the original `promise` is deliberately NOT abandoned to the void: callers attach a
   * no-op handler to it so a widget that eventually does resolve/reject long after we've moved
   * on never surfaces as an "Uncaught (in promise)" console error. This is what stands between
   * one stuck Workday widget and a fill that hangs forever (the bug report this build fixes).
   */
  function withTimeout(promise, ms) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        var err = new Error('timed out after ' + ms + 'ms');
        err.isTimeout = true;
        reject(err);
      }, ms);
      promise.then(function (v) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(v);
      }, function (e) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(e);
      });
    });
  }

  /** Runs a (possibly-Promise-returning) applyFill call under the per-field timeout. */
  function runFieldAttempt(entry, value, timeoutMs) {
    var raw = Promise.resolve().then(function () { return ApplyPilotScanner.applyFill(entry, value); });
    raw.then(function () {}, function () {}); // swallow a late settle after we stop waiting
    return withTimeout(raw, timeoutMs);
  }

  // ---------------------------------------------------------------------
  // never overwrite the user — see README "Never overwrite the user". Before writing a field,
  // if it already holds a non-empty value that differs from what we're about to write, we skip
  // it with reason "kept your value" instead of clobbering it (Workday prefills several fields
  // from the operator's account profile; the operator may also have typed into the form before
  // clicking Fill).
  //
  // Deliberately scoped to the plain 'element' (input/select/textarea) and 'radio-group' entry
  // kinds only, where getCurrentValue() returning "empty" is unambiguous. The Workday popup
  // widgets are excluded on purpose:
  //   - wd-dropdown's un-opened button often shows non-empty PLACEHOLDER text ("Select One"),
  //     which is not a real answer and must not be mistaken for one;
  //   - wd-prompt (Field of Study, Skills, ...) is ADDITIVE — typing a new term never erases an
  //     existing one — so "already has a value" is never a reason to skip adding more.
  // ---------------------------------------------------------------------
  var USER_VALUE_PROTECTED_KINDS = { element: true, 'radio-group': true };

  function isMeaningfulExistingValue(value) {
    if (typeof value === 'boolean') return value === true;
    return typeof value === 'string' && value.trim() !== '';
  }

  /** Every underlying DOM element a registry entry actually touches. */
  function entryElements(entry) {
    var els = [];
    if (entry.el) els.push(entry.el);
    if (entry.highlightEl) els.push(entry.highlightEl);
    if (entry.elements) els = els.concat(entry.elements);
    if (entry.monthEl) els.push(entry.monthEl);
    if (entry.yearEl) els.push(entry.yearEl);
    if (entry.maskedEl) els.push(entry.maskedEl);
    if (entry.button) els.push(entry.button);
    if (entry.input) els.push(entry.input);
    return els;
  }

  // ---------------------------------------------------------------------
  // "kept your value" must protect ONLY a value that predates OUR OWN résumé attach — not one
  // the résumé's own parsing just produced. Lever/Ashby/Workday all parse an uploaded résumé and
  // can repopulate (sometimes wrongly) fields like name/email/phone/experience a moment after
  // upload; since "RÉSUMÉ FIRST" (see file header) attaches it before scanning, a naive "is the
  // current value non-empty and different" check would mistake the ATS's own guess for the
  // operator's own input and protect it from ever being corrected by the real profile value.
  //
  // Two independent signals decide "this predates our own work, so it's real user input":
  //   1. a snapshot of every field's value taken BEFORE expansion/résumé/scanning even started
  //      (snapshotValuesByElement() below, keyed by the actual DOM element so it still lines up
  //      even if the résumé attach reshuffled field order or ids);
  //   2. a genuine, browser-trusted input/change event on that element at ANY point during this
  //      run (installUserInputTracker() below) — catches the operator typing into the form
  //      WHILE our own expand/résumé/settle steps are still running, a window the one-time
  //      snapshot alone can't see into. Our own programmatic fills never set Event.isTrusted, so
  //      this can never mistake our own write for the operator's.
  // ---------------------------------------------------------------------

  /** element -> its value at the moment this was taken (see getCurrentValue's own shape). */
  function snapshotValuesByElement(scanResult) {
    var map = new Map();
    var reg = (scanResult && scanResult.registry) || {};
    for (var id in reg) {
      if (!Object.prototype.hasOwnProperty.call(reg, id)) continue;
      var entry = reg[id];
      var value;
      try { value = ApplyPilotScanner.getCurrentValue(entry); } catch (e) { continue; }
      var els = entryElements(entry);
      for (var i = 0; i < els.length; i++) map.set(els[i], value);
    }
    return map;
  }

  var userTouchedElements = (typeof WeakSet !== 'undefined') ? new WeakSet() : null;
  var userInputTrackerInstalled = false;

  /** Records every element that gets a real (isTrusted) input/change event while installed. */
  function installUserInputTracker(doc) {
    if (userInputTrackerInstalled || !userTouchedElements) return function () {};
    userInputTrackerInstalled = true;
    function onUserInput(e) {
      if (!e || !e.isTrusted || !e.target) return; // our own writes are never isTrusted
      userTouchedElements.add(e.target);
    }
    doc.addEventListener('input', onUserInput, true);
    doc.addEventListener('change', onUserInput, true);
    return function removeUserInputTracker() {
      doc.removeEventListener('input', onUserInput, true);
      doc.removeEventListener('change', onUserInput, true);
      userInputTrackerInstalled = false;
    };
  }

  /** True if `entry` held a value before our own résumé attach, or the operator typed into it. */
  function isProtectedByPriorUserActivity(entry, preResumeValues) {
    var els = entryElements(entry);
    for (var i = 0; i < els.length; i++) {
      if (userTouchedElements && userTouchedElements.has(els[i])) return true;
      if (preResumeValues && preResumeValues.has(els[i]) && isMeaningfulExistingValue(preResumeValues.get(els[i]))) return true;
    }
    return false;
  }

  /** Loose equality between a getCurrentValue() reading and a /resolve fill value/values. */
  function isSameValue(existing, target) {
    if (Array.isArray(target)) {
      var ex = String(existing == null ? '' : existing).toLowerCase();
      return target.every(function (v) { return ex.indexOf(String(v).toLowerCase()) !== -1; });
    }
    if (typeof existing === 'boolean') {
      var want = target === true || /^(true|yes|1|on)$/i.test(String(target));
      return existing === want;
    }
    return String(existing == null ? '' : existing).trim().toLowerCase() ===
      String(target == null ? '' : target).trim().toLowerCase();
  }

  // ---------------------------------------------------------------------
  // applying fills — one field at a time, with live progress, cancellation and per-field
  // timeouts. Fills are applied ONE AT A TIME, in order, awaiting each before starting the
  // next — never concurrently. Most widgets are synchronous (ApplyPilotScanner.applyFill()
  // returns a plain boolean for them), but the Workday popup widgets (wd-dropdown, wd-prompt)
  // return a Promise instead, since each has to open a popup and wait for Workday's own UI to
  // catch up before the next field is safe to touch (see scanner.js's applyFill()).
  // ---------------------------------------------------------------------

  /**
   * Skills-shaped fields (`fill.values` is an array with >1 entries, on a `wd-prompt` entry)
   * are applied ONE TERM AT A TIME from here — rather than handing the whole array to
   * scanner.js's applyFill() in one call — purely so this file can check cancellation, apply a
   * timeout, and report "adding N/M" progress between items. scanner.js's own
   * fillWorkdayPromptValue() already supports being called with a single term (that's exactly
   * what it does internally for every non-array value), so calling it once per term here is
   * behaviourally identical to the single array call, just observable from the outside.
   */
  function applyMultiValueWithProgress(entry, values, label, fieldIndex, fieldTotal, ctx) {
    var anyOk = false;
    var lastReason = '';
    function next(i) {
      if (i >= values.length) return Promise.resolve();
      if (ctx.isCancelled() || ctx.overBudget()) return Promise.resolve();
      ctx.onProgress({ current: fieldIndex + 1, total: fieldTotal, label: label + ' (adding ' + (i + 1) + '/' + values.length + ')' });
      return runFieldAttempt(entry, values[i], ctx.fieldTimeoutMs).then(function (ok) {
        if (ok) anyOk = true; else lastReason = entry._lastReason || lastReason;
        return next(i + 1);
      }, function (e) {
        lastReason = (e && e.isTimeout) ? ('timed out adding "' + values[i] + '"') : (entry._lastReason || String(e && e.message ? e.message : e));
        return next(i + 1); // one stuck/failed item must not block the rest of the list
      });
    }
    return next(0).then(function () {
      entry._lastReason = lastReason;
      return anyOk; // matches fillWorkdayPromptValue()'s own "at least one added" success rule
    });
  }

  /**
   * @param fields Object<string, FieldDescriptor> — id -> the originally-scanned descriptor,
   *   used only to get a human-readable label for progress text and the "needs you" list.
   * @param ctx { isCancelled(), overBudget(), fieldTimeoutMs, onProgress(progress) }
   */
  function applyFills(fills, skipped, fieldsById, ctx) {
    fills = fills || [];
    skipped = skipped || [];
    fieldsById = fieldsById || {};
    var applied = [];
    var failed = [];
    var needsYou = [];
    var total = fills.length;

    function labelFor(fill) {
      var f = fieldsById[fill.id];
      return (f && (f.label || f.name)) || ('field #' + fill.id);
    }

    function step(i) {
      if (i >= fills.length) return Promise.resolve();

      if (ctx.isCancelled()) {
        for (var c = i; c < fills.length; c++) failed.push({ id: fills[c].id, reason: 'Not attempted — cancelled' });
        return Promise.resolve();
      }
      if (ctx.overBudget()) {
        for (var b = i; b < fills.length; b++) failed.push({ id: fills[b].id, reason: 'Not attempted — time budget exceeded' });
        return Promise.resolve();
      }

      var fill = fills[i];
      var entry = registry[fill.id];
      var label = labelFor(fill);
      ctx.onProgress({ current: i + 1, total: total, label: label });

      if (!entry) {
        failed.push({ id: fill.id, reason: 'Field no longer found on the page (did the page change after scanning?)' });
        return step(i + 1);
      }
      if (!fill.auto_fill) {
        // The service found a candidate but didn't clear it for auto-fill (e.g. low
        // confidence). Treat exactly like a skip: highlight, never write.
        var targets = ApplyPilotScanner.getHighlightTargets(entry);
        for (var t = 0; t < targets.length; t++) highlight(targets[t], 'skipped', fill.reason || 'Not confident enough to auto-fill');
        needsYou.push({ id: fill.id, label: label, reason: fill.reason || 'Not confident enough to auto-fill', tag: (fieldsById[fill.id] || {}).tag });
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
      // service side.
      var isMultiValue = Array.isArray(fill.values) && fill.values.length > 1 && entry.kind === 'wd-prompt';
      var fillValue = (Array.isArray(fill.values) && fill.values.length) ? fill.values : fill.value;

      // Never overwrite the user: a non-empty existing value that (a) predates our own résumé
      // attach or was typed by the operator at any point during this run — see
      // isProtectedByPriorUserActivity()'s doc comment — and (b) differs from what we're about
      // to write, is left exactly as it was. A value that only appeared AFTER our own résumé
      // attach (an ATS's own, possibly wrong, résumé parse) is deliberately NOT protected.
      if (USER_VALUE_PROTECTED_KINDS[entry.kind] &&
          isMeaningfulExistingValue(priorValues[fill.id]) &&
          isProtectedByPriorUserActivity(entry, ctx.preResumeValues) &&
          !isSameValue(priorValues[fill.id], fillValue)) {
        delete priorValues[fill.id]; // nothing was written — nothing for Undo to restore
        var keepTargets = ApplyPilotScanner.getHighlightTargets(entry);
        for (var kt = 0; kt < keepTargets.length; kt++) highlight(keepTargets[kt], 'skipped', 'kept your value');
        needsYou.push({ id: fill.id, label: label, reason: 'kept your value', tag: (fieldsById[fill.id] || {}).tag });
        return step(i + 1);
      }

      var work = isMultiValue
        ? applyMultiValueWithProgress(entry, fill.values, label, i, total, ctx).then(
            function (ok) { return { ok: ok, timedOut: false }; },
            function (e) { return { ok: false, timedOut: !!(e && e.isTimeout), error: e }; })
        : runFieldAttempt(entry, fillValue, ctx.fieldTimeoutMs).then(
            function (ok) { return { ok: ok, timedOut: false }; },
            function (e) { return { ok: false, timedOut: !!(e && e.isTimeout), error: e }; });

      return work.then(function (outcome) {
        var hlTargets = ApplyPilotScanner.getHighlightTargets(entry);
        var isDraft = !!fill.draft || fill.source === 'draft';
        if (outcome.ok) {
          var reasonText = (fill.reason || 'Filled') + (fill.profile_key ? ' [' + fill.profile_key + ']' : '');
          if (isDraft) reasonText += ' — drafted — review before submitting';
          for (var h = 0; h < hlTargets.length; h++) highlight(hlTargets[h], isDraft ? 'draft' : 'filled', reasonText);
          applied.push({
            id: fill.id, label: label, value: fill.value,
            values: (Array.isArray(fill.values) && fill.values.length) ? fill.values : null,
            reason: fill.reason, profile_key: fill.profile_key, source: fill.source, draft: isDraft
          });
        } else if (outcome.timedOut) {
          for (var h2 = 0; h2 < hlTargets.length; h2++) highlight(hlTargets[h2], 'skipped', 'Timed out waiting for this field to respond');
          failed.push({ id: fill.id, label: label, reason: 'Timed out after ' + Math.round(ctx.fieldTimeoutMs / 1000) + 's — the page did not respond in time' });
        } else {
          // entry._lastReason is set by scanner.js's applyFill() for the Workday popup
          // widgets (e.g. "no confident match for ... among dropdown options") — surface it
          // when present rather than only the generic message.
          var extra = entry._lastReason ? (' — ' + entry._lastReason) : (outcome.error ? (' — ' + (outcome.error.message || outcome.error)) : '');
          for (var h3 = 0; h3 < hlTargets.length; h3++) highlight(hlTargets[h3], 'skipped', 'Could not match "' + fill.value + '" to an option' + extra);
          failed.push({ id: fill.id, label: label, reason: 'Could not match value "' + fill.value + '" to an option on the page' + extra });
        }
      }).then(function () { return step(i + 1); });
    }

    return step(0).then(function () {
      for (var s = 0; s < skipped.length; s++) {
        var skip = skipped[s];
        var sEntry = registry[skip.id];
        needsYou.push({ id: skip.id, label: (fieldsById[skip.id] || {}).label || (fieldsById[skip.id] || {}).name, reason: skip.reason || 'Skipped — please answer this yourself', tag: (fieldsById[skip.id] || {}).tag });
        if (!sEntry) continue;
        var sTargets = ApplyPilotScanner.getHighlightTargets(sEntry);
        for (var st = 0; st < sTargets.length; st++) highlight(sTargets[st], 'skipped', skip.reason || 'Skipped — please answer this yourself');
      }
      return { applied: applied, failed: failed, needsYou: needsYou, total: total };
    });
  }

  // Real ATS forms are React/Angular-based, and a field can look successfully filled the
  // instant we write it yet still get wiped moments later by the page's own re-render (a
  // controlled component re-asserting its old state) or by a combobox clearing its search text
  // on blur. Counting that as "Filled" would overstate what actually landed. After the whole
  // apply loop finishes, this waits briefly for any such re-render to happen, then reads every
  // successfully-applied field back and moves anything that no longer matches what we set out
  // of `applied` and into `failed` as "didn't stick" — the summary only ever counts VERIFIED
  // fills.
  var VERIFY_SETTLE_MS = 500;

  function verifyAppliedFills(result) {
    if (!result.applied.length) return Promise.resolve(result);
    return new Promise(function (resolve) { setTimeout(resolve, VERIFY_SETTLE_MS); }).then(function () {
      var stillApplied = [];
      var reverted = [];
      result.applied.forEach(function (a) {
        var entry = registry[a.id];
        if (!entry) { stillApplied.push(a); return; } // can't re-check — don't penalize it for that
        var after;
        try { after = ApplyPilotScanner.getCurrentValue(entry); } catch (e) { stillApplied.push(a); return; }
        var target = (a.values && a.values.length) ? a.values : a.value;
        if (isSameValue(after, target)) {
          stillApplied.push(a);
        } else {
          var hlTargets = ApplyPilotScanner.getHighlightTargets(entry);
          for (var h = 0; h < hlTargets.length; h++) {
            highlight(hlTargets[h], 'skipped', "Didn't stick — the page reverted this field after it was filled");
          }
          reverted.push({ id: a.id, label: a.label, reason: "Didn't stick — the page reverted this field after it was filled (re-render, or the widget cleared itself)" });
        }
      });
      result.applied = stillApplied;
      result.failed = result.failed.concat(reverted);
      return result;
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
   * Best-effort résumé attachment, run as the FIRST step of scanning (see "RÉSUMÉ FIRST" in the
   * file header and prepareAndScan() below) — i.e. only ever as a step inside an explicit,
   * user-initiated fill, never on page load. Fails soft at every step: no résumé-shaped field on
   * the page, no service reachable, no résumé stored, or a widget that rejects programmatic
   * assignment all resolve to a reported (never thrown) failure. The target gets the same
   * highlight() treatment as a normal fill/skip so it's visible on the page, not just in the
   * panel's summary.
   */
  function maybeAttachResume() {
    // Cheap local check first — skip the round-trip to the background worker
    // (and the local service) entirely when there's nowhere on this page to
    // put a résumé.
    var target = ApplyPilotScanner.findResumeFileTarget(document);
    if (!target) return Promise.resolve({ attempted: false });

    // `errorCode` mirrors background.js's callResume() error codes (e.g.
    // "no-resume") so the panel can tell "nothing is stored yet — go add one"
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

  /**
   * Resolves once `doc` has gone ~DOM_QUIET_MS without a mutation, or after DOM_QUIET_MAX_MS
   * regardless — the generic "let the page settle" wait after a résumé attach (see "RÉSUMÉ
   * FIRST" above) for ATSs with no more specific upload-confirmation signal to poll instead
   * (attachResumeFile() already handles Workday's own confirmation marker internally).
   */
  function waitForDomQuiet(doc, quietMs, maxMs) {
    return new Promise(function (resolve) {
      var root = doc && (doc.body || doc.documentElement);
      if (!root || typeof MutationObserver === 'undefined') {
        setTimeout(resolve, Math.min(quietMs, maxMs));
        return;
      }
      var settled = false;
      var quietTimer = null;
      var maxTimer = null;
      function finish() {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(quietTimer);
        clearTimeout(maxTimer);
        resolve();
      }
      var observer = new MutationObserver(function () {
        clearTimeout(quietTimer);
        quietTimer = setTimeout(finish, quietMs);
      });
      observer.observe(root, { childList: true, subtree: true, attributes: true, characterData: true });
      quietTimer = setTimeout(finish, quietMs);
      maxTimer = setTimeout(finish, maxMs);
    });
  }

  // ---------------------------------------------------------------------
  // "could not read" — visible, interactive-control-shaped elements that never made it into
  // the scanner's registry (see README "Report honestly"). Built entirely from functions
  // scanner.js already exports (isVisible) plus plain DOM queries — never a new scanner.js
  // export — so this whole concern lives in content.js, which this build owns.
  // ---------------------------------------------------------------------
  var UNREAD_SELECTOR = 'input, select, textarea, [role="combobox"], [role="radiogroup"]';
  var UNREAD_EXCLUDED_INPUT_TYPES = { hidden: true, submit: true, button: true, image: true, reset: true };

  /** Every element any registry entry actually points at, so it can be excluded below. */
  function registeredElements(reg) {
    var known = [];
    for (var id in reg) {
      if (!Object.prototype.hasOwnProperty.call(reg, id)) continue;
      known = known.concat(entryElements(reg[id]));
    }
    return known;
  }

  function countUnreadControls(doc, reg) {
    var known = registeredElements(reg);
    var all = Array.prototype.slice.call(doc.querySelectorAll(UNREAD_SELECTOR));
    var unread = 0;
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.disabled) continue;
      var tag = el.tagName.toLowerCase();
      if (tag === 'input' && UNREAD_EXCLUDED_INPUT_TYPES[(el.type || 'text').toLowerCase()]) continue;
      if (!ApplyPilotScanner.isVisible(el)) continue;
      if (known.indexOf(el) !== -1) continue;
      unread++;
    }
    return unread;
  }

  /**
   * Restores every field this run actually changed, back to the value it held immediately
   * before this run touched it — and reports only how many restores were VERIFIED by reading
   * the field back afterwards (getCurrentValue()), not how many restores were merely attempted.
   * applyFill() can silently fail to stick on a widget that rejects programmatic writes, and a
   * count that doesn't distinguish "tried" from "actually restored" would overstate what Undo
   * did. Async because applyFill() returns a Promise for the Workday popup widgets
   * (wd-dropdown/wd-prompt) — this used to fire-and-forget those, undercounting silently.
   */
  function undo() {
    var ids = Object.keys(priorValues);
    var restored = 0;

    function next(i) {
      if (i >= ids.length) return Promise.resolve();
      var id = ids[i];
      var entry = registry[id];
      if (!entry) return next(i + 1);
      var target = priorValues[id];
      return Promise.resolve().then(function () {
        return ApplyPilotScanner.applyFill(entry, target);
      }).then(function () {
        var after;
        try { after = ApplyPilotScanner.getCurrentValue(entry); } catch (e) { after = undefined; }
        if (isSameValue(after, target)) restored++;
      }, function () {
        // best-effort — a restore that errors just doesn't count; never aborts the rest
      }).then(function () { return next(i + 1); });
    }

    return next(0).then(function () {
      priorValues = {};
      for (var i = 0; i < highlightedElements.length; i++) clearHighlight(highlightedElements[i]);
      highlightedElements = [];
      return { restored: restored };
    });
  }

  // ---------------------------------------------------------------------
  // per-tab persisted state — reported to background.js, which is the only context that
  // actually writes chrome.storage.session (content scripts default to no access to it; see
  // README.md). background.js merges every frame's own report into ONE combined result per
  // tab; the panel only ever reads that combined result, plus chrome.storage.onChanged — it
  // never depends on being open for any frame's fill to run to completion.
  // ---------------------------------------------------------------------

  function emptyCounts() { return { filled: 0, drafts: 0, needsYou: 0, failed: 0 }; }

  function freshState(status, startedAt) {
    return {
      status: status,
      url: location.href,
      startedAt: startedAt || null,
      updatedAt: Date.now(),
      progress: null,
      counts: emptyCounts(),
      filled: [],
      drafts: [],
      needsYou: [],
      failed: [],
      resume: null,
      shieldFired: false,
      undoAvailable: highlightedElements.length > 0 && Object.keys(priorValues).length > 0,
      error: null,
      note: null
    };
  }

  // The most recent state object this frame has reported, kept around so a standalone action
  // that happens OUTSIDE the normal prepareAndScan/applyFills pipeline (inserting a cover-letter
  // draft, item 2; "remember my answers" touches no DOM so it doesn't need this) can update just
  // its own corner of that state (e.g. undoAvailable, or append one more drafted field) and
  // re-report it, WITHOUT resetting the visible fill summary the way starting a fresh
  // freshState('running'/'idle') would. Never read by anything outside this file.
  var lastReportedState = null;

  function sendStateUpdate(state) {
    state.updatedAt = Date.now();
    lastReportedState = state;
    try {
      var p = chrome.runtime.sendMessage({ type: 'FILL_STATE_UPDATE', state: state });
      // Fire-and-forget from this file's point of view — the pipeline must not stall waiting
      // for background.js's storage write to finish. Still attach a no-op rejection handler:
      // "Extension context invalidated" (e.g. mid-navigation) must never surface as an
      // unhandled rejection in this page's console.
      if (p && typeof p.then === 'function') p.then(function () {}, function () {});
    } catch (e) {
      // Same reasoning as above — best-effort only.
    }
  }

  /**
   * Finalizes this frame's participation in the current run: removes ITS submit shield, clears
   * `activeRun`/`currentPrepare`, and pushes one last terminal state update. This is the ONE
   * place that happens, no matter whether the run ends because APPLY_FILLS finished normally,
   * because PREPARE_AND_SCAN itself found nothing to do (empty page, cancelled before scanning
   * finished, or errored), or because background.js sent ABORT_FILL (e.g. the merged /resolve
   * call failed) — every one of those paths calls this instead of duplicating the cleanup.
   */
  function finishRun(run, status, note) {
    if (currentPrepare && currentPrepare.run === run) {
      currentPrepare.removeShield();
      currentPrepare.removeUserInputTracker();
      var state = currentPrepare.state;
      state.status = status;
      if (note) state.note = note;
      var total = (state.progress && state.progress.total) || 0;
      state.progress = { current: total, total: total, label: '' };
      sendStateUpdate(state);
      currentPrepare = null;
    }
    if (activeRun === run) activeRun = null;
  }

  /**
   * Phase 1 of this frame's fill: expand repeating sections, attach the résumé and wait for the
   * page to settle, THEN scan (see "RÉSUMÉ FIRST" above). Resolves with
   * `{ ok, fields, skippedFrames, unreadControls, cancelled }` for background.js to merge with
   * every other frame's scan and send in ONE /resolve call — this function never calls
   * /resolve itself. Leaves `activeRun`/the submit shield installed on success (with fields to
   * apply) so APPLY_FILLS can pick them back up later; finalizes immediately (via finishRun())
   * on cancellation, an empty page, or an error, since nothing will ever call APPLY_FILLS for
   * those cases.
   */
  function prepareAndScan() {
    if (activeRun) {
      return Promise.resolve({ ok: false, error: 'already-running' });
    }
    runCounter++;
    var run = { token: runCounter, cancelled: false };
    activeRun = run;

    var startedAt = Date.now();
    var state = freshState('running', startedAt);
    state.undoAvailable = false;

    // ONE shield for this frame's ENTIRE participation in the fill — expansion, résumé,
    // scanning, and (once APPLY_FILLS arrives) every field — see "the one rule that matters" in
    // README.md. This spans two separate incoming messages (this one and, later, APPLY_FILLS),
    // so it is removed in exactly one place — finishRun() — never here.
    var removeShield = ApplyPilotScanner.installSubmitShield(document, function () {
      state.shieldFired = true;
      push();
    });
    // Same lifetime as the shield above — see "kept your value" / isProtectedByPriorUserActivity().
    var removeUserInputTracker = installUserInputTracker(document);

    function isCancelled() { return run.cancelled; }
    function push() { sendStateUpdate(state); }
    push();

    currentPrepare = {
      state: state, run: run, removeShield: removeShield, removeUserInputTracker: removeUserInputTracker,
      startedAt: startedAt, fieldsById: {}, preResumeValues: null
    };

    // Snapshot every field's value BEFORE expansion/résumé/scanning touch anything — this is
    // what lets the apply phase tell "was already here" (protect it) apart from "the résumé
    // parse just put this here" (fine to overwrite with the real profile value) — see
    // isProtectedByPriorUserActivity()'s doc comment above.
    currentPrepare.preResumeValues = snapshotValuesByElement(ApplyPilotScanner.scanAll(document));

    return Promise.resolve()
      .then(function () {
        state.progress = { current: 0, total: 0, label: 'Expanding repeating sections…' };
        push();
        return expandSections();
      })
      .then(function (expansion) {
        state.expansion = expansion;
        if (isCancelled()) return null;
        state.progress = { current: 0, total: 0, label: 'Attaching résumé…' };
        push();
        return maybeAttachResume();
      })
      .then(function (resume) {
        if (resume === null || isCancelled()) return null;
        state.resume = resume;
        push();
        if (resume && resume.attempted && resume.attached) {
          state.progress = { current: 0, total: 0, label: 'Waiting for the page to settle…' };
          push();
          return waitForDomQuiet(document, DOM_QUIET_MS, DOM_QUIET_MAX_MS);
        }
      })
      .then(function (settleResult) {
        if (isCancelled()) return null;
        state.progress = { current: 0, total: 0, label: 'Scanning the page…' };
        push();
        return ApplyPilotScanner.scanAll(document);
      })
      .then(function (result) {
        if (result === null || isCancelled()) {
          return { ok: true, cancelled: true, fields: [], skippedFrames: 0, unreadControls: 0 };
        }
        registry = result.registry;
        result.fields.forEach(function (f) { currentPrepare.fieldsById[f.id] = f; });
        var unreadControls = countUnreadControls(document, registry);
        return { ok: true, cancelled: false, fields: result.fields, skippedFrames: result.skippedFrames, unreadControls: unreadControls };
      })
      .catch(function (e) {
        var msg = 'Error while preparing this page: ' + (e && e.message ? e.message : String(e));
        state.error = msg;
        return { ok: false, error: msg, fields: [], skippedFrames: 0, unreadControls: 0 };
      });
  }

  // ---------------------------------------------------------------------
  // COVER LETTER (item 2) — a "Draft cover letter" click in the panel needs the page's own
  // visible text (a last-resort job description source — the service tries the operator's jobs
  // DB and the ATS's own public posting API first, see job_context.py) and, separately, whether
  // there's somewhere on THIS page to put the finished draft. Both are read-only / additive:
  // extracting text touches nothing, finding the field only SCANS, and inserting goes through the
  // exact same guarded applyFill()/highlight()/priorValues path a normal fill uses so Undo can
  // restore it too — see insertCoverLetterDraft() below. Top frame only, same as "Report page".
  // ---------------------------------------------------------------------
  var MAX_PAGE_TEXT_CHARS = 15000;
  var PAGE_TEXT_EXCLUDED_TAGS = { script: 1, style: 1, noscript: 1, input: 1, select: 1, textarea: 1, button: 1, option: 1, optgroup: 1 };

  function isInsideExcludedField(el) {
    var n = el;
    while (n) {
      if (n.nodeType === 1 && PAGE_TEXT_EXCLUDED_TAGS[n.tagName.toLowerCase()]) return true;
      n = n.parentElement;
    }
    return false;
  }

  /**
   * The page's own main visible text, capped at MAX_PAGE_TEXT_CHARS, excluding form fields —
   * walks live text nodes (never a detached clone: visibility needs real layout, and
   * ApplyPilotScanner.isVisible() only works on a node that's actually in the rendered document)
   * so it naturally skips display:none/hidden copy the same way the scanner already does for
   * fields. Read-only — never touches the DOM.
   */
  function extractVisiblePageText(doc) {
    var root = doc && doc.body;
    if (!root || typeof doc.createTreeWalker !== 'function') return '';
    var walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var parts = [];
    var total = 0;
    var node;
    while ((node = walker.nextNode())) {
      if (total >= MAX_PAGE_TEXT_CHARS) break;
      var raw = node.nodeValue;
      if (!raw || !raw.trim()) continue;
      var parentEl = node.parentElement;
      if (!parentEl || isInsideExcludedField(parentEl)) continue;
      try {
        if (!ApplyPilotScanner.isVisible(parentEl)) continue;
      } catch (e) {
        continue;
      }
      var text = raw.replace(/\s+/g, ' ').trim();
      if (!text) continue;
      parts.push(text);
      total += text.length + 1;
    }
    return parts.join(' ').slice(0, MAX_PAGE_TEXT_CHARS);
  }

  var COVER_LETTER_LABEL_RE = /cover\s*letter/i;

  /**
   * A textarea/text-input FieldDescriptor whose resolved label mentions "cover letter", or null.
   * Runs a real scanAll() (merged additively into the live `registry` — never replacing it, so
   * ids an earlier fill's Undo still depends on keep resolving) purely to find candidates; nothing
   * is written here.
   */
  function findCoverLetterField() {
    var result = ApplyPilotScanner.scanAll(document);
    for (var id in result.registry) {
      if (Object.prototype.hasOwnProperty.call(result.registry, id)) registry[id] = result.registry[id];
    }
    var match = null;
    result.fields.forEach(function (f) {
      if (match) return;
      var tag = String(f.tag || '').toLowerCase();
      var type = String(f.type || '').toLowerCase();
      var isTextish = tag === 'textarea' || (tag === 'input' && (type === '' || type === 'text'));
      if (isTextish && COVER_LETTER_LABEL_RE.test(f.label || '')) match = f;
    });
    return match ? { id: match.id, label: match.label || '' } : null;
  }

  /**
   * Republishes whatever this frame last reported, with just `patch` applied, WITHOUT resetting
   * filled/drafts/needsYou/failed to empty the way starting a brand-new freshState() would — so a
   * standalone action taken between fills (or before any fill has run at all) never wipes an
   * already-visible fill summary. If nothing has been reported yet, seeds a minimal 'done' state
   * so the panel has something to render.
   */
  function patchReportedState(patch) {
    var state = lastReportedState || freshState('done', Date.now());
    for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) state[k] = patch[k];
    sendStateUpdate(state);
    return state;
  }

  /**
   * Inserts `text` into the field `fieldId` (from findCoverLetterField(), or a stale id from a
   * page that has since changed) through the SAME guarded applyFill() + highlight() path a normal
   * fill uses, so it survives a controlled-input re-render the same way, is recorded in
   * `priorValues` (Undo replays it exactly like any other field — see undo() above), and is
   * highlighted 'draft' (blue), never 'filled' (green) — this is generated text the operator must
   * review, same rule as every other draft in this extension.
   */
  function insertCoverLetterDraft(fieldId, text) {
    var entry = registry[fieldId];
    if (!entry) return Promise.resolve({ ok: false, error: 'That field is no longer on the page (did it change since the draft was requested?).' });
    if (!Object.prototype.hasOwnProperty.call(priorValues, fieldId)) {
      try { priorValues[fieldId] = ApplyPilotScanner.getCurrentValue(entry); } catch (e) { priorValues[fieldId] = undefined; }
    }
    return Promise.resolve().then(function () {
      return ApplyPilotScanner.applyFill(entry, text);
    }).then(function (ok) {
      var targets = ApplyPilotScanner.getHighlightTargets(entry);
      if (!ok) {
        return { ok: false, error: 'The page would not accept the draft text in that field.' };
      }
      for (var i = 0; i < targets.length; i++) {
        highlight(targets[i], 'draft', 'Cover letter draft inserted — review before submitting');
      }
      var label = (currentPrepare && currentPrepare.fieldsById[fieldId] && currentPrepare.fieldsById[fieldId].label) || 'Cover letter';
      var state = lastReportedState;
      var already = state && (state.drafts || []).some(function (d) { return d.id === fieldId; });
      if (!already) {
        var drafts = (state && state.drafts || []).concat([{
          id: fieldId, label: label, value: text, reason: 'Cover letter draft inserted', profile_key: null,
          source: 'cover-letter', draft: true
        }]);
        var counts = (state && state.counts) || emptyCounts();
        patchReportedState({
          status: 'done', drafts: drafts,
          counts: { filled: counts.filled || 0, drafts: drafts.length, needsYou: counts.needsYou || 0, failed: counts.failed || 0 },
          undoAvailable: true
        });
      } else {
        patchReportedState({ undoAvailable: true });
      }
      return { ok: true };
    }, function (e) {
      return { ok: false, error: String(e && e.message ? e.message : e) };
    });
  }

  // ---------------------------------------------------------------------
  // REMEMBER MY ANSWERS (item 3) — background.js's rememberAnswersForTab() asks this frame to
  // read back the CURRENT value of specific fields (by LOCAL id — background.js has already
  // stripped the frame qualifier off before sending this) once the operator has typed answers
  // into whatever a fill left for them and clicks the panel's button. Never automatic. Deliberately
  // excludes anything password/file-shaped and anything still empty — those are simply left out
  // of the returned map rather than included as "" or a placeholder value.
  // ---------------------------------------------------------------------
  function readFieldsForAnswers(ids) {
    var values = {};
    (ids || []).forEach(function (id) {
      var entry = registry[id];
      if (!entry) return;
      var els = entryElements(entry);
      var isSensitive = els.some(function (el) {
        if (!el || el.tagName !== 'INPUT') return false;
        var t = String(el.type || '').toLowerCase();
        return t === 'password' || t === 'file';
      });
      if (isSensitive) return;
      var val;
      try { val = ApplyPilotScanner.getCurrentValue(entry); } catch (e) { return; }
      if (val == null || typeof val === 'boolean') return; // a checkbox's true/false isn't answer text
      var text = String(val).trim();
      if (text) values[id] = text;
    });
    return values;
  }

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (!msg || typeof msg !== 'object') return false;

    if (msg.type === 'PREPARE_AND_SCAN') {
      // Deliberately not a message this frame sent to itself — this always arrives FROM
      // background.js, once per frame, at the start of a RUN_FILL it is coordinating (see that
      // file's header). Not awaited by background.js beyond this one response: the frame keeps
      // running independently of anything else once it replies.
      prepareAndScan().then(function (result) {
        var run = currentPrepare ? currentPrepare.run : null;
        if (!result || !result.ok || result.cancelled || !(result.fields || []).length) {
          // Nothing here for background.js to fold into a /resolve call — finalize locally
          // right now rather than waiting for an APPLY_FILLS that will never be sent for this
          // frame (see finishRun()'s doc comment).
          var status = (result && result.cancelled) ? 'cancelled' : (result && result.ok ? 'done' : 'error');
          var note = (result && result.ok && !result.cancelled && !(result.fields || []).length)
            ? 'No fillable fields found in this frame.' : null;
          if (run) finishRun(run, status, note);
        }
        sendResponse({
          ok: !!(result && result.ok),
          cancelled: !!(result && result.cancelled),
          error: (result && result.error) || null,
          fields: (result && result.fields) || [],
          skippedFrames: (result && result.skippedFrames) || 0,
          unreadControls: (result && result.unreadControls) || 0
        });
      });
      return true; // async response
    }

    if (msg.type === 'APPLY_FILLS') {
      var pending = currentPrepare;
      if (!pending) {
        sendResponse({ ok: false, error: 'no prepared scan for this frame' });
        return false;
      }
      var run = pending.run;
      var state = pending.state;
      var fieldTimeoutMs = (typeof msg.fieldTimeoutMs === 'number' && msg.fieldTimeoutMs > 0) ? msg.fieldTimeoutMs : DEFAULT_FIELD_TIMEOUT_MS;
      var budgetMs = (typeof msg.budgetMs === 'number' && msg.budgetMs > 0) ? msg.budgetMs : DEFAULT_BUDGET_MS;
      var startedAt = pending.startedAt;

      function isCancelled() { return run.cancelled; }
      function overBudget() { return (Date.now() - startedAt) > budgetMs; }

      applyFills(msg.fills || [], msg.skipped || [], pending.fieldsById || {}, {
        isCancelled: isCancelled,
        overBudget: overBudget,
        fieldTimeoutMs: fieldTimeoutMs,
        preResumeValues: pending.preResumeValues,
        onProgress: function (progress) { state.progress = progress; sendStateUpdate(state); }
      }).then(function (result) {
        state.progress = { current: result.total, total: result.total, label: 'Confirming fields stuck…' };
        sendStateUpdate(state);
        return verifyAppliedFills(result);
      }).then(function (result) {
        var factApplied = result.applied.filter(function (a) { return !a.draft; });
        var draftApplied = result.applied.filter(function (a) { return a.draft; });
        state.filled = factApplied;
        state.drafts = draftApplied;
        state.needsYou = result.needsYou;
        state.failed = result.failed;
        state.counts = {
          filled: factApplied.length,
          drafts: draftApplied.length,
          needsYou: result.needsYou.length,
          failed: result.failed.length
        };
        state.undoAvailable = result.applied.length > 0;
        finishRun(run, isCancelled() ? 'cancelled' : 'done');
      }, function (e) {
        finishRun(run, 'error', 'Error while filling: ' + (e && e.message ? e.message : String(e)));
      });

      // Acknowledge receipt immediately — deliberately NOT awaiting the work above, the same
      // "fire and forget, report progress via storage instead" contract the old START_FILL had.
      sendResponse({ ok: true, started: true });
      return false;
    }

    if (msg.type === 'ABORT_FILL') {
      // Sent by background.js only when it could not carry this frame's already-scanned fields
      // any further (e.g. the merged /resolve call itself failed) — this frame did everything
      // right, but there is nothing to apply. Finalize as an error so the panel doesn't spin.
      if (currentPrepare) {
        finishRun(currentPrepare.run, 'error', msg.error || 'The fill could not continue for this frame.');
      }
      sendResponse({ ok: true });
      return false;
    }

    if (msg.type === 'CANCEL_FILL') {
      if (activeRun) {
        activeRun.cancelled = true;
        sendResponse({ ok: true, cancelling: true });
      } else {
        sendResponse({ ok: true, cancelling: false, reason: 'no fill in progress' });
      }
      return false;
    }

    try {
      if (msg.type === 'DETECT') {
        sendResponse(detect());
        return false;
      }
      if (msg.type === 'UNDO') {
        undo().then(function (result) {
          var idle = freshState('idle');
          idle.note = 'Restored ' + result.restored + ' field(s) to their previous values.';
          sendStateUpdate(idle);
          sendResponse(result);
        });
        return true; // async response
      }
      if (msg.type === 'CAPTURE') {
        // Structure only — see capture.js for the no-values invariant.
        var cap = self.ApplyPilotCapture;
        sendResponse(cap ? { ok: true, structure: cap.captureStructure(document) }
                         : { ok: false, error: 'capture.js not loaded' });
        return false;
      }
      if (msg.type === 'EXTRACT_PAGE_TEXT') {
        // Read-only, top frame only (sidepanel.js never sends this to any other frame) — see
        // "COVER LETTER" above.
        sendResponse({ ok: true, text: extractVisiblePageText(document) });
        return false;
      }
      if (msg.type === 'FIND_COVER_LETTER_FIELD') {
        sendResponse({ ok: true, field: findCoverLetterField() });
        return false;
      }
      if (msg.type === 'INSERT_COVER_LETTER') {
        insertCoverLetterDraft(msg.fieldId, String(msg.text || '')).then(sendResponse);
        return true; // async response
      }
      if (msg.type === 'READ_FIELDS_FOR_ANSWERS') {
        sendResponse({ ok: true, values: readFieldsForAnswers(msg.ids) });
        return false;
      }
    } catch (e) {
      sendResponse({ error: String(e && e.message ? e.message : e) });
      return false;
    }
    return false;
  });
})();
