/**
 * ApplyPilot Copilot — on-device AI bridge.
 *
 * Chrome's built-in model (Gemini Nano, the Prompt API's `LanguageModel`) runs
 * inside the browser, so the local Python service can't call it. While the side
 * panel or the options page is open, this loop asks the service for work
 * (GET /llm/next, held up to ~20 s), runs each job with LanguageModel, and posts
 * the answer back (POST /llm/result). The service's llm_bridge.BridgeClient
 * waits for that answer, so every AI feature (drafted answers, cover letters,
 * résumé import) works unchanged.
 *
 * Inert where the browser has no LanguageModel at all (older Chrome, test
 * browsers). Reads the service URL and token from chrome.storage.local — the
 * same keys background.js's getConfig() uses. Never runs in the service worker:
 * its lifetime is too short for a long-poll loop.
 */
(function (root) {
  'use strict';

  var DEFAULT_SERVICE_URL = 'http://127.0.0.1:8787';
  var IDLE_WAIT_MS = 5000;
  var STATUSES = ['available', 'downloadable', 'downloading', 'unavailable'];
  var running = false;

  function model() { return root.LanguageModel; }

  function destroy(session) {
    try { if (session && typeof session.destroy === 'function') session.destroy(); } catch (e) { /* ignore */ }
  }

  /** 'available' | 'downloadable' | 'downloading' | 'unavailable' — never rejects. */
  function availability() {
    var LM = model();
    if (!LM || typeof LM.availability !== 'function') return Promise.resolve('unavailable');
    return Promise.resolve().then(function () { return LM.availability(); }).then(function (a) {
      return STATUSES.indexOf(a) === -1 ? 'unavailable' : a;
    }, function () { return 'unavailable'; });
  }

  function getConfig() {
    return root.chrome.storage.local.get(['serviceUrl', 'token']).then(function (d) {
      return {
        serviceUrl: String(d.serviceUrl || DEFAULT_SERVICE_URL).replace(/\/+$/, ''),
        token: d.token || ''
      };
    });
  }

  /**
   * Python chat messages -> { initialPrompts, input } for the Prompt API. Every
   * system message is merged into ONE leading system prompt (the API accepts a
   * system role only first); the last user message is the input; the turns in
   * between stay in order.
   */
  function toPromptApi(messages) {
    var list = (messages || []).filter(function (m) { return m && typeof m.content === 'string'; });
    var last = -1;
    for (var i = list.length - 1; i >= 0; i--) {
      if (list[i].role === 'user') { last = i; break; }
    }
    if (last === -1) throw new Error('no user message to answer');
    var systemParts = [];
    var turns = [];
    for (var j = 0; j < last; j++) {
      var m = list[j];
      if (m.role === 'system') systemParts.push(m.content);
      else turns.push({ role: m.role === 'assistant' ? 'assistant' : 'user', content: m.content });
    }
    var initialPrompts = systemParts.length ? [{ role: 'system', content: systemParts.join('\n\n') }] : [];
    return { initialPrompts: initialPrompts.concat(turns), input: list[last].content };
  }

  /** Runs one job with the on-device model; resolves to its text. */
  function runJob(job) {
    var LM = model();
    return Promise.resolve().then(function () {
      var p = toPromptApi(job.messages);
      var paramsP = typeof LM.params === 'function'
        ? Promise.resolve().then(function () { return LM.params(); }).catch(function () { return null; })
        : Promise.resolve(null);
      return paramsP.then(function (params) {
        var opts = { initialPrompts: p.initialPrompts };
        if (params && typeof job.temperature === 'number') {
          // The API takes temperature and topK together, or neither.
          opts.temperature = Math.max(0, Math.min(job.temperature, params.maxTemperature));
          opts.topK = params.defaultTopK;
        }
        return LM.create(opts);
      }).then(function (session) {
        return Promise.resolve(session.prompt(p.input)).then(function (text) {
          destroy(session);
          return String(text == null ? '' : text);
        }, function (err) {
          destroy(session);
          throw err;
        });
      });
    });
  }

  function postResult(cfg, body) {
    return root.fetch(cfg.serviceUrl + '/llm/result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-ApplyPilot-Token': cfg.token },
      body: JSON.stringify(body)
    }).then(function (resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp;
    });
  }

  /** One poll: report status, take at most one job, answer it. Resolves { waitMs }; never rejects. */
  function pollOnce() {
    return Promise.all([getConfig(), availability()]).then(function (r) {
      var cfg = r[0];
      var status = r[1];
      if (!cfg.token) return { waitMs: IDLE_WAIT_MS };
      return root.fetch(cfg.serviceUrl + '/llm/next?status=' + encodeURIComponent(status), {
        headers: { 'X-ApplyPilot-Token': cfg.token }
      }).then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      }).then(function (data) {
        var job = data && data.job;
        if (!job) return { waitMs: status === 'available' ? 0 : IDLE_WAIT_MS };
        return runJob(job).then(function (text) {
          return postResult(cfg, { id: job.id, text: text });
        }, function (err) {
          return postResult(cfg, { id: job.id, error: String((err && err.message) || err).slice(0, 300) });
        }).then(function () { return { waitMs: 0 }; });
      });
    }).catch(function (err) {
      try { root.console && root.console.error && root.console.error('ApplyPilot AI bridge:', err); } catch (e) {}
      return { waitMs: IDLE_WAIT_MS };
    });
  }

  /** Starts the loop, once per page. Returns false (and does nothing) without LanguageModel. */
  function start() {
    if (running || !model()) return false;
    running = true;
    (function loop() {
      var p;
      try {
        p = pollOnce();
      } catch (e) {
        root.setTimeout(loop, IDLE_WAIT_MS);
        return;
      }
      p.then(function (r) { root.setTimeout(loop, r.waitMs); });
    })();
    return true;
  }

  /**
   * Downloads the on-device model. MUST be called straight from a click handler:
   * Chrome only starts the download with a user gesture, so create() is called
   * before anything is awaited.
   */
  function download(onProgress) {
    var LM = model();
    if (!LM || typeof LM.create !== 'function') {
      return Promise.reject(new Error("this version of Chrome doesn't include built-in AI"));
    }
    var created = LM.create({
      monitor: function (m) {
        m.addEventListener('downloadprogress', function (e) { if (onProgress) onProgress(e.loaded); });
      }
    });
    return Promise.resolve(created).then(function (session) {
      destroy(session);
      return 'available';
    });
  }

  root.ApplyPilotLlmBridge = {
    start: start,
    availability: availability,
    download: download,
    toPromptApi: toPromptApi,
    runJob: runJob,
    pollOnce: pollOnce,
    IDLE_WAIT_MS: IDLE_WAIT_MS
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
