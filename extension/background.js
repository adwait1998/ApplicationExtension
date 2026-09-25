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
 * FILL_STATE_UPDATE message FROM a content.js frame and is persisted keyed by that sender
 * tab's id, so a fill result survives the side panel closing, a tab switch, or this worker
 * itself being restarted — see content.js's file header and README.md).
 *
 * FILL EVERY FRAME — this file is also the cross-frame COORDINATOR for a fill. Only an
 * extension page (never a content script) can chrome.tabs.sendMessage a SPECIFIC frameId, so
 * this is the one place that can ask several frames of the same tab to do something and stitch
 * their answers back together. A RUN_FILL message (sent by sidepanel.js right after it injects
 * scanner.js/capture.js/content.js into every frame the operator granted) drives, per tab:
 *
 *   1. chrome.webNavigation.getAllFrames() to find every http(s) frame currently in the tab.
 *   2. Ask EVERY frame to PREPARE_AND_SCAN itself (expand repeating sections, attach its own
 *      résumé if it has one, wait for the page to settle, then scan) — one request/response
 *      round trip per frame, all in parallel.
 *   3. Merge every frame's fields into ONE list (each field's id prefixed with its frameId so
 *      two frames' "f0" don't collide) and call the local /resolve service exactly ONCE for
 *      the whole page.
 *   4. Split the answer back into per-frame slices (stripping the frameId prefix back off) and
 *      fire APPLY_FILLS at each frame that actually had fields — fire-and-forget, exactly like
 *      the old single-frame START_FILL: this function returns as soon as every frame has been
 *      told to start, not when any of them finish filling.
 *
 * From there, each frame runs its own apply loop (progress, Cancel, per-field timeout, its own
 * slice of the one shared submit shield lifetime) completely independently — see content.js —
 * and reports back via the same FILL_STATE_UPDATE mechanism as before. This file merges every
 * frame's own report into ONE combined per-tab result (see applyFrameReport()) so the panel
 * keeps rendering a single, simple state object exactly as it did before this build, whether
 * the page had one frame or several.
 */

var DEFAULT_SERVICE_URL = 'http://127.0.0.1:8787';

// Clicking the toolbar icon opens the side panel directly (no popup) — set at top level so it
// takes effect as soon as this worker first registers, per chrome.sidePanel's own docs. Guarded
// for environments (e.g. an older Chrome hitting this file directly) where the API is absent.
if (self.chrome && chrome.sidePanel && typeof chrome.sidePanel.setPanelBehavior === 'function') {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(function () {});
}

// ---------------------------------------------------------------------
// AUTO-CONNECT — no terminal, no pasted token.
//
// manifest.json pins this extension's id (via its top-level "key") to the one value the native
// host's install-host command writes into com.applypilot.copilot.json's allowed_origins, so
// chrome.runtime.sendNativeMessage below only ever reaches a host that was deliberately set up
// for THIS extension. Chrome itself refuses to start the host at all for any other extension id
// — this file never has to check that itself.
//
// The host speaks a tiny two-command protocol (see native_host.py):
//   {cmd:"hello"}         -> {ok:true, version}            — not used here, kept for options.js
//   {cmd:"ensure_server"} -> {ok:true, port, token, started} once a service answers /health, or
//                            {ok:false, error} (not installed, or it never came up).
// A missing host is a normal, expected state (the operator hasn't run
// `applypilot extension install-host` yet) — never a hard failure. In that case this file falls
// back to exactly the manual-token flow that existed before this build (paste serviceUrl+token
// in Options), and the panel gets a one-line tip via chrome.storage.local.serviceConnection.
// ---------------------------------------------------------------------
var NATIVE_HOST_NAME = 'com.applypilot.copilot';

function sendNativeMessage(message) {
  return new Promise(function (resolve) {
    try {
      if (!chrome.runtime.sendNativeMessage) { resolve({ ok: false, error: 'nativeMessaging unavailable in this browser' }); return; }
      chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, message, function (response) {
        var err = chrome.runtime.lastError;
        if (err) { resolve({ ok: false, error: (err && err.message) || String(err) }); return; }
        resolve(response && typeof response === 'object' ? response : { ok: false, error: 'empty response from the native host' });
      });
    } catch (e) {
      resolve({ ok: false, error: String(e && e.message ? e.message : e) });
    }
  });
}

// Chrome's own wording for "no host manifest registered for this id" — this is the expected,
// common case on a machine that never ran `applypilot extension install-host`.
function isHostMissingError(message) {
  return typeof message === 'string' && /native messaging host not found/i.test(message);
}

