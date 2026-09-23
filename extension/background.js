/**
 * ApplyPilot Copilot — background service worker.
 *
 * This is the ONLY place that talks to the local ApplyPilot service. It holds the token
 * (read from chrome.storage.local — never synced to a Google account) and makes the fetch
 * calls, which keeps the token out of every page's JS context. Content scripts and the
 * popup only ever talk to this worker via chrome.runtime.sendMessage.
 *
 * This file never fetches, clicks, or navigates any tab. It only ever calls the local
 * 127.0.0.1 service.
 */

var DEFAULT_SERVICE_URL = 'http://127.0.0.1:8765';

function getConfig() {
  return chrome.storage.local.get(['serviceUrl', 'token']).then(function (data) {
    return {
      serviceUrl: (data.serviceUrl || DEFAULT_SERVICE_URL).replace(/\/+$/, ''),
      token: data.token || ''
    };
  });
}

function friendlyFetchError(serviceUrl) {
  return 'Could not reach the local ApplyPilot service at ' + serviceUrl + '. ' +
    'Is `applypilot serve-extension` running? (Check the service URL in the extension options page.)';
}

function callResolve(url, fields) {
  return getConfig().then(function (cfg) {
    if (!cfg.token) {
      return { ok: false, error: 'no-token', message: 'No service token configured. Open the extension options page and paste the token printed by `applypilot serve-extension`.' };
    }
    return fetch(cfg.serviceUrl + '/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-ApplyPilot-Token': cfg.token },
      body: JSON.stringify({ url: url, fields: fields })
    }).then(function (resp) {
      return handleResponse(resp, cfg.serviceUrl);
    }, function () {
      return { ok: false, error: 'unreachable', message: friendlyFetchError(cfg.serviceUrl) };
    });
  });
}

function callHealth() {
  return getConfig().then(function (cfg) {
    var headers = cfg.token ? { 'X-ApplyPilot-Token': cfg.token } : {};
    return fetch(cfg.serviceUrl + '/health', { headers: headers }).then(function (resp) {
      return handleResponse(resp, cfg.serviceUrl);
    }, function () {
      return { ok: false, error: 'unreachable', message: friendlyFetchError(cfg.serviceUrl) };
    });
  });
}

function handleResponse(resp, serviceUrl) {
  if (resp.status === 401) {
    return { ok: false, error: 'unauthorized', message: 'The service rejected the token (401 Unauthorized). Check the token in the extension options page.' };
  }
  if (!resp.ok) {
    return resp.text().then(function (text) {
      return { ok: false, error: 'http-' + resp.status, message: 'Service returned HTTP ' + resp.status + (text ? (': ' + text.slice(0, 200)) : '') };
    }, function () {
      return { ok: false, error: 'http-' + resp.status, message: 'Service returned HTTP ' + resp.status };
    });
  }
  return resp.json().then(function (data) {
    return { ok: true, data: data };
  }, function () {
    return { ok: false, error: 'bad-json', message: 'Service at ' + serviceUrl + ' returned a response that was not valid JSON.' };
  });
}

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg || typeof msg !== 'object') return false;
  if (msg.type === 'RESOLVE') {
    callResolve(msg.url, msg.fields).then(sendResponse);
    return true; // keep the message channel open for the async response
  }
  if (msg.type === 'HEALTH') {
    callHealth().then(sendResponse);
    return true;
  }
  return false;
});
