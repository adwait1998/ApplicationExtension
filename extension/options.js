/**
 * ApplyPilot Copilot — options page logic.
 *
 * Two independent pieces:
 *   1. Connection settings: { serviceUrl, token } in chrome.storage.local, plus a
 *      "Test connection" button that asks the background worker to call GET /health
 *      (never fetched from this page directly — background.js is the sole fetcher for
 *      the autofill path, see its file header).
 *   2. The profile editor: talks to the local service's profile-management endpoints
 *      directly (GET/POST /profile/full, GET /profiles, POST /profiles,
 *      POST /profiles/{id}/activate). This page already holds the token in the clear
 *      to let the operator view/paste it, so there is no extra exposure in it also
 *      making these requests itself — host_permissions already scope fetch to
 *      127.0.0.1 for every extension page, not just the background worker.
 *
 * The editor never constructs a fresh JSON object from only the fields it renders.
 * It keeps the full profile object returned by GET /profile/full in `profileData` and
 * mutates that in place, so sections this UI doesn't know about (eeo_voluntary,
 * resume_facts, availability, ...) round-trip untouched instead of being silently
 * deleted on save. personal.password is never in that object in the first place —
 * the service strips it before this page ever sees it — and this file never adds a
 * field for it.
 */
(function () {
  'use strict';

  var DEFAULT_SERVICE_URL = 'http://127.0.0.1:8787';

  // -- Connection settings (existing behaviour, unchanged) --------------------

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
      if (tokenEl.value) {
        refreshProfileArea();
      }
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
      refreshProfileArea();
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

  // -- Profile editor -----------------------------------------------------------

  var legacyNoticeEl = document.getElementById('legacyNotice');
  var profileSwitcherEl = document.getElementById('profileSwitcher');
  var profileSelectEl = document.getElementById('profileSelect');
  var newProfileBtn = document.getElementById('newProfileBtn');
  var reloadProfileBtn = document.getElementById('reloadProfileBtn');
  var newProfileRowEl = document.getElementById('newProfileRow');
  var newProfileIdEl = document.getElementById('newProfileId');
  var createProfileBtn = document.getElementById('createProfileBtn');
  var cancelNewProfileBtn = document.getElementById('cancelNewProfileBtn');
  var profileEditorEl = document.getElementById('profileEditor');
  var saveProfileBtn = document.getElementById('saveProfile');
  var profileStatusEl = document.getElementById('profileStatus');
  var workHistoryListEl = document.getElementById('workHistoryList');
  var educationListEl = document.getElementById('educationList');
  var workHistoryTemplate = document.getElementById('workHistoryRowTemplate');
  var educationTemplate = document.getElementById('educationRowTemplate');

  var PROFILE_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

  // The full profile object as last loaded from the service. Every editor
  // control reads from / writes into this object in place — see the file
  // header for why that matters.
  var profileData = {};
  var profilesListLoaded = false;

  function setProfileStatus(kind, text) {
    profileStatusEl.className = kind;
    profileStatusEl.textContent = text;
  }

  function apiUrl(path) {
    var base = (serviceUrlEl.value || DEFAULT_SERVICE_URL).trim().replace(/\/+$/, '');
    return base + path;
  }

  function apiHeaders(withJson) {
    var h = { 'X-ApplyPilot-Token': (tokenEl.value || '').trim() };
    if (withJson) h['Content-Type'] = 'application/json';
    return h;
  }

  function apiResponse(resp) {
    return resp.text().then(function (text) {
      var data = null;
      if (text) {
        try { data = JSON.parse(text); } catch (e) { data = null; }
      }
      if (!resp.ok) {
        var msg = (data && data.detail) ? data.detail : ('HTTP ' + resp.status);
        var err = new Error(msg);
        err.status = resp.status;
        throw err;
      }
      return data;
    });
  }

  function apiGet(path) {
    return fetch(apiUrl(path), { headers: apiHeaders(false) }).then(apiResponse);
  }

  function apiPost(path, body) {
    return fetch(apiUrl(path), { method: 'POST', headers: apiHeaders(true), body: JSON.stringify(body || {}) })
      .then(apiResponse);
  }

  // -- dotted-path helpers (mirror server.py's _dotted_get/_dotted_set) --------

  function dottedGet(obj, path) {
    var parts = path.split('.');
    var cur = obj;
    for (var i = 0; i < parts.length; i++) {
      if (cur === null || typeof cur !== 'object' || !(parts[i] in cur)) return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  function dottedSet(obj, path, value) {
    var parts = path.split('.');
    var cur = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      if (typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = value;
  }

  // -- loading -------------------------------------------------------------------

  function refreshProfileArea() {
    if (!tokenEl.value) return;
    loadProfilesList().then(loadFullProfile);
  }

  function loadProfilesList() {
    return apiGet('/profiles').then(function (data) {
      profilesListLoaded = true;
      renderProfileSwitcher(data || { profiles: [], legacy: true });
    }, function (err) {
      profilesListLoaded = false;
      // Not fatal — legacy installs and a not-yet-reachable service both land
      // here. The single-profile editor below still works via /profile/full.
      profileSwitcherEl.style.display = 'none';
      legacyNoticeEl.style.display = 'none';
      setProfileStatus('err', 'Could not reach the profile service: ' + err.message);
    });
  }

  function renderProfileSwitcher(data) {
    if (data.legacy) {
      profileSwitcherEl.style.display = 'none';
      newProfileRowEl.style.display = 'none';
      legacyNoticeEl.style.display = 'block';
      return;
    }
    legacyNoticeEl.style.display = 'none';
    profileSwitcherEl.style.display = 'flex';
    profileSelectEl.innerHTML = '';
    (data.profiles || []).forEach(function (p) {
      var opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.id + (p.name ? ' — ' + p.name : '') + (p.active ? '  (active)' : '');
      if (p.active) opt.selected = true;
      profileSelectEl.appendChild(opt);
    });
  }

  function loadFullProfile() {
    profileEditorEl.classList.add('disabled');
    return apiGet('/profile/full').then(function (data) {
      profileData = data || {};
      renderEditor();
      profileEditorEl.classList.remove('disabled');
      setProfileStatus('info', 'Profile loaded.');
    }, function (err) {
      profileEditorEl.classList.remove('disabled');
      setProfileStatus('err', 'Could not load profile: ' + err.message);
    });
  }

  // -- rendering -------------------------------------------------------------------

  function renderEditor() {
    // plain text/select/url/etc inputs bound via data-path
    profileEditorEl.querySelectorAll('input[data-path]:not([type=radio]), select[data-path], textarea[data-path]')
      .forEach(function (el) {
        var v = dottedGet(profileData, el.getAttribute('data-path'));
        el.value = v == null ? '' : v;
      });

    // tri-state boolean radios: select "unset" unless the stored value is
    // strictly true/false — never guess Yes or No for a legally-sensitive
    // question just because the field happens to be blank.
    var boolPaths = {};
    profileEditorEl.querySelectorAll('input[type=radio][data-path]').forEach(function (el) {
      boolPaths[el.getAttribute('data-path')] = true;
    });
    Object.keys(boolPaths).forEach(function (path) {
      var v = dottedGet(profileData, path);
      var want = v === true ? 'yes' : (v === false ? 'no' : 'unset');
      profileEditorEl.querySelectorAll('input[type=radio][data-path="' + path + '"]').forEach(function (el) {
        el.checked = (el.value === want);
      });
    });

    renderRepeatable(workHistoryListEl, workHistoryTemplate, profileData.work_history || [], 'Position');
    renderRepeatable(educationListEl, educationTemplate, profileData.education || [], 'Education');
  }

  function renderRepeatable(listEl, template, items, badgeLabel) {
    listEl.innerHTML = '';
    items.forEach(function (item) {
      addRepeatRow(listEl, template, item, badgeLabel);
    });
    renumberRows(listEl, badgeLabel);
  }

  function addRepeatRow(listEl, template, item, badgeLabel) {
    var node = template.content.cloneNode(true);
    var row = node.querySelector('.repeat-row');
    item = item || {};
    row.querySelectorAll('[data-field]').forEach(function (el) {
      var field = el.getAttribute('data-field');
      if (el.type === 'checkbox') {
        el.checked = !!item[field];
      } else {
        el.value = item[field] == null ? '' : item[field];
      }
    });
    var currentCb = row.querySelector('[data-field=current]');
    var endInput = row.querySelector('[data-field=end]');
    if (currentCb && endInput) {
      endInput.disabled = currentCb.checked;
      currentCb.addEventListener('change', function () {
        endInput.disabled = currentCb.checked;
        if (currentCb.checked) endInput.value = '';
      });
    }
    row.querySelector('[data-action=up]').addEventListener('click', function () {
      var prev = row.previousElementSibling;
      if (prev) listEl.insertBefore(row, prev);
      renumberRows(listEl, badgeLabel);
    });
    row.querySelector('[data-action=down]').addEventListener('click', function () {
      var next = row.nextElementSibling;
      if (next) listEl.insertBefore(next, row);
      renumberRows(listEl, badgeLabel);
    });
    row.querySelector('[data-action=remove]').addEventListener('click', function () {
      row.remove();
      renumberRows(listEl, badgeLabel);
    });
    listEl.appendChild(node);
  }

  function renumberRows(listEl, badgeLabel) {
    var rows = listEl.querySelectorAll('.repeat-row');
    rows.forEach(function (row, i) {
      row.querySelector('.badge').textContent = badgeLabel + ' ' + (i + 1) + (i === 0 ? ' — most recent' : '');
      row.querySelector('[data-action=up]').disabled = (i === 0);
      row.querySelector('[data-action=down]').disabled = (i === rows.length - 1);
    });
  }

  document.getElementById('addWorkHistoryBtn').addEventListener('click', function () {
    addRepeatRow(workHistoryListEl, workHistoryTemplate, {}, 'Position');
    renumberRows(workHistoryListEl, 'Position');
  });
  document.getElementById('addEducationBtn').addEventListener('click', function () {
    addRepeatRow(educationListEl, educationTemplate, {}, 'Education');
    renumberRows(educationListEl, 'Education');
  });

  function collectRepeatable(listEl, fields) {
    var out = [];
    listEl.querySelectorAll('.repeat-row').forEach(function (row) {
      var obj = {};
      fields.forEach(function (field) {
        var el = row.querySelector('[data-field="' + field + '"]');
        if (!el) return;
        obj[field] = el.type === 'checkbox' ? el.checked : el.value;
      });
      out.push(obj);
    });
    return out;
  }

  // -- saving -------------------------------------------------------------------

  function collectFormIntoProfileData() {
    profileEditorEl.querySelectorAll('input[data-path]:not([type=radio]), select[data-path], textarea[data-path]')
      .forEach(function (el) {
        dottedSet(profileData, el.getAttribute('data-path'), el.value);
      });

    var boolPaths = {};
    profileEditorEl.querySelectorAll('input[type=radio][data-path]').forEach(function (el) {
      boolPaths[el.getAttribute('data-path')] = true;
    });
    Object.keys(boolPaths).forEach(function (path) {
      var checked = profileEditorEl.querySelector('input[type=radio][data-path="' + path + '"]:checked');
      var val = checked ? checked.value : 'unset';
      dottedSet(profileData, path, val === 'yes' ? true : (val === 'no' ? false : null));
    });

    profileData.work_history = collectRepeatable(
      workHistoryListEl, ['title', 'company', 'location', 'start', 'end', 'current', 'description']);
    profileData.education = collectRepeatable(
      educationListEl, ['school', 'degree', 'field', 'start', 'end']);
  }

  saveProfileBtn.addEventListener('click', function () {
    if (!tokenEl.value) {
      setProfileStatus('err', 'Set a service token above first.');
      return;
    }
    collectFormIntoProfileData();
    saveProfileBtn.disabled = true;
    setProfileStatus('info', 'Saving…');
    apiPost('/profile', profileData).then(function () {
      setProfileStatus('ok', 'Profile saved.');
      saveProfileBtn.disabled = false;
    }, function (err) {
      setProfileStatus('err', 'Could not save profile: ' + err.message);
      saveProfileBtn.disabled = false;
    });
  });

  reloadProfileBtn.addEventListener('click', function () {
    setProfileStatus('info', 'Reloading…');
    refreshProfileArea();
  });

  // -- profile switcher / create -------------------------------------------------

  profileSelectEl.addEventListener('change', function () {
    var pid = profileSelectEl.value;
    if (!pid) return;
    setProfileStatus('info', 'Switching to "' + pid + '"…');
    apiPost('/profiles/' + encodeURIComponent(pid) + '/activate', {}).then(function () {
      return loadProfilesList();
    }).then(function () {
      return loadFullProfile();
    }).catch(function (err) {
      setProfileStatus('err', 'Could not switch profile: ' + err.message);
    });
  });

  newProfileBtn.addEventListener('click', function () {
    newProfileRowEl.style.display = 'flex';
    newProfileIdEl.value = '';
    newProfileIdEl.focus();
  });

  cancelNewProfileBtn.addEventListener('click', function () {
    newProfileRowEl.style.display = 'none';
  });

  createProfileBtn.addEventListener('click', function () {
    var pid = (newProfileIdEl.value || '').trim();
    if (!PROFILE_ID_RE.test(pid)) {
      setProfileStatus('err', 'Profile id must be lowercase letters, digits, "-" or "_" (max 64 chars).');
      return;
    }
    setProfileStatus('info', 'Creating "' + pid + '"…');
    apiPost('/profiles', { id: pid }).then(function () {
      // Immediately switch to the profile just created so the operator can
      // start filling it in without a second click.
      return apiPost('/profiles/' + encodeURIComponent(pid) + '/activate', {});
    }).then(function () {
      newProfileRowEl.style.display = 'none';
      return loadProfilesList();
    }).then(function () {
      return loadFullProfile();
    }).then(function () {
      setProfileStatus('ok', 'Created and switched to profile "' + pid + '".');
    }).catch(function (err) {
      setProfileStatus('err', 'Could not create profile: ' + err.message);
    });
  });

  load();
})();
