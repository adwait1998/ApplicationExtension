/**
 * ApplyPilot Copilot — popup logic.
 *
 * Orchestrates the whole flow but performs none of the sensitive work itself:
 *   1. injects scanner.js + content.js into the active tab (activeTab-scoped, on click only)
 *   2. asks the content script to scan -> FieldDescriptor[]
 *   3. asks the background worker to resolve them against the local service
 *   4. asks the content script to apply only the auto_fill:true results, and highlight the rest
 *      — this also drives résumé attachment: content.js's APPLY_FILLS response always includes
 *      a `resume` key ({attempted:false} | {attempted:true, attached, filename} |
 *      {attempted:true, attached:false, reason, errorCode}), and renderResumeLine() below
 *      ALWAYS shows a line built from it — one of "Attached <file>", "Couldn't attach —
 *      <reason>", "No résumé upload on this page", or "No résumé stored — upload one in
 *      Settings". Silently hiding this line is exactly the bug report that prompted it: with
 *      no line at all, an operator on a page with no file field reasonably concludes résumé
 *      attachment doesn't work at all.
 *   5. renders the review list (facts / drafts / needs-you, never merged into one flat list),
 *      a one-line nudge toward Settings when open-ended questions were left for the human
 *      because drafts are off, and offers Undo
 *
 * This file never injects into every tab automatically and never triggers a submit/navigate.
 */
