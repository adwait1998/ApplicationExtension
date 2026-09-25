/**
 * ApplyPilot Copilot — content script.
 *
 * Injected on demand (via chrome.scripting.executeScript, triggered by the side panel) into
 * the active tab only — never registered as an always-on content script, so the extension
 * never touches a page the user hasn't explicitly invoked it on (injection happens only when
 * the operator clicks "Fill this page" or "Report page" in the panel — never on tab switch,
 * never on panel open).
 *
 * Loaded AFTER scanner.js in the same isolated world, so `ApplyPilotScanner` is already a
 * global here. Nothing in this file ever calls form.submit(), clicks a submit button, or
 * navigates — see README.md "Safety invariants".
 *
 * Re-running this file on a page that already has it (e.g. the panel re-injecting) is safe:
 * everything below is guarded so a second injection reuses the existing state instead of
 * creating a second listener.
 *
 * DETACHED FILL / PANEL-INDEPENDENCE: the side panel can be closed, its window can lose focus,
 * or the user can switch tabs at any moment — none of that may interrupt a fill in progress.
 * So the entire scan -> expand -> resolve -> apply -> résumé pipeline lives in THIS file
 * (see runFill() below) and is kicked off by a single fire-and-forget START_FILL message from
 * the panel. Progress and the final result are reported by messaging the background worker
 * (chrome.runtime.sendMessage — this works whether or not any panel/popup is open, because it
 * targets the extension's own service worker, not a UI page), which persists them into
 * chrome.storage.session keyed by this tab's id. The panel only ever READS that storage (plus
 * chrome.storage.onChanged for live updates); it never depends on staying open, and it never
 * writes the fill-result state itself.
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
  // tab. `activeRun` is non-null only while runFill() is in flight; CANCEL_FILL flips its
  // `cancelled` flag, which the loops below check between fields and between list items.
  var activeRun = null;
  var runCounter = 0;

  // Defaults per the spec: no single field may stall the fill for more than ~12s, and the
  // whole fill gives up on remaining (not-yet-attempted) fields after ~120s. Both are
  // overridable ONLY via an explicit message field — used exclusively by
  // scripts/chrome_panel_test.py so it can prove the timeout/budget paths in well under Chrome
  // launch time, and documented as test-only in extension/README.md. Absent, behavior is
  // exactly the production default.
  var DEFAULT_FIELD_TIMEOUT_MS = 12000;
  var DEFAULT_BUDGET_MS = 120000;

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
  // Runs ONLY as the first step of an explicit, user-initiated fill (see runFill() below) —
  // never on page load, never on DETECT. Asks the local service how many
  // work_history/education entries the profile actually has, then clicks "Add Another" just
  // enough times to make room for them before scanning, so the structured tier's existing
  // section_index -> work_history[index-1]/education[index-1] mapping has somewhere to write
  // block 2, 3, ... into. All the actual DOM/safety work (the guard, the click, counting
  // blocks, finding the right button) lives in scanner.js so it can be exercised offline in
  // jsdom (see selftest.js) exactly like every other scanning concern in this extension.
  //
  // This function no longer installs its own submit shield — runFill() now installs ONE
  // shield that spans the entire fill (expansion through résumé attach, see the file header
  // and "the one rule that matters" in README.md), closing a gap that used to exist between
  // expansion finishing and the fill itself starting.

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
          applied.push({ id: fill.id, label: label, value: fill.value, reason: fill.reason, profile_key: fill.profile_key, source: fill.source, draft: isDraft });
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

  function base64ToUint8Array(base64) {
    var binary = atob(base64);
    var len = binary.length;
    var bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  /**
   * Best-effort résumé attachment, run as part of runFill() — i.e. only ever as a step inside
   * an explicit, user-initiated fill, never on page load (see file header). Fails soft at
   * every step: no résumé-shaped field on the page, no service reachable, no résumé stored, or
   * a widget that rejects programmatic assignment all resolve to a reported (never thrown)
   * failure. The target gets the same highlight() treatment as a normal fill/skip so it's
   * visible on the page, not just in the panel's summary.
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

  // ---------------------------------------------------------------------
  // per-tab persisted state — reported to background.js, which is the only context that
  // actually writes chrome.storage.session (content scripts default to no access to it; see
  // README.md). The panel only ever reads this, plus chrome.storage.onChanged — it never
  // depends on being open for the fill below to run to completion.
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

  function sendStateUpdate(state) {
    state.updatedAt = Date.now();
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
   * Runs the whole fill pipeline: expand repeating sections -> scan -> ask the local service
   * -> apply fills (with live progress, cancellation and per-field timeouts) -> attach the
   * résumé. Kicked off by START_FILL and NOT awaited by the message handler that starts it —
   * see the file header for why. `opts.fieldTimeoutMs` / `opts.budgetMs` are test-only
   * overrides (see the constants above); production callers never set them.
   */
  function runFill(opts) {
    opts = opts || {};
    var fieldTimeoutMs = (typeof opts.fieldTimeoutMs === 'number' && opts.fieldTimeoutMs > 0) ? opts.fieldTimeoutMs : DEFAULT_FIELD_TIMEOUT_MS;
    var budgetMs = (typeof opts.budgetMs === 'number' && opts.budgetMs > 0) ? opts.budgetMs : DEFAULT_BUDGET_MS;

    if (activeRun) {
      return Promise.resolve({ ok: false, error: 'already-running' });
    }
    runCounter++;
    var run = { token: runCounter, cancelled: false };
    activeRun = run;

    var startedAt = Date.now();
    var state = freshState('running', startedAt);
    state.undoAvailable = false;

    // ONE shield for the entire fill — expansion, scanning, the /resolve round-trip, every
    // field, and the résumé attach — closing the gap that used to exist between expansion
    // finishing and APPLY_FILLS starting. See "the one rule that matters" in README.md.
    var removeShield = ApplyPilotScanner.installSubmitShield(document, function () {
      state.shieldFired = true;
    });

    function isCancelled() { return run.cancelled; }
    function overBudget() { return (Date.now() - startedAt) > budgetMs; }
    function push() { sendStateUpdate(state); }

    push();

    return Promise.resolve()
      .then(function () {
        state.progress = { current: 0, total: 0, label: 'Expanding repeating sections…' };
        push();
        return expandSections();
      })
      .then(function (expansion) {
        state.expansion = expansion;
        if (isCancelled()) return null;
        state.progress = { current: 0, total: 0, label: 'Scanning the page…' };
        push();
        var result = ApplyPilotScanner.scanAll(document);
        registry = result.registry;
        state.skippedFrames = result.skippedFrames;
        return result.fields;
      })
      .then(function (fields) {
        if (fields === null || isCancelled()) return null;
        if (!fields.length) {
          state.note = 'No fillable fields found on this page.';
          return null;
        }
        var fieldsById = {};
        fields.forEach(function (f) { fieldsById[f.id] = f; });
        state.progress = { current: 0, total: fields.length, label: 'Asking the local ApplyPilot service…' };
        push();
        return chrome.runtime.sendMessage({ type: 'RESOLVE', url: location.href, fields: fields }).then(function (resolveResp) {
          return { resolveResp: resolveResp, fieldsById: fieldsById };
        });
      })
      .then(function (ctx) {
        if (!ctx || isCancelled()) return null;
        var resolveResp = ctx.resolveResp;
        if (!resolveResp || !resolveResp.ok) {
          state.error = (resolveResp && resolveResp.message) || 'The local service call failed.';
          return null;
        }
        var data = resolveResp.data || {};
        return applyFills(data.fills || [], data.skipped || [], ctx.fieldsById, {
          isCancelled: isCancelled,
          overBudget: overBudget,
          fieldTimeoutMs: fieldTimeoutMs,
          onProgress: function (progress) { state.progress = progress; push(); }
        });
      })
      .then(function (result) {
        if (!result) return;
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
        if (isCancelled()) return;
        state.progress = { current: result.total, total: result.total, label: 'Attaching résumé…' };
        push();
        return maybeAttachResume().then(function (resume) { state.resume = resume; });
      })
      .catch(function (e) {
        state.error = 'Error while filling: ' + (e && e.message ? e.message : String(e));
      })
      // A real Promise.prototype.finally() (safe: minimum_chrome_version is 116) rather than a
      // .then(fn, fn) pair — this callback runs exactly once no matter how the chain above
      // settled, and removeShield()/activeRun cleanup run FIRST, before anything that could
      // conceivably throw (state bookkeeping, push()), so the shield is guaranteed removed even
      // in a failure mode nothing above anticipated. This is the one invariant that must never
      // have an exit path that skips it.
      .finally(function () {
        removeShield();
        if (activeRun === run) activeRun = null;
        state.status = isCancelled() ? 'cancelled' : (state.error ? 'error' : 'done');
        var total = (state.progress && state.progress.total) || 0;
        state.progress = { current: total, total: total, label: '' };
        push();
      });
  }

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (!msg || typeof msg !== 'object') return false;

    if (msg.type === 'START_FILL') {
      // Deliberately NOT awaited: this handler's job is only to acknowledge that the message
      // was received and the fill has begun. The fill itself must survive the panel (or this
      // whole message channel) going away — see the file header — so it reports its own
      // progress/result via sendStateUpdate() rather than via sendResponse().
      runFill({ fieldTimeoutMs: msg.fieldTimeoutMs, budgetMs: msg.budgetMs }).catch(function () {});
      sendResponse({ ok: true, started: true });
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
        var result = undo();
        var idle = freshState('idle');
        idle.note = 'Restored ' + result.restored + ' field(s) to their previous values.';
        sendStateUpdate(idle);
        sendResponse(result);
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
