#!/usr/bin/env node
/*
 * Tests extension/llm_bridge.js in plain Node -- no browser, no npm packages:
 *   node extension/llm_bridge_selftest.js
 * Each test loads the file into a fresh vm context with fake chrome/fetch/LanguageModel.
 * Dev-only: never shipped (not in packaging/extension_files.txt).
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, 'llm_bridge.js'), 'utf8');

function load(globals) {
  const ctx = vm.createContext(Object.assign({ setTimeout: () => 0 }, globals));
  vm.runInContext(SRC, ctx);
  return ctx.ApplyPilotLlmBridge;
}

function fakeChrome(store) {
  return { storage: { local: { get: () => Promise.resolve(Object.assign({}, store)) } } };
}

function jsonResponse(obj, status) {
  const code = status || 200;
  return { ok: code < 400, status: code, json: () => Promise.resolve(obj) };
}

const checks = [];
function expect(name, cond) { checks.push({ name, pass: !!cond }); }

async function main() {
  // ---- toPromptApi ----
  {
    const B = load({});
    const p = B.toPromptApi([
      { role: 'system', content: 'A' }, { role: 'system', content: 'B' },
      { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' }, { role: 'user', content: 'q2' }]);
    expect('toPromptApi: every system message merged into one leading system prompt',
      p.initialPrompts[0].role === 'system' && p.initialPrompts[0].content === 'A\n\nB');
    expect('toPromptApi: earlier turns kept in order',
      p.initialPrompts.length === 3 && p.initialPrompts[1].content === 'q1' && p.initialPrompts[2].content === 'a1');
    expect('toPromptApi: the last user message is the input', p.input === 'q2');
    let threw = false;
    try { B.toPromptApi([{ role: 'system', content: 'x' }]); } catch (e) { threw = true; }
    expect('toPromptApi: no user message throws', threw);
  }

  // ---- availability ----
  {
    expect('availability: no LanguageModel -> unavailable', (await load({}).availability()) === 'unavailable');
    const odd = load({ LanguageModel: { availability: () => Promise.resolve('weird') } });
    expect('availability: an unknown value -> unavailable', (await odd.availability()) === 'unavailable');
    const ok = load({ LanguageModel: { availability: () => Promise.resolve('available') } });
    expect('availability: passes "available" through', (await ok.availability()) === 'available');
    const boom = load({ LanguageModel: { availability: () => Promise.reject(new Error('x')) } });
    expect('availability: a throwing API -> unavailable', (await boom.availability()) === 'unavailable');
  }

  // ---- start() is inert without LanguageModel ----
  {
    let fetched = 0;
    const B = load({ chrome: fakeChrome({ token: 't' }),
      fetch: () => { fetched++; return Promise.resolve(jsonResponse({ job: null })); } });
    expect('start: inert where the browser has no LanguageModel', B.start() === false && fetched === 0);
  }

  // ---- pollOnce reports status; nothing runs while the model isn't ready ----
  {
    const calls = [];
    const B = load({
      chrome: fakeChrome({ token: 'tok', serviceUrl: 'http://127.0.0.1:9999/' }),
      LanguageModel: { availability: () => Promise.resolve('downloadable'),
        create: () => { throw new Error('must not run'); } },
      fetch: (url, opts) => { calls.push([url, opts]); return Promise.resolve(jsonResponse({ job: null })); }
    });
    const r = await B.pollOnce();
    expect('pollOnce: reports its status to /llm/next (trailing slash on the URL trimmed)',
      calls.length === 1 && calls[0][0] === 'http://127.0.0.1:9999/llm/next?status=downloadable');
    expect('pollOnce: sends the token', calls[0][1].headers['X-ApplyPilot-Token'] === 'tok');
    expect('pollOnce: model not ready -> waits before the next poll', r.waitMs === B.IDLE_WAIT_MS);
  }

  // ---- no token yet: no request at all ----
  {
    let fetched = 0;
    const B = load({ chrome: fakeChrome({}), LanguageModel: { availability: () => Promise.resolve('available') },
      fetch: () => { fetched++; return Promise.resolve(jsonResponse({ job: null })); } });
    const r = await B.pollOnce();
    expect('pollOnce: no token yet -> no request, idle wait', fetched === 0 && r.waitMs === B.IDLE_WAIT_MS);
  }

  // ---- a job is run and answered ----
  {
    const posts = [];
    const created = [];
    const B = load({
      chrome: fakeChrome({ token: 'tok' }),
      LanguageModel: {
        availability: () => Promise.resolve('available'),
        params: () => Promise.resolve({ defaultTopK: 3, maxTopK: 128, defaultTemperature: 1, maxTemperature: 2 }),
        create: (opts) => {
          created.push(opts);
          return Promise.resolve({ prompt: (input) => Promise.resolve('ANSWER to ' + input), destroy: () => {} });
        }
      },
      fetch: (url, opts) => {
        if (url.indexOf('/llm/next') !== -1) {
          return Promise.resolve(jsonResponse({ job: { id: '7', temperature: 5,
            messages: [{ role: 'system', content: 'S' }, { role: 'user', content: 'Q' }] } }));
        }
        posts.push([url, JSON.parse(opts.body)]);
        return Promise.resolve(jsonResponse({ ok: true }));
      }
    });
    const r = await B.pollOnce();
    expect('job: the answer is posted to /llm/result with its id',
      posts.length === 1 && posts[0][0] === 'http://127.0.0.1:8787/llm/result' &&
      posts[0][1].id === '7' && posts[0][1].text === 'ANSWER to Q');
    expect('job: temperature clamped to the model maximum, sent together with topK',
      created[0].temperature === 2 && created[0].topK === 3);
    expect('job: the system message is the first initial prompt',
      created[0].initialPrompts[0].role === 'system' && created[0].initialPrompts[0].content === 'S');
    expect('job: polls again immediately', r.waitMs === 0);
  }

  // ---- a model error is reported, never swallowed ----
  {
    const posts = [];
    const B = load({
      chrome: fakeChrome({ token: 'tok' }),
      LanguageModel: { availability: () => Promise.resolve('available'),
        create: () => Promise.resolve({ prompt: () => Promise.reject(new Error('input too long')), destroy: () => {} }) },
      fetch: (url, opts) => {
        if (url.indexOf('/llm/next') !== -1) {
          return Promise.resolve(jsonResponse({ job: { id: '8', temperature: 0.3, messages: [{ role: 'user', content: 'Q' }] } }));
        }
        posts.push(JSON.parse(opts.body));
        return Promise.resolve(jsonResponse({ ok: true }));
      }
    });
    await B.pollOnce();
    expect('job error: posted back as {id, error}',
      posts.length === 1 && posts[0].id === '8' && /input too long/.test(posts[0].error) && posts[0].text === undefined);
  }

  // ---- service unreachable ----
  {
    const B = load({ chrome: fakeChrome({ token: 'tok' }), LanguageModel: { availability: () => Promise.resolve('available') },
      fetch: () => Promise.reject(new Error('ECONNREFUSED')) });
    const r = await B.pollOnce();
    expect('pollOnce: service unreachable -> idle wait, never throws', r.waitMs === B.IDLE_WAIT_MS);
  }

  // ---- start(): the loop reschedules itself (Important #3) ----
  {
    let timeoutCalls = 0;
    const MAX_ITERS = 3;
    function fakeSetTimeout(fn) {
      timeoutCalls++;
      if (timeoutCalls < MAX_ITERS) fn();
      return 0;
    }
    const B = load({
      chrome: fakeChrome({ token: 'tok' }),
      LanguageModel: { availability: () => Promise.resolve('unavailable') },
      fetch: () => Promise.resolve(jsonResponse({ job: null })),
      setTimeout: fakeSetTimeout
    });
    expect('start: returns true and starts the loop', B.start() === true);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect('start: the loop reschedules itself across iterations', timeoutCalls >= MAX_ITERS);
  }

  // ---- start(): a synchronous throw from a callee does not kill the loop (Important #2) ----
  {
    let timeoutCalls = 0;
    const MAX_ITERS = 3;
    function fakeSetTimeout(fn) {
      timeoutCalls++;
      if (timeoutCalls < MAX_ITERS) fn();
      return 0;
    }
    const throwingChrome = { storage: { local: { get: () => { throw new Error('sync boom'); } } } };
    const B = load({
      chrome: throwingChrome,
      LanguageModel: { availability: () => Promise.resolve('available') },
      fetch: () => Promise.resolve(jsonResponse({ job: null })),
      setTimeout: fakeSetTimeout
    });
    B.start();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect('start: survives a synchronous throw from getConfig and keeps polling', timeoutCalls >= MAX_ITERS);
  }

  // ---- download() ----
  {
    let msg = '';
    try { await load({}).download(); } catch (e) { msg = e.message; }
    expect('download: no LanguageModel -> a clear error', /built-in AI/.test(msg));

    const progress = [];
    let createdSync = false;
    const B = load({ LanguageModel: {
      create: (opts) => {
        createdSync = true;
        opts.monitor({ addEventListener: (type, fn) => { if (type === 'downloadprogress') { fn({ loaded: 0.5 }); fn({ loaded: 1 }); } } });
        return Promise.resolve({ destroy: () => {} });
      } } });
    const pending = B.download((loaded) => progress.push(loaded));
    expect('download: create() is called synchronously (Chrome needs the click gesture)', createdSync);
    expect('download: resolves "available"', (await pending) === 'available');
    expect('download: reports progress', progress.join(',') === '0.5,1');
  }

  let failed = 0;
  for (const c of checks) {
    console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.name}`);
    if (!c.pass) failed++;
  }
  console.log(`\n${checks.length - failed}/${checks.length} checks passed.`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
