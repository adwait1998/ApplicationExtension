/**
 * ApplyPilot Copilot — options page logic.
 *
 * Reads/writes { serviceUrl, token } in chrome.storage.local, and offers a "Test connection"
 * button that asks the background worker to call GET /health (never called from this page
 * directly — background.js is the only fetcher, see its file header).
 */
(function () {
  'use strict';

  var DEFAULT_SERVICE_URL = 'http://127.0.0.1:8765';

  var serviceUrlEl = document.getElementById('serviceUrl');
  var tokenEl = document.getElementById('token');
  var toggleTokenEl = document.getElementById('toggleToken');
  var saveEl = document.getElementById('save');
  var testEl = document.getElementById('test');
  var statusEl = document.getElementById('status');

  function setStatus(kind, text) {
    statusEl.className = kind;
    statusEl.textContent = text;
  }

  function load() {
    chrome.storage.local.get(['serviceUrl', 'token']).then(function (data) {
      serviceUrlEl.value = data.serviceUrl || DEFAULT_SERVICE_URL;
      tokenEl.value = data.token || '';
    });
  }

  toggleTokenEl.addEventListener('click', function () {
    var isHidden = tokenEl.type === 'password';
    tokenEl.type = isHidden ? 'text' : 'password';
    toggleTokenEl.textContent = isHidden ? 'Hide' : 'Show';
  });

  saveEl.addEventListener('click', function () {
    var serviceUrl = (serviceUrlEl.value || DEFAULT_SERVICE_URL).trim().replace(/\/+$/, '');
    var token = (tokenEl.value || '').trim();
    if (!/^http:\/\/127\.0\.0\.1(:\d+)?$/.test(serviceUrl)) {
      setStatus('err', 'Service URL must be http://127.0.0.1[:port] — the extension only ever talks to your own machine.');
      return;
    }
    chrome.storage.local.set({ serviceUrl: serviceUrl, token: token }).then(function () {
      setStatus('ok', 'Saved.');
    });
  });

  testEl.addEventListener('click', function () {
    setStatus('info', 'Checking...');
    chrome.runtime.sendMessage({ type: 'HEALTH' }).then(function (resp) {
      if (!resp || !resp.ok) {
        setStatus('err', (resp && resp.message) || 'Could not reach the service.');
        return;
      }
      var data = resp.data || {};
      var tiers = Array.isArray(data.tiers_available) ? data.tiers_available.join(', ') : 'unknown';
      setStatus('ok', 'Connected. Decision tiers available: ' + tiers + '.');
    }, function (err) {
      setStatus('err', 'Could not reach the extension background worker: ' + err);
    });
  });

  load();
})();
