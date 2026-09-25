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

  // -- remember my answers (item 3) --
  var rememberBoxEl = document.getElementById('rememberBox');
  var rememberBtn = document.getElementById('rememberBtn');
  var rememberStatusEl = document.getElementById('rememberStatus');
  var rememberDetailsEl = document.getElementById('rememberDetails');

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

    if (r.attempted && r.attached) {
      resumeLineEl.className = 'resume-line ok';
      resumeLineEl.textContent = '✓ Attached ' + (r.filename || 'résumé file') + '.';
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

  // Facts / drafts / needs-you / failed are always rendered as separate, clearly-labelled
  // groups — never merged into one flat list (drafts in particular must never be mistaken for
  // a fact pulled straight from the profile).
  async function renderResults(state) {
    resultsBox.innerHTML = '';
    var filled = state.filled || [];
    var drafts = state.drafts || [];
    var needsYou = state.needsYou || [];
    var failed = state.failed || [];

    if (filled.length) {
      var filledTitle = document.createElement('div');
      filledTitle.className = 'section-title';
      filledTitle.textContent = 'Filled (' + filled.length + ')';
      resultsBox.appendChild(filledTitle);
      filled.forEach(function (a) {
        var row = document.createElement('div');
        row.className = 'field-row filled';
        row.innerHTML =
          '<div class="value">' + escapeHtml(a.value) + '</div>' +
          '<div class="reason">' + escapeHtml(a.reason || '') + (a.profile_key ? ' &middot; ' + escapeHtml(a.profile_key) : '') + '</div>';
        resultsBox.appendChild(row);
      });
    }

    if (drafts.length) {
      var draftTitle = document.createElement('div');
      draftTitle.className = 'section-title draft-title';
      draftTitle.textContent = 'Drafted — review before submitting (' + drafts.length + ')';
      resultsBox.appendChild(draftTitle);
      drafts.forEach(function (a) {
        var row = document.createElement('div');
        row.className = 'field-row draft';
        row.innerHTML =
          '<div class="draft-badge">DRAFT</div>' +
          '<div class="value">' + escapeHtml(a.value) + '</div>' +
          '<div class="reason">' + escapeHtml(a.reason || '') + (a.profile_key ? ' &middot; ' + escapeHtml(a.profile_key) : '') + '</div>';
        resultsBox.appendChild(row);
      });
    }

    if (needsYou.length) {
      var skipTitle = document.createElement('div');
      skipTitle.className = 'section-title';
      skipTitle.textContent = 'Need you (' + needsYou.length + ')';
      resultsBox.appendChild(skipTitle);
      needsYou.forEach(function (s) {
        var row = document.createElement('div');
        row.className = 'field-row skipped';
        row.innerHTML = '<div class="reason">' + escapeHtml(s.reason || 'Left for you to fill in') + '</div>';
        resultsBox.appendChild(row);
      });

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
      failed.forEach(function (f) {
        var row = document.createElement('div');
        row.className = 'field-row failed';
        row.innerHTML = '<div class="reason">' + escapeHtml(f.reason || '') + '</div>';
        resultsBox.appendChild(row);
      });
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

    renderProgress(isRunning ? state.progress : null);

    if (!scriptable) {
      setStatus('Open a job application page (http/https) in this tab to use ApplyPilot here.', true);
    } else if (isRunning) {
      setStatus(cancelRequestedForTab === tabId
        ? 'Cancelling — finishing the field in progress, then stopping…'
        : 'Filling…');
    } else if (!state) {
      setStatus('Ready — click Fill this page to scan and fill.');
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
  }

  function setActiveTab(tabId) {
    activeTabId = tabId;
    // A cover-letter draft is specific to whichever job/tab it was written for — never carry it
    // over to a different tab you switch to (see "draft cover letter" below).
    coverLetterFieldForTab = null;
    coverLetterBoxEl.hidden = true;
    setCoverLetterStatus('');
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

  undoBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    try {
      // Fans out to every frame and sums how many fields were actually restored (read back),
      // not how many restores were merely attempted — see background.js's undoFillForTab() and
      // content.js's undo().
      var resp = await chrome.runtime.sendMessage({ type: 'UNDO_TAB', tabId: activeTabId });
      setStatus('Restored ' + ((resp && resp.restored) || 0) + ' field(s) to their previous values.');
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
