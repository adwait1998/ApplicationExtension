/**
 * ApplyPilot Copilot — background service worker.
 *
 * This is the ONLY place that talks to the local ApplyPilot service. It holds the token
 * (read from chrome.storage.local — never synced to a Google account) and makes the fetch
 * calls, which keeps the token out of every page's JS context. Content scripts and the
 * side panel only ever talk to this worker via chrome.runtime.sendMessage.
 *
 * This file never fetches, clicks, or navigates any tab. It only ever calls the local
 * 127.0.0.1 service.
 *
 * This is also the ONLY place that writes chrome.storage.session (content scripts default to
 * NO access to chrome.storage.session, and this file never grants them any via
 * setAccessLevel() — every fill-progress/result write below arrives here as a
 * FILL_STATE_UPDATE message FROM content.js and is persisted keyed by that sender tab's id, so
 * a fill result survives the side panel closing, a tab switch, or this worker itself being
 * restarted — see content.js's file header and README.md).
 */

var DEFAULT_SERVICE_URL = 'http://127.0.0.1:8787';

// Clicking the toolbar icon opens the side panel directly (no popup) — set at top level so it
// takes effect as soon as this worker first registers, per chrome.sidePanel's own docs. Guarded
// for environments (e.g. an older Chrome hitting this file directly) where the API is absent.
if (self.chrome && chrome.sidePanel && typeof chrome.sidePanel.setPanelBehavior === 'function') {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(function () {});
}

// ---------------------------------------------------------------------
// per-tab fill state — chrome.storage.session, keyed by tab id
// ---------------------------------------------------------------------
//
// One key per tab (rather than one shared object) so two tabs filling at the same time can
// never race each other's read-modify-write, and so a listing/cleanup never has to parse a
// giant shared blob. Content.js sends the FULL current snapshot on every update (never a
// partial patch), so writing here is always a plain overwrite — no merge, so no lost-update
// race with the browser's own storage IPC either.
function tabStateKey(tabId) {
  return 'fillState_' + tabId;
}

// Per-tab promise chain so that if two FILL_STATE_UPDATE messages from the SAME tab are ever
// dispatched to this listener before the first one's storage.session.set() resolves, the writes
// still land in the order they were sent rather than however their underlying promises happen
// to settle.
var tabWriteQueues = Object.create(null);
function queueTabWrite(tabId, fn) {
  var prev = tabWriteQueues[tabId] || Promise.resolve();
  var next = prev.then(fn, fn);
  tabWriteQueues[tabId] = next.catch(function () {});
  return next;
}

chrome.tabs.onRemoved.addListener(function (tabId) {
  delete tabWriteQueues[tabId];
  chrome.storage.session.remove(tabStateKey(tabId)).catch(function () {});
});

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

// Chunked to avoid "Maximum call stack size exceeded" from
// String.fromCharCode.apply on a large array. Base64 is the simplest reliable
// way to move binary bytes across the background-worker -> content-script
// message boundary (structured clone does carry ArrayBuffer/Uint8Array too,
// but base64 + JSON is what chrome.runtime.sendMessage's promise API round-trips
// most predictably). Measured cost for a ~1MB PDF: ~8ms to encode here, ~4ms to
// decode in content.js, ~1.33x size inflation (1MB -> ~1.33MB of base64 text) —
// negligible next to the localhost fetch itself.
function bufferToBase64(buffer) {
  var bytes = new Uint8Array(buffer);
  var CHUNK = 0x8000;
  var parts = [];
  for (var i = 0; i < bytes.length; i += CHUNK) {
    parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK)));
  }
  return btoa(parts.join(''));
}

function parseFilenameFromDisposition(disposition) {
  if (!disposition) return '';
  var star = /filename\*=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  if (star) {
    try { return decodeURIComponent(star[1]); } catch (e) { return star[1]; }
  }
  var plain = /filename="?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1] : '';
}

/**
 * Fetches the operator's résumé bytes for the content script to attach to a
 * file input. GET /resume/info (filename/size) is consulted first for a
 * clean filename, but /resume itself is still tried even if that lookup
 * fails or the endpoint doesn't exist yet — the Content-Type header on the
 * bytes response is authoritative either way. Fails soft: no token, service
 * unreachable, or no résumé stored (404) all resolve to { ok: false, ... }
 * rather than throwing, so content.js can fall back to today's skip behaviour.
 */
