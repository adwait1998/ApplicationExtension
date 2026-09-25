/**
 * ApplyPilot Copilot — side panel logic.
 *
 * This replaces the old action popup. Chrome tears down a popup's whole JS context on any
 * focus change (switching tabs, clicking DevTools, ...), which used to kill a fill mid-way on
 * slow pages — see README.md. A side panel's document stays alive across tab switches and
 * window focus changes, so this file is now a pure, disposable RENDERER:
 *
 *   - it injects scanner.js/capture.js/content.js into a tab ONLY when the operator clicks
 *     Fill this page or Report page — never on load, never on a tab switch (that's the whole
 *     point of host_permissions covering http/https: this panel can always draw the RIGHT UI
 *     for whichever tab is active, but that is not the same as running anything on it);
 *   - it tells content.js to start a fill (START_FILL) and does NOT await the result — the
 *     fill runs to completion in the page regardless of whether this panel stays open, gets
 *     closed, or the operator switches to a different tab and back;
 *   - it never writes the fill-result state itself. content.js reports progress/results to
 *     background.js (via chrome.runtime.sendMessage), which is the only context that persists
 *     them into chrome.storage.session, keyed per tab id. This file only ever READS that
 *     storage (chrome.storage.session.get) and listens for chrome.storage.onChanged to render
 *     live progress and survive its own reloads;
 *   - because the panel is one instance per WINDOW (not per tab), it follows
 *     chrome.tabs.onActivated / onUpdated itself and always renders whichever tab is active in
 *     its own window — activeTab alone cannot inject into a tab you switched to after opening
 *     the panel, which is exactly why host_permissions for http/https were added (see
 *     README.md "Permissions").
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

  function stateKey(tabId) {
    return 'fillState_' + tabId;
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
  }

  function setActiveTab(tabId) {
    activeTabId = tabId;
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

  async function checkHealth() {
    try {
      var resp = await chrome.runtime.sendMessage({ type: 'HEALTH' });
      if (resp && resp.ok) {
        setDot('ok');
        var tiers = (resp.data && resp.data.tiers_available) || [];
        tiersLine.textContent = 'Service connected. Tiers: ' + (tiers.length ? tiers.join(', ') : 'none reported');
      } else {
        setDot('err');
        tiersLine.textContent = (resp && resp.message) || 'Service unreachable.';
      }
    } catch (e) {
      setDot('err');
      tiersLine.textContent = 'Could not reach background worker.';
    }
  }

  scanBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    var tab = await safeGetTab(activeTabId);
    if (!tab || !canScript(tab.url)) {
      setStatus('Open a job application page (http/https) in this tab, then try again.', true);
      return;
    }
    scanBtn.disabled = true;
    try {
      // The ONLY two places this extension ever injects a script into a page: this click, and
      // Report page below. Never on load, never on a tab switch.
      await ensureInjected(activeTabId);
      var resp = await chrome.tabs.sendMessage(activeTabId, { type: 'START_FILL' });
      if (!resp || !resp.ok) {
        setStatus('Could not start the fill' + ((resp && resp.error) ? (': ' + resp.error) : '') + '.', true);
        scanBtn.disabled = false;
        return;
      }
      // Deliberately not awaiting completion here — the fill now runs independently of this
      // panel (see file header). chrome.storage.onChanged drives every further UI update.
      setStatus('Filling…');
    } catch (e) {
      setStatus('Could not access this page (' + (e && e.message ? e.message : e) + '). Some pages (chrome://, the Web Store, PDF viewer) cannot be scripted.', true);
      scanBtn.disabled = false;
    }
  });

  cancelBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    cancelRequestedForTab = activeTabId;
    try {
      await chrome.tabs.sendMessage(activeTabId, { type: 'CANCEL_FILL' });
    } catch (e) {
      setStatus('Could not cancel: ' + (e && e.message ? e.message : e), true);
    } finally {
      await renderForTab(activeTabId);
    }
  });

  undoBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    try {
      var resp = await chrome.tabs.sendMessage(activeTabId, { type: 'UNDO' });
      setStatus('Restored ' + ((resp && resp.restored) || 0) + ' field(s) to their previous values.');
      await renderForTab(activeTabId);
    } catch (e) {
      setStatus('Could not undo: ' + (e && e.message ? e.message : e), true);
    }
  });

  // "Report this page": download the form's STRUCTURE (never values) so a page that fills
  // badly can be diagnosed from its real markup instead of a guess.
  reportBtn.addEventListener('click', async function () {
    if (activeTabId == null) return;
    reportBtn.disabled = true;
    try {
      await ensureInjected(activeTabId);
      var resp = await chrome.tabs.sendMessage(activeTabId, { type: 'CAPTURE' });
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
