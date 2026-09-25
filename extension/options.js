/**
 * ApplyPilot Copilot — options page logic.
 *
 * Three independent pieces:
 *   1. Connection settings: { serviceUrl, token } in chrome.storage.local, plus a
 *      "Test connection" button that asks the background worker to call GET /health
 *      (never fetched from this page directly — background.js is the sole fetcher for
 *      the autofill path, see its file header).
 *   2. Résumé import (v3): multipart POST /profile/import-resume on the local service.
 *      This is the "Get started" onboarding action — the whole point of v3 is that the
 *      operator uploads a résumé instead of retyping their career. The service returns a
 *      DRAFT profile plus per-field provenance ("deterministic" | "llm") and never saves
 *      anything itself. This file merges the draft into the in-memory editor state,
 *      marks which fields came from the résumé, and requires an explicit Save — see
 *      mergeImportedDraft() for why that matters (a silently-saved wrong employer would
 *      propagate into real applications).
 *   3. The profile editor: talks to the local service's profile-management endpoints
 *      directly (GET/POST /profile/full, GET /profiles, POST /profiles,
 *      POST /profiles/{id}/activate). This page already holds the token in the clear
 *      to let the operator view/paste it, so there is no extra exposure in it also
 *      making these requests itself — host_permissions already scope fetch to
 *      127.0.0.1 for every extension page, not just the background worker.
 *
 * The editor never constructs a fresh JSON object from only the fields it renders.
 * It keeps the full profile object returned by GET /profile/full in `profileData` and
 * mutates that in place, so sections this UI doesn't know about (resume_facts,
 * availability, ...) round-trip untouched instead of being silently
 * deleted on save. personal.password is never in that object in the first place —
 * the service strips it before this page ever sees it — and this file never adds a
 * field for it, never renders it, and strips it defensively if a future response
 * ever includes it (see mergeImportedDraft).
 *
 * Canary fields (work_authorization.*) are legally sensitive and are NEVER populated
 * from a résumé import, even defensively if the service ever sent one back — see the
 * CANARY_PREFIXES guard in mergeImportedDraft(). They stay exactly what the operator
 * typed into the Work authorization section themselves.
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
      } else {
        updateCompleteness();
      }
      loadSmartFillSettings();
      loadLlmAvailability();
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
      applyHealthData(data);
    }, function (err) {
      setStatus('err', 'Could not reach the extension background worker: ' + err);
    });
  });

  // ===============================================================================
  // -- Smart fill: "reuse my past answers" / "draft open-ended answers" -----------
  // ===============================================================================
  //
  // Talks to the (new, may not exist yet on an older service) GET/POST /settings
  // endpoints, contract { answers_enabled, drafts_enabled, max_drafts }. Degrades
  // gracefully when they 404: the toggles still work as a LOCAL-ONLY preference
  // (chrome.storage.local, keys smartFillAnswersEnabled/smartFillDraftsEnabled —
  // also what popup.js reads to decide whether to show its "turn on drafts" hint),
  // the section is visibly disabled with an explanation, and nothing else on this
  // page is affected.
  //
  // "Drafts" also depends on an LLM actually being configured server-side — GET
  // /health is extended with `llm_available`/`llm_provider` for that. When the
  // operator has drafts on but no model is available, this says so plainly rather
  // than letting the toggle silently do nothing on every /resolve call.

  var smartFillCardEl = document.getElementById('smartFillCard');
  var smartFillAnswersEl = document.getElementById('smartFillAnswers');
  var smartFillDraftsEl = document.getElementById('smartFillDrafts');
  var smartFillMaxDraftsEl = document.getElementById('smartFillMaxDrafts');
  var smartFillLlmWarningEl = document.getElementById('smartFillLlmWarning');
  var smartFillCloudEl = document.getElementById('smartFillCloud');
  var smartFillModelLineEl = document.getElementById('smartFillModelLine');
  var smartFillSaveEl = document.getElementById('smartFillSave');
  var smartFillStatusEl = document.getElementById('smartFillStatus');

  // null = not known yet (no HEALTH response received). Only an explicit `false`
  // shows the warning — never guess "no model" before we've actually asked.
  var llmAvailable = null;

  function setSmartFillStatus(kind, text) {
    smartFillStatusEl.className = kind;
    smartFillStatusEl.textContent = text;
  }

  function updateLlmWarning() {
    smartFillLlmWarningEl.style.display = (smartFillDraftsEl.checked && llmAvailable === false) ? 'block' : 'none';
  }

  function applyHealthData(data) {
    llmAvailable = data && typeof data.llm_available === 'boolean' ? data.llm_available : null;
    updateLlmWarning();
    // Say plainly where AI text would go — the old copy said nothing ever
    // left the machine, which was only true with a local model.
    if (!data || typeof data.llm_available !== 'boolean') {
      smartFillModelLineEl.textContent = 'AI model: unknown (service not reachable).';
    } else if (!data.llm_available) {
      smartFillModelLineEl.textContent = 'AI model: none available — AI features are skipped.';
    } else if (data.llm_local) {
      smartFillModelLineEl.textContent = 'AI model: ' + (data.llm_label || 'local') + ' — runs on this computer.';
    } else {
      smartFillModelLineEl.textContent = 'AI model: ' + (data.llm_label || data.llm_provider) +
        ' — NOT on this computer. ' + (data.cloud_llm_allowed ? 'Allowed below.' : 'AI features are off until you allow it below.');
    }
  }

  function loadLlmAvailability() {
    chrome.runtime.sendMessage({ type: 'HEALTH' }).then(function (resp) {
      applyHealthData(resp && resp.ok ? resp.data : null);
    }, function () {
      applyHealthData(null);
    });
  }

  // The operator's toggle state is the source of truth for popup.js's local hint
  // regardless of whether the service has caught up with /settings yet — persisted
  // on every change, not just on Save, so it's never stale relative to what's on screen.
  function persistSmartFillLocally() {
    chrome.storage.local.set({
      smartFillAnswersEnabled: smartFillAnswersEl.checked,
      smartFillDraftsEnabled: smartFillDraftsEl.checked
    });
  }

  smartFillAnswersEl.addEventListener('change', persistSmartFillLocally);
  smartFillDraftsEl.addEventListener('change', function () {
    updateLlmWarning();
    persistSmartFillLocally();
  });

  function loadSmartFillSettings() {
    return chrome.storage.local.get(['smartFillAnswersEnabled', 'smartFillDraftsEnabled']).then(function (local) {
      // Reuse my past answers: on by default. Draft answers: off by default.
      smartFillAnswersEl.checked = local.smartFillAnswersEnabled !== false;
      smartFillDraftsEl.checked = local.smartFillDraftsEnabled === true;
      smartFillCardEl.classList.remove('section-disabled');
      updateLlmWarning();

      if (!tokenEl.value) {
        setSmartFillStatus('info', 'Set a service token above to sync these with your ApplyPilot service.');
        return;
      }

      return apiGet('/settings').then(function (data) {
        data = data || {};
        if (typeof data.answers_enabled === 'boolean') smartFillAnswersEl.checked = data.answers_enabled;
        if (typeof data.drafts_enabled === 'boolean') smartFillDraftsEl.checked = data.drafts_enabled;
        if (data.max_drafts != null) smartFillMaxDraftsEl.value = data.max_drafts;
        if (typeof data.cloud_llm_allowed === 'boolean') smartFillCloudEl.checked = data.cloud_llm_allowed;
        smartFillStatusEl.className = '';
        smartFillStatusEl.textContent = '';
        persistSmartFillLocally();
        updateLlmWarning();
      }, function (err) {
        if (err && err.status === 404) {
          // Graceful degrade: the Python side hasn't shipped /settings yet on this
          // install. Disable the section rather than pretend a Save does anything
          // server-side, but leave the toggles' last-known state visible.
          smartFillCardEl.classList.add('section-disabled');
          setSmartFillStatus('info', "This ApplyPilot service doesn't support smart-fill settings yet — update it, or your choices here stay local to this browser only.");
        } else {
          setSmartFillStatus('err', 'Could not load smart-fill settings: ' + err.message);
        }
      });
    });
  }

  smartFillSaveEl.addEventListener('click', function () {
    if (!tokenEl.value) {
      setSmartFillStatus('err', 'Set a service token above first.');
      return;
    }
    var maxDrafts = parseInt(smartFillMaxDraftsEl.value, 10);
    if (!(maxDrafts >= 0)) maxDrafts = 5;
    smartFillMaxDraftsEl.value = maxDrafts;
    persistSmartFillLocally();

    smartFillSaveEl.disabled = true;
    setSmartFillStatus('info', 'Saving…');
    apiPost('/settings', {
      answers_enabled: smartFillAnswersEl.checked,
      drafts_enabled: smartFillDraftsEl.checked,
      max_drafts: maxDrafts,
      cloud_llm_allowed: smartFillCloudEl.checked
    }).then(function () {
      loadLlmAvailability();
      smartFillCardEl.classList.remove('section-disabled');
      setSmartFillStatus('ok', 'Saved.');
      smartFillSaveEl.disabled = false;
    }, function (err) {
      smartFillSaveEl.disabled = false;
      if (err && err.status === 404) {
        setSmartFillStatus('warn', "Saved locally in this browser only — this ApplyPilot service doesn't support smart-fill settings yet.");
      } else {
        setSmartFillStatus('err', 'Could not save smart-fill settings: ' + err.message);
      }
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
        // `detail` is usually a plain string (FastAPI's ordinary HTTPException shape), but some
        // endpoints (e.g. /profile/import-resume's 409 identity_mismatch) send a structured
        // object instead — prefer its own .message for display, and keep the raw object on
        // err.detail so a caller that needs the structured fields (code/resume_name/
        // profile_name) doesn't have to re-parse anything.
        var detail = data && data.detail;
        var msg = typeof detail === 'string' ? detail
          : (detail && detail.message) ? detail.message
          : ('HTTP ' + resp.status);
        var err = new Error(msg);
        err.status = resp.status;
        err.detail = detail;
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

  function truthy(v) {
    return v != null && String(v).trim() !== '';
  }

  // -- EEO selects: default to "Decline to self-identify", preserve unrecognised values --------

  var EEO_DEFAULT = 'Decline to self-identify';

  // Appends a synthetic <option> (visibly marked as unrecognised) when `value` doesn't match any
  // of a <select>'s built-in options -- e.g. a legacy free-text veteran_status string saved
  // before this dropdown existed. Preserves and shows it as selected instead of silently
  // overwriting it with the default or dropping it on the next save.
  function ensurePreservedOption(selectEl, value) {
    var has = Array.prototype.some.call(selectEl.options, function (o) { return o.value === value; });
    if (has) return;
    var opt = document.createElement('option');
    opt.value = value;
    opt.textContent = value + ' (unrecognized value — kept as-is)';
    selectEl.appendChild(opt);
  }

  // -- loading -------------------------------------------------------------------

  function refreshProfileArea() {
    if (!tokenEl.value) { updateCompleteness(); return; }
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
      resumeMarks = {};
      renderEditor();
      applyDefaultCollapseState();
      updateOnboardCopy();
      profileEditorEl.classList.remove('disabled');
      setProfileStatus('info', 'Profile loaded.');
    }, function (err) {
      profileEditorEl.classList.remove('disabled');
      setProfileStatus('err', 'Could not load profile: ' + err.message);
      updateCompleteness();
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

    // EEO selects: default to "Decline to self-identify" when the stored value is absent or
    // empty. An unrecognised stored value (e.g. a legacy veteran_status string from before this
    // dropdown existed) is preserved and shown as an extra selected option instead -- see
    // ensurePreservedOption(). Screening selects are NOT touched here: their "(unset)" option
    // already has value="" and the generic binding above already selects it correctly.
    profileEditorEl.querySelectorAll('select[data-path^="eeo_voluntary."]').forEach(function (el) {
      var stored = dottedGet(profileData, el.getAttribute('data-path'));
      var want = truthy(stored) ? String(stored) : EEO_DEFAULT;
      ensurePreservedOption(el, want);
      el.value = want;
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
      var wrap = document.getElementById(path === 'work_authorization.legally_authorized_to_work' ? 'waAuthorizedField' : 'waSponsorshipField');
      if (wrap) wrap.classList.toggle('unset', want === 'unset');
    });

    renderRepeatable(workHistoryListEl, workHistoryTemplate, profileData.work_history || [], 'Position');
    renderRepeatable(educationListEl, educationTemplate, profileData.education || [], 'Education');
    renderSkills();

    applyProvenanceMarks();
    updateSectionMeta();
    updateCompleteness();
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
      updateCompleteness();
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
    updateCompleteness();
  });
  document.getElementById('addEducationBtn').addEventListener('click', function () {
    addRepeatRow(educationListEl, educationTemplate, {}, 'Education');
    renumberRows(educationListEl, 'Education');
    updateCompleteness();
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

  // -- skills editor (profile.skills_boundary: {category: [skill, ...]}) --------
  //
  // Category names are free-form (a résumé import writes things like
  // "languages", "data_platform", or "skills") so, unlike work_history/education,
  // this isn't a fixed template repeated per item — each category gets its own
  // block with an editable name, a list of removable skill chips, and an
  // add-skill input. Rendered from profileData.skills_boundary on load/switch;
  // collectSkillsIntoProfileData() rebuilds the whole object fresh from the DOM
  // on every input/change and on Save, the same "DOM is the source of truth at
  // save time" pattern collectRepeatable() uses above.

  var skillsCategoryListEl = document.getElementById('skillsCategoryList');
  var addSkillCategoryBtn = document.getElementById('addSkillCategoryBtn');

  function addSkillChip(chipListEl, skill) {
    skill = String(skill == null ? '' : skill).trim();
    if (!skill) return;
    var exists = Array.prototype.some.call(chipListEl.querySelectorAll('.skill-chip'), function (c) {
      return c.getAttribute('data-skill') === skill;
    });
    if (exists) return;

    var chip = document.createElement('span');
    chip.className = 'skill-chip';
    chip.setAttribute('data-skill', skill);

    var label = document.createElement('span');
    label.textContent = skill;

    var rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'skill-chip-remove';
    rm.setAttribute('aria-label', 'Remove ' + skill);
    rm.textContent = '×';
    rm.addEventListener('click', function () {
      chip.remove();
      updateCompleteness();
    });

    chip.appendChild(label);
    chip.appendChild(rm);
    chipListEl.appendChild(chip);
  }

  function addSkillCategoryBlock(name, skills) {
    var block = document.createElement('div');
    block.className = 'skills-category';

    var head = document.createElement('div');
    head.className = 'skills-category-head';

    var nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'skills-category-name';
    nameInput.value = name || '';
    nameInput.placeholder = 'category name, e.g. languages';
    nameInput.autocomplete = 'off';
    nameInput.spellcheck = false;
    nameInput.setAttribute('aria-label', 'Skill category name');

    var removeCatBtn = document.createElement('button');
    removeCatBtn.type = 'button';
    removeCatBtn.className = 'danger small';
    removeCatBtn.textContent = 'Remove category';
    removeCatBtn.addEventListener('click', function () {
      block.remove();
      updateCompleteness();
    });

    head.appendChild(nameInput);
    head.appendChild(removeCatBtn);

    var chipList = document.createElement('div');
    chipList.className = 'skills-chip-list';
    (skills || []).forEach(function (s) { addSkillChip(chipList, s); });

    var addRow = document.createElement('div');
    addRow.className = 'row';

    var newSkillInput = document.createElement('input');
    newSkillInput.type = 'text';
    newSkillInput.placeholder = 'add a skill and press Enter';
    newSkillInput.autocomplete = 'off';
    newSkillInput.spellcheck = false;

    var addSkillBtn = document.createElement('button');
    addSkillBtn.type = 'button';
    addSkillBtn.className = 'secondary small';
    addSkillBtn.textContent = 'Add';

    function commitNewSkill() {
      addSkillChip(chipList, newSkillInput.value);
      newSkillInput.value = '';
      newSkillInput.focus();
      updateCompleteness();
    }
    addSkillBtn.addEventListener('click', commitNewSkill);
    newSkillInput.addEventListener('keydown', function (evt) {
      if (evt.key === 'Enter') { evt.preventDefault(); commitNewSkill(); }
    });

    addRow.appendChild(newSkillInput);
    addRow.appendChild(addSkillBtn);

    block.appendChild(head);
    block.appendChild(chipList);
    block.appendChild(addRow);
    skillsCategoryListEl.appendChild(block);
  }

  function renderSkills() {
    skillsCategoryListEl.innerHTML = '';
    var boundary = profileData.skills_boundary;
    if (boundary && typeof boundary === 'object') {
      Object.keys(boundary).forEach(function (cat) {
        var v = boundary[cat];
        addSkillCategoryBlock(cat, Array.isArray(v) ? v : []);
      });
    }
  }

  addSkillCategoryBtn.addEventListener('click', function () {
    addSkillCategoryBlock('', []);
    var names = skillsCategoryListEl.querySelectorAll('.skills-category-name');
    var last = names[names.length - 1];
    if (last) last.focus();
    updateCompleteness();
  });

  function collectSkillsIntoProfileData() {
    var out = {};
    skillsCategoryListEl.querySelectorAll('.skills-category').forEach(function (block) {
      var name = (block.querySelector('.skills-category-name').value || '').trim();
      if (!name) return; // an unnamed category is dropped rather than saved as ""
      var skills = Array.prototype.map.call(
        block.querySelectorAll('.skill-chip'),
        function (c) { return c.getAttribute('data-skill'); }
      );
      if (out[name]) {
        // Two categories renamed to the same name: merge rather than let the
        // second silently clobber the first.
        skills.forEach(function (s) { if (out[name].indexOf(s) === -1) out[name].push(s); });
      } else {
        out[name] = skills;
      }
    });
    profileData.skills_boundary = out;
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
    collectSkillsIntoProfileData();
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
      // Once saved, the operator has explicitly accepted whatever came from
      // the résumé — the "please double-check this" marking has done its
      // job, so clear it rather than nagging forever.
      resumeMarks = {};
      applyProvenanceMarks();
      applyDefaultCollapseState();
      updateOnboardCopy();
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

  // ===============================================================================
  // -- Résumé import (v3 onboarding) ---------------------------------------------
  // ===============================================================================

  var resumeFileEl = document.getElementById('resumeFile');
  var fileBtnLabel = document.getElementById('fileBtnLabel');
  var resumeFileLabelText = document.getElementById('resumeFileLabelText');
  var importStatusEl = document.getElementById('importStatus');
  var onboardTitleEl = document.getElementById('onboardTitle');
  var onboardSubEl = document.getElementById('onboardSub');

  var ALLOWED_RESUME_EXT = /\.(pdf|docx|txt)$/i;
  var MAX_RESUME_BYTES = 8 * 1024 * 1024;

  // Canary fields have real legal/immigration consequences for the operator.
  // A résumé cannot reliably state them and this project refuses to guess
  // them anywhere else, so the import merge defensively refuses to touch
  // any path under these prefixes even if a future response ever included
  // one — see the file header.
  var CANARY_PREFIXES = ['work_authorization'];

  function setImportStatus(kind, html) {
    importStatusEl.className = kind;
    importStatusEl.innerHTML = html;
  }

  function updateOnboardCopy() {
    var hasName = truthy(dottedGet(profileData, 'personal.full_name'));
    var hasEmail = truthy(dottedGet(profileData, 'personal.email'));
    if (hasName || hasEmail) {
      onboardTitleEl.textContent = 'Update from a résumé';
      onboardSubEl.textContent = 'Upload a newer résumé to refresh the fields below — nothing is overwritten until you review and save.';
      resumeFileLabelText.textContent = 'Choose file';
    } else {
      onboardTitleEl.textContent = 'Get started';
      onboardSubEl.textContent = "Upload your résumé and we'll fill this in.";
      resumeFileLabelText.textContent = 'Choose file';
    }
  }

  resumeFileEl.addEventListener('change', function () {
    var file = resumeFileEl.files && resumeFileEl.files[0];
    if (!file) return; // dialog cancelled — nothing to do

    if (!ALLOWED_RESUME_EXT.test(file.name)) {
      setImportStatus('err', "That file type isn't supported — upload a .pdf, .docx, or .txt résumé.");
      resumeFileEl.value = '';
      return;
    }
    if (file.size === 0) {
      setImportStatus('err', 'That file looks empty — pick your résumé file again.');
      resumeFileEl.value = '';
      return;
    }
    if (file.size > MAX_RESUME_BYTES) {
      setImportStatus('err', 'That résumé file is too large (max 8 MB).');
      resumeFileEl.value = '';
      return;
    }
    if (!tokenEl.value) {
      setImportStatus('err', 'Set a service token above first, then try again.');
      resumeFileEl.value = '';
      return;
    }

    importResume(file);
  });

  // `allowIdentityChange` is NEVER set automatically — the only caller that ever passes it is
  // the "Replace <profile> with <résumé>" button built by showIdentityMismatchWarning(), i.e.
  // an explicit operator click after seeing the warning below. A plain upload always calls this
  // with just `file`.
  function importResume(file, allowIdentityChange) {
    fileBtnLabel.classList.add('busy');
    setImportStatus('info', allowIdentityChange ? 'Replacing the active profile’s identity and re-reading your résumé…' : 'Reading your résumé…');

    var formData = new FormData();
    formData.append('file', file, file.name);
    if (allowIdentityChange) formData.append('allow_identity_change', 'true');

    fetch(apiUrl('/profile/import-resume'), {
      method: 'POST',
      headers: { 'X-ApplyPilot-Token': (tokenEl.value || '').trim() }, // no Content-Type: browser sets the multipart boundary
      body: formData
    }).then(function (resp) {
      return apiResponse(resp);
    }).then(function (data) {
      handleImportSuccess(data || {});
    }, function (err) {
      fileBtnLabel.classList.remove('busy');
      resumeFileEl.value = '';
      if (err instanceof TypeError) {
        // fetch() throws a bare TypeError on network failure (service down,
        // wrong port, CORS) — no HTTP response to read a message from.
        setImportStatus('err', 'Could not reach the local ApplyPilot service. Is `applypilot serve-extension` running?');
        return;
      }
      if (err && err.status === 401) {
        setImportStatus('err', 'The service rejected the token — check it in the connection settings above.');
        return;
      }
      if (err && err.status === 409 && err.detail && err.detail.code === 'identity_mismatch') {
        // The service wrote NOTHING in this case (see server-side identity_mismatch check) --
        // it's purely the operator's call whether the résumé is meant to replace who this
        // profile is for. Shown as a distinct warning, never folded into the generic error path.
        showIdentityMismatchWarning(err.detail, file);
        return;
      }
      // Every other case (unsupported type, oversized file, corrupt/encrypted
      // PDF, no extractable text, ...) is raised by the service as a safe,
      // human-readable detail message — see resume_import.py. Surface it
      // as-is rather than re-wording it.
      setImportStatus('err', 'Could not import that résumé: ' + ((err && err.message) || 'unknown error') + '.');
    });
  }

  // detail: { code: "identity_mismatch", resume_name, profile_name, message }. Renders a
  // clearly-styled warning (not the generic error box) with exactly two operator choices:
  // "Cancel" (does nothing further — the failed attempt already wrote nothing) or "Replace ...",
  // which re-sends the SAME File object with allow_identity_change=true. Never retried
  // automatically — only this button's click ever takes that path.
  function showIdentityMismatchWarning(detail, file) {
    detail = detail || {};
    var resumeName = detail.resume_name || 'the résumé';
    var profileName = detail.profile_name || 'the active profile';
    var message = detail.message ||
      ('This résumé looks like it belongs to "' + resumeName + '", not "' + profileName + '".');

    importStatusEl.className = 'warn';
    importStatusEl.innerHTML = '';

    var msgEl = document.createElement('div');
    msgEl.textContent = message;
    importStatusEl.appendChild(msgEl);

    var actions = document.createElement('div');
    actions.style.marginTop = '10px';
    actions.style.display = 'flex';
    actions.style.flexWrap = 'wrap';
    actions.style.gap = '8px';

    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'secondary small';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', function () {
      setImportStatus('', '');
    });

    var replaceBtn = document.createElement('button');
    replaceBtn.type = 'button';
    replaceBtn.className = 'small';
    replaceBtn.textContent = 'Replace ' + profileName + ' with ' + resumeName;
    replaceBtn.addEventListener('click', function () {
      importResume(file, true);
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(replaceBtn);
    importStatusEl.appendChild(actions);
  }

  function handleImportSuccess(data) {
    fileBtnLabel.classList.remove('busy');
    resumeFileEl.value = '';

    var draft = data.profile || data.draft_profile || null;
    var provenance = data.provenance || {};
    var warnings = Array.isArray(data.warnings) ? data.warnings : [];

    if (!draft || typeof draft !== 'object') {
      setImportStatus('err', 'The service did not return a profile to review — nothing was imported.');
      return;
    }

    var marked = mergeImportedDraft(draft, provenance);
    renderEditor();
    forceOpenMarkedSections();

    var html = '';
    if (marked.length) {
      html += '<div>Imported ' + marked.length + ' field' + (marked.length === 1 ? '' : 's') +
        ' from your résumé — <strong>highlighted below</strong>. Review them, then click <strong>Save profile</strong>. Nothing is saved yet.</div>';
    } else {
      html += '<div>Read your résumé, but nothing new was found to fill in. Nothing was changed.</div>';
    }
    var resumeMeta = data.resume || (data.saved_filename ? { filename: data.saved_filename } : null);
    if (resumeMeta && resumeMeta.filename) {
      var sizeStr = resumeMeta.size ? ' (' + Math.max(1, Math.round(resumeMeta.size / 1024)) + ' KB)' : '';
      html += '<div class="hint" style="margin-top:6px;">Saved as <code>' + escapeHtml(resumeMeta.filename) + '</code>' + sizeStr +
        ' — this is also what gets attached to application forms for you.</div>';
    }
    var kind = 'ok';
    if (warnings.length) {
      kind = 'warn';
      html += '<ul class="warn-list">' + warnings.map(function (w) { return '<li>' + escapeHtml(w) + '</li>'; }).join('') + '</ul>';
    }
    setImportStatus(kind, html);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function isCanaryPath(path) {
    return CANARY_PREFIXES.some(function (prefix) {
      return path === prefix || path.indexOf(prefix + '.') === 0;
    });
  }

  // Merges a résumé-derived draft profile into the live editor state.
  //
  // `provenance` is a map of dotted-path (or the sentinels "work_history" /
  // "education") -> "deterministic" | "llm", exactly as returned by
  // resume_import.py. Only paths actually listed in `provenance` are copied
  // — this is what lets the badge/highlight be authoritative rather than a
  // guess, and it means a response that only ran the deterministic pass
  // (no LLM configured) naturally only touches contact fields.
  //
  // Returns the list of {path, source} entries actually applied, which the
  // caller uses both for the "imported N fields" message and for
  // forceOpenMarkedSections().
  function mergeImportedDraft(draft, provenance) {
    var applied = [];
    Object.keys(provenance || {}).forEach(function (path) {
      var source = provenance[path];
      if (isCanaryPath(path)) return; // defense in depth — see CANARY_PREFIXES
      if (path === 'personal.password') return; // defense in depth — never rendered, never merged

      if (path === 'work_history' || path === 'education') {
        var arr = draft[path];
        if (Array.isArray(arr) && arr.length) {
          profileData[path] = arr;
          applied.push({ path: path, source: source || 'deterministic' });
        }
        return;
      }

      var value = dottedGet(draft, path);
      if (value === undefined || value === null || value === '') return;
      dottedSet(profileData, path, value);
      applied.push({ path: path, source: source || 'deterministic' });
    });

    resumeMarks = {};
    applied.forEach(function (a) { resumeMarks[a.path] = a.source; });
    return applied;
  }

  // -- provenance marking ("from your résumé — check this") ---------------------

  // path -> "deterministic" | "llm", populated by mergeImportedDraft(), cleared
  // on profile (re)load and on successful Save.
  var resumeMarks = {};

  function applyProvenanceMarks() {
    profileEditorEl.querySelectorAll('.from-resume').forEach(function (el) {
      el.classList.remove('from-resume', 'from-resume-llm');
    });

    Object.keys(resumeMarks).forEach(function (path) {
      var source = resumeMarks[path];
      if (path === 'work_history' || path === 'education') {
        var listEl = path === 'work_history' ? workHistoryListEl : educationListEl;
        listEl.querySelectorAll('.repeat-row').forEach(function (row) {
          row.classList.add('from-resume');
          if (source === 'llm') row.classList.add('from-resume-llm');
        });
        return;
      }
      profileEditorEl.querySelectorAll('[data-path]').forEach(function (el) {
        var p = el.getAttribute('data-path');
        if (p !== path && p.indexOf(path + '.') !== 0) return;
        if (el.type === 'radio') return; // canary fields never reach here, but stay defensive
        var wrap = el.parentElement;
        if (!wrap) return;
        wrap.classList.add('from-resume');
        if (source === 'llm') wrap.classList.add('from-resume-llm');
      });
    });
  }

  // Once the operator edits a specific résumé-derived field by hand, treat
  // it as reviewed and drop just that one mark — the rest of the import
  // stays flagged until they've looked at it too.
  profileEditorEl.addEventListener('input', function (evt) {
    var path = evt.target.getAttribute && evt.target.getAttribute('data-path');
    if (path && resumeMarks[path]) {
      delete resumeMarks[path];
      applyProvenanceMarks();
    }
    updateCompleteness();
  });
  profileEditorEl.addEventListener('change', function (evt) {
    var path = evt.target.getAttribute && evt.target.getAttribute('data-path');
    if (path && resumeMarks[path]) {
      delete resumeMarks[path];
      applyProvenanceMarks();
    }
    if (evt.target.type === 'radio') updateBoolFieldClasses();
    updateSectionMeta();
    updateCompleteness();
  });

  // Live-refreshes the amber "not set — required" ring on the work-auth
  // radio groups as the operator clicks Yes/No — renderEditor() sets the
  // initial state from profileData, this keeps it in sync afterwards.
  function updateBoolFieldClasses() {
    [
      { path: 'work_authorization.legally_authorized_to_work', wrapId: 'waAuthorizedField' },
      { path: 'work_authorization.require_sponsorship', wrapId: 'waSponsorshipField' }
    ].forEach(function (g) {
      var checked = profileEditorEl.querySelector('input[type=radio][data-path="' + g.path + '"]:checked');
      var wrap = document.getElementById(g.wrapId);
      if (wrap) wrap.classList.toggle('unset', !checked || checked.value === 'unset');
    });
  }

  function forceOpenMarkedSections() {
    var sectionForPath = {
      personal: 'sectionPersonal',
      compensation: 'sectionCompensation',
      experience: 'sectionExperience',
      work_history: 'sectionWorkHistory',
      education: 'sectionEducation'
    };
    Object.keys(resumeMarks).forEach(function (path) {
      var top = path.split('.')[0];
      var sectionId = sectionForPath[top];
      if (!sectionId) return;
      var section = document.getElementById(sectionId);
      if (section) section.open = true;
    });
  }

  // -- collapsible sections: collapse once populated, expand to edit ------------

  var SECTION_POPULATED = {
    sectionPersonal: function (p) {
      return truthy(dottedGet(p, 'personal.full_name')) && truthy(dottedGet(p, 'personal.email'));
    },
    sectionWorkAuth: function (p) {
      var a = dottedGet(p, 'work_authorization.legally_authorized_to_work');
      var s = dottedGet(p, 'work_authorization.require_sponsorship');
      return (a === true || a === false) && (s === true || s === false);
    },
    sectionCompensation: function (p) {
      return truthy(dottedGet(p, 'compensation.salary_expectation')) ||
        (truthy(dottedGet(p, 'compensation.salary_range_min')) && truthy(dottedGet(p, 'compensation.salary_range_max')));
    },
    sectionExperience: function (p) {
      return truthy(dottedGet(p, 'experience.current_job_title')) && truthy(dottedGet(p, 'experience.years_of_experience_total'));
    },
    sectionWorkHistory: function (p) { return (p.work_history || []).length > 0; },
    sectionEducation: function (p) { return (p.education || []).length > 0; }
  };

  // Only called right after a fresh load / profile switch — never mid-edit,
  // or a section would snap shut under the operator's cursor while typing.
  function applyDefaultCollapseState() {
    Object.keys(SECTION_POPULATED).forEach(function (id) {
      var section = document.getElementById(id);
      if (!section) return;
      section.open = !SECTION_POPULATED[id](profileData);
    });
  }

  function updateSectionMeta() {
    Object.keys(SECTION_POPULATED).forEach(function (id) {
      var metaId = { sectionPersonal: 'personalMeta', sectionWorkAuth: 'workAuthMeta',
        sectionCompensation: 'compMeta', sectionExperience: 'expMeta',
        sectionWorkHistory: 'workHistoryMeta', sectionEducation: 'educationMeta' }[id];
      var meta = document.getElementById(metaId);
      if (!meta) return;
      var populated = SECTION_POPULATED[id](profileData);
      if (id === 'sectionWorkAuth' && !populated) {
        meta.textContent = 'Not set';
        meta.className = 'section-meta required';
      } else if (populated) {
        meta.textContent = 'Complete';
        meta.className = 'section-meta ok';
      } else {
        meta.textContent = 'Optional';
        meta.className = 'section-meta';
        if (id === 'sectionPersonal' || id === 'sectionWorkHistory' || id === 'sectionEducation' || id === 'sectionExperience') {
          meta.textContent = 'Incomplete';
        }
      }
    });
  }

  // -- completeness meter ---------------------------------------------------------

  var meterTextEl = document.getElementById('meterText');
  var meterFillEl = document.getElementById('meterFill');
  var meterMissingEl = document.getElementById('meterMissing');

  // The essentials that determine how much of a job-application form the
  // extension can actually fill. Picked to cover: identity, location,
  // legally-required work-authorization answers, and at least one work
  // history / education entry (many forms require at least one row of each).
  var ESSENTIALS = [
    { label: 'Full legal name', section: 'sectionPersonal', el: 'p_full_name', check: function (p) { return truthy(dottedGet(p, 'personal.full_name')); } },
    { label: 'Email', section: 'sectionPersonal', el: 'p_email', check: function (p) { return truthy(dottedGet(p, 'personal.email')); } },
    { label: 'Phone', section: 'sectionPersonal', el: 'p_phone', check: function (p) { return truthy(dottedGet(p, 'personal.phone')); } },
    { label: 'City', section: 'sectionPersonal', el: 'p_city', check: function (p) { return truthy(dottedGet(p, 'personal.city')); } },
    { label: 'State / Province', section: 'sectionPersonal', el: 'p_state', check: function (p) { return truthy(dottedGet(p, 'personal.province_state')); } },
    { label: 'Country', section: 'sectionPersonal', el: 'p_country', check: function (p) { return truthy(dottedGet(p, 'personal.country')); } },
    { label: 'Work authorization', section: 'sectionWorkAuth', el: null, check: function (p) { var v = dottedGet(p, 'work_authorization.legally_authorized_to_work'); return v === true || v === false; } },
    { label: 'Visa sponsorship answer', section: 'sectionWorkAuth', el: null, check: function (p) { var v = dottedGet(p, 'work_authorization.require_sponsorship'); return v === true || v === false; } },
    { label: 'Current job title', section: 'sectionExperience', el: 'e_title', check: function (p) { return truthy(dottedGet(p, 'experience.current_job_title')); } },
    { label: 'Years of experience', section: 'sectionExperience', el: 'e_years', check: function (p) { return truthy(dottedGet(p, 'experience.years_of_experience_total')); } },
    { label: 'Work history', section: 'sectionWorkHistory', el: null, check: function (p) { return (p.work_history || []).length > 0; } },
    { label: 'Education', section: 'sectionEducation', el: null, check: function (p) { return (p.education || []).length > 0; } }
  ];

  function updateCompleteness() {
    if (!tokenEl.value) {
      meterTextEl.textContent = 'Set a service token above to load your profile.';
      meterFillEl.style.width = '0%';
      meterFillEl.classList.remove('complete');
      meterMissingEl.innerHTML = '';
      return;
    }
    // Keep profileData in sync with whatever's currently typed in the form so
    // the meter reflects live edits, not just the last save.
    collectFormIntoProfileData();

    var missing = ESSENTIALS.filter(function (e) { return !e.check(profileData); });
    var done = ESSENTIALS.length - missing.length;
    meterTextEl.textContent = done + ' of ' + ESSENTIALS.length + ' essentials' +
      (missing.length ? ' · missing: ' + missing.map(function (m) { return m.label.toLowerCase(); }).join(', ') : '');
    var pct = Math.round((done / ESSENTIALS.length) * 100);
    meterFillEl.style.width = pct + '%';
    meterFillEl.classList.toggle('complete', missing.length === 0);

    meterMissingEl.innerHTML = '';
    if (!missing.length) {
      var done_ = document.createElement('div');
      done_.className = 'meter-complete-msg';
      done_.textContent = 'All essentials set — ready to fill forms.';
      meterMissingEl.appendChild(done_);
      return;
    }
    missing.forEach(function (m) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.textContent = m.label;
      chip.addEventListener('click', function () { jumpToEssential(m); });
      meterMissingEl.appendChild(chip);
    });
  }

  function jumpToEssential(essential) {
    var section = document.getElementById(essential.section);
    if (section) section.open = true;
    var target = essential.el ? document.getElementById(essential.el) : section;
    if (target && target.scrollIntoView) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    if (essential.el) {
      var input = document.getElementById(essential.el);
      if (input) input.focus({ preventScroll: true });
    }
  }

  load();
})();