function callResume() {
  return getConfig().then(function (cfg) {
    if (!cfg.token) {
      return { ok: false, error: 'no-token', message: 'No service token configured. Open the extension options page and paste the token printed by `applypilot serve-extension`.' };
    }
    var headers = { 'X-ApplyPilot-Token': cfg.token };

    function fetchBytes(fallbackFilename) {
      return fetch(cfg.serviceUrl + '/resume', { headers: headers }).then(function (resp) {
        if (resp.status === 404) {
          return { ok: false, error: 'no-resume', message: 'No résumé is stored yet. Upload one on the extension options page.' };
        }
        if (resp.status === 401) {
          return { ok: false, error: 'unauthorized', message: 'The service rejected the token (401 Unauthorized). Check the token in the extension options page.' };
        }
        if (!resp.ok) {
          return { ok: false, error: 'http-' + resp.status, message: 'Service returned HTTP ' + resp.status + ' fetching the résumé.' };
        }
        var contentType = resp.headers.get('Content-Type') || 'application/octet-stream';
        var headerFilename = parseFilenameFromDisposition(resp.headers.get('Content-Disposition') || '');
        return resp.arrayBuffer().then(function (buf) {
          return {
            ok: true,
            data: {
              filename: headerFilename || fallbackFilename || 'resume.pdf',
              contentType: contentType,
              size: buf.byteLength,
              base64: bufferToBase64(buf)
            }
          };
        });
      }, function () {
        return { ok: false, error: 'unreachable', message: friendlyFetchError(cfg.serviceUrl) };
      });
    }

    return fetch(cfg.serviceUrl + '/resume/info', { headers: headers }).then(function (infoResp) {
      if (!infoResp.ok) return fetchBytes(''); // /resume itself still 404s cleanly if nothing is stored
      return infoResp.json().then(function (info) {
        return fetchBytes(info && info.filename);
      }, function () {
        return fetchBytes('');
      });
    }, function () {
      // /resume/info unreachable for the same reason /resume would be — try
      // /resume directly anyway rather than giving up on a filename lookup alone.
      return fetchBytes('');
    });
  });
}

/**
 * GET /profile/counts -> { work_history: <int>, education: <int> }, used ONLY to decide how
 * many times to click a Workday-style "Add Another" button before scanning (see content.js's
 * expandSections(), driven exclusively from an explicit "Fill this page" click, never on
 * load). This endpoint is new; a 404 here means the local service hasn't been upgraded yet —
 * content.js treats ANY non-ok response (404, unreachable, no token, ...) the same way: skip
 * expansion entirely and fill the page exactly as it did before this feature existed.
 */
function callProfileCounts() {
  return getConfig().then(function (cfg) {
    if (!cfg.token) {
      return { ok: false, error: 'no-token', message: 'No service token configured. Open the extension options page and paste the token printed by `applypilot serve-extension`.' };
    }
    return fetch(cfg.serviceUrl + '/profile/counts', {
      headers: { 'X-ApplyPilot-Token': cfg.token }
    }).then(function (resp) {
      if (resp.status === 404) {
        return { ok: false, error: 'not-found', message: 'The local service does not support /profile/counts yet.' };
      }
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
  if (msg.type === 'GET_RESUME') {
    callResume().then(sendResponse);
    return true;
  }
  if (msg.type === 'PROFILE_COUNTS') {
    callProfileCounts().then(sendResponse);
    return true;
  }
  if (msg.type === 'FILL_STATE_UPDATE') {
    // Only ever sent by content.js, whose sender.tab is always populated (a real tab, never
    // the side panel or options page). Silently ignored otherwise rather than throwing.
    if (!sender || !sender.tab || typeof sender.tab.id !== 'number') {
      sendResponse({ ok: false, error: 'no sender tab' });
      return false;
    }
    var tabId = sender.tab.id;
    queueTabWrite(tabId, function () {
      var obj = {};
      obj[tabStateKey(tabId)] = msg.state;
      return chrome.storage.session.set(obj);
    }).then(function () {
      sendResponse({ ok: true });
    }, function (e) {
      sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
    });
    return true;
  }
  return false;
});