/** Merges `patch` into chrome.storage.local.serviceConnection — read by the Settings page. */
function recordServiceConnection(patch) {
  return chrome.storage.local.get(['serviceConnection']).then(function (data) {
    var merged = {};
    var existing = data.serviceConnection || {};
    for (var k in existing) if (Object.prototype.hasOwnProperty.call(existing, k)) merged[k] = existing[k];
    for (var k2 in patch) if (Object.prototype.hasOwnProperty.call(patch, k2)) merged[k2] = patch[k2];
    merged.checkedAt = Date.now();
    return chrome.storage.local.set({ serviceConnection: merged });
  });
}

/**
 * Asks the native host to make sure the local service is up, and if it answers with a real
 * {port, token}, stores them into chrome.storage.local under the SAME keys getConfig() already
 * reads (serviceUrl/token) — so this is indistinguishable, to every existing call site, from the
 * operator having pasted them into Options. Always resolves (never rejects); the caller decides
 * what to do next.
 */
function ensureServerViaNativeHost() {
  return sendNativeMessage({ cmd: 'ensure_server' }).then(function (resp) {
    if (resp && resp.ok && resp.port && resp.token) {
      return chrome.storage.local.set({
        serviceUrl: 'http://127.0.0.1:' + resp.port,
        token: resp.token
      }).then(function () {
        return recordServiceConnection({ mode: 'native', ok: true, error: null, started: !!resp.started });
      }).then(function () {
        return { ok: true };
      });
    }
    var errMsg = (resp && resp.error) || 'the native host gave no usable response';
    var hostMissing = isHostMissingError(errMsg);
    return recordServiceConnection({
      mode: hostMissing ? 'manual' : 'native',
      ok: false,
      error: errMsg
    }).then(function () {
      return { ok: false, hostMissing: hostMissing, error: errMsg };
    });
  });
}

/**
 * Wraps any of the service-call functions below (each of which independently reads
 * serviceUrl/token via getConfig()) with the auto-connect contract from the build spec:
 *   - if nothing is configured yet, call ensure_server BEFORE the first real attempt;
 *   - if a real attempt still comes back unauthorized/unreachable/unconfigured, call ensure_server
 *     ONCE more and retry the SAME call ONCE more.
 * At most one native-host round trip and at most two HTTP attempts per call, either way — this
 * can never loop. `callFn` takes no arguments and returns the same {ok, error, ...} shape every
 * call*() function below already returns; this changes none of those shapes.
 */
function requestWithAutoConnect(callFn) {
  var connectAttempted = false;
  function isRetryableFailure(result) {
    return !!(result && result.ok === false &&
      (result.error === 'unauthorized' || result.error === 'unreachable' || result.error === 'no-token'));
  }
  function attempt() {
    return callFn().then(function (result) {
      if (isRetryableFailure(result) && !connectAttempted) {
        connectAttempted = true;
        return ensureServerViaNativeHost().then(attempt);
      }
      return result;
    });
  }
  return getConfig().then(function (cfg) {
    if (!cfg.token && !connectAttempted) {
      connectAttempted = true;
      return ensureServerViaNativeHost().then(attempt);
    }
    return attempt();
  });
}

// ---------------------------------------------------------------------
// per-tab fill state — chrome.storage.session, keyed by tab id
// ---------------------------------------------------------------------
//
// One key per tab (rather than one shared object) so two tabs filling at the same time can
// never race each other's read-modify-write, and so a listing/cleanup never has to parse a
// giant shared blob. Every write here (both the per-frame merge in applyFrameReport() and the
// tab-wide patches runFillForTab() makes) goes through queueTabWrite() so a tab's writes are
// always a strict read-then-write sequence, never two concurrent read-modify-writes racing.
function tabStateKey(tabId) {
  return 'fillState_' + tabId;
}

// Per-tab promise chain so that if two writes for the SAME tab are ever triggered before the
// first one's storage.session.set() resolves, they still land in the order they were queued
// rather than however their underlying promises happen to settle.
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

/** Read-modify-write helper: `updater(existingCombinedStateOrNull)` returns the new value. */
function updateStoredState(tabId, updater) {
  return queueTabWrite(tabId, function () {
    return chrome.storage.session.get(tabStateKey(tabId)).then(function (stored) {
      var existing = stored[tabStateKey(tabId)] || null;
      var updated = updater(existing);
      var obj = {};
      obj[tabStateKey(tabId)] = updated;
      return chrome.storage.session.set(obj);
    });
  });
}

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
  return requestWithAutoConnect(function () { return callResolveRaw(url, fields); });
}

