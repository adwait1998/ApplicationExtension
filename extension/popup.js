/**
 * ApplyPilot Copilot — popup logic.
 *
 * Orchestrates the whole flow but performs none of the sensitive work itself:
 *   1. injects scanner.js + content.js into the active tab (activeTab-scoped, on click only)
 *   2. asks the content script to scan -> FieldDescriptor[]
 *   3. asks the background worker to resolve them against the local service
 *   4. asks the content script to apply only the auto_fill:true results, and highlight the rest
 *   5. renders the review list and offers Undo
 *
 * This file never injects into every tab automatically and never triggers a submit/navigate.
 */
(function () {
  'use strict';

  var scanBtn = document.getElementById('scanBtn');
  var undoBtn = document.getElementById('undoBtn');
  var statusBox = document.getElementById('statusBox');
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

  function renderResults(applyResp, resolveData) {
    resultsBox.innerHTML = '';
    var fills = (resolveData && resolveData.fills) || [];
    var skipped = (resolveData && resolveData.skipped) || [];
    var appliedIds = {};
    (applyResp.applied || []).forEach(function (a) { appliedIds[a.id] = a; });
    var failedIds = {};
    (applyResp.failed || []).forEach(function (f) { failedIds[f.id] = f; });

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
      skipTitle.textContent = 'Skipped — answer these yourself (' + skipRows.length + ')';
      resultsBox.appendChild(skipTitle);
      skipRows.forEach(function (s) {
        var row = document.createElement('div');
        row.className = 'field-row skipped';
        row.innerHTML = '<div class="reason">' + escapeHtml(s.reason || 'Left for you to fill in') + '</div>';
        resultsBox.appendChild(row);
      });
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
    resultsBox.innerHTML = '';
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

      var applyResp = await chrome.tabs.sendMessage(activeTabId, {
        type: 'APPLY_FILLS',
        fills: data.fills || [],
        skipped: data.skipped || []
      });

      var appliedList = applyResp.applied || [];
      var filledCount = appliedList.length;
      var draftCount = appliedList.filter(function (a) { return a.draft; }).length;
      var summary = 'Filled ' + filledCount + (draftCount ? ' (' + draftCount + ' draft' + (draftCount === 1 ? '' : 's') + ' to review)' : '') + '.';
      setStatus(summary + ' Review below, then submit yourself when ready.');
      renderResults(applyResp, data);
      undoBtn.disabled = filledCount === 0;
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
      resultsBox.innerHTML = '';
      undoBtn.disabled = true;
    } catch (e) {
      setStatus('Could not undo: ' + (e && e.message ? e.message : e), true);
    }
  });

  init();
})();
