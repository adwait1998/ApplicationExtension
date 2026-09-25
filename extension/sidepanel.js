/**
 * ApplyPilot Copilot — side panel logic.
 *
 * This replaces the old action popup. Chrome tears down a popup's whole JS context on any
 * focus change (switching tabs, clicking DevTools, ...), which used to kill a fill mid-way on
 * slow pages — see README.md. A side panel's document stays alive across tab switches and
 * window focus changes, so this file is now a pure, disposable RENDERER:
 *
 *   - because the panel is one instance per WINDOW (not per tab), it follows
 *     chrome.tabs.onActivated / onUpdated itself and always renders whichever tab is active in
 *     its own window — activeTab alone cannot inject into (or even draw the right UI for) a tab
 *     you switched to after opening the panel. host_permissions itself only ever covers
 *     127.0.0.1 (the local service); every other site is an OPTIONAL permission this file asks
 *     for, per site, the first time it's needed — see gatePermissions()/refreshOriginInfo()
 *     below and README.md "Permissions" — never a standing grant covering http/https up front;
 *   - clicking Fill this page (or Report page) is the ONLY thing that (a) may prompt for that
 *     site's permission and (b) injects scanner.js/capture.js/content.js into a tab — never on
 *     load, never on a tab switch;
 *   - Fill then hands the tab off to background.js (RUN_FILL) and does NOT await the result —
 *     background.js coordinates every frame's own fill (see its file header) independently of
 *     this panel, so the fill runs to completion regardless of whether this panel stays open,
 *     gets closed, or the operator switches to a different tab and back. Cancel/Undo are the
 *     same shape (CANCEL_FILL_TAB/UNDO_TAB) — this file only ever tells background.js what tab
 *     to act on, never talks to a frame's content.js directly except for CAPTURE (Report page,
 *     top frame only, unchanged from before this build);
 *   - it never writes the fill-result state itself. Every frame reports its own progress/results
 *     to background.js (via chrome.runtime.sendMessage), which is the only context that merges
 *     them and persists the combined result into chrome.storage.session, keyed per tab id. This
 *     file only ever READS that storage (chrome.storage.session.get) and listens for
 *     chrome.storage.onChanged to render live progress and survive its own reloads.
 */