function callResolveRaw(url, fields) {
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
  return requestWithAutoConnect(callResumeRaw);
}

function callResumeRaw() {
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
  return requestWithAutoConnect(callProfileCountsRaw);
}

function callProfileCountsRaw() {
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
  return requestWithAutoConnect(callHealthRaw);
}

function callHealthRaw() {
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

/**
 * Like handleResponse(), but on a non-2xx JSON body carries the FastAPI-style {"detail": "..."}
 * through VERBATIM (as `.detail`, never truncated) instead of handleResponse()'s generic
 * "HTTP <code>: <first 200 chars>" text — used for endpoints whose error detail is itself the
 * whole point of showing the operator something (cover-letter's 403 "model not allowed on this
 * computer" / 422 "no job description", see the build spec). `.message` is kept in sync with
 * `.detail` so a caller that only reads `.message` (the older convention every other call* here
 * uses) still gets something sensible.
 */
function handleResponseVerbatim(resp, serviceUrl) {
  if (resp.status === 401) {
    return { ok: false, error: 'unauthorized', message: 'The service rejected the token (401 Unauthorized). Check the token in the extension options page.' };
  }
  if (!resp.ok) {
    return resp.json().then(function (body) {
      var detail = body && body.detail;
      var text = typeof detail === 'string' ? detail : (detail != null ? JSON.stringify(detail) : ('HTTP ' + resp.status));
      return { ok: false, error: 'http-' + resp.status, status: resp.status, detail: text, message: text };
    }, function () {
      var text = 'Service returned HTTP ' + resp.status;
      return { ok: false, error: 'http-' + resp.status, status: resp.status, detail: text, message: text };
    });
  }
  return resp.json().then(function (data) {
    return { ok: true, data: data };
  }, function () {
    return { ok: false, error: 'bad-json', message: 'Service at ' + serviceUrl + ' returned a response that was not valid JSON.' };
  });
}

function postJson(cfg, path, body, responseHandler) {
  return fetch(cfg.serviceUrl + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-ApplyPilot-Token': cfg.token },
    body: JSON.stringify(body)
  }).then(function (resp) {
    return responseHandler(resp, cfg.serviceUrl);
  }, function () {
    return { ok: false, error: 'unreachable', message: friendlyFetchError(cfg.serviceUrl) };
  });
}

function noTokenResult() {
  return { ok: false, error: 'no-token', message: 'No service token configured. Open the extension options page and paste the token printed by `applypilot serve-extension`.' };
}

// ---------------------------------------------------------------------
// COVER LETTER (item 2) — POST /cover-letter {urls, page_text} -> {text, warnings, draft:true,
// provider, job:{title,company,source}}. Errors (403 "model not allowed on this computer yet",
// 422 "no job description"/"drafting refused", or anything else) are surfaced with their real
// `detail` text intact — see handleResponseVerbatim() above — so the panel can show them
// verbatim rather than a generic failure.
// ---------------------------------------------------------------------
function callCoverLetterRaw(urls, pageText) {
  return getConfig().then(function (cfg) {
    if (!cfg.token) return noTokenResult();
    return postJson(cfg, '/cover-letter', { urls: urls || [], page_text: pageText || '' }, handleResponseVerbatim);
  });
}
function callCoverLetter(urls, pageText) {
  return requestWithAutoConnect(function () { return callCoverLetterRaw(urls, pageText); });
}

// ---------------------------------------------------------------------
// REMEMBER MY ANSWERS (item 3) — POST /answers/learn {items:[{question,answer}]} ->
// {saved:[question...], skipped:[{question, reason}...]}. Never automatic — only ever called
// from rememberAnswersForTab() below, itself only ever triggered by the panel's own click.
// ---------------------------------------------------------------------
function callAnswersLearnRaw(items) {
  return getConfig().then(function (cfg) {
    if (!cfg.token) return noTokenResult();
    return postJson(cfg, '/answers/learn', { items: items || [] }, handleResponseVerbatim);
  });
}
function callAnswersLearn(items) {
  return requestWithAutoConnect(function () { return callAnswersLearnRaw(items); });
}