(function () {
  'use strict';

  var scanBtn = document.getElementById('scanBtn');
  var undoBtn = document.getElementById('undoBtn');
  var statusBox = document.getElementById('statusBox');
  var fillSummaryEl = document.getElementById('fillSummary');
  var resumeLineEl = document.getElementById('resumeLine');
  var resultsBox = document.getElementById('results');
  var tiersLine = document.getElementById('tiersLine');
  var serviceDot = document.getElementById('serviceDot');
  var optionsBtn = document.getElementById('optionsBtn');

  var activeTabId = null;
  var activeTabUrl = null;

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

  function hideFillOutputs() {
    fillSummaryEl.hidden = true;
    fillSummaryEl.textContent = '';
    resumeLineEl.hidden = true;
    resumeLineEl.textContent = '';
    resumeLineEl.className = 'resume-line';
    resultsBox.innerHTML = '';
  }

  async function ensureInjected(tabId) {
    await chrome.scripting.executeScript({ target: { tabId: tabId }, files: ['scanner.js', 'content.js'] });
  }

  async function init() {
    optionsBtn.addEventListener('click', function () {
      chrome.runtime.openOptionsPage();
    });

    var tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    var tab = tabs[0];
    if (!tab || !canScript(tab.url)) {
      setStatus('Open a job application page (http/https) in this tab, then reopen this popup.', true);
      scanBtn.disabled = true;
      return;
    }
    activeTabId = tab.id;
    activeTabUrl = tab.url;

    try {
      await ensureInjected(activeTabId);
      var detected = await chrome.tabs.sendMessage(activeTabId, { type: 'DETECT' });
      if (detected && detected.looksLikeForm) {
        setStatus('This page looks like a form — ' + detected.fieldCount + ' fillable field(s) detected.');
      } else if (detected) {
        setStatus('No obvious application form detected (' + detected.fieldCount + ' field(s)). You can still try scanning.');
      }
      if (detected && detected.skippedFrames) {
        setStatus(statusBox.textContent + ' (' + detected.skippedFrames + ' embedded frame(s) could not be scanned — likely cross-origin.)');
      }
    } catch (e) {
      setStatus('Could not access this page (' + (e && e.message ? e.message : e) + '). Some pages (chrome://, the Web Store, PDF viewer) cannot be scripted.', true);
      scanBtn.disabled = true;
      return;
    }

    checkHealth();
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
        tiersLine.textContent = (resp && resp.message) || 'Service unreachable.';
      }
    } catch (e) {
      setDot('err');
      tiersLine.textContent = 'Could not reach background worker.';
    }
  }

  // Counts that mean something, shown as one headline line directly under
  // the primary button: "Filled 12 · 2 drafts to review · 3 need you".
  function buildSummaryLine(appliedList, skippedCount, failedCount) {
    var filled = appliedList.filter(function (a) { return !a.draft; }).length;
    var drafts = appliedList.filter(function (a) { return a.draft; }).length;
    var parts = ['Filled ' + filled];
    if (drafts) parts.push(drafts + ' draft' + (drafts === 1 ? '' : 's') + ' to review');
    if (skippedCount) parts.push(skippedCount + (skippedCount === 1 ? ' needs' : ' need') + ' you');
    var line = parts.join(' · ');
    if (failedCount) line += ' · ' + failedCount + ' failed';
    return line;
  }

  // Résumé attachment gets its own clear line, independent of the field
  // results — content.js reports it as part of APPLY_FILLS (see file header)
  // and this ALWAYS renders something from it. Hiding this line whenever
  // there was nothing to report is precisely how an operator on a page with
  // no file field ends up concluding résumé attachment doesn't work at all.
  function renderResumeLine(applyResp) {
    var r = (applyResp && applyResp.resume) || { attempted: false };
    resumeLineEl.hidden = false;

    if (r.attempted && r.attached) {
      resumeLineEl.className = 'resume-line ok';
      resumeLineEl.textContent = '✓ Attached ' + (r.filename || 'résumé file') + '.';
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
    // !r.attempted: findResumeFileTarget() found no file input on this page at all.
    resumeLineEl.className = 'resume-line muted';
    resumeLineEl.textContent = 'No résumé upload on this page.';
  }

  function renderResults(applyResp, resolveData, scannedFields, draftsEnabledLocally) {
    resultsBox.innerHTML = '';
    var fills = (resolveData && resolveData.fills) || [];
    var skipped = (resolveData && resolveData.skipped) || [];

    var factApplied = (applyResp.applied || []).filter(function (a) { return !a.draft; });
    var draftApplied = (applyResp.applied || []).filter(function (a) { return a.draft; });

    if (factApplied.length) {
      var filledTitle = document.createElement('div');
      filledTitle.className = 'section-title';
      filledTitle.textContent = 'Filled (' + factApplied.length + ')';
      resultsBox.appendChild(filledTitle);
      factApplied.forEach(function (a) {
        var row = document.createElement('div');
        row.className = 'field-row filled';
        row.innerHTML =
          '<div class="value">' + escapeHtml(a.value) + '</div>' +
          '<div class="reason">' + escapeHtml(a.reason || '') + (a.profile_key ? ' &middot; ' + escapeHtml(a.profile_key) : '') + '</div>';
        resultsBox.appendChild(row);
      });
    }

    // Drafts are generated text, not facts pulled from the profile — kept in their own
    // clearly-labelled, visually distinct group so they can never be mistaken for a fact.
    if (draftApplied.length) {
      var draftTitle = document.createElement('div');
      draftTitle.className = 'section-title draft-title';
      draftTitle.textContent = 'Drafted — review before submitting (' + draftApplied.length + ')';
      resultsBox.appendChild(draftTitle);
      draftApplied.forEach(function (a) {
        var row = document.createElement('div');
        row.className = 'field-row draft';
        row.innerHTML =
          '<div class="draft-badge">DRAFT</div>' +
          '<div class="value">' + escapeHtml(a.value) + '</div>' +
          '<div class="reason">' + escapeHtml(a.reason || '') + (a.profile_key ? ' &middot; ' + escapeHtml(a.profile_key) : '') + '</div>';
        resultsBox.appendChild(row);
      });
    }

    var skipRows = skipped.concat(
      fills.filter(function (f) { return !f.auto_fill; })
    );
    if (skipRows.length) {
      var skipTitle = document.createElement('div');
      skipTitle.className = 'section-title';
      skipTitle.textContent = 'Need you (' + skipRows.length + ')';
      resultsBox.appendChild(skipTitle);
      skipRows.forEach(function (s) {
        var row = document.createElement('div');
        row.className = 'field-row skipped';
        row.innerHTML = '<div class="reason">' + escapeHtml(s.reason || 'Left for you to fill in') + '</div>';
        resultsBox.appendChild(row);
      });

      // A single, once-per-scan nudge: only when drafts are off (the operator's own
      // Settings toggle, read from local storage into `draftsEnabledLocally` by the
      // caller below) AND at least one "need you" field is a genuinely open-ended
      // question (a <textarea> — a free-text essay-style prompt, not just any short
      // text input).
      if (!draftsEnabledLocally) {
        var fieldById = {};
        (scannedFields || []).forEach(function (f) { fieldById[f.id] = f; });
        var hasOpenEnded = skipRows.some(function (s) {
          var f = fieldById[s.id];
          return f && f.tag === 'textarea';
        });
        if (hasOpenEnded) {
          var hint = document.createElement('div');
          hint.className = 'draft-hint';
          hint.textContent = 'Turn on drafts in Settings to get a first draft of open-ended answers.';
          resultsBox.appendChild(hint);
        }
      }
    }

    if (applyResp.failed && applyResp.failed.length) {
      var failTitle = document.createElement('div');
      failTitle.className = 'section-title';
      failTitle.textContent = 'Could not fill (' + applyResp.failed.length + ')';
      resultsBox.appendChild(failTitle);
      applyResp.failed.forEach(function (f) {
        var row = document.createElement('div');
        row.className = 'field-row failed';
        row.innerHTML = '<div class="reason">' + escapeHtml(f.reason || '') + '</div>';
        resultsBox.appendChild(row);
      });
    }

    if (!resultsBox.children.length) {
      resultsBox.innerHTML = '<div class="status">Nothing to show.</div>';
    }
  }

  scanBtn.addEventListener('click', async function () {
    if (!activeTabId) return;
    scanBtn.disabled = true;
    undoBtn.disabled = true;
    hideFillOutputs();
    try {
      setStatus('Scanning page for fillable fields...');
      var scanResp = await chrome.tabs.sendMessage(activeTabId, { type: 'SCAN' });
      var fields = (scanResp && scanResp.fields) || [];
      if (!fields.length) {
        setStatus('No fillable fields found on this page.');
        scanBtn.disabled = false;
        return;
      }

      setStatus('Found ' + fields.length + ' field(s). Asking the local ApplyPilot service...');
      var tab = await chrome.tabs.get(activeTabId);
      var resolveResp = await chrome.runtime.sendMessage({ type: 'RESOLVE', url: tab.url, fields: fields });

      if (!resolveResp || !resolveResp.ok) {
        setStatus((resolveResp && resolveResp.message) || 'The local service call failed.', true);
        setDot('err');
        scanBtn.disabled = false;
        return;
      }
      setDot('ok');
      var data = resolveResp.data || {};

      setStatus('Filling…');
      var applyResp = await chrome.tabs.sendMessage(activeTabId, {
        type: 'APPLY_FILLS',
        fills: data.fills || [],
        skipped: data.skipped || []
      });

      var appliedList = applyResp.applied || [];
      var skippedCount = ((data.skipped || []).length) + ((data.fills || []).filter(function (f) { return !f.auto_fill; }).length);
      var failedCount = (applyResp.failed || []).length;

      // Local-only UI preference (see options.js "Smart fill" section) — read directly
      // from storage rather than round-tripping through the background worker, purely
      // to decide whether the "turn on drafts" hint below is still relevant to show.
      var draftsPref = await chrome.storage.local.get(['smartFillDraftsEnabled']);
      var draftsEnabledLocally = draftsPref.smartFillDraftsEnabled === true;

      setStatus('Done. Review below, then submit yourself when ready.');
      fillSummaryEl.hidden = false;
      fillSummaryEl.textContent = buildSummaryLine(appliedList, skippedCount, failedCount);
      renderResumeLine(applyResp);
      renderResults(applyResp, data, fields, draftsEnabledLocally);
      undoBtn.disabled = appliedList.length === 0;
    } catch (e) {
      setStatus('Something went wrong: ' + (e && e.message ? e.message : e), true);
    } finally {
      scanBtn.disabled = false;
    }
  });

  undoBtn.addEventListener('click', async function () {
    if (!activeTabId) return;
    try {
      var resp = await chrome.tabs.sendMessage(activeTabId, { type: 'UNDO' });
      setStatus('Restored ' + ((resp && resp.restored) || 0) + ' field(s) to their previous values.');
      hideFillOutputs();
      undoBtn.disabled = true;
    } catch (e) {
      setStatus('Could not undo: ' + (e && e.message ? e.message : e), true);
    }
  });

  init();
})();