(function () {
  'use strict';

  var scanBtn = document.getElementById('scanBtn');
  var cancelBtn = document.getElementById('cancelBtn');
  var undoBtn = document.getElementById('undoBtn');
  var reportBtn = document.getElementById('reportBtn');
  var exportReportBtn = document.getElementById('exportReportBtn');
  var statusBox = document.getElementById('statusBox');
  var staleBanner = document.getElementById('staleBanner');
  var progressBox = document.getElementById('progressBox');
  var progressLabel = document.getElementById('progressLabel');
  var progressFill = document.getElementById('progressFill');
  var fillSummaryEl = document.getElementById('fillSummary');
  var resumeLineEl = document.getElementById('resumeLine');
  var resultsBox = document.getElementById('results');
  var tiersLine = document.getElementById('tiersLine');
  var serviceDot = document.getElementById('serviceDot');
  var optionsBtn = document.getElementById('optionsBtn');

  // -- cover letter (item 2) --
  var coverLetterBtn = document.getElementById('coverLetterBtn');
  var coverLetterStatusEl = document.getElementById('coverLetterStatus');
  var coverLetterBoxEl = document.getElementById('coverLetterBox');
  var coverLetterJobEl = document.getElementById('coverLetterJob');
  var coverLetterTextEl = document.getElementById('coverLetterText');
  var coverLetterWarningsEl = document.getElementById('coverLetterWarnings');
  var coverLetterCopyBtn = document.getElementById('coverLetterCopyBtn');
  var coverLetterDownloadBtn = document.getElementById('coverLetterDownloadBtn');
  var coverLetterInsertBtn = document.getElementById('coverLetterInsertBtn');

  // -- tailor my résumé --
  var tailorResumeBtn = document.getElementById('tailorResumeBtn');
  var tailorResumeStatusEl = document.getElementById('tailorResumeStatus');
  var tailorResumeBoxEl = document.getElementById('tailorResumeBox');
  var tailorResumeJobEl = document.getElementById('tailorResumeJob');
  var tailorResumeVerdictEl = document.getElementById('tailorResumeVerdict');
  var tailorResumeIssuesEl = document.getElementById('tailorResumeIssues');
  var tailorResumeWarningsEl = document.getElementById('tailorResumeWarnings');
  var tailorResumeTextEl = document.getElementById('tailorResumeText');
  var tailorResumeDownloadBtn = document.getElementById('tailorResumeDownloadBtn');
  var tailorResumeUseBtn = document.getElementById('tailorResumeUseBtn');
  var tailorResumeUseStatusEl = document.getElementById('tailorResumeUseStatus');
  var attachTailoredRowEl = document.getElementById('attachTailoredRow');
  var attachTailoredBtn = document.getElementById('attachTailoredBtn');

  // -- remember my answers (item 3) --
  var rememberBoxEl = document.getElementById('rememberBox');
  var rememberBtn = document.getElementById('rememberBtn');
  var rememberStatusEl = document.getElementById('rememberStatus');
  var rememberDetailsEl = document.getElementById('rememberDetails');

  // -- application log (item 4) --
  var logLineEl = document.getElementById('logLine');
  var logStatusTextEl = document.getElementById('logStatusText');
  var markAppliedBtn = document.getElementById('markAppliedBtn');

  // -- multi-step continuation (item 8) --
  var continuationToggle = document.getElementById('continuationToggle');
  var continuationStepLineEl = document.getElementById('continuationStepLine');

  // TEST-ONLY: "?tabId=<id>" pins this panel instance to a specific tab for its whole lifetime
  // instead of following chrome.tabs.onActivated/onUpdated in its own window. This exists
  // purely so scripts/chrome_panel_test.py can point two independent panel page loads at two
  // specific tabs deterministically, without depending on real OS-level tab/window focus in an
  // automated browser. It must never be set by production code (nothing in this extension ever
  // navigates the panel to such a URL) — its ABSENCE, the normal case, leaves every code path
  // below exactly as if this block did not exist.
  var pinnedTabId = (function () {
    try {
      var raw = new URLSearchParams(location.search).get('tabId');
      if (raw == null) return null;
      var n = parseInt(raw, 10);
      return Number.isFinite(n) ? n : null;
    } catch (e) {
      return null;
    }
  })();

  var currentWindowId = null;
  var activeTabId = null;
  // Set to a tabId right after this panel asks content.js to cancel that tab's fill, so the
  // status line can say "Cancelling…" instead of flipping back to a generic "Filling…" while
  // the loop finishes its current field. Cleared once that tab is no longer 'running'.
  var cancelRequestedForTab = null;
  // The last FIND_COVER_LETTER_FIELD result for activeTabId ({id, label}), or null — see
  // "DRAFT COVER LETTER" below. Reset whenever the active tab changes (see setActiveTab()).
  var coverLetterFieldForTab = null;
  // The last successful /resume/tailor result for activeTabId ({id, filename, text, status,
  // judge, warnings, job}), or null — see "TAILOR MY RÉSUMÉ" below. Reset whenever the active
  // tab changes, same reasoning as coverLetterFieldForTab: a tailored résumé is specific to
  // whichever job/tab it was written for.
  var tailorResumeForTab = null;

  function stateKey(tabId) {
    return 'fillState_' + tabId;
  }

  // ---------------------------------------------------------------------
  // least-privilege host access — see README "Permissions". host_permissions only ever
  // statically covers 127.0.0.1 (the local service); every other site is an OPTIONAL
  // permission (declared in manifest.json's optional_host_permissions) requested per-origin,
  // asked once per site, and revocable any time from chrome://extensions. `originInfoByTab`
  // caches, per tab, every http(s) origin a fill would touch (the tab's own origin plus every
  // frame's — see refreshOriginInfo()) and which of those are already granted, computed at
  // RENDER time so the Fill/Report click handlers below can call chrome.permissions.request()
  // as the very first thing they do, with no `await` before it — Chrome only honors that call
  // as part of the user gesture that triggered the click if nothing has yielded to the event
  // loop first.
  // ---------------------------------------------------------------------
  var originInfoByTab = Object.create(null);

  function toOriginPattern(url) {
    try {
      var u = new URL(url);
      if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
      return u.protocol + '//' + u.host + '/*';
    } catch (e) {
      return null;
    }
  }

  function hostFromPattern(p) {
    var m = /^https?:\/\/([^/]+)\/\*$/.exec(p);
    return m ? m[1] : p;
  }

  async function refreshOriginInfo(tabId, tabUrl) {
    var patterns = [];
    var seen = Object.create(null);
    var top = toOriginPattern(tabUrl);
    if (top) { seen[top] = true; patterns.push(top); }
    try {
      if (chrome.webNavigation && typeof chrome.webNavigation.getAllFrames === 'function') {
        var frames = await chrome.webNavigation.getAllFrames({ tabId: tabId });
        (frames || []).forEach(function (f) {
          var p = toOriginPattern(f.url);
          if (p && !seen[p]) { seen[p] = true; patterns.push(p); }
        });
      }
    } catch (e) {
      // best-effort — fall back to just the tab's own origin, already captured above
    }
    if (!patterns.length) {
      delete originInfoByTab[tabId];
      return;
    }
    var missing = [];
    try {
      for (var i = 0; i < patterns.length; i++) {
        var has = await chrome.permissions.contains({ origins: [patterns[i]] });
        if (!has) missing.push(patterns[i]);
      }
    } catch (e) {
      missing = patterns.slice(); // fail safe: assume all missing rather than silently skip the gate
    }
    originInfoByTab[tabId] = { all: patterns, missing: missing };
  }

  /**
   * Synchronous by design: reads the cache refreshOriginInfo() already populated and, if
   * anything is missing, calls chrome.permissions.request() immediately — the caller (a click
   * handler) must invoke this with NO `await` beforehand. Returns { requested, promise }, never
   * itself a Promise, so calling it can never itself introduce an await.
   */
  function gatePermissions(tabId) {
    var info = originInfoByTab[tabId];
    var missing = info ? info.missing : [];
    if (!missing.length) return { requested: [], promise: Promise.resolve(true) };
    return { requested: missing, promise: chrome.permissions.request({ origins: missing }) };
  }

  function setStatus(text, isError) {
    statusBox.textContent = text || '';
    statusBox.className = 'status' + (isError ? ' err' : '');
  }

  function setDot(kind) {
    serviceDot.className = 'dot' + (kind ? ' ' + kind : '');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function canScript(url) {
    return typeof url === 'string' && /^https?:\/\//.test(url);
  }

  async function ensureInjected(tabId) {
    await chrome.scripting.executeScript({ target: { tabId: tabId }, files: ['scanner.js', 'capture.js', 'content.js'] });
  }

  // Fill (unlike Report) must reach every frame the operator granted — including a cross-origin
  // iframe embedding e.g. a Greenhouse form on a company's own careers page — so it injects with
  // allFrames: true. Chrome silently skips any frame this extension lacks host permission for
  // rather than failing the whole call; gatePermissions() above is what makes sure every frame
  // this fill will actually try to use has already been granted before this runs.
  async function ensureInjectedAllFrames(tabId) {
    await chrome.scripting.executeScript({ target: { tabId: tabId, allFrames: true }, files: ['scanner.js', 'capture.js', 'content.js'] });
  }

  async function safeGetTab(tabId) {
    try {
      return await chrome.tabs.get(tabId);
    } catch (e) {
      return null; // tab closed, or this is a tabId this profile can no longer see
    }
  }

  // Counts that mean something, shown as one headline line directly under the primary button:
  // "Filled 12 · 2 drafts to review · 3 need you". Reads the COUNTS content.js stored, rather
  // than recomputing from the lists, so this stays correct even if a future change trims a
  // list for display without updating the count (or vice versa).
  function buildSummaryLine(state) {
    var c = state.counts || {};
    var parts = ['Filled ' + (c.filled || 0)];
    if (c.drafts) parts.push(c.drafts + ' draft' + (c.drafts === 1 ? '' : 's') + ' to review');
    if (c.needsYou) parts.push(c.needsYou + (c.needsYou === 1 ? ' needs' : ' need') + ' you');
    var line = parts.join(' · ');
    if (c.failed) line += ' · ' + c.failed + ' failed';
    // Honest reporting (README "Report honestly"): visible interactive controls the scanner
    // never registered (unrecognized widgets, plus any cross-origin frame that could not be
    // injected at all) — never silently folded into "Filled" or left out of the summary.
    if (state.couldNotRead) line += ' · ' + state.couldNotRead + " couldn't read";
    return line;
  }

  // Résumé attachment gets its own clear line, independent of the field results — content.js
  // always reports it (see its file header) and this ALWAYS renders something from it. Hiding
  // this line whenever there was nothing to report is precisely how an operator on a page with
  // no file field ends up concluding résumé attachment doesn't work at all.
  function renderResumeLine(resume) {
    var r = resume || { attempted: false };
    resumeLineEl.hidden = false;

    // Build spec item 3: say which file actually got attached — background.js's
    // callResumeForAttach()/content.js's maybeAttachResume() tag every result with `kind`
    // ('base' or 'tailored'); a result from before this feature (or one this tab never routed
    // through the tailored path) simply has no kind, so nothing extra is shown for it.
    var kindSuffix = r.kind === 'tailored' ? ' (tailored)' : (r.kind === 'base' ? ' (base)' : '');

    if (r.attempted && r.attached) {
      resumeLineEl.className = 'resume-line ok';
      resumeLineEl.textContent = '✓ Attached ' + (r.filename || 'résumé file') + kindSuffix + '.';
      return;
    }
    if (r.attempted && r.alreadyAttached) {
      resumeLineEl.className = 'resume-line ok';
      resumeLineEl.textContent = '✓ A résumé is already attached' + (r.filename ? (' (' + r.filename + ')') : '') + ' — left it as is.';
      return;
    }
    if (r.attempted && !r.attached && r.errorCode === 'no-resume') {
      resumeLineEl.className = 'resume-line warn';
      resumeLineEl.textContent = 'No résumé stored — upload one in Settings.';
      return;
    }
    if (r.attempted && !r.attached) {
      resumeLineEl.className = 'resume-line err';
      resumeLineEl.textContent = "Couldn't attach" + (r.reason ? ' — ' + r.reason : '') + '. Attach it yourself before submitting.';
      return;
    }
    resumeLineEl.className = 'resume-line muted';
    resumeLineEl.textContent = 'No résumé upload on this page.';
  }

  // Item 5 (Review rows): the human-facing word for each of content.js's fixed `status` values
  // (see applyFills()/verifyAppliedFills() there), and which CSS modifier draws it.
  var STATUS_LABELS = {
    verified: 'Verified', draft: 'Draft', left_for_you: 'Left for you',
    kept_value: 'Kept your value', failed: 'Failed', didnt_stick: "Didn't stick"
  };

  /** Required-not-yet-filled rows first, stable otherwise — item 5's "required fields that are
   * not filled come first". Only ever applied to needsYou/failed (nothing in filled/drafts is
   * "not filled"). */
  function sortRequiredFirst(list) {
    return list
      .map(function (item, idx) { return { item: item, idx: idx }; })
      .sort(function (a, b) {
        var ra = a.item.required ? 0 : 1;
        var rb = b.item.required ? 0 : 1;
        return ra !== rb ? ra - rb : a.idx - b.idx;
      })
      .map(function (w) { return w.item; });
  }

  /** One row: label + status badge, its value (facts/drafts only — never for needsYou/failed,
   * which never had one), and a reason/source line. `data-field-id` (the qualified id
   * background.js's applyFrameReport() stamped on) is what a click routes to SCROLL_TO_FIELD —
   * absent (e.g. a row from before this build's ids existed) simply makes that row unclickable,
   * never an error.
   */
  function buildRow(entry, cssClass, showValue) {
    var row = document.createElement('div');
    row.className = 'field-row ' + cssClass;
    if (entry.id) {
      row.dataset.fieldId = entry.id;
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.title = 'Click to jump to this field on the page';
    }
    var statusText = STATUS_LABELS[entry.status] || '';
    var html = '<div class="row-top"><span class="label">' + escapeHtml(entry.label || '(unlabeled field)') + '</span>' +
      (statusText ? '<span class="status-badge status-' + escapeHtml(entry.status) + '">' + escapeHtml(statusText) + '</span>' : '') +
      '</div>';
    if (showValue && entry.value != null && entry.value !== '') {
      html += '<div class="value">' + escapeHtml(entry.value) + '</div>';
    }
    var reasonBits = [];
    if (entry.reason) reasonBits.push(escapeHtml(entry.reason));
    if (entry.source) reasonBits.push('source: ' + escapeHtml(entry.source));
    if (entry.profile_key) reasonBits.push(escapeHtml(entry.profile_key));
    if (reasonBits.length) html += '<div class="reason">' + reasonBits.join(' &middot; ') + '</div>';
    row.innerHTML = html;
    return row;
  }

  // Facts / drafts / needs-you / failed are always rendered as separate, clearly-labelled
  // groups — never merged into one flat list (drafts in particular must never be mistaken for
  // a fact pulled straight from the profile).
  async function renderResults(state) {
    resultsBox.innerHTML = '';
    var filled = state.filled || [];
    var drafts = state.drafts || [];
    var needsYou = sortRequiredFirst(state.needsYou || []);
    var failed = sortRequiredFirst(state.failed || []);

    if (filled.length) {
      var filledTitle = document.createElement('div');
      filledTitle.className = 'section-title';
      filledTitle.textContent = 'Filled (' + filled.length + ')';
      resultsBox.appendChild(filledTitle);
      filled.forEach(function (a) { resultsBox.appendChild(buildRow(a, 'filled', true)); });
    }

    if (drafts.length) {
      var draftTitle = document.createElement('div');
      draftTitle.className = 'section-title draft-title';
      draftTitle.textContent = 'Drafted — review before submitting (' + drafts.length + ')';
      resultsBox.appendChild(draftTitle);
      drafts.forEach(function (a) { resultsBox.appendChild(buildRow(a, 'draft', true)); });
    }

    if (needsYou.length) {
      var skipTitle = document.createElement('div');
      skipTitle.className = 'section-title';
      skipTitle.textContent = 'Need you (' + needsYou.length + ')';
      resultsBox.appendChild(skipTitle);
      needsYou.forEach(function (s) { resultsBox.appendChild(buildRow(s, 'skipped', false)); });

      // A single, once-per-render nudge: only when drafts are off (the operator's own Settings
      // toggle, a LOCAL-ONLY preference read directly from storage) AND at least one "need
      // you" field is a genuinely open-ended question (a <textarea>, tagged onto the needsYou
      // entry by content.js).
      var draftsPref = await chrome.storage.local.get(['smartFillDraftsEnabled']);
      var draftsEnabledLocally = draftsPref.smartFillDraftsEnabled === true;
      if (!draftsEnabledLocally) {
        var hasOpenEnded = needsYou.some(function (s) { return s.tag === 'textarea'; });
        if (hasOpenEnded) {
          var hint = document.createElement('div');
          hint.className = 'draft-hint';
          hint.textContent = 'Turn on drafts in Settings to get a first draft of open-ended answers.';
          resultsBox.appendChild(hint);
        }
      }
    }

    if (failed.length) {
      var failTitle = document.createElement('div');
      failTitle.className = 'section-title';
      failTitle.textContent = 'Could not fill (' + failed.length + ')';
      resultsBox.appendChild(failTitle);
      failed.forEach(function (f) { resultsBox.appendChild(buildRow(f, 'failed', false)); });
    }

    if (!resultsBox.children.length) {
      resultsBox.innerHTML = '<div class="status">Nothing to show yet.</div>';
    }
  }

  function renderProgress(progress) {
    if (!progress || !progress.total) {
      progressBox.hidden = true;
      return;
    }
    progressBox.hidden = false;
    progressLabel.textContent = 'Filling ' + progress.current + '/' + progress.total + (progress.label ? (' — ' + progress.label) : '');
    var pct = progress.total ? Math.round((progress.current / progress.total) * 100) : 0;
    progressFill.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  /**
   * The single render pass for "whichever tab is active in this window". Called on init, on
   * every chrome.tabs.onActivated / onUpdated relevant to this tab, on every
   * chrome.storage.onChanged for this tab's key, and right after this panel's own button
   * clicks (for immediate feedback — chrome.storage.onChanged will re-confirm it moments
   * later, so a lost race here is only ever cosmetic, never a correctness issue).
   */
  async function renderForTab(tabId) {
    if (tabId == null) return;
    var tab = await safeGetTab(tabId);

    if (!tab) {
      staleBanner.hidden = true;
      setStatus('This tab is no longer open.', true);
      scanBtn.disabled = true;
      cancelBtn.hidden = true;
      undoBtn.disabled = true;
      reportBtn.disabled = true;
      exportReportBtn.disabled = true;
      tailorResumeBtn.disabled = true;
      tailorResumeUseBtn.disabled = true;
      attachTailoredBtn.disabled = true;
      progressBox.hidden = true;
      fillSummaryEl.hidden = true;
      resumeLineEl.hidden = true;
      resultsBox.innerHTML = '';
      return;
    }
    reportBtn.disabled = false;

    var stored = await chrome.storage.session.get(stateKey(tabId));
    var state = stored[stateKey(tabId)] || null;

    // Never present a previous page's results as if they were this page's: compare the URL the
    // fill actually ran against to the tab's CURRENT url. A mismatch means the tab navigated
    // since — the content-script world (and its field registry) from that run is gone even if
    // the tab itself is still open.
    var stale = !!(state && state.url && tab.url && state.url !== tab.url);
    staleBanner.hidden = !stale;
    if (stale) {
      staleBanner.textContent = 'The results below are from a previous page on this tab (it has since navigated to ' +
        (tab.url.length > 60 ? tab.url.slice(0, 60) + '…' : tab.url) + ') — not current.';
    }

    var isRunning = !!(state && state.status === 'running' && !stale);
    if (!isRunning && cancelRequestedForTab === tabId) cancelRequestedForTab = null;

    var scriptable = canScript(tab.url);
    if (scriptable) {
      // Precompute (never at click time — see gatePermissions()) which of this tab's origins
      // (its own, plus every frame's) are already granted, so the Fill/Report click handlers
      // can call chrome.permissions.request() synchronously with no await first.
      await refreshOriginInfo(tabId, tab.url);
    } else {
      delete originInfoByTab[tabId];
    }
    scanBtn.disabled = !scriptable || isRunning;
    cancelBtn.hidden = !isRunning;
    undoBtn.disabled = isRunning || stale || !state || !state.undoAvailable;
    // Tailor my résumé / Use for this application / Attach tailored résumé all touch the page
    // (page-text extraction, or the résumé file input directly) — same "must not run while a
    // fill is already touching this frame" reasoning as scanBtn/continuationToggle above.
    tailorResumeBtn.disabled = !scriptable || isRunning;
    tailorResumeUseBtn.disabled = isRunning;
    attachTailoredBtn.disabled = isRunning;
    // Item 6 (Export fill report): anything worth reporting on — enabled once there's at least
    // one row in any of the four lists, on a result that's current for this page.
    var hasRows = !!(state && (((state.filled || []).length) + ((state.drafts || []).length) +
      ((state.needsYou || []).length) + ((state.failed || []).length)) > 0);
    exportReportBtn.disabled = isRunning || stale || !hasRows;

    // Item 8 (multi-step continuation): render whatever background.js has recorded — this file
    // never decides on its own whether the watch is on. "Never on a different host than the one
    // the toggle was turned on for" is enforced on the WATCHING side too (content.js self-disarms
    // on an in-page navigation to a different host — see runContinuationCheck()), but a full page
    // reload/navigation tears down that content.js instance entirely without a chance to report
    // back, so this is the other half: if the tab's CURRENT host no longer matches the host the
    // toggle was armed for, treat it as off here and tell background.js to clean up the stale
    // record, rather than showing a toggle that looks on but has nothing left watching.
    var cont = state && state.continuation;
    if (cont && cont.enabled && scriptable) {
      var currentHost = null;
      try { currentHost = new URL(tab.url).hostname; } catch (e) { /* ignore */ }
      if (currentHost && cont.host && currentHost !== cont.host) {
        chrome.runtime.sendMessage({ type: 'SET_CONTINUATION', tabId: tabId, enabled: false }).catch(function () {});
        cont = null;
      }
    }
    continuationToggle.checked = !!(cont && cont.enabled);
    continuationToggle.disabled = !scriptable || isRunning;
    if (cont && cont.enabled && cont.steps > 0) {
      continuationStepLineEl.hidden = false;
      continuationStepLineEl.textContent = 'Step ' + cont.steps;
    } else {
      continuationStepLineEl.hidden = true;
    }

    renderProgress(isRunning ? state.progress : null);

    if (!scriptable) {
      setStatus('Open a job application page (http/https) in this tab to use ApplyPilot here.', true);
    } else if (isRunning) {
      setStatus(cancelRequestedForTab === tabId
        ? 'Cancelling — finishing the field in progress, then stopping…'
        : 'Filling…');
    } else if (!state) {
      setStatus('Ready — click Fill this page to scan and fill.');
    } else if (state.permissionNeeded && (state.permissionNeeded.hosts || []).length) {
      // Item 7: the fill-page keyboard shortcut found a cross-origin frame it wasn't already
      // allowed to fill and refused to partially fill the rest silently — same wording the
      // panel's own Fill/Report click handlers use when chrome.permissions.request() is declined.
      setStatus('ApplyPilot needs permission to fill forms on ' + state.permissionNeeded.hosts.join(', ') +
        ' — nothing runs until you allow it. Click Fill this page to grant it.', true);
    } else if (state.error) {
      setStatus('SAFETY/ERROR: ' + state.error, true);
    } else if (state.shieldFired) {
      setStatus('SAFETY: blocked an attempted form submit while filling. Review this page very carefully before doing anything else.', true);
    } else if (state.note) {
      setStatus(state.note);
    } else if (state.status === 'cancelled') {
      setStatus('Cancelled. Fields filled so far are kept — review below.');
    } else if (state.status === 'done') {
      setStatus('Done. Review below, then submit yourself when ready.');
    } else {
      setStatus('Ready — click Fill this page to scan and fill.');
    }

    if (!state || isRunning || state.status === 'idle') {
      fillSummaryEl.hidden = true;
      resumeLineEl.hidden = true;
      resultsBox.innerHTML = '';
      rememberBoxEl.hidden = true;
      logLineEl.hidden = true;
      return;
    }

    fillSummaryEl.hidden = false;
    var line = buildSummaryLine(state);
    if (state.skippedFrames) {
      line += ' — ' + state.skippedFrames + ' embedded frame(s) could not be scanned (likely cross-origin)';
    }
    if (state.shieldFired) line += ' — ⚠ submit shield fired, see above';
    fillSummaryEl.textContent = line;
    renderResumeLine(state.resume);
    await renderResults(state);

    // "Remember my answers" (item 3) only makes sense once there's at least one field a fill
    // left for the operator to answer themselves, on a result that's actually current for this
    // page (never a stale, previous-page result — see `stale` above).
    rememberBoxEl.hidden = stale || !((state.needsYou || []).length > 0);

    // Application log (item 4): background.js logs a fill automatically right after it
    // completes (see logFillCompletion() there) and stores the result as state.logEntry — this
    // is a pure renderer for that, never itself the thing that decides to log.
    if (!stale && state.logEntry && state.logEntry.id) {
      logLineEl.hidden = false;
      var applied = state.logEntry.status === 'applied';
      logStatusTextEl.textContent = applied ? 'Logged — marked as applied.' : 'Logged.';
      markAppliedBtn.hidden = applied;
    } else {
      logLineEl.hidden = true;
    }
  }

  function setActiveTab(tabId) {
    activeTabId = tabId;
    // A cover-letter draft is specific to whichever job/tab it was written for — never carry it
    // over to a different tab you switch to (see "draft cover letter" below).
    coverLetterFieldForTab = null;
    coverLetterBoxEl.hidden = true;
    setCoverLetterStatus('');
    // Likewise, a tailored résumé (and the offer to attach it) is specific to whichever tab/job
    // it was drafted for — see "TAILOR MY RÉSUMÉ" below.
    tailorResumeForTab = null;
    tailorResumeBoxEl.hidden = true;
    setTailorResumeStatus('');
    setTailorResumeUseStatus('');
    attachTailoredRowEl.hidden = true;
    // Likewise, a "Saved N answers" confirmation is specific to whichever tab/fill produced it.
    setRememberStatus('');
    rememberDetailsEl.innerHTML = '';
    renderForTab(tabId);
  }

  chrome.tabs.onActivated.addListener(function (info) {
    if (pinnedTabId != null) return; // test-only pin: ignore real activation changes entirely
    if (currentWindowId != null && info.windowId !== currentWindowId) return;
    setActiveTab(info.tabId);
  });

  chrome.tabs.onUpdated.addListener(function (tabId, changeInfo) {
    if (tabId !== activeTabId) return;
    if (changeInfo.url || changeInfo.status) renderForTab(activeTabId);
  });

  chrome.storage.onChanged.addListener(function (changes, areaName) {
    if (areaName !== 'session' || activeTabId == null) return;
    if (Object.prototype.hasOwnProperty.call(changes, stateKey(activeTabId))) {
      renderForTab(activeTabId);
    }
  });

  // AUTO-CONNECT (see background.js): background.js already tried the native host itself before
  // answering HEALTH (see requestWithAutoConnect there), so by the time this runs the outcome is
  // final for this check — this only decides what to SHOW. chrome.storage.local.serviceConnection
  // is written on every attempt (native success/failure, or "host not installed at all"); the one
  // case worth a dedicated hint is "host not installed" while nothing is configured yet, since
  // that's the one thing the operator can fix in one command.
  async function maybeNativeHostTip() {
    try {
      var data = await chrome.storage.local.get(['serviceConnection', 'token']);
      var conn = data.serviceConnection;
      if (!data.token && conn && conn.mode === 'manual' && conn.ok === false &&
          /native messaging host not found/i.test(conn.error || '')) {
        return ' Tip: run `applypilot extension install-host` once and the service will start by itself.';
      }
    } catch (e) {
      // best-effort hint only — never blocks the rest of the status line
    }
    return '';
  }

  async function checkHealth() {
    try {
      var resp = await chrome.runtime.sendMessage({ type: 'HEALTH' });
      if (resp && resp.ok) {
        setDot('ok');
        var tiers = (resp.data && resp.data.tiers_available) || [];
        tiersLine.textContent = 'Service connected. Tiers: ' + (tiers.length ? tiers.join(', ') : 'none reported');
      } else {
        setDot('err');
        var tip = await maybeNativeHostTip();
        tiersLine.textContent = ((resp && resp.message) || 'Service unreachable.') + tip;
      }
    } catch (e) {
      setDot('err');
      tiersLine.textContent = 'Could not reach background worker.';
    }
  }

  // Fill this page: gatePermissions() below is called SYNCHRONOUSLY, with no `await` before it,
  // so a still-missing origin's chrome.permissions.request() prompt is honored as part of THIS
  // click's user gesture (see the block comment above gatePermissions()). Everything after that
  // point is ordinary async setup — inject, then hand the tab off to background.js, which
  // coordinates every frame's fill from here (see background.js's file header).
  scanBtn.addEventListener('click', function () {
    if (activeTabId == null) return;
    var tabId = activeTabId;
    var gate = gatePermissions(tabId);
    gate.promise.then(async function (granted) {
      if (!granted) {
        setStatus('ApplyPilot needs permission to fill forms on ' +
          gate.requested.map(hostFromPattern).join(', ') + ' — nothing runs until you allow it.', true);
        return;
      }
      var tab = await safeGetTab(tabId);
      if (!tab || !canScript(tab.url)) {
        setStatus('Open a job application page (http/https) in this tab, then try again.', true);
        return;
      }
      scanBtn.disabled = true;
      try {
        // The ONLY two places this extension ever injects a script into a page: this click, and
        // Report page below. Never on load, never on a tab switch.
        await ensureInjectedAllFrames(tabId);
        var resp = await chrome.runtime.sendMessage({ type: 'RUN_FILL', tabId: tabId, url: tab.url });
        if (!resp || !resp.ok) {
          setStatus('Could not start the fill' + ((resp && resp.error) ? (': ' + resp.error) : '') + '.', true);
          scanBtn.disabled = false;
          return;
        }
        // Deliberately not awaiting completion here — the fill now runs independently of this
        // panel, in every frame, coordinated by background.js (see its file header).
        // chrome.storage.onChanged drives every further UI update.
        setStatus('Filling…');
      } catch (e) {
        setStatus('Could not access this page (' + (e && e.message ? e.message : e) + '). Some pages (chrome://, the Web Store, PDF viewer) cannot be scripted.', true);
        scanBtn.disabled = false;
      }
    });
  });

  cancelBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    cancelRequestedForTab = activeTabId;
    try {
      // Fans out to every frame currently participating in this tab's fill — see
      // background.js's cancelFillForTab().
      await chrome.runtime.sendMessage({ type: 'CANCEL_FILL_TAB', tabId: activeTabId });
    } catch (e) {
      setStatus('Could not cancel: ' + (e && e.message ? e.message : e), true);
    } finally {
      await renderForTab(activeTabId);
    }
  });

  // ---------------------------------------------------------------------
  // MULTI-STEP CONTINUATION (item 8) toggle. OFF by default. Turning it ON is gated on the SAME
  // synchronous permission-then-inject pattern Fill/Report use (content.js must already be
  // listening in the top frame for SET_CONTINUATION to reach it) — nothing here runs on a page
  // until this click. Turning it OFF never touches permissions or injection at all.
  // ---------------------------------------------------------------------
  continuationToggle.addEventListener('change', function () {
    if (activeTabId == null) return;
    var tabId = activeTabId;
    var wantOn = continuationToggle.checked;
    if (!wantOn) {
      chrome.runtime.sendMessage({ type: 'SET_CONTINUATION', tabId: tabId, enabled: false }).catch(function () {});
      return;
    }
    var gate = gatePermissions(tabId); // synchronous, no await before this — see gatePermissions()
    gate.promise.then(async function (granted) {
      if (!granted) {
        continuationToggle.checked = false;
        setStatus('ApplyPilot needs permission to keep filling on ' +
          gate.requested.map(hostFromPattern).join(', ') + ' — nothing runs until you allow it.', true);
        return;
      }
      var tab = await safeGetTab(tabId);
      if (!tab || !canScript(tab.url)) { continuationToggle.checked = false; return; }
      try {
        await ensureInjected(tabId); // top frame only — content.js must be listening for this
        var resp = await chrome.runtime.sendMessage({ type: 'SET_CONTINUATION', tabId: tabId, enabled: true, tabUrl: tab.url });
        if (!resp || !resp.ok) {
          continuationToggle.checked = false;
          setStatus((resp && resp.error) || 'Could not enable step continuation.', true);
        }
        await renderForTab(tabId);
      } catch (e) {
        continuationToggle.checked = false;
        setStatus('Could not enable step continuation: ' + (e && e.message ? e.message : e), true);
      }
    });
  });

  undoBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    try {
      // Fans out to every frame and sums how many fields were actually restored (read back),
      // not how many restores were merely attempted — see background.js's undoFillForTab() and
      // content.js's undo().
      var resp = await chrome.runtime.sendMessage({ type: 'UNDO_TAB', tabId: activeTabId });
      var notRestored = (resp && resp.notRestored) || 0;
      setStatus('Restored ' + ((resp && resp.restored) || 0) + ' field(s) to their previous values.' +
        (notRestored ? (' ' + notRestored + ' could not be confirmed restored — check them by hand.') : ''),
        notRestored > 0);
      await renderForTab(activeTabId);
    } catch (e) {
      setStatus('Could not undo: ' + (e && e.message ? e.message : e), true);
    }
  });

  // "Report this page": download the form's STRUCTURE (never values) so a page that fills
  // badly can be diagnosed from its real markup instead of a guess. Only ever touches the top
  // frame (unchanged from before this build) — gated on the same permission check as Fill
  // since it also injects a script into the tab.
  reportBtn.addEventListener('click', function () {
    if (activeTabId == null) return;
    var tabId = activeTabId;
    var gate = gatePermissions(tabId);
    gate.promise.then(async function (granted) {
      if (!granted) {
        setStatus('ApplyPilot needs permission to read this page on ' +
          gate.requested.map(hostFromPattern).join(', ') + ' — nothing runs until you allow it.', true);
        return;
      }
      reportBtn.disabled = true;
      try {
        await ensureInjected(tabId);
        var resp = await chrome.tabs.sendMessage(tabId, { type: 'CAPTURE' });
        if (!resp || !resp.ok) throw new Error((resp && resp.error) || 'no response');
        var blob = new Blob([JSON.stringify(resp.structure, null, 2)], { type: 'application/json' });
        var a = document.createElement('a');
        var host = (resp.structure.host || 'page').replace(/[^a-z0-9.-]/gi, '_');
        a.href = URL.createObjectURL(blob);
        a.download = 'applypilot-page-' + host + '.json';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
        setStatus('Saved the page structure (no field values included). Send the file to your developer.');
      } catch (e) {
        setStatus('Could not capture this page: ' + (e && e.message ? e.message : e), true);
      } finally {
        reportBtn.disabled = false;
      }
    });
  });

  // ---------------------------------------------------------------------
  // EXPORT FILL REPORT (item 6). Purely a transform of state already sitting in
  // chrome.storage.session — no page access, no permission gate, nothing injected. Per field:
  // frame, label, tag/widget, status, source and reason — NEVER a value, on purpose, since this
  // is meant to be sent to someone else to diagnose a bad fill.
  // ---------------------------------------------------------------------
  // Reviewer round 3, item 1: a free-text `reason` can carry a real value — a quoted attempted
  // value ("Senior Engineer"), but just as easily one in single/curly quotes or no quotes at all
  // (an unquoted skills term, a canary question's own wording), and the SERVICE's own `reason`
  // text is outside this file's control regardless of how carefully it's written today. Rather
  // than pattern-match for quotes (the old redactQuoted() approach, which only ever caught the
  // double-quoted case and is removed here), this export is now built from an ALLOW-LIST: every
  // field content.js already tags with a fixed, closed-vocabulary `category` at the exact point
  // it's created (see content.js's categoryForSource() and every push site in applyFills()) — the
  // free-text `reason` itself never leaves this function, for ANY row, under any circumstance.
  var KNOWN_REPORT_CATEGORIES = {
    canary: 1, deterministic: 1, structured: 1, laya: 1, answer_bank: 1, draft: 1,
    secret_guard: 1, unresolved: 1, other: 1, kept_value: 1, didnt_stick: 1, timed_out: 1,
    no_match: 1, cancelled: 1, time_budget: 1, field_missing: 1
  };
  function reportRow(entry) {
    return {
      frame: entry.frame || null,
      label: entry.label || '',
      tag: entry.tag || '',
      widget: entry.widget || '',
      status: entry.status || '',
      source: entry.source || '',
      // Defensively re-validated against the SAME allow-list one more time here (belt and
      // braces, matching this codebase's own habit elsewhere) so a future bug that puts
      // something unexpected into `category` still can't smuggle free text into this file.
      category: KNOWN_REPORT_CATEGORIES[entry.category] ? entry.category : 'other'
    };
  }

  function buildFillReport(state) {
    var host = '', path = '';
    try {
      var u = new URL(state.url || '');
      host = u.host;
      path = u.pathname;
    } catch (e) {
      // a state with no valid url at all — page/host stay empty rather than throwing
    }
    return {
      page: { host: host, path: path },
      counts: state.counts || {},
      couldNotRead: state.couldNotRead || 0,
      skippedFrames: state.skippedFrames || 0,
      fields: []
        .concat((state.filled || []).map(reportRow))
        .concat((state.drafts || []).map(reportRow))
        .concat((state.needsYou || []).map(reportRow))
        .concat((state.failed || []).map(reportRow))
    };
  }

  exportReportBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    try {
      var stored = await chrome.storage.session.get(stateKey(activeTabId));
      var state = stored[stateKey(activeTabId)];
      if (!state) { setStatus('Nothing to export yet — fill this page first.', true); return; }
      var report = buildFillReport(state);
      var blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
      var a = document.createElement('a');
      var hostPart = (report.page.host || 'page').replace(/[^a-z0-9.-]/gi, '_');
      a.href = URL.createObjectURL(blob);
      a.download = 'applypilot-fill-report-' + hostPart + '.json';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
      setStatus('Saved the fill report (labels/status/source only — never values). Send it when a form fills badly.');
    } catch (e) {
      setStatus('Could not export the fill report: ' + (e && e.message ? e.message : e), true);
    }
  });

  // ---------------------------------------------------------------------
  // DRAFT COVER LETTER (item 2). Top frame only, gated on the same permission check as Fill/
  // Report (it reads the page's visible text and scans for a cover-letter field). The service
  // call itself goes through background.js (it holds the token); everything else here talks
  // straight to content.js, the same pattern "Report page" already uses for CAPTURE.
  // ---------------------------------------------------------------------
  function setCoverLetterStatus(text, kind) {
    if (!text) { coverLetterStatusEl.hidden = true; coverLetterStatusEl.textContent = ''; return; }
    coverLetterStatusEl.hidden = false;
    coverLetterStatusEl.textContent = text;
    coverLetterStatusEl.className = 'cover-letter-status' + (kind ? ' ' + kind : '');
  }

  async function gatherFrameUrls(tabId, topUrl) {
    var urls = [topUrl];
    try {
      if (chrome.webNavigation && typeof chrome.webNavigation.getAllFrames === 'function') {
        var frames = await chrome.webNavigation.getAllFrames({ tabId: tabId });
        (frames || []).forEach(function (f) {
          if (f.url && urls.indexOf(f.url) === -1) urls.push(f.url);
        });
      }
    } catch (e) {
      // best-effort — the top URL alone is still a useful ATS-recognizable URL most of the time
    }
    return urls;
  }

  function renderCoverLetter(data) {
    coverLetterBoxEl.hidden = false;
    var job = data.job || {};
    var jobBits = [];
    if (job.title) jobBits.push(job.title);
    if (job.company) jobBits.push(job.company);
    var jobLine = jobBits.length ? jobBits.join(' · ') : 'Job details not identified';
    coverLetterJobEl.textContent = jobLine + ' — drafted by ' + (data.provider || 'the local model') +
      (job.source ? (' (job source: ' + job.source + ')') : '');
    coverLetterTextEl.value = data.text || '';
    var warnings = data.warnings || [];
    coverLetterWarningsEl.hidden = !warnings.length;
    if (warnings.length) coverLetterWarningsEl.textContent = 'Review before sending: ' + warnings.join(' · ');
    coverLetterInsertBtn.hidden = !coverLetterFieldForTab;
  }

  coverLetterBtn.addEventListener('click', function () {
    if (activeTabId == null) return;
    var tabId = activeTabId;
    // Synchronous, no `await` before it — see the block comment on gatePermissions() above.
    var gate = gatePermissions(tabId);
    gate.promise.then(async function (granted) {
      if (!granted) {
        setCoverLetterStatus('ApplyPilot needs permission to read this page on ' +
          gate.requested.map(hostFromPattern).join(', ') + ' — nothing runs until you allow it.', 'error');
        return;
      }
      var tab = await safeGetTab(tabId);
      if (!tab || !canScript(tab.url)) {
        setCoverLetterStatus('Open a job application page (http/https) in this tab, then try again.', 'error');
        return;
      }
      coverLetterBtn.disabled = true;
      coverLetterBoxEl.hidden = true;
      coverLetterFieldForTab = null;
      setCoverLetterStatus('Drafting…');
      try {
        // Top frame only — content.js's EXTRACT_PAGE_TEXT/FIND_COVER_LETTER_FIELD (scanner.js is
        // a dependency of both; capture.js rides along unused, same as every other injection here).
        await ensureInjected(tabId);
        var textResp = await chrome.tabs.sendMessage(tabId, { type: 'EXTRACT_PAGE_TEXT' });
        var fieldResp = await chrome.tabs.sendMessage(tabId, { type: 'FIND_COVER_LETTER_FIELD' });
        coverLetterFieldForTab = (fieldResp && fieldResp.field) || null;
        var urls = await gatherFrameUrls(tabId, tab.url);
        var resp = await chrome.runtime.sendMessage({
          type: 'DRAFT_COVER_LETTER', tabId: tabId, urls: urls,
          pageText: (textResp && textResp.text) || ''
        });
        if (!resp || !resp.ok) {
          // 403 (model not allowed on this computer) / 422 (no job description / drafting
          // refused) carry the service's own `.detail` verbatim — see handleResponseVerbatim()
          // in background.js — shown exactly as the service wrote it, no extra wrapping.
          setCoverLetterStatus((resp && (resp.detail || resp.message)) || 'Could not draft a cover letter.', 'error');
          return;
        }
        renderCoverLetter(resp.data || {});
        setCoverLetterStatus('');
      } catch (e) {
        setCoverLetterStatus('Could not draft a cover letter: ' + (e && e.message ? e.message : e), 'error');
      } finally {
        coverLetterBtn.disabled = false;
      }
    });
  });

  coverLetterCopyBtn.addEventListener('click', async function () {
    try {
      await navigator.clipboard.writeText(coverLetterTextEl.value);
      setCoverLetterStatus('Copied to clipboard.', 'ok');
    } catch (e) {
      setCoverLetterStatus('Could not copy: ' + (e && e.message ? e.message : e), 'error');
    }
  });

  coverLetterDownloadBtn.addEventListener('click', function () {
    var blob = new Blob([coverLetterTextEl.value], { type: 'text/plain' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'cover-letter.txt';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  });

  coverLetterInsertBtn.addEventListener('click', async function () {
    if (activeTabId == null || !coverLetterFieldForTab) return;
    var tabId = activeTabId;
    coverLetterInsertBtn.disabled = true;
    try {
      // Goes through content.js's normal guarded applyFill()/highlight() path — recorded as a
      // draft and undoable exactly like any other field (see insertCoverLetterDraft() there).
      var resp = await chrome.tabs.sendMessage(tabId, {
        type: 'INSERT_COVER_LETTER', fieldId: coverLetterFieldForTab.id, text: coverLetterTextEl.value
      });
      if (resp && resp.ok) {
        setCoverLetterStatus('Inserted into "' + (coverLetterFieldForTab.label || 'the cover letter field') +
          '" — review the page before submitting.', 'ok');
        await renderForTab(tabId);
      } else {
        setCoverLetterStatus((resp && resp.error) || 'Could not insert the draft into that field.', 'error');
      }
    } catch (e) {
      setCoverLetterStatus('Could not insert the draft: ' + (e && e.message ? e.message : e), 'error');
    } finally {
      coverLetterInsertBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // TAILOR MY RÉSUMÉ. Mirrors DRAFT COVER LETTER above: same permission gate, same top-frame
  // page-text source, same verbatim 403/422/503 handling (POST /resume/tailor -> {id, url,
  // title, company, status, judge:{verdict,issues}, warnings, created, pdf, text}). The review
  // box shows the status (and the judge's own issues when there's a warning), the validator
  // warnings, a collapsible preview of the tailored text, Download PDF and Use for this
  // application — never auto-inserted anywhere, always reviewed here first.
  // ---------------------------------------------------------------------
  function setTailorResumeStatus(text, kind) {
    if (!text) { tailorResumeStatusEl.hidden = true; tailorResumeStatusEl.textContent = ''; return; }
    tailorResumeStatusEl.hidden = false;
    tailorResumeStatusEl.textContent = text;
    tailorResumeStatusEl.className = 'cover-letter-status' + (kind ? ' ' + kind : '');
  }

  function setTailorResumeUseStatus(text, kind) {
    if (!text) { tailorResumeUseStatusEl.hidden = true; tailorResumeUseStatusEl.textContent = ''; return; }
    tailorResumeUseStatusEl.hidden = false;
    tailorResumeUseStatusEl.textContent = text;
    tailorResumeUseStatusEl.className = 'cover-letter-status' + (kind ? ' ' + kind : '');
  }

  function renderTailorResume(data) {
    tailorResumeBoxEl.hidden = false;
    var jobBits = [];
    if (data.title) jobBits.push(data.title);
    if (data.company) jobBits.push(data.company);
    tailorResumeJobEl.textContent = jobBits.length ? jobBits.join(' · ') : 'Job details not identified';

    var warned = data.status === 'approved_with_judge_warning';
    tailorResumeVerdictEl.className = 'tailor-resume-verdict' + (warned ? ' warn' : '');
    tailorResumeVerdictEl.textContent = warned
      ? 'Approved, with a note from the safety check — review carefully before using.'
      : 'Approved — passed the safety check.';

    var judge = data.judge || {};
    var issues = judge.issues || [];
    tailorResumeIssuesEl.hidden = !(warned && issues.length);
    if (warned && issues.length) tailorResumeIssuesEl.textContent = "The judge's own issues: " + issues.join(' · ');

    var warnings = data.warnings || [];
    tailorResumeWarningsEl.hidden = !warnings.length;
    if (warnings.length) tailorResumeWarningsEl.textContent = 'Review before using: ' + warnings.join(' · ');

    tailorResumeTextEl.value = data.text || '';
    tailorResumeForTab = { id: data.id, status: data.status || '' };
    setTailorResumeUseStatus('');
    attachTailoredRowEl.hidden = true;
  }

  tailorResumeBtn.addEventListener('click', function () {
    if (activeTabId == null) return;
    var tabId = activeTabId;
    // Synchronous, no `await` before it — see the block comment on gatePermissions() above.
    var gate = gatePermissions(tabId);
    gate.promise.then(async function (granted) {
      if (!granted) {
        setTailorResumeStatus('ApplyPilot needs permission to read this page on ' +
          gate.requested.map(hostFromPattern).join(', ') + ' — nothing runs until you allow it.', 'error');
        return;
      }
      var tab = await safeGetTab(tabId);
      if (!tab || !canScript(tab.url)) {
        setTailorResumeStatus('Open a job application page (http/https) in this tab, then try again.', 'error');
        return;
      }
      tailorResumeBtn.disabled = true;
      tailorResumeBoxEl.hidden = true;
      tailorResumeForTab = null;
      setTailorResumeStatus('Tailoring…');
      try {
        // Top frame only — same content.js EXTRACT_PAGE_TEXT the cover letter uses (scanner.js
        // is a dependency of both; capture.js rides along unused, same as every other injection).
        await ensureInjected(tabId);
        var textResp = await chrome.tabs.sendMessage(tabId, { type: 'EXTRACT_PAGE_TEXT' });
        var urls = await gatherFrameUrls(tabId, tab.url);
        var resp = await chrome.runtime.sendMessage({
          type: 'RESUME_TAILOR', tabId: tabId, urls: urls,
          pageText: (textResp && textResp.text) || ''
        });
        if (!resp || !resp.ok) {
          // 403/422/503 carry the service's own `.detail` verbatim — see handleResponseVerbatim()
          // in background.js, shown exactly as the service wrote it.
          setTailorResumeStatus((resp && (resp.detail || resp.message)) || 'Could not tailor a résumé.', 'error');
          return;
        }
        renderTailorResume(resp.data || {});
        setTailorResumeStatus('');
      } catch (e) {
        setTailorResumeStatus('Could not tailor a résumé: ' + (e && e.message ? e.message : e), 'error');
      } finally {
        tailorResumeBtn.disabled = false;
      }
    });
  });

  function base64ToBlob(base64, contentType) {
    var binary = atob(base64 || '');
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: contentType || 'application/pdf' });
  }

  tailorResumeDownloadBtn.addEventListener('click', async function () {
    if (!tailorResumeForTab || !tailorResumeForTab.id) return;
    tailorResumeDownloadBtn.disabled = true;
    try {
      // The panel can't fetch this itself (it never holds the token — see background.js's file
      // header); background.js hands back the bytes and this builds the download client-side,
      // same pattern the cover letter's own Download .txt already uses.
      var resp = await chrome.runtime.sendMessage({ type: 'GET_TAILORED_RESUME_PDF', id: tailorResumeForTab.id });
      if (!resp || !resp.ok) {
        setTailorResumeStatus((resp && resp.message) || 'Could not download the tailored résumé.', 'error');
        return;
      }
      var d = resp.data || {};
      var blob = base64ToBlob(d.base64, d.contentType);
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = d.filename || 'Resume.pdf'; // the service's own Content-Disposition filename
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
    } catch (e) {
      setTailorResumeStatus('Could not download the tailored résumé: ' + (e && e.message ? e.message : e), 'error');
    } finally {
      tailorResumeDownloadBtn.disabled = false;
    }
  });

  /** Renders what an ATTACH_RESUME_NOW-shaped result (from USE_TAILORED_RESUME's own immediate
   * attach, or ATTACH_TAILORED_RESUME_NOW) means for the operator — the three outcomes the build
   * spec calls for: nothing to attach to yet (fine — the next Fill will use it), something is
   * already attached (refuse, offer the retry button), or it attached (or failed to). */
  function renderAttachResult(attach) {
    if (!attach || !attach.attempted) {
      setTailorResumeUseStatus('Saved. If this page has a résumé upload, the next Fill will use the tailored PDF instead of the base résumé.', 'ok');
      attachTailoredRowEl.hidden = true;
      return;
    }
    if (attach.alreadyAttached) {
      setTailorResumeUseStatus('A résumé is already attached — remove it on the page, then click Attach tailored résumé.', 'error');
      attachTailoredRowEl.hidden = false;
      return;
    }
    if (attach.attached) {
      setTailorResumeUseStatus('Attached the tailored résumé to this page.', 'ok');
      attachTailoredRowEl.hidden = true;
      return;
    }
    setTailorResumeUseStatus('Saved, but could not attach it immediately' + (attach.reason ? (' — ' + attach.reason) : '') +
      '. The next Fill on this page will try again.', 'error');
    attachTailoredRowEl.hidden = true;
  }

  tailorResumeUseBtn.addEventListener('click', function () {
    if (activeTabId == null || !tailorResumeForTab || !tailorResumeForTab.id) return;
    var tabId = activeTabId;
    var useId = tailorResumeForTab.id;
    // Synchronous, no `await` before it — the immediate-attach half of this needs the page
    // scripted; a decline here still lets the preference itself save below (nothing about
    // recording "use the tailored PDF next" requires page access).
    var gate = gatePermissions(tabId);
    tailorResumeUseBtn.disabled = true;
    setTailorResumeUseStatus('Saving…');
    gate.promise.then(async function (granted) {
      try {
        var tab = await safeGetTab(tabId);
        if (granted && tab && canScript(tab.url)) {
          try { await ensureInjectedAllFrames(tabId); } catch (e) { /* best-effort — see below */ }
        }
        var resp = await chrome.runtime.sendMessage({ type: 'USE_TAILORED_RESUME', tabId: tabId, id: useId });
        if (!resp || !resp.ok) {
          setTailorResumeUseStatus((resp && resp.error) || 'Could not save this preference.', 'error');
          return;
        }
        renderAttachResult(resp.attach);
        await renderForTab(tabId);
      } catch (e) {
        setTailorResumeUseStatus('Could not save this preference: ' + (e && e.message ? e.message : e), 'error');
      } finally {
        tailorResumeUseBtn.disabled = false;
      }
    });
  });

  attachTailoredBtn.addEventListener('click', function () {
    if (activeTabId == null) return;
    var tabId = activeTabId;
    var gate = gatePermissions(tabId);
    attachTailoredBtn.disabled = true;
    gate.promise.then(async function (granted) {
      try {
        var tab = await safeGetTab(tabId);
        if (granted && tab && canScript(tab.url)) {
          try { await ensureInjectedAllFrames(tabId); } catch (e) { /* best-effort */ }
        }
        var resp = await chrome.runtime.sendMessage({ type: 'ATTACH_TAILORED_RESUME_NOW', tabId: tabId });
        if (!resp || !resp.ok) {
          setTailorResumeUseStatus((resp && resp.error) || 'Could not attach the tailored résumé.', 'error');
          return;
        }
        renderAttachResult(resp.attach);
        await renderForTab(tabId);
      } catch (e) {
        setTailorResumeUseStatus('Could not attach the tailored résumé: ' + (e && e.message ? e.message : e), 'error');
      } finally {
        attachTailoredBtn.disabled = false;
      }
    });
  });

  // ---------------------------------------------------------------------
  // REMEMBER MY ANSWERS (item 3). Never automatic — only ever runs on this click. All the actual
  // work (reading the right frames' current field values, filtering, calling /answers/learn)
  // happens in background.js's rememberAnswersForTab(); this is a thin renderer for its result.
  // ---------------------------------------------------------------------
  function setRememberStatus(text, kind) {
    if (!text) { rememberStatusEl.hidden = true; rememberStatusEl.textContent = ''; return; }
    rememberStatusEl.hidden = false;
    rememberStatusEl.textContent = text;
    rememberStatusEl.className = 'remember-status' + (kind ? ' ' + kind : '');
  }

  function renderRememberDetails(saved, skipped) {
    rememberDetailsEl.innerHTML = '';
    (saved || []).forEach(function (q) {
      var row = document.createElement('div');
      row.className = 'remember-row saved';
      row.textContent = 'Saved: ' + q;
      rememberDetailsEl.appendChild(row);
    });
    (skipped || []).forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'remember-row skipped';
      row.textContent = (s.question || '(no question text)') + ' — ' + (s.reason || 'skipped');
      rememberDetailsEl.appendChild(row);
    });
  }

  rememberBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    rememberBtn.disabled = true;
    setRememberStatus('Saving…');
    rememberDetailsEl.innerHTML = '';
    try {
      var resp = await chrome.runtime.sendMessage({ type: 'REMEMBER_ANSWERS', tabId: activeTabId });
      if (!resp || !resp.ok) {
        setRememberStatus((resp && (resp.detail || resp.message)) || 'Could not save your answers.', 'error');
        return;
      }
      if (resp.noAnswersFound) {
        setRememberStatus('Nothing to remember yet — none of the fields left for you have an answer typed in.');
        return;
      }
      var data = resp.data || {};
      var saved = data.saved || [];
      var skipped = data.skipped || [];
      var line = 'Saved ' + saved.length + (saved.length === 1 ? ' answer' : ' answers');
      if (skipped.length) line += ', skipped ' + skipped.length + (skipped.length === 1 ? ' question' : ' questions');
      setRememberStatus(line, 'ok');
      renderRememberDetails(saved, skipped);
    } catch (e) {
      setRememberStatus('Could not save your answers: ' + (e && e.message ? e.message : e), 'error');
    } finally {
      rememberBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // APPLICATION LOG (item 4). Logging itself already happened automatically (background.js,
  // right after the fill this state belongs to completed) — this button only ever sets the
  // status the applicant themselves knows is true (did you actually submit it?).
  // ---------------------------------------------------------------------
  markAppliedBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    var tabId = activeTabId;
    var stored = await chrome.storage.session.get(stateKey(tabId));
    var state = stored[stateKey(tabId)];
    var id = state && state.logEntry && state.logEntry.id;
    if (!id) return;
    markAppliedBtn.disabled = true;
    try {
      var resp = await chrome.runtime.sendMessage({ type: 'LOG_STATUS', tabId: tabId, id: id, status: 'applied' });
      if (!resp || !resp.ok) {
        setStatus((resp && (resp.detail || resp.message)) || 'Could not mark this application as applied.', true);
      }
      await renderForTab(tabId);
    } catch (e) {
      setStatus('Could not mark this application as applied: ' + (e && e.message ? e.message : e), true);
    } finally {
      markAppliedBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // REVIEW ROWS (item 5): clicking (or pressing Enter/Space on, for keyboard users) any row with
  // a field id scrolls that field into view in its own frame and flashes its highlight —
  // background.js's SCROLL_TO_FIELD splits the qualified id and messages the right frame. One
  // delegated listener (resultsBox's contents are fully replaced on every render — see
  // renderResults()/buildRow() above) rather than one per row.
  // ---------------------------------------------------------------------
  function scrollToRow(row) {
    if (!row || !row.dataset || !row.dataset.fieldId || activeTabId == null) return;
    chrome.runtime.sendMessage({ type: 'SCROLL_TO_FIELD', tabId: activeTabId, id: row.dataset.fieldId })
      .catch(function () {});
  }
  resultsBox.addEventListener('click', function (e) {
    scrollToRow(e.target.closest('[data-field-id]'));
  });
  resultsBox.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var row = e.target.closest('[data-field-id]');
    if (!row) return;
    e.preventDefault();
    scrollToRow(row);
  });

  optionsBtn.addEventListener('click', function () {
    chrome.runtime.openOptionsPage();
  });

  async function init() {
    try {
      var win = await chrome.windows.getCurrent();
      currentWindowId = win.id;
    } catch (e) {
      // best-effort — onActivated filtering just falls back to "any window" if this failed
    }

    if (pinnedTabId != null) {
      activeTabId = pinnedTabId;
    } else {
      var tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      activeTabId = tabs[0] ? tabs[0].id : null;
    }

    if (activeTabId == null) {
      setStatus('No active tab found in this window.', true);
      scanBtn.disabled = true;
      return;
    }

    await renderForTab(activeTabId);
    checkHealth();
    // The panel is now long-lived (that is the whole point of this build) — a one-time health
    // check at open, as the old popup did, would go stale the first time the local service
    // restarts while the panel stays docked open. Re-check periodically instead.
    setInterval(checkHealth, 20000);

    // TEST-ONLY marker: flips to true once the first render for the active tab has finished,
    // so scripts/chrome_panel_test.py can wait for init() to actually complete (activeTabId
    // assigned, first render done) instead of racing button clicks against page load. Nothing
    // in this file reads it back — production code never sets or checks it.
    window.__applyPilotPanelReady = true;
  }

  init();
})();