/**
 * "Remember my answers": rereads the CURRENT value of every field this tab's last fill left for
 * the human (state.needsYou — qualified ids, see applyFrameReport()), grouped by the frame each
 * one actually lives in (exactly one READ_FIELDS_FOR_ANSWERS round trip per frame that has any),
 * and hands the non-empty ones to /answers/learn with that field's own resolved label as the
 * question. content.js is what enforces "non-empty only, never passwords/files" (see
 * readFieldsForAnswers() there) — this function only routes to the right frames and re-attaches
 * each answer to the label the fill already worked out, since content.js's reply is just
 * {id -> value}, not full field metadata.
 */
function rememberAnswersForTab(tabId) {
  return chrome.storage.session.get(tabStateKey(tabId)).then(function (stored) {
    var state = stored[tabStateKey(tabId)];
    var needsYou = (state && state.needsYou) || [];
    var byFrame = {}; // frameId -> [{ localId, label }]
    needsYou.forEach(function (n) {
      var parts = splitQualifiedId(n.id);
      if (!parts) return;
      byFrame[parts.frameId] = byFrame[parts.frameId] || [];
      byFrame[parts.frameId].push({ localId: parts.localId, label: n.label || '' });
    });
    var frameIds = Object.keys(byFrame);
    if (!frameIds.length) {
      return { ok: true, data: { saved: [], skipped: [] }, noAnswersFound: true };
    }
    return Promise.all(frameIds.map(function (frameIdStr) {
      var frameId = parseInt(frameIdStr, 10);
      var localIds = byFrame[frameIdStr].map(function (e) { return e.localId; });
      return chrome.tabs.sendMessage(tabId, { type: 'READ_FIELDS_FOR_ANSWERS', ids: localIds }, { frameId: frameId })
        .then(function (resp) { return { frameIdStr: frameIdStr, values: (resp && resp.values) || {} }; })
        .catch(function () { return { frameIdStr: frameIdStr, values: {} }; });
    })).then(function (perFrame) {
      var items = [];
      perFrame.forEach(function (pf) {
        (byFrame[pf.frameIdStr] || []).forEach(function (e) {
          var val = pf.values[e.localId];
          if (val != null && String(val).trim() !== '') {
            items.push({ question: e.label, answer: String(val) });
          }
        });
      });
      if (!items.length) {
        return { ok: true, data: { saved: [], skipped: [] }, noAnswersFound: true };
      }
      return callAnswersLearn(items);
    });
  });
}

// ---------------------------------------------------------------------
// cross-frame coordination — see the file header.
// ---------------------------------------------------------------------

function frameOrigin(url) {
  try {
    var u = new URL(url);
    return u.protocol + '//' + u.host;
  } catch (e) {
    return null;
  }
}

/**
 * scanner.js's OWN scanAll() already recurses ONE level into a SAME-ORIGIN direct child iframe
 * of whatever document it is given (see scanner.js's "Same-origin iframes are scanned too" in
 * README.md). Since this file now ALSO independently injects into and scans every frame
 * webNavigation reports, a same-origin child would otherwise be scanned and FILLED TWICE — once
 * by its parent's own recursive scan, once more by its own independent top-level scan. This
 * filters the frame list down to exactly the frames NOT already covered by an ancestor's
 * one-level same-origin recursion, so every frame is handled by exactly one of the two paths. A
 * frame that is cross-origin from its own parent is never covered by that recursion — closing
 * that exact gap is what "fill every frame" is for — so it is always kept here regardless of
 * nesting depth.
 */
function excludeFramesCoveredByParentRecursion(frames) {
  var byId = {};
  frames.forEach(function (f) { byId[f.frameId] = f; });
  var included = {};

  function resolve(frameId) {
    if (Object.prototype.hasOwnProperty.call(included, frameId)) return included[frameId];
    var f = byId[frameId];
    var parent = (f && typeof f.parentFrameId === 'number' && f.parentFrameId >= 0) ? byId[f.parentFrameId] : null;
    if (!f || !parent) {
      included[frameId] = true; // the top frame, or a frame whose parent isn't in our own list
      return true;
    }
    var parentIncluded = resolve(parent.frameId);
    var sameOriginAsParent = frameOrigin(f.url) && frameOrigin(f.url) === frameOrigin(parent.url);
    var isIncluded = !(parentIncluded && sameOriginAsParent);
    included[frameId] = isIncluded;
    return isIncluded;
  }

  frames.forEach(function (f) { resolve(f.frameId); });
  return frames.filter(function (f) { return included[f.frameId]; });
}

/**
 * Every frame of `tabId` this file should independently PREPARE_AND_SCAN/APPLY_FILLS: every
 * http(s) frame webNavigation reports, minus any same-origin child already covered by its
 * parent's own recursive scan (see excludeFramesCoveredByParentRecursion()). Falls back to just
 * the top frame, with `usedWebNavigation: false`, if the API itself is unavailable — in that
 * fallback ONLY, a frame's own scanAll()-reported skippedFrames count is the sole signal this
 * extension has about any nested iframe at all (see runFillForTab()).
 */
function getFrames(tabId) {
  if (!chrome.webNavigation || typeof chrome.webNavigation.getAllFrames !== 'function') {
    return Promise.resolve({ frames: [{ frameId: 0, url: '' }], usedWebNavigation: false });
  }
  return chrome.webNavigation.getAllFrames({ tabId: tabId }).then(function (frames) {
    frames = (frames || []).filter(function (f) { return /^https?:\/\//.test(f.url || ''); });
    if (!frames.length) return { frames: [{ frameId: 0, url: '' }], usedWebNavigation: false };
    return { frames: excludeFramesCoveredByParentRecursion(frames), usedWebNavigation: true };
  }, function () {
    return { frames: [{ frameId: 0, url: '' }], usedWebNavigation: false };
  });
}

// Field ids coming out of scanner.js already look like "f0", "f1", ... — qualifying them with
// a plain string prefix (rather than trying to parse/rewrite that shape) keeps this trivially
// reversible no matter what scanner.js's own id format is. "__" is safe as a separator because
// scanner.js never puts an underscore in an id itself.
var FRAME_ID_SEP = '__';
function qualifyId(frameId, localId) { return 'fr' + frameId + FRAME_ID_SEP + localId; }
function splitQualifiedId(qid) {
  var s = String(qid == null ? '' : qid);
  if (s.slice(0, 2) !== 'fr') return null;
  var idx = s.indexOf(FRAME_ID_SEP);
  if (idx === -1) return null;
  var frameIdStr = s.slice(2, idx);
  if (!/^-?\d+$/.test(frameIdStr)) return null;
  var localId = s.slice(idx + FRAME_ID_SEP.length);
  if (!localId) return null;
  return { frameId: parseInt(frameIdStr, 10), localId: localId };
}

function initialCombinedState(frameIds) {
  return {
    status: 'running',
    url: '',
    startedAt: Date.now(),
    updatedAt: Date.now(),
    progress: { current: 0, total: 0, label: 'Starting…' },
    counts: { filled: 0, drafts: 0, needsYou: 0, failed: 0 },
    filled: [], drafts: [], needsYou: [], failed: [],
    resume: null,
    shieldFired: false,
    undoAvailable: false,
    error: null,
    note: null,
    skippedFrames: 0,
    couldNotRead: 0,
    _frameStates: {},
    _expectedFrameIds: (frameIds || []).slice()
  };
}

/**
 * Folds one frame's own FILL_STATE_UPDATE report into the tab-wide combined state the panel
 * actually reads. `combined` is mutated in place and returned. The combined view is fully
 * recomputed from every frame's LATEST report each time (never incrementally patched), so a
 * stale field from an earlier report can never linger after a newer one supersedes it.
 */
function applyFrameReport(combined, frameId, frameState) {
  combined._frameStates = combined._frameStates || {};
  combined._frameStates[frameId] = frameState;
  var expected = combined._expectedFrameIds || [];
  if (expected.indexOf(frameId) === -1) expected = expected.concat([frameId]);
  combined._expectedFrameIds = expected;

  var reported = [];
  var reportedFrameIds = []; // same index correspondence as `reported` — see concatList() below
  for (var i = 0; i < expected.length; i++) {
    var s = combined._frameStates[expected[i]];
    if (s) { reported.push(s); reportedFrameIds.push(expected[i]); }
  }
  var anyRunning = reported.length < expected.length || reported.some(function (s) { return s.status === 'running'; });

  combined.status = anyRunning ? 'running'
    : reported.some(function (s) { return s.status === 'error'; }) ? 'error'
    : reported.some(function (s) { return s.status === 'cancelled'; }) ? 'cancelled'
    : reported.length && reported.every(function (s) { return s.status === 'idle'; }) ? 'idle'
    : 'done';

  var topState = combined._frameStates[0];
  combined.url = (topState && topState.url) || (reported[0] && reported[0].url) || combined.url || '';
  combined.startedAt = combined.startedAt || (reported[0] && reported[0].startedAt) || Date.now();
  combined.updatedAt = Date.now();

  // Every entry's `id` is re-qualified with ITS OWN frame's id (reusing the same qualifyId() the
  // /resolve round trip uses) as it's folded into the combined, panel-visible state — so a row in
  // filled/drafts/needsYou/failed is always independently routable back to "which frame, which
  // local field id" (e.g. to scroll/flash it, or to read a field back for "remember my answers"),
  // never just a same-shaped local id two frames could otherwise collide on. Everything else on
  // the entry (label/value/reason/...) passes through unchanged; a `frame` field is added too
  // (this frame's own last-reported url) for anything that wants to show/report where a field
  // lives without a second round trip.
  function concatList(key) {
    var out = [];
    reported.forEach(function (s, idx) {
      var frameId = reportedFrameIds[idx];
      (s[key] || []).forEach(function (item) {
        var copy = {};
        for (var k in item) if (Object.prototype.hasOwnProperty.call(item, k)) copy[k] = item[k];
        if (copy.id != null) copy.id = qualifyId(frameId, copy.id);
        copy.frame = { frameId: frameId, url: s.url || '' };
        out.push(copy);
      });
    });
    return out;
  }
  combined.filled = concatList('filled');
  combined.drafts = concatList('drafts');
  combined.needsYou = concatList('needsYou');
  combined.failed = concatList('failed');
  combined.counts = {
    filled: combined.filled.length,
    drafts: combined.drafts.length,
    needsYou: combined.needsYou.length,
    failed: combined.failed.length
  };
  combined.shieldFired = reported.some(function (s) { return s.shieldFired; });
  combined.undoAvailable = reported.some(function (s) { return s.undoAvailable; });

  var errors = reported.map(function (s) { return s.error; }).filter(Boolean);
  combined.error = errors.length ? errors.join(' | ') : (combined.error || null);
  var notes = reported.map(function (s) { return s.note; }).filter(Boolean);
  combined.note = notes.length ? notes.join(' ') : (combined.note || null);

  var withResume = reported.filter(function (s) { return s.resume && s.resume.attempted; });
  if (withResume.length) combined.resume = withResume[0].resume;

  var runningStates = reported.filter(function (s) { return s.status === 'running' && s.progress; });
  if (runningStates.length) {
    var cur = 0, tot = 0;
    runningStates.forEach(function (s) { cur += s.progress.current || 0; tot += s.progress.total || 0; });
    var lastLabel = runningStates[runningStates.length - 1].progress.label || '';
    combined.progress = {
      current: cur,
      total: tot,
      label: lastLabel + (expected.length > 1 ? ' [' + reported.length + '/' + expected.length + ' frames]' : '')
    };
  } else if (!anyRunning) {
    var doneTotal = combined.counts.filled + combined.counts.drafts + combined.counts.needsYou + combined.counts.failed;
    combined.progress = { current: doneTotal, total: doneTotal, label: '' };
  }

  return combined;
}

/**
 * Runs one fill across every frame of `tabId`. Resolves once every frame has been told to
 * start applying its fills (or once it's clear there is nothing to apply anywhere) — NOT once
 * any of them finish; see the file header for the full sequence. `opts`: { url, fieldTimeoutMs,
 * budgetMs } — the latter two are test-only overrides threaded straight through to every
 * frame's APPLY_FILLS (see content.js).
 */
function runFillForTab(tabId, opts) {
  opts = opts || {};
  return getFrames(tabId).then(function (frameInfo) {
    var frames = frameInfo.frames;
    var usedWebNavigation = frameInfo.usedWebNavigation;
    var frameIds = frames.map(function (f) { return f.frameId; });

    return updateStoredState(tabId, function () { return initialCombinedState(frameIds); })
      .then(function () {
        return Promise.all(frames.map(function (f) {
          return chrome.tabs.sendMessage(tabId, { type: 'PREPARE_AND_SCAN' }, { frameId: f.frameId })
            .then(function (resp) { return { frameId: f.frameId, ok: true, resp: resp }; })
            .catch(function (e) { return { frameId: f.frameId, ok: false, error: String(e && e.message ? e.message : e) }; });
        }));
      })
      .then(function (results) {
        var allFields = [];
        var preparedFrameIds = [];
        var notPrepared = 0;
        var scanSkipSum = 0;
        var unreadSum = 0;
        var anyCancelled = false;

        results.forEach(function (r) {
          if (!r.ok || !r.resp || !r.resp.ok) { notPrepared++; return; }
          scanSkipSum += r.resp.skippedFrames || 0;
          unreadSum += r.resp.unreadControls || 0;
          if (r.resp.cancelled) anyCancelled = true;
          if ((r.resp.fields || []).length) {
            preparedFrameIds.push(r.frameId);
            r.resp.fields.forEach(function (f) {
              var qf = {};
              for (var k in f) if (Object.prototype.hasOwnProperty.call(f, k)) qf[k] = f[k];
              qf.id = qualifyId(r.frameId, f.id);
              allFields.push(qf);
            });
          }
        });

        // scanSkipSum is each frame's OWN scanAll()-reported count of DIRECT same-origin-check
        // failures (cross-origin children it could not recurse into). When webNavigation gave
        // us the real, complete frame list, every one of those children is ALSO independently
        // handled via this file's own top-level mechanism (prepared, or counted in
        // notPrepared) — folding scanSkipSum in too would double-count exactly the gap this
        // build closes. It is only genuine NEW information in the fallback case, where
        // webNavigation couldn't tell us about nested frames at all.
        var skippedFramesTotal = notPrepared + (usedWebNavigation ? 0 : scanSkipSum);
        var couldNotReadTotal = unreadSum + skippedFramesTotal;

        if (!allFields.length) {
          return updateStoredState(tabId, function (existing) {
            var combined = existing || initialCombinedState(frameIds);
            combined._expectedFrameIds = [];
            combined.status = anyCancelled ? 'cancelled' : 'done';
            combined.note = combined.note || 'No fillable fields found on this page.';
            combined.skippedFrames = skippedFramesTotal;
            combined.couldNotRead = couldNotReadTotal;
            combined.progress = { current: 0, total: 0, label: '' };
            return combined;
          });
        }

        return callResolve(opts.url || '', allFields).then(function (resolveResp) {
          if (!resolveResp || !resolveResp.ok) {
            var message = (resolveResp && resolveResp.message) || 'The local service call failed.';
            return Promise.all(preparedFrameIds.map(function (frameId) {
              return chrome.tabs.sendMessage(tabId, { type: 'ABORT_FILL', error: message }, { frameId: frameId }).catch(function () {});
            })).then(function () {
              return updateStoredState(tabId, function (existing) {
                var combined = existing || initialCombinedState(frameIds);
                combined._expectedFrameIds = [];
                combined.status = 'error';
                combined.error = message;
                combined.skippedFrames = skippedFramesTotal;
                combined.couldNotRead = couldNotReadTotal;
                combined.progress = { current: 0, total: 0, label: '' };
                return combined;
              });
            });
          }

          var data = resolveResp.data || {};
          var perFrameFills = {};
          var perFrameSkipped = {};
          preparedFrameIds.forEach(function (fid) { perFrameFills[fid] = []; perFrameSkipped[fid] = []; });

          (data.fills || []).forEach(function (fill) {
            var parts = splitQualifiedId(fill.id);
            if (!parts || !perFrameFills[parts.frameId]) return;
            var f2 = {};
            for (var k in fill) if (Object.prototype.hasOwnProperty.call(fill, k)) f2[k] = fill[k];
            f2.id = parts.localId;
            perFrameFills[parts.frameId].push(f2);
          });
          (data.skipped || []).forEach(function (skip) {
            var parts = splitQualifiedId(skip.id);
            if (!parts || !perFrameSkipped[parts.frameId]) return;
            var s2 = {};
            for (var k in skip) if (Object.prototype.hasOwnProperty.call(skip, k)) s2[k] = skip[k];
            s2.id = parts.localId;
            perFrameSkipped[parts.frameId].push(s2);
          });

          return Promise.all(preparedFrameIds.map(function (frameId) {
            return chrome.tabs.sendMessage(tabId, {
              type: 'APPLY_FILLS',
              fills: perFrameFills[frameId] || [],
              skipped: perFrameSkipped[frameId] || [],
              fieldTimeoutMs: opts.fieldTimeoutMs,
              budgetMs: opts.budgetMs
            }, { frameId: frameId }).then(function () { return frameId; }, function () { return null; });
          })).then(function (dispatched) {
            var liveFrameIds = dispatched.filter(function (fid) { return fid !== null; });
            return updateStoredState(tabId, function (existing) {
              var combined = existing || initialCombinedState(frameIds);
              combined._expectedFrameIds = liveFrameIds;
              combined.skippedFrames = skippedFramesTotal;
              combined.couldNotRead = couldNotReadTotal;
              if (!liveFrameIds.length) {
                combined.status = 'error';
                combined.error = 'Could not hand any frame its fills to apply.';
                combined.progress = { current: 0, total: 0, label: '' };
              }
              return combined;
            });
          });
        }, function (e) {
          return updateStoredState(tabId, function (existing) {
            var combined = existing || initialCombinedState(frameIds);
            combined._expectedFrameIds = [];
            combined.status = 'error';
            combined.error = 'Error while resolving fields: ' + (e && e.message ? e.message : e);
            combined.skippedFrames = skippedFramesTotal;
            combined.couldNotRead = couldNotReadTotal;
            combined.progress = { current: 0, total: 0, label: '' };
            return combined;
          });
        });
      });
  });
}

/** Fans CANCEL_FILL out to every known frame of `tabId`; best-effort, never throws. */
function cancelFillForTab(tabId) {
  return getFrames(tabId).then(function (frameInfo) {
    return Promise.all(frameInfo.frames.map(function (f) {
      return chrome.tabs.sendMessage(tabId, { type: 'CANCEL_FILL' }, { frameId: f.frameId })
        .catch(function () { return null; });
    }));
  }).then(function (results) {
    return { ok: true, cancelling: results.some(function (r) { return r && r.cancelling; }) };
  });
}

/** Fans UNDO out to every known frame of `tabId` and sums how many fields were restored. */
function undoFillForTab(tabId) {
  return getFrames(tabId).then(function (frameInfo) {
    var frames = frameInfo.frames;
    var frameIds = frames.map(function (f) { return f.frameId; });
    // Reset which frames are "expected" to whatever the page currently has, so the per-frame
    // 'idle' reports that follow merge into a clean combined 'idle' state instead of getting
    // stuck waiting forever on a frame from an earlier fill that no longer exists.
    return updateStoredState(tabId, function (existing) {
      var combined = existing || initialCombinedState(frameIds);
      combined._expectedFrameIds = frameIds;
      return combined;
    }).then(function () {
      return Promise.all(frames.map(function (f) {
        return chrome.tabs.sendMessage(tabId, { type: 'UNDO' }, { frameId: f.frameId })
          .then(function (r) { return (r && r.restored) || 0; })
          .catch(function () { return 0; });
      }));
    });
  }).then(function (counts) {
    var total = counts.reduce(function (a, b) { return a + b; }, 0);
    return { ok: true, restored: total };
  });
}

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg || typeof msg !== 'object') return false;

  if (msg.type === 'RUN_FILL') {
    runFillForTab(msg.tabId, { url: msg.url, fieldTimeoutMs: msg.fieldTimeoutMs, budgetMs: msg.budgetMs })
      .then(function () { sendResponse({ ok: true, started: true }); },
            function (e) { sendResponse({ ok: false, error: String(e && e.message ? e.message : e) }); });
    return true;
  }
  if (msg.type === 'CANCEL_FILL_TAB') {
    cancelFillForTab(msg.tabId).then(sendResponse, function (e) {
      sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
    });
    return true;
  }
  if (msg.type === 'UNDO_TAB') {
    undoFillForTab(msg.tabId).then(sendResponse, function (e) {
      sendResponse({ ok: false, error: String(e && e.message ? e.message : e), restored: 0 });
    });
    return true;
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
  if (msg.type === 'DRAFT_COVER_LETTER') {
    callCoverLetter(msg.urls, msg.pageText).then(sendResponse);
    return true;
  }
  if (msg.type === 'REMEMBER_ANSWERS') {
    rememberAnswersForTab(msg.tabId).then(sendResponse, function (e) {
      sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
    });
    return true;
  }
  if (msg.type === 'FILL_STATE_UPDATE') {
    // Only ever sent by a content.js frame, whose sender.tab is always populated (a real tab,
    // never the side panel or options page) and whose sender.frameId Chrome always fills in.
    // Silently ignored otherwise rather than throwing.
    if (!sender || !sender.tab || typeof sender.tab.id !== 'number') {
      sendResponse({ ok: false, error: 'no sender tab' });
      return false;
    }
    var tabId = sender.tab.id;
    var frameId = typeof sender.frameId === 'number' ? sender.frameId : 0;
    updateStoredState(tabId, function (existing) {
      var combined = existing || initialCombinedState([frameId]);
      return applyFrameReport(combined, frameId, msg.state);
    }).then(function () {
      sendResponse({ ok: true });
    }, function (e) {
      sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
    });
    return true;
  }
  return false;
});
