#!/usr/bin/env node
/**
 * ApplyPilot Copilot — offline scanning self-test.
 *
 * Loads test-page.html into a jsdom document, evaluates scanner.js inside that document's
 * own window (so it behaves exactly like a real content script sharing the page's realm),
 * runs ApplyPilotScanner.scanFields() over it, prints every extracted FieldDescriptor, and
 * asserts the tricky cases from the design spec (label-for, wrapping label, aria-label,
 * placeholder-only, aria-labelledby, nearby-text-only, hidden-field exclusion, aria-hidden
 * exclusion, select options, radio group, textarea, and a bad-looking auto-generated id).
 *
 * This script is a developer/verification tool only. It is NEVER loaded by the extension
 * itself, and the shipped extension has zero npm dependencies. To run it you need `jsdom`
 * available on NODE_PATH — it is deliberately NOT vendored into extension/ or committed to
 * this repo, e.g.:
 *
 *   mkdir -p /tmp/apc-jsdom-test && cd /tmp/apc-jsdom-test && npm init -y && npm install jsdom
 *   NODE_PATH=/tmp/apc-jsdom-test/node_modules node <path-to>/extension/selftest.js
 *
 * If you'd rather not install anything, open test-page.html directly in Chrome, open
 * DevTools console, paste the contents of scanner.js, then run:
 *
 *   const { fields } = ApplyPilotScanner.scanAll(document);
 *   console.table(fields);
 *
 * That exercises the identical scanning code the real content script runs, in the identical
 * way the extension runs it (see README.md "Manual verification in a real browser").
 */

const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.error('jsdom is not installed / not on NODE_PATH. See the comment at the top of this');
  console.error('file for how to install it (dev-only — not an extension dependency) or how to');
  console.error('run the equivalent check in a real browser console instead.');
  process.exit(1);
}

const html = fs.readFileSync(path.join(__dirname, 'test-page.html'), 'utf8');
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  resources: 'usable',
  pretendToBeVisual: true,
  url: 'https://example.test/apply'
});

// jsdom has no real layout engine, so getBoundingClientRect() always reports a 0x0 box, which
// would make our (correct, browser-accurate) visibility check reject every field. jsdom DOES
// track computed `display`/`visibility`/`aria-hidden` correctly, so we only need to patch the
// size, not the display/visibility logic itself.
dom.window.HTMLElement.prototype.getBoundingClientRect = function () {
  return { width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10 };
};

const scannerSrc = fs.readFileSync(path.join(__dirname, 'scanner.js'), 'utf8');
dom.window.eval(scannerSrc);
const Scanner = dom.window.ApplyPilotScanner;
if (!Scanner) {
  console.error('scanner.js did not attach ApplyPilotScanner to the window — aborting.');
  process.exit(1);
}

const { fields, registry } = Scanner.scanFields(dom.window.document);

console.log(`Scanned ${fields.length} field descriptor(s):\n`);
for (const f of fields) {
  console.log(JSON.stringify(f));
}

// --- lightweight assertions -------------------------------------------------
const byName = {};
for (const f of fields) if (f.name) byName[f.name] = f;

const checks = [];
function expect(name, cond) { checks.push({ name, pass: !!cond }); }
// Promises from async test blocks (the Workday dropdown/prompt/résumé widgets all need to wait
// for a popup or a MutationObserver-driven confirmation) -- collected here and awaited via
// Promise.all() right before the final tally, so `checks` is guaranteed complete before it's
// printed. Every synchronous test block above and below still just pushes into `checks` directly.
const pending = [];

expect('label[for] resolves ("Full Name")', byName.full_name && byName.full_name.label === 'Full Name');
expect('wrapping <label> resolves (contains "Email")', byName.email && /email/i.test(byName.email.label));
expect('aria-label resolves exactly ("Phone Number")', byName.phone && byName.phone.label === 'Phone Number');
expect('placeholder-only resolves (contains "LinkedIn")', byName.linkedin && /linkedin/i.test(byName.linkedin.label));
expect('aria-labelledby resolves ("Portfolio URL")', byName.portfolio && byName.portfolio.label === 'Portfolio URL');
expect('nearby preceding text resolves (contains "hear about")', byName.referral && /hear about/i.test(byName.referral.label));
expect('react-style controlled input is captured', !!byName.react_input);
expect('select captured with 3+ visible option texts', fields.some(f => f.tag === 'select' && f.options.length >= 3 && f.options.includes('Senior (6+ years)')));
expect('radio group captured as ONE field with 2 options', fields.filter(f => f.type === 'radio').length === 1 && fields.find(f => f.type === 'radio').options.length === 2);
expect('radio group label resolves via <legend>', /authorized to work/i.test(fields.find(f => f.type === 'radio').label));
expect('textarea captured', fields.some(f => f.tag === 'textarea' && f.name === 'cover_note'));
expect('hidden input excluded', !byName.hidden_token);
expect('submit button excluded', !fields.some(f => f.type === 'submit'));
expect('aria-hidden field excluded', !byName.aria_hidden_field);
expect('display:none field excluded', !byName.display_none_field);
expect('auto-generated-id field still captured, with a non-#id selector', (() => {
  const f = byName.weird_id_field;
  return f && !f.selector.startsWith('#:r3:') && f.label === 'Auto-generated id field';
})());
expect('every selector is present and non-empty', fields.every(f => typeof f.selector === 'string' && f.selector.length > 0));

// --- section / section_index — Workday-style repeating "Work Experience" blocks --------
// Block 1: <fieldset><legend>Work Experience 1</legend>...</fieldset>
expect('block 1 section resolved via <fieldset><legend> ("Work Experience 1")',
  byName['workExperience-1--jobTitle'] && byName['workExperience-1--jobTitle'].section === 'Work Experience 1');
expect('block 1 section_index parsed from the legend text',
  byName['workExperience-1--jobTitle'] && byName['workExperience-1--jobTitle'].section_index === 1);
expect('block 1 company field carries the same section_index',
  byName['workExperience-1--company'] && byName['workExperience-1--company'].section_index === 1);
expect('block 1 "I currently work here" checkbox carries the same section_index',
  fields.some(f => f.name === 'workExperience-1--currentlyWorkHere' && f.type === 'checkbox' && f.section_index === 1));

// Block 2: no <fieldset>/<legend> — a preceding class*="heading" div is the section instead.
expect('block 2 section resolved via a class*="heading" block ("Work Experience 2")',
  byName['workExperience-2--jobTitle'] && byName['workExperience-2--jobTitle'].section === 'Work Experience 2');
expect('block 2 section_index parsed from that heading text',
  byName['workExperience-2--jobTitle'] && byName['workExperience-2--jobTitle'].section_index === 2);
expect('block 2 location field carries the same section_index',
  byName['workExperience-2--location'] && byName['workExperience-2--location'].section_index === 2);

// Block 3: heading text carries NO number at all ("Additional Work Experience") — the index
// must come only from the Workday-style field name (workExperience-3--...).
expect('block 3 heading text has no number ("Additional Work Experience")',
  byName['workExperience-3--jobTitle'] && byName['workExperience-3--jobTitle'].section === 'Additional Work Experience');
expect('block 3 section_index resolved ONLY from the field name, not the heading',
  byName['workExperience-3--jobTitle'] && byName['workExperience-3--jobTitle'].section_index === 3);
expect('block 3 company field also gets the name-derived section_index',
  byName['workExperience-3--company'] && byName['workExperience-3--company'].section_index === 3);

// Negative case: a heading with an unrelated digit next to it ("Phone Numbers (2 max)") must
// NOT be mistaken for a section index — no section-ish keyword sits next to that "2", and the
// field's own name ("altPhone") has no digit either.
expect('negative case: digit near an unrelated heading does not yield a bogus section_index',
  byName.altPhone && byName.altPhone.section_index === null);
expect('negative case: unrelated pre-existing field has no section_index bleed-through',
  byName.full_name && byName.full_name.section_index === null);

// --- custom select widgets (select2 / Chosen pairing) -----------------------
// The real bug from a live Avature/Viasat form: a select2- or Chosen-style
// library hides the native <select> (display:none) and renders a styled
// div/span in its place. The plain visibility filter correctly drops the
// hidden <select> on its own — proving the field would otherwise vanish
// entirely is as important as proving the fix works.
(() => {
  const doc = dom.window.document;

  expect('select2-style hidden <select> is scanned (would vanish without pairing)',
    !!byName.state_s2);
  expect('select2 field labelled "State/Province" (via its own <label for>)',
    byName.state_s2 && byName.state_s2.label === 'State/Province');
  expect('select2 field options captured from the hidden native <select>',
    byName.state_s2 && byName.state_s2.options.join(',') === '— Make a Selection —,Arizona,California,New York');

  expect('Chosen-style hidden <select> is scanned (would vanish without pairing)',
    !!byName.state_chosen);
  expect('Chosen field labelled from nearby preceding text (contains "State")',
    byName.state_chosen && /state/i.test(byName.state_chosen.label));

  expect('decoy hidden <select> with no paired widget stays excluded',
    !byName.decoy_hidden_select);

  // Highlighting must target the VISIBLE widget, never the hidden <select>
  // the operator can't see (see scanner.js getHighlightTargets()).
  const s2Entry = registry[byName.state_s2.id];
  const s2Highlight = Scanner.getHighlightTargets(s2Entry)[0];
  expect('select2 highlight target is the visible widget, not the hidden select',
    s2Highlight !== s2Entry.el && /select2/.test(s2Highlight.className));

  const chosenEntry = registry[byName.state_chosen.id];
  const chosenHighlight = Scanner.getHighlightTargets(chosenEntry)[0];
  expect('Chosen highlight target is the visible widget, not the hidden select',
    chosenHighlight !== chosenEntry.el && /chosen-container/.test(chosenHighlight.className));

  // Filling writes to the underlying native <select>, and its value is read
  // back afterwards — never trust the write blind (see setSelectValue()).
  // select2 block: options are state NAMES with non-code values ("1"/"2"/"3"),
  // so filling by the 2-letter code "AZ" can only succeed via the new
  // state-code cross-match tier, not a pre-existing exact match.
  let ok = Scanner.applyFill(s2Entry, 'Arizona');
  expect('select2 select filled by full state NAME, applyFill reports success', ok === true);
  expect('select2 select value read back after fill-by-name matches "Arizona" (value="1")',
    s2Entry.el.value === '1');

  s2Entry.el.selectedIndex = 0; // reset to the placeholder before the next fill
  ok = Scanner.applyFill(s2Entry, 'AZ');
  expect('select2 select filled by 2-letter CODE, applyFill reports success', ok === true);
  expect('select2 select value read back after fill-by-code cross-matched to "Arizona" (value="1")',
    s2Entry.el.value === '1');

  // Chosen block: the opposite shape — options are 2-letter CODES, so
  // filling by the full name "Arizona" can only succeed via the same
  // cross-match tier from the other direction.
  ok = Scanner.applyFill(chosenEntry, 'AZ');
  expect('Chosen select filled by 2-letter CODE, applyFill reports success', ok === true);
  expect('Chosen select value read back after fill-by-code matches "AZ"',
    chosenEntry.el.value === 'AZ');

  chosenEntry.el.selectedIndex = 0; // reset to the placeholder before the next fill
  ok = Scanner.applyFill(chosenEntry, 'Arizona');
  expect('Chosen select filled by full state NAME, applyFill reports success', ok === true);
  expect('Chosen select value read back after fill-by-name cross-matched to "AZ"',
    chosenEntry.el.value === 'AZ');

  // getFieldLabel()'s widget-fallback branch specifically: when the hidden
  // <select> itself has NO resolvable label at all (no <label for>, no
  // wrapping <label>, no aria-label/aria-labelledby, no placeholder, no
  // nearby text), the label must come from the visible widget instead.
  //
  // Built in a fresh, otherwise-empty JSDOM document rather than appended to
  // the shared test-page.html one: getLabel()'s "nearby preceding text"
  // fallback walks up to 4 ancestor levels and, at each level, back through
  // EVERY earlier sibling looking for text -- appended to the real page's
  // <body> it would walk straight past the empty wrapper and pick up the
  // page's own <h1>, defeating the point of this test.
  const freshDom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
  freshDom.window.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10 };
  };
  const fdoc = freshDom.window.document;

  const wrap = fdoc.createElement('div');
  const hiddenSelect = fdoc.createElement('select');
  hiddenSelect.id = 'synthetic_hidden_select';
  hiddenSelect.className = 'select2-hidden-accessible';
  hiddenSelect.setAttribute('aria-hidden', 'true');
  hiddenSelect.style.display = 'none';
  const opt = fdoc.createElement('option');
  opt.value = 'x';
  opt.textContent = 'X';
  hiddenSelect.appendChild(opt);
  const widget = fdoc.createElement('span');
  widget.className = 'select2-container';
  widget.setAttribute('aria-label', 'Synthetic Widget Label');
  wrap.appendChild(hiddenSelect);
  wrap.appendChild(widget);
  fdoc.body.appendChild(wrap);

  expect('getLabel() alone finds nothing for the synthetic select (sanity check for the fixture itself)',
    Scanner.getLabel(hiddenSelect) === '');

  const foundWidget = Scanner.findPairedWidget(hiddenSelect);
  expect('findPairedWidget() finds a marker-class widget with no <label for> on the select',
    foundWidget === widget);
  expect('getFieldLabel() falls back to the widget\'s own aria-label when the select has none',
    Scanner.getFieldLabel(hiddenSelect, foundWidget) === 'Synthetic Widget Label');
})();

// --- the defining safety invariant ------------------------------------------
// Filling a radio/checkbox needs a native .click(). Nothing else may ever be
// clicked, because a click on a submit button would send a real application.
(() => {
  const mk = (tag, type) => { const e = dom.window.document.createElement(tag); if (type) e.type = type; return e; };
  expect('click guard allows radio', Scanner.isClickSafe(mk('input', 'radio')) === true);
  expect('click guard allows checkbox', Scanner.isClickSafe(mk('input', 'checkbox')) === true);
  expect('click guard REFUSES submit input', Scanner.isClickSafe(mk('input', 'submit')) === false);
  expect('click guard REFUSES button input', Scanner.isClickSafe(mk('input', 'button')) === false);
  expect('click guard REFUSES image input', Scanner.isClickSafe(mk('input', 'image')) === false);
  expect('click guard REFUSES a <button>', Scanner.isClickSafe(mk('button')) === false);
  expect('click guard REFUSES a text input', Scanner.isClickSafe(mk('input', 'text')) === false);
  expect('click guard REFUSES null', Scanner.isClickSafe(null) === false);
})();

// --- résumé attachment: target selection (v3 spec §B) -----------------------
// jsdom (verified against the version this harness runs) has NO DataTransfer
// or DragEvent implementation at all, and its `input.files` setter only
// accepts a real FileList (which nothing here can construct). That means the
// actual "assign the file, dispatch drop, read back input.files[0]" mechanics
// in attachResumeFile() CANNOT be exercised end-to-end offline — only in a
// real browser (see scripts/chrome_load_test.py, owned elsewhere; this repo's
// operator adds the real-Chrome attachment assertions there separately).
//
// What CAN be verified here, and is below:
//   1. findResumeFileTarget()'s résumé-vs-cover-letter selection logic — pure
//      DOM/text matching, no browser-only APIs involved.
//   2. attachResumeFile()'s fail-soft guard when DataTransfer is unavailable —
//      genuinely exercises the "never throw, report a clear failure instead"
//      path (the same path a site that rejects programmatic assignment would
//      also hit), rather than a vacuous pass.
(() => {
  const doc = dom.window.document;

  const ghTarget = Scanner.findResumeFileTarget(doc);
  expect('résumé target found on the page', !!ghTarget);
  expect('résumé target is the Greenhouse dropzone input, not the cover letter or the ambiguous "Attach" input',
    ghTarget && ghTarget.el.id === 'gh_resume_input');
  expect('résumé target is correctly flagged as a dropzone',
    ghTarget && ghTarget.isDropzone === true);
  expect('résumé target context text captured the "Resume/CV" label despite the hidden input having no <label for>',
    ghTarget && /resume\s*\/?\s*cv/i.test(ghTarget.contextText));

  expect('generic scanFields() excludes the hidden Greenhouse input (display:none)',
    !fields.some(f => f.name === 'resume_gh'));
  expect('generic scanFields() still sees the visible cover-letter and "Attach" file inputs',
    fields.some(f => f.name === 'cover_letter') && fields.some(f => f.name === 'attach_file'));

  // Remove the explicitly-labeled dropzone to test rule 2 in isolation: with no
  // résumé-labeled candidate left, the first non-cover-letter file input wins —
  // even though the cover-letter input is EARLIER in DOM order (proving the
  // cover-letter exclusion, not DOM order, is what's doing the work).
  const dz = doc.getElementById('gh_resume_dropzone');
  dz.parentNode.removeChild(dz);
  const fallbackTarget = Scanner.findResumeFileTarget(doc);
  expect('rule 2 (no explicit résumé label): first non-cover-letter file input wins over an earlier cover-letter input',
    fallbackTarget && fallbackTarget.el.id === 'attach_upload');

  // Remove every remaining file input to test the "nothing to attach to" case -- including the
  // separate Workday résumé fixture (labelled "Attachments", not "Resume/CV", specifically so
  // it never competed with gh_resume_input/attach_upload above; it still has to be cleared out
  // here for this to be a genuine "zero file inputs anywhere" case).
  doc.getElementById('attach_upload').remove();
  doc.getElementById('cover_letter_upload').remove();
  doc.getElementById('wd_resume_input').remove();
  expect('no eligible file input on the page -> null target', Scanner.findResumeFileTarget(doc) === null);

  const noTargetPromise = Scanner.attachResumeFile(doc, new dom.window.File(['x'], 'resume.pdf', { type: 'application/pdf' }));
  expect('attachResumeFile() returns a Promise', noTargetPromise && typeof noTargetPromise.then === 'function');
  pending.push(noTargetPromise.then((noTargetResult) => {
    expect('attachResumeFile() reports attempted:false when there is nothing to attach to',
      noTargetResult && noTargetResult.attempted === false);
  }));
})();

// --- résumé attachment: accept-attribute tie-break (no explicit label anywhere) ---
// Built in its own isolated JSDOM instance for the same reason as the custom-select
// label-fallback test above: it needs to control every file input on the "page" with
// no interference from (or from being removed by) the destructive steps in the block
// above.
(() => {
  const freshDom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
  const fdoc = freshDom.window.document;

  const photoInput = fdoc.createElement('input');
  photoInput.type = 'file';
  photoInput.id = 'photo_upload';
  photoInput.name = 'photo';
  photoInput.setAttribute('accept', 'image/*');
  fdoc.body.appendChild(photoInput);

  const docInput = fdoc.createElement('input');
  docInput.type = 'file';
  docInput.id = 'doc_upload';
  docInput.name = 'attachment';
  docInput.setAttribute('accept', '.pdf,.doc,.docx');
  fdoc.body.appendChild(docInput);

  // Neither input has any résumé-ish label at all (rule 1 finds nothing) — the
  // document-shaped accept attribute is the only signal distinguishing them.
  const target = Scanner.findResumeFileTarget(fdoc);
  expect('no explicit résumé label anywhere: a document-shaped accept ("pdf/doc") wins over a photo-only accept ("image/*")',
    target && target.el.id === 'doc_upload');
})();

// --- résumé attachment: fail-soft guard when DataTransfer is unavailable ----
(() => {
  const doc = dom.window.document;
  const input = doc.createElement('input');
  input.type = 'file';
  input.id = 'guard_resume_input';
  doc.body.appendChild(input);
  const labelNode = doc.createElement('label');
  labelNode.setAttribute('for', 'guard_resume_input');
  labelNode.textContent = 'Resume/CV';
  doc.body.appendChild(labelNode);

  const file = new dom.window.File(['hello'], 'resume.pdf', { type: 'application/pdf' });
  let threw = false;
  let resultPromise;
  try {
    resultPromise = Scanner.attachResumeFile(doc, file);
  } catch (e) {
    threw = true;
  }
  expect('attachResumeFile() never throws synchronously even when DataTransfer is unavailable', threw === false);
  expect('attachResumeFile() always returns a Promise', resultPromise && typeof resultPromise.then === 'function');
  pending.push(Promise.resolve(resultPromise).then((result) => {
    expect('attachResumeFile() reports a clear, non-silent failure when DataTransfer is unavailable (jsdom has none)',
      !!result && result.attempted === true && result.attached === false && /DataTransfer/.test(result.reason || ''));
  }));
})();

// --- "Add Another" guard: isAddAnotherButtonSafe() (v4 spec Part 1) --------
// A SECOND, independent click path alongside isClickSafe() above — every
// condition must hold. Exists specifically to refuse the HTML subtlety where
// a <button> with no type="" attribute defaults to type="submit" and, with a
// form owner, submits that form when clicked.
(() => {
  const doc = dom.window.document;
  const mk = (tag, attrs, text) => {
    const e = doc.createElement(tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    doc.body.appendChild(e); // must be connected for isVisible() to see it at all
    return e;
  };

  // --- allowed shapes ---
  expect('guard allows a type=button "Add Another" button with no form owner',
    Scanner.isAddAnotherButtonSafe(mk('button', { type: 'button' }, 'Add Another')) === true);
  expect('guard allows a role=button div ("+ Add Position")',
    Scanner.isAddAnotherButtonSafe(mk('div', { role: 'button' }, '+ Add Position')) === true);
  expect('guard allows a type=submit button that has NO form owner (nothing it could submit)',
    Scanner.isAddAnotherButtonSafe(mk('button', { type: 'submit' }, 'Add Another')) === true);
  expect('guard allows an <a> with href="#" ("Add Education")',
    Scanner.isAddAnotherButtonSafe(mk('a', { href: '#' }, 'Add Education')) === true);
  expect('guard allows an <a> with href="javascript:void(0)"',
    Scanner.isAddAnotherButtonSafe(mk('a', { href: 'javascript:void(0)' }, 'Add Another')) === true);

  // --- refused shapes ---
  const bareTrapForm = doc.createElement('form');
  const bareTrapBtn = doc.createElement('button'); // NO type="" attribute at all
  bareTrapBtn.textContent = 'Add Another';
  bareTrapForm.appendChild(bareTrapBtn);
  doc.body.appendChild(bareTrapForm);
  expect('standalone trap: no-type <button> defaults to type "submit"', bareTrapBtn.type === 'submit');
  expect('standalone trap: has a form owner', bareTrapBtn.form === bareTrapForm);
  expect('guard REFUSES a no-type <button> inside a <form> (type defaults to "submit", has a form owner)',
    Scanner.isAddAnotherButtonSafe(bareTrapBtn) === false);

  expect('guard REFUSES input[type=submit] outright, even with no form owner',
    Scanner.isAddAnotherButtonSafe(mk('input', { type: 'submit', value: 'Add Another' })) === false);
  expect('guard REFUSES input[type=image] outright',
    Scanner.isAddAnotherButtonSafe(mk('input', { type: 'image' })) === false);
  expect('guard REFUSES "Submit Application" (does not read as an add action)',
    Scanner.isAddAnotherButtonSafe(mk('button', { type: 'button' }, 'Submit Application')) === false);
  expect('guard REFUSES "Save and Continue" (does not read as an add action)',
    Scanner.isAddAnotherButtonSafe(mk('button', { type: 'button' }, 'Save and Continue')) === false);
  expect('guard REFUSES "Add and Submit" (matches the ADD pattern but ALSO the deny pattern)',
    Scanner.isAddAnotherButtonSafe(mk('button', { type: 'button' }, 'Add and Submit')) === false);
  expect('guard REFUSES an invisible (display:none) role=button div',
    Scanner.isAddAnotherButtonSafe(mk('div', { role: 'button', style: 'display:none' }, 'Add Another')) === false);
  const disabledBtn = mk('button', { type: 'button' }, 'Add Another');
  disabledBtn.disabled = true;
  expect('guard REFUSES a disabled button', Scanner.isAddAnotherButtonSafe(disabledBtn) === false);
  expect('guard REFUSES null', Scanner.isAddAnotherButtonSafe(null) === false);
  expect('guard REFUSES an <a> with a real navigating href',
    Scanner.isAddAnotherButtonSafe(mk('a', { href: 'https://example.test/apply/submit' }, 'Add Another')) === false);

  // --- the fixture trap itself (test-page.html's standalone <form id="trap-form">) ---
  const fixtureTrap = doc.getElementById('trap_add_btn');
  expect('fixture trap: no type="" attribute means .type is "submit"', fixtureTrap.type === 'submit');
  expect('fixture trap: has a form owner (<form id="trap-form">)',
    !!fixtureTrap.form && fixtureTrap.form.id === 'trap-form');
  expect('guard REFUSES the fixture\'s no-type in-form trap button', Scanner.isAddAnotherButtonSafe(fixtureTrap) === false);
})();

// --- repeating-section expansion: "Add Another" (v4 spec Part 1) -----------
// Exercises the whole discovery path against the #my-experience-step fixture:
// counting blocks with the SAME section/section_index machinery the scanner
// already uses, finding the correct section's button (never the other
// section's), and a guarded click actually growing the DOM by one block.
// Scoped to #my-experience-step rather than the whole document because the
// pre-existing static "Work Experience 1/2/3" fixture above exists for a
// different purpose (section_index parsing) and already reuses those same
// heading numbers — mixing the two would make "how many blocks exist"
// ambiguous. content.js's real usage scopes to `document`, which is safe on
// a real page that (unlike this test fixture) has no such artificial clash.
(() => {
  const doc = dom.window.document;
  const step = doc.getElementById('my-experience-step');
  const workBtn = doc.getElementById('myexp_add_btn');
  const eduBtn = doc.getElementById('myedu_add_btn');

  expect('countSectionBlocks: exactly 1 work_history block before any click',
    Scanner.countSectionBlocks(step, 'work_history') === 1);
  expect('countSectionBlocks: exactly 1 education block before any click',
    Scanner.countSectionBlocks(step, 'education') === 1);

  expect('findAddButtonForKind picks the WORK "Add Another" button for work_history',
    Scanner.findAddButtonForKind(step, 'work_history') === workBtn);
  expect('findAddButtonForKind picks the EDUCATION add control for education (a role=button div)',
    Scanner.findAddButtonForKind(step, 'education') === eduBtn);
  expect('the two kinds never resolve to the same button',
    Scanner.findAddButtonForKind(step, 'work_history') !== Scanner.findAddButtonForKind(step, 'education'));

  // Document-wide sanity: even scanning the WHOLE page (which also contains the static
  // Work Experience fixture's own inert "+ Add Another" button, much earlier in document
  // order), anchoring on "the LAST field of this kind" means the real button for THIS
  // section is still what gets picked — never the page's first add-shaped button.
  expect('findAddButtonForKind(document, work_history) still resolves to the real button, not an earlier unrelated one',
    Scanner.findAddButtonForKind(doc, 'work_history') === workBtn);

  expect('clicking the WORK add button is allowed and reported as clicked',
    Scanner.safeClickAddButton(workBtn) === true);
  expect('Work Experience block 2 now exists in the DOM', !!doc.getElementById('myexp-block-2'));
  expect('countSectionBlocks now reports 2 work_history blocks after the click',
    Scanner.countSectionBlocks(step, 'work_history') === 2);
  expect('education is unaffected by clicking the work add button',
    Scanner.countSectionBlocks(step, 'education') === 1);

  expect('clicking the EDUCATION add control (a role=button div) is allowed and reported as clicked',
    Scanner.safeClickAddButton(eduBtn) === true);
  expect('Education block 2 now exists in the DOM', !!doc.getElementById('myedu-block-2'));
  expect('countSectionBlocks now reports 2 education blocks after the click',
    Scanner.countSectionBlocks(step, 'education') === 2);
})();

// --- the fixture trap: guard refuses it, AND proves it is a REAL trap ------
(() => {
  const doc = dom.window.document;
  const trapBtn = doc.getElementById('trap_add_btn');

  expect('safeClickAddButton refuses to click the trap at all', Scanner.safeClickAddButton(trapBtn) === false);
  expect('...so it never actually got clicked: __FORM_SUBMITTED__ was never set',
    !dom.window.__FORM_SUBMITTED__);

  // Proves the trap is a genuine hazard, not inert decoration: clicking it DIRECTLY
  // (bypassing our guard entirely, simulating what a naive "first add-shaped button on
  // the page" integration would do) DOES submit the form.
  trapBtn.click();
  expect('bypassing the guard and clicking the trap directly DOES fire its form\'s submit handler',
    dom.window.__FORM_SUBMITTED__ === true);
})();

// --- belt and braces: the submit shield (v4 spec Part 1) -------------------
// Independent of the guard AND of the fixture trap above: even if a bug let
// isAddAnotherButtonSafe() approve something dangerous, installSubmitShield()
// must still stop the submit from having any effect, and say so.
(() => {
  const doc = dom.window.document;
  const form = doc.createElement('form');
  const btn = doc.createElement('button'); // no type="" -> submits `form` when clicked
  btn.textContent = 'Add Another';
  form.appendChild(btn);
  doc.body.appendChild(form);

  let formSubmitFired = false;
  form.addEventListener('submit', (e) => { e.preventDefault(); formSubmitFired = true; });

  expect('shield-test fixture: the guard independently refuses this no-type in-form button too',
    Scanner.isAddAnotherButtonSafe(btn) === false);

  let shieldBlocked = false;
  const removeShield = Scanner.installSubmitShield(doc, () => { shieldBlocked = true; });
  btn.click(); // deliberately bypasses the guard, simulating "the guard let something through"
  removeShield();

  expect('installSubmitShield() blocks the submit and reports it via onBlocked()', shieldBlocked === true);
  expect('while installed, the shield prevents the form\'s OWN submit handler from ever running',
    formSubmitFired === false);

  btn.click(); // shield has been removed now
  expect('after removeShield(), the same click reaches the form\'s own submit handler normally',
    formSubmitFired === true);

  form.remove();
})();

// --- split month/year date inputs (v4 spec Part 2) --------------------------
(() => {
  const doc = dom.window.document;
  const rescan = Scanner.scanFields(doc);
  const byNameLocal = {};
  for (const f of rescan.fields) if (f.name) byNameLocal[f.name] = f;

  const monthEl = doc.getElementById('myexp1_from_month');
  const yearEl = doc.getElementById('myexp1_from_year');

  const dateField = rescan.fields.find(f => rescan.registry[f.id] && rescan.registry[f.id].kind === 'date-parts'
    && rescan.registry[f.id].monthEl === monthEl && rescan.registry[f.id].yearEl === yearEl);
  expect('split month/year pair is scanned as exactly ONE merged field', !!dateField);
  expect('the merged field is labelled "From" (resolved via aria-labelledby on the shared wrapper)',
    dateField && dateField.label === 'From');
  expect('the merged field type is "text", not "date" (service must emit raw MM/YYYY, not reformat to ISO)',
    dateField && dateField.type === 'text');
  expect('the merged field carries the block\'s section_index (1)', dateField && dateField.section_index === 1);

  const strayHalves = rescan.fields.filter(f => (f.name === 'myexp-1--fromMonth' || f.name === 'myexp-1--fromYear')
    && rescan.registry[f.id].kind !== 'date-parts');
  expect('neither half of the pair is ALSO scanned as its own separate (non-merged) field', strayHalves.length === 0);

  const dateEntry = rescan.registry[dateField.id];
  let ok = Scanner.applyFill(dateEntry, '09/2020');
  expect('applyFill splits "09/2020" into the month + year inputs and reports success', ok === true);
  expect('month input reads back "09"', monthEl.value === '09');
  expect('year input reads back "2020"', yearEl.value === '2020');
  expect('getCurrentValue reassembles the pair back into "09/2020"',
    Scanner.getCurrentValue(dateEntry) === '09/2020');

  ok = Scanner.applyFill(dateEntry, 'not-a-date');
  expect('applyFill refuses a value that is not MM/YYYY rather than guessing at a split', ok === false);

  // A plain single MM/YYYY text input elsewhere in the SAME block must keep working exactly
  // as before — proves the new date-pair detection did not change ordinary single-input
  // date field handling.
  const plainTo = byNameLocal['myexp-1--endDate'];
  expect('a plain single MM/YYYY text input is still scanned as its own ordinary field',
    !!plainTo && plainTo.type === 'text' && rescan.registry[plainTo.id].kind === 'element');
  const plainOk = Scanner.applyFill(rescan.registry[plainTo.id], '01/2019');
  expect('the plain MM/YYYY field still fills exactly as before',
    plainOk === true && doc.getElementById('myexp1_to').value === '01/2019');
})();

// --- split month/year: a month <select> + year <input> pair, isolated ------
// Built in its own fresh JSDOM, same discipline as the other isolated tests
// above — no interference from (or with) the shared test-page.html document.
(() => {
  const freshDom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
  freshDom.window.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10 };
  };
  const fdoc = freshDom.window.document;

  const label = fdoc.createElement('label');
  label.id = 'grad_date_label';
  label.textContent = 'Graduation date';
  fdoc.body.appendChild(label);

  const wrap = fdoc.createElement('div');
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-labelledby', 'grad_date_label');
  fdoc.body.appendChild(wrap);

  const monthSelect = fdoc.createElement('select');
  monthSelect.setAttribute('data-automation-id', 'gradMonth-select');
  monthSelect.name = 'gradMonth';
  ['', 'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
    .forEach((m) => {
      const opt = fdoc.createElement('option');
      opt.value = m;
      opt.textContent = m || '-- Month --';
      monthSelect.appendChild(opt);
    });
  wrap.appendChild(monthSelect);

  const yearInput = fdoc.createElement('input');
  yearInput.type = 'text';
  yearInput.setAttribute('data-automation-id', 'gradYear-input');
  yearInput.name = 'gradYear';
  wrap.appendChild(yearInput);

  const scanned = Scanner.scanFields(fdoc);
  const merged = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'date-parts');
  expect('month <select> + year <input> pair is also detected and merged into one field', !!merged);
  expect('that merged field is labelled "Graduation date"', merged && merged.label === 'Graduation date');

  const strayCount = scanned.fields.filter(f => (f.name === 'gradMonth' || f.name === 'gradYear')
    && scanned.registry[f.id].kind !== 'date-parts').length;
  expect('neither half of the select+input pair is ALSO scanned as its own separate field', strayCount === 0);

  const entry = scanned.registry[merged.id];
  const ok = Scanner.applyFill(entry, '03/2018');
  expect('filling the select+input pair splits "03/2018" correctly', ok === true);
  expect('the month <select> lands on "March" via month-name matching', monthSelect.value === 'March');
  expect('the year <input> reads back "2018"', yearInput.value === '2018');
})();

// =====================================================================
// Workday widgets (v6 spec) — ground truth from berellevy/job_app_filler and
// ankitsharma38/Workday-Autofill-Assistant, exercised against the fixtures added to
// test-page.html's #wd-form. See that file's own comments for what each fixture proves.
// =====================================================================

// --- scanning shape: each widget becomes exactly one FieldDescriptor -------
(() => {
  const doc = dom.window.document;
  const scanned = Scanner.scanFields(doc);

  const monthEl = doc.getElementById('wd_start_month');
  const yearEl = doc.getElementById('wd_start_year');
  const myField = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'wd-date-my'
    && scanned.registry[f.id].monthEl === monthEl && scanned.registry[f.id].yearEl === yearEl);
  expect('Workday month/year dateInputWrapper is scanned as ONE field with widget "wd-date-my"',
    !!myField && myField.widget === 'wd-date-my');
  expect('...labelled "From" (resolved from the formField-* container\'s own <label>)',
    myField && myField.label === 'From');
  expect('...type is "text", not "date" (service must emit raw MM/YYYY, never reformatted)',
    myField && myField.type === 'text');

  const eduYearEl = doc.getElementById('wd_edu_from_year');
  const yField = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'wd-date-y'
    && scanned.registry[f.id].yearEl === eduYearEl);
  expect('Workday year-only dateInputWrapper (no Month input at all) is scanned with widget "wd-date-y"',
    !!yField && yField.widget === 'wd-date-y');

  const maskedEl = doc.getElementById('wd_cert_masked');
  const maskedField = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'wd-date-my'
    && scanned.registry[f.id].maskedEl === maskedEl);
  expect('Workday masked single-input dateInputWrapper (no aria-label Month/Year at all) is ALSO scanned, widget "wd-date-my"',
    !!maskedField);

  const strayWd = scanned.fields.filter(f => {
    const e = scanned.registry[f.id];
    return e && e.kind === 'element' && (e.el === monthEl || e.el === yearEl || e.el === eduYearEl || e.el === maskedEl);
  });
  expect('Workday spinner/masked date inputs are never ALSO scanned as ordinary text fields', strayWd.length === 0);

  const degreeBtn = doc.getElementById('wd_degree_button');
  const degreeField = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'wd-dropdown'
    && scanned.registry[f.id].button === degreeBtn);
  expect('Workday dropdown button[aria-haspopup=listbox] is scanned as a field with widget "wd-dropdown"',
    !!degreeField && degreeField.widget === 'wd-dropdown' && degreeField.label === 'Degree');

  const fosInput = doc.getElementById('wd_fos_input');
  const skillsInput = doc.getElementById('wd_skills_input');
  const fosField = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'wd-prompt'
    && scanned.registry[f.id].input === fosInput);
  expect('Workday multiSelectContainer prompt is scanned as a field with widget "wd-prompt"',
    !!fosField && fosField.widget === 'wd-prompt' && fosField.label === 'Field of Study');

  const strayPromptInputs = scanned.fields.filter(f => {
    const e = scanned.registry[f.id];
    return e && e.kind === 'element' && (e.el === fosInput || e.el === skillsInput);
  });
  expect('the prompts\' inner <input>s are never ALSO scanned as ordinary text fields', strayPromptInputs.length === 0);
})();

// --- spinner dates: the ArrowUp technique, its retry-once path, and the masked fallback ----
(() => {
  const doc = dom.window.document;
  const scanned = Scanner.scanFields(doc);
  const monthEl = doc.getElementById('wd_start_month');
  const yearEl = doc.getElementById('wd_start_year');
  const myEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-date-my' && e.monthEl === monthEl);
  expect('found the Work Experience "From" wd-date-my registry entry', !!myEntry);

  // Prove the mock is genuinely adversarial: the WRONG (previously-shipped) technique —
  // plain value + input/change — really is ignored, not just assumed to be.
  monthEl.value = '07';
  monthEl.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  monthEl.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  expect('the mock Workday spinner genuinely IGNORES plain value+input/change (proves the mock, not just the fix)',
    monthEl.value !== '07');

  const ok = Scanner.applyFill(myEntry, '09/2020');
  expect('applyFill commits "09/2020" into the Month/Year spinner pair via the set-then-ArrowUp technique', ok === true);
  expect('month spinner reads back 9', parseInt(monthEl.value, 10) === 9);
  expect('year spinner reads back 2020', parseInt(yearEl.value, 10) === 2020);
  expect('getCurrentValue reassembles the pair as "09/2020" (zero-padded month)', Scanner.getCurrentValue(myEntry) === '09/2020');

  const okBad = Scanner.applyFill(myEntry, 'not-a-date');
  expect('a non-MM/YYYY value is refused rather than guessed at', okBad === false);

  // Year-only field whose mock spinner deliberately needs ArrowUp TWICE per unit.
  const eduYearEl = doc.getElementById('wd_edu_from_year');
  const yEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-date-y' && e.yearEl === eduYearEl);
  expect('found the Education "From" wd-date-y registry entry', !!yEntry);
  const okY = Scanner.applyFill(yEntry, '2016');
  expect('setWorkdaySpinnerValue\'s retry-once path commits a year into a spinner that needs ArrowUp TWICE per unit', okY === true);
  expect('year-only spinner reads back 2016', parseInt(eduYearEl.value, 10) === 2016);

  const okY2 = Scanner.applyFill(yEntry, '05/2017');
  expect('a "MM/YYYY" value sent to a wd-date-y field uses ONLY the year part',
    okY2 === true && parseInt(eduYearEl.value, 10) === 2017);

  // Masked single-input fallback (ankitsharma38).
  const maskedEl = doc.getElementById('wd_cert_masked');
  const maskedEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-date-my' && e.maskedEl === maskedEl);
  expect('found the masked single-input wd-date-my registry entry', !!maskedEntry);

  maskedEl.value = '11/2025'; // plain write, no preceding keydown at all
  maskedEl.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  expect('the mock masked field genuinely IGNORES plain value+input with no preceding keydown',
    maskedEl.value !== '11/2025');

  const okMasked = Scanner.applyFill(maskedEntry, '03/2021');
  expect('typeMaskedTextField types "03/2021" character by character (keydown/keypress/input/keyup per char)', okMasked === true);
  expect('masked field reads back "03/2021"', maskedEl.value === '03/2021');
})();

// --- Workday guards: hard-deny-by-automation-id + never-an-arbitrary-element scope checks ---
(() => {
  const doc = dom.window.document;
  // Every element this block creates goes under ONE scratch container, removed at the end —
  // several of them are deliberately option/listbox-shaped (that's the point of the guard
  // tests), and left sitting loose in <body> they would otherwise be picked up by a LATER
  // test's document-wide "no scoped results found" fallback query (see fillWorkdayPromptTerm's
  // waitFor() in scanner.js) as if they were real search results for an unrelated field.
  const scratch = doc.createElement('div');
  doc.body.appendChild(scratch);
  const mk = (tag, attrs, text) => {
    const e = doc.createElement(tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    scratch.appendChild(e);
    return e;
  };

  // --- dropdown opener guard ---
  const legitField = mk('div', { 'data-automation-id': 'formField-legit-test' });
  const legitBtn = doc.createElement('button');
  legitBtn.setAttribute('aria-haspopup', 'listbox');
  legitBtn.textContent = 'Select One';
  legitField.appendChild(legitBtn);
  expect('isWorkdayDropdownOpenerSafe allows a button[aria-haspopup=listbox] inside formField-*',
    Scanner.isWorkdayDropdownOpenerSafe(legitBtn) === true);

  const trapField = mk('div', { 'data-automation-id': 'formField-trap-test' });
  const trapBtn = doc.createElement('button');
  trapBtn.setAttribute('aria-haspopup', 'listbox');
  trapBtn.setAttribute('data-automation-id', 'bottom-navigation-submit-button');
  trapBtn.textContent = 'Select One';
  trapField.appendChild(trapBtn);
  expect('isWorkdayDropdownOpenerSafe REFUSES a listbox-shaped button whose OWN automation id contains "bottom-navigation" (hard deny, regardless of shape)',
    Scanner.isWorkdayDropdownOpenerSafe(trapBtn) === false);

  const nextBtn = doc.createElement('button');
  nextBtn.setAttribute('aria-haspopup', 'listbox');
  nextBtn.setAttribute('data-automation-id', 'wd-next-button');
  legitField.appendChild(nextBtn);
  expect('isWorkdayDropdownOpenerSafe REFUSES an automation id containing "next" (hard deny)',
    Scanner.isWorkdayDropdownOpenerSafe(nextBtn) === false);

  expect('isWorkdayDropdownOpenerSafe REFUSES a button with no formField-* ancestor at all',
    Scanner.isWorkdayDropdownOpenerSafe(mk('button', { 'aria-haspopup': 'listbox' }, 'Select One')) === false);

  const denyTextField = mk('div', { 'data-automation-id': 'formField-deny-text' });
  const denyTextBtn = doc.createElement('button');
  denyTextBtn.setAttribute('aria-haspopup', 'listbox');
  denyTextBtn.textContent = 'Submit';
  denyTextField.appendChild(denyTextBtn);
  expect('isWorkdayDropdownOpenerSafe REFUSES a button whose text matches the existing deny list ("Submit")',
    Scanner.isWorkdayDropdownOpenerSafe(denyTextBtn) === false);

  const disabledOpener = doc.createElement('button');
  disabledOpener.setAttribute('aria-haspopup', 'listbox');
  disabledOpener.disabled = true;
  legitField.appendChild(disabledOpener);
  expect('isWorkdayDropdownOpenerSafe REFUSES a disabled opener', Scanner.isWorkdayDropdownOpenerSafe(disabledOpener) === false);
  expect('isWorkdayDropdownOpenerSafe REFUSES null', Scanner.isWorkdayDropdownOpenerSafe(null) === false);

  // --- option guard: must look like an option AND live inside the given scope ---
  const scopeUl = mk('ul', { role: 'listbox' });
  const goodOption = doc.createElement('li');
  goodOption.setAttribute('role', 'option');
  goodOption.textContent = 'Option A';
  scopeUl.appendChild(goodOption);
  expect('isWorkdayOptionSafe allows role=option inside the given scope', Scanner.isWorkdayOptionSafe(goodOption, scopeUl) === true);

  const outsideOption = doc.createElement('li');
  outsideOption.setAttribute('role', 'option');
  outsideOption.textContent = 'Option B (elsewhere on the page)';
  scratch.appendChild(outsideOption);
  expect('isWorkdayOptionSafe REFUSES a role=option element that is NOT inside the given scope (never an arbitrary element)',
    Scanner.isWorkdayOptionSafe(outsideOption, scopeUl) === false);

  const trapOption = doc.createElement('li');
  trapOption.setAttribute('role', 'option');
  trapOption.setAttribute('data-automation-id', 'bottom-navigation-submit-button');
  trapOption.textContent = 'Option C';
  scopeUl.appendChild(trapOption);
  expect('isWorkdayOptionSafe REFUSES an in-scope role=option whose automation id contains "bottom-navigation" (hard deny)',
    Scanner.isWorkdayOptionSafe(trapOption, scopeUl) === false);

  const notOptionShaped = doc.createElement('li');
  notOptionShaped.textContent = 'Not option-shaped';
  scopeUl.appendChild(notOptionShaped);
  expect('isWorkdayOptionSafe REFUSES an in-scope element with no role=option / promptOption / checkboxItem shape',
    Scanner.isWorkdayOptionSafe(notOptionShaped, scopeUl) === false);

  const promptOptionShaped = doc.createElement('div');
  promptOptionShaped.setAttribute('data-automation-id', 'promptOption-3');
  promptOptionShaped.textContent = 'Prompt option';
  scopeUl.appendChild(promptOptionShaped);
  expect('isWorkdayOptionSafe allows a data-automation-id*=promptOption element (role=option not required)',
    Scanner.isWorkdayOptionSafe(promptOptionShaped, scopeUl) === true);
  expect('isWorkdayOptionSafe REFUSES null', Scanner.isWorkdayOptionSafe(null, scopeUl) === false);

  // --- spinner input guard ---
  const dateField = mk('div', { 'data-automation-id': 'formField-guard-date' });
  const wrapper = doc.createElement('div');
  wrapper.setAttribute('data-automation-id', 'dateInputWrapper');
  dateField.appendChild(wrapper);
  const monthGuardIn = doc.createElement('input');
  monthGuardIn.setAttribute('aria-label', 'Month');
  wrapper.appendChild(monthGuardIn);
  const yearGuardIn = doc.createElement('input');
  yearGuardIn.setAttribute('aria-label', 'Year');
  wrapper.appendChild(yearGuardIn);
  expect('isWorkdaySpinnerInputSafe allows input[aria-label=Month] inside dateInputWrapper',
    Scanner.isWorkdaySpinnerInputSafe(monthGuardIn) === true);
  expect('isWorkdaySpinnerInputSafe allows input[aria-label=Year] inside dateInputWrapper',
    Scanner.isWorkdaySpinnerInputSafe(yearGuardIn) === true);

  const outsideMonth = doc.createElement('input');
  outsideMonth.setAttribute('aria-label', 'Month');
  scratch.appendChild(outsideMonth);
  expect('isWorkdaySpinnerInputSafe REFUSES an aria-label=Month input that is NOT inside a dateInputWrapper',
    Scanner.isWorkdaySpinnerInputSafe(outsideMonth) === false);

  const wrongLabel = doc.createElement('input');
  wrongLabel.setAttribute('aria-label', 'Day');
  wrapper.appendChild(wrongLabel);
  expect('isWorkdaySpinnerInputSafe REFUSES aria-label values other than exactly "Month"/"Year"',
    Scanner.isWorkdaySpinnerInputSafe(wrongLabel) === false);

  const trapWrapper = doc.createElement('div');
  trapWrapper.setAttribute('data-automation-id', 'dateInputWrapper');
  dateField.appendChild(trapWrapper);
  const trapMonth = doc.createElement('input');
  trapMonth.setAttribute('aria-label', 'Month');
  trapMonth.setAttribute('data-automation-id', 'wd-save-and-continue');
  trapWrapper.appendChild(trapMonth);
  expect('isWorkdaySpinnerInputSafe REFUSES an aria-label=Month input whose OWN automation id contains "save" (hard deny)',
    Scanner.isWorkdaySpinnerInputSafe(trapMonth) === false);

  monthGuardIn.disabled = true;
  expect('isWorkdaySpinnerInputSafe REFUSES a disabled spinner input', Scanner.isWorkdaySpinnerInputSafe(monthGuardIn) === false);
  monthGuardIn.disabled = false;
  expect('isWorkdaySpinnerInputSafe REFUSES null', Scanner.isWorkdaySpinnerInputSafe(null) === false);

  // --- masked date input guard: only safe when its wrapper holds exactly ONE input ---
  const maskedWrapper = doc.createElement('div');
  maskedWrapper.setAttribute('data-automation-id', 'dateInputWrapper');
  const maskedGuardIn = doc.createElement('input');
  maskedWrapper.appendChild(maskedGuardIn);
  scratch.appendChild(maskedWrapper);
  expect('isWorkdayMaskedDateInputSafe allows the single <input> in an otherwise-empty dateInputWrapper',
    Scanner.isWorkdayMaskedDateInputSafe(maskedGuardIn) === true);
  const extraIn = doc.createElement('input');
  maskedWrapper.appendChild(extraIn);
  expect('isWorkdayMaskedDateInputSafe REFUSES it once the SAME wrapper holds a second <input> (a real Month/Year pair, not a masked fallback)',
    Scanner.isWorkdayMaskedDateInputSafe(maskedGuardIn) === false);
  maskedWrapper.removeChild(extraIn);

  // --- prompt input guard ---
  const promptField = mk('div', { 'data-automation-id': 'formField-guard-prompt' });
  const msContainer = doc.createElement('div');
  msContainer.setAttribute('data-automation-id', 'multiSelectContainer');
  promptField.appendChild(msContainer);
  const promptGuardIn = doc.createElement('input');
  msContainer.appendChild(promptGuardIn);
  expect('isWorkdayPromptInputSafe allows an <input> inside multiSelectContainer inside formField-*',
    Scanner.isWorkdayPromptInputSafe(promptGuardIn) === true);

  const orphanPromptIn = doc.createElement('input');
  scratch.appendChild(orphanPromptIn);
  expect('isWorkdayPromptInputSafe REFUSES an <input> with no multiSelectContainer ancestor',
    Scanner.isWorkdayPromptInputSafe(orphanPromptIn) === false);

  scratch.remove();
})();

// --- matchChoiceOption: shared choice/option matcher (decline / yes-no / gender / veteran /
//     disability / race answer families, plus word-boundary containment) -------------------
// Pure-function checks, no DOM needed -- exercises the exact matcher shared by findOptionMatch
// (native <select>), setRadioValue (radio labels), and matchWorkdayDropdownOption (Workday's
// custom dropdown). The bug this replaces: the old final tier was a raw substring "contains
// either direction" check, which picked "Female" for value "Male" because
// "female".indexOf("male") !== -1 -- see the project brief.
(() => {
  const mco = Scanner.matchChoiceOption;

  // -- decline family: every real-world option phrasing from the bug report must resolve -----
  const declineOptions = [
    "I don't wish to answer",
    'I do not want to answer',
    'Decline To Self Identify',
    'I DO NOT WISH TO SELF-IDENTIFY',
    'Not Declared',
    'Prefer not to say',
    'I do not wish to answer. (United States of America)'
  ];
  declineOptions.forEach((opt) => {
    expect('decline family: "Decline to self-identify" matches option "' + opt + '"',
      mco('Decline to self-identify', [opt]) === 0);
  });
  expect('decline family: recognised value with ONLY [Yes, No] options finds nothing (-1), never guesses Yes/No',
    mco('Decline to self-identify', ['Yes', 'No']) === -1);

  // -- gender family: the original reported bug, directly -------------------------------------
  expect('the original bug is fixed: "Male" never matches "Female" via raw substring ("female".indexOf("male") !== -1)',
    mco('Male', ['Female']) === -1);
  expect('gender family: "Male" with [Female, Man, Woman] picks "Man"',
    mco('Male', ['Female', 'Man', 'Woman']) === 1);
  expect('gender family: "Male" with [Female, Other] (no fitting option) is -1, never falls through to fuzzy containment',
    mco('Male', ['Female', 'Other']) === -1);
  expect('gender family: "Female" with [Male, Female] picks "Female"',
    mco('Female', ['Male', 'Female']) === 1);
  expect('gender family: "Non-binary" matches an option reading "Non-Binary"',
    mco('Non-binary', ['Male', 'Female', 'Non-Binary']) === 2);
  expect('gender family: bare "Man" (via generic word-boundary containment, not family dispatch) picks "Man (he/him)", never "Woman"',
    mco('Man', ['Woman (she/her)', 'Man (he/him)', 'Non-binary (they/them)']) === 1);

  // -- veteran family: four distinct known phrasings, each its own fallback chain -------------
  expect('veteran family: "I am not a veteran" exact-matches a Workday-style option set',
    mco('I am not a veteran', [
      'I am not a veteran',
      'I am a veteran but not a protected veteran',
      'I identify as one or more of the classifications of protected veteran listed above',
      'I do not wish to self-identify'
    ]) === 0);
  expect('veteran family: "I am not a veteran" with NO exact option falls back to "not a protected veteran" on a Greenhouse-style set',
    mco('I am not a veteran', [
      'I identify as one or more of the classifications of protected veteran',
      'I am not a protected veteran',
      "I don't wish to answer"
    ]) === 1);
  expect('veteran family: "I am a veteran, but not a protected veteran" also picks "I am not a protected veteran"',
    mco('I am a veteran, but not a protected veteran', [
      'I identify as one or more of the classifications of protected veteran',
      'I am not a protected veteran',
      "I don't wish to answer"
    ]) === 1);
  expect('veteran family: "I am a protected veteran" picks the no-negation option, never the "not a protected veteran" one',
    mco('I am a protected veteran', [
      'I identify as one or more of the classifications of protected veteran',
      'I am not a protected veteran',
      "I don't wish to answer"
    ]) === 0);
  expect('veteran family: legacy "I identify as one or more of the classifications of protected veteran" also uses the no-negation rule',
    mco('I identify as one or more of the classifications of protected veteran', [
      "I don't wish to answer",
      'I am not a protected veteran',
      'I identify as one or more of the classifications of protected veteran listed above'
    ]) === 2);
  expect('veteran family: legacy "I am not a protected veteran" is ambiguous with no exact/phrase match -- refuses to guess (-1)',
    mco('I am not a protected veteran', [
      'I am not a veteran',
      'I identify as one or more of the classifications of protected veteran listed above',
      'I do not wish to self-identify'
    ]) === -1);

  // -- disability family ------------------------------------------------------------------------
  const disabilityOptions = [
    'Yes, I have a disability, or have had one in the past',
    'No, I do not have a disability and have not had one in the past',
    'I do not want to answer'
  ];
  expect('disability family: "No, I do not have a disability" picks the No option, not the decline-ish "I do not want to answer"',
    mco('No, I do not have a disability', disabilityOptions) === 1);
  expect('disability family: "Yes, I have a disability (or had one in the past)" picks the Yes option',
    mco('Yes, I have a disability (or had one in the past)', disabilityOptions) === 0);

  // -- race / ethnicity family: anchored-start match, never cross-matches another race ---------
  const raceOptions = [
    'Hispanic or Latino (United States of America)',
    'Asian (United States of America)',
    'White (United States of America)'
  ];
  expect('race family: "Asian" picks "Asian (United States of America)"', mco('Asian', raceOptions) === 1);
  expect('race family: "Asian" never picks "Hispanic or Latino"', mco('Asian', ['Hispanic or Latino']) === -1);
  expect('race family: "Hispanic or Latino" never picks "Asian"', mco('Hispanic or Latino', ['Asian (United States of America)']) === -1);
  expect('race family: "Black or African American" anchored-start match with a suffix',
    mco('Black or African American', ['Black or African American (United States of America)']) === 0);

  // -- plain yes/no family ----------------------------------------------------------------------
  expect('yes/no family: "No" with an exact "No" option present picks it directly',
    mco('No', ['Not Declared', 'None of the above', 'No']) === 2);
  expect('yes/no family: "No" with no exact/prefix match among decline-ish options is -1, never picks "Not Declared"',
    mco('No', ['Not Declared', 'None of the above']) === -1);

  // -- generic word-boundary containment tier (non-family, non-prose values) -------------------
  expect('generic containment: forward direction still matches a plain non-family value on a word boundary',
    mco('Senior', ['Junior (0-2 years)', 'Senior (6+ years)']) === 1);
  expect('generic containment: reverse direction (value contains option) works for options >= 4 chars',
    mco('Bachelor of Science in Computer Science', ['Computer Science']) === 0);
  expect('generic containment: reverse direction is refused for options under 4 chars (no coincidental short-option match)',
    mco('Not sure', ['No']) === -1);

  // -- prose guard: a long free-text answer is never matched by containment, only by exact text.
  //    Without this, a rambling sentence can coincidentally contain an option as a raw
  //    substring OR as a genuine whole word -- neither means the user picked that option.
  expect('prose guard: a long sentence containing "know" (which contains "no") never matches a "No" option',
    mco('I know Figma deeply and use it daily', ['Yes', 'No']) === -1);
  expect('prose guard: a long sentence never matches a "Yes"/"No" option even when unrelated to either',
    mco('I know relocation can be hard, but I am open to it', ['Yes', 'No']) === -1);
  expect('prose guard: closes the gap the >= 4 char reverse-containment rule alone would miss (a real "Open" option, coincidentally a whole word in an unrelated sentence)',
    mco('I know relocation can be hard, but I am open to it', ['Open']) === -1);
  expect('prose guard: does not affect a short (<= 6 word), non-family, non-prose value',
    mco('Bachelor of Science in Computer Science', ['Computer Science']) === 0);
  expect('prose guard: does not affect a recognised answer family even though the value itself is long (9 words)',
    mco('I am a veteran, but not a protected veteran', [
      'I identify as one or more of the classifications of protected veteran',
      'I am not a protected veteran',
      "I don't wish to answer"
    ]) === 1);
})();

// --- Workday dropdown value matching: gender + a decline-only negative control, plus the
//     designer-degree family synonyms added alongside the EEO matcher work above. Exercises
//     matchWorkdayDropdownOption directly (pure function, no popup needed) since the
//     open-popup/click plumbing is already covered by the existing async dropdown block below.
(() => {
  const mwdo = Scanner.matchWorkdayDropdownOption;
  expect('Workday dropdown: "Male" with [Female, Male] picks the Male option',
    mwdo('Male', ['Female (she/her)', 'Male (he/him)']) === 1);
  expect('Workday dropdown: "Male" with [Female, Decline] (no fitting option) is -1, never guesses',
    mwdo('Male', ['Female (she/her)', 'Decline']) === -1);

  expect('Workday degree family: "B.Des" normalises to the bachelor family',
    Scanner.degreeFamilyOf('B.Des') === 'bachelor');
  expect('Workday degree family: "Master of Design" normalises to the master family',
    Scanner.degreeFamilyOf('Master of Design') === 'master');
  expect('Workday degree family: "BFA" normalises to the bachelor family',
    Scanner.degreeFamilyOf('BFA') === 'bachelor');
  expect('Workday degree family: "Master of Human-Computer Interaction" normalises to the master family (hyphen-tolerant)',
    Scanner.degreeFamilyOf('Master of Human-Computer Interaction') === 'master');
})();

// --- end-to-end: setSelectValue / setRadioValue through the real fill path, via the hidden
//     EEO fixtures on test-page.html (see the comment there). Also a negative control where the
//     only options are Yes/No and the field must stay completely untouched.
(() => {
  const doc = dom.window.document;

  const genderSelect = doc.getElementById('eeo_gender_select_fixture');
  const genderOk = Scanner.setSelectValue(genderSelect, 'Decline to self-identify');
  expect('end-to-end: setSelectValue resolves "Decline to self-identify" on a native <select> via the decline family',
    genderOk === true && genderSelect.options[genderSelect.selectedIndex].textContent === 'I do not wish to answer');

  const hispanicRadios = Array.from(doc.querySelectorAll('input[name="eeo_hispanic_fixture"]'));
  const hispanicOk = Scanner.setRadioValue(hispanicRadios, 'Decline to self-identify');
  const hispanicChecked = hispanicRadios.filter((r) => r.checked);
  expect('end-to-end: setRadioValue resolves "Decline to self-identify" on a radio group via the decline family',
    hispanicOk === true && hispanicChecked.length === 1 && hispanicChecked[0].value === 'decline');

  // Negative control: ONLY Yes/No options -- a decline-shaped value must find no confident
  // match and must leave the field COMPLETELY untouched (no radio checked at all).
  const negControlRadios = Array.from(doc.querySelectorAll('input[name="eeo_negative_control_fixture"]'));
  const negControlOk = Scanner.setRadioValue(negControlRadios, 'Decline to self-identify');
  const negControlChecked = negControlRadios.filter((r) => r.checked);
  expect('negative control: setRadioValue with only [Yes, No] options returns false for a decline value',
    negControlOk === false);
  expect('negative control: setRadioValue leaves the Yes/No field completely untouched (nothing checked)',
    negControlChecked.length === 0);

  // Negative control: a prose-shaped answer to a Yes/No radio group must also leave it
  // completely untouched (the prose guard, exercised through the real fill path).
  const negControlOkProse = Scanner.setRadioValue(negControlRadios, 'I know relocation can be hard, but I am open to it');
  const negControlCheckedProse = negControlRadios.filter((r) => r.checked);
  expect('negative control: setRadioValue with a prose-shaped value returns false on a Yes/No field',
    negControlOkProse === false);
  expect('negative control: a prose-shaped value leaves the Yes/No field completely untouched (nothing checked)',
    negControlCheckedProse.length === 0);
})();

// --- Workday dropdown + prompt + résumé, run STRICTLY SEQUENTIALLY -----------------------
// One combined async block, each step awaited before the next starts — deliberately mirroring
// how content.js's applyFills() now processes real fills (one field at a time, never
// concurrently; see content.js). Running these as independent, concurrently-interleaving
// pending promises is not just unrealistic, it is actively wrong for this suite: the dropdown
// and prompt widgets share the SAME document-wide `[role="option"]`/promptOption fallback
// query (see fillWorkdayDropdown/fillWorkdayPromptTerm's waitFor() in scanner.js), so two of
// these tests genuinely racing would let one widget's popup answer another's query.
pending.push((async () => {
  const doc = dom.window.document;

  // ---- dropdown: degree/country synonyms, no-blind-first-option, Escape-to-close ----
  {
    const scanned = Scanner.scanFields(doc);
    const degreeBtn = doc.getElementById('wd_degree_button');
    const degreeEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-dropdown' && e.button === degreeBtn);
    expect('found the Degree wd-dropdown registry entry', !!degreeEntry);

    const ok = await Scanner.applyFill(degreeEntry, 'MS');
    expect('applyFill on a wd-dropdown resolves to a boolean', typeof ok === 'boolean');
    expect('Degree dropdown filled by synonym "MS" resolves via the master-degree family to "Masters Degree or Equivalent"',
      ok === true && doc.getElementById('wd_degree_button_text').textContent === 'Masters Degree or Equivalent');

    const okBE = await Scanner.applyFill(degreeEntry, 'B.E.');
    expect('Degree dropdown filled by synonym "B.E." (dots normalised away) resolves via the bachelor-degree family',
      okBE === true && doc.getElementById('wd_degree_button_text').textContent === 'Bachelors Degree or Equivalent');

    // Design/creative-field degree synonyms (the primary user is a product/UX designer).
    const okBDes = await Scanner.applyFill(degreeEntry, 'B.Des');
    expect('Degree dropdown filled by design synonym "B.Des" resolves via the bachelor-degree family',
      okBDes === true && doc.getElementById('wd_degree_button_text').textContent === 'Bachelors Degree or Equivalent');

    const okMDes = await Scanner.applyFill(degreeEntry, 'Master of Design');
    expect('Degree dropdown filled by design synonym "Master of Design" resolves via the master-degree family',
      okMDes === true && doc.getElementById('wd_degree_button_text').textContent === 'Masters Degree or Equivalent');

    // Negative control: re-filling with the bachelor-family synonym afterwards must land back
    // on Bachelors, proving "B.Des" never resolves to the Masters option it was just showing.
    const okBDesAgain = await Scanner.applyFill(degreeEntry, 'B.Des');
    expect('"B.Des" never picks "Masters Degree or Equivalent" -- filling it after Master of Design lands back on Bachelors',
      okBDesAgain === true && doc.getElementById('wd_degree_button_text').textContent === 'Bachelors Degree or Equivalent');

    const okBad = await Scanner.applyFill(degreeEntry, 'Xyzzy Nonexistent Degree');
    expect('a Degree value with no matching option (exact, family, or contains) selects NOTHING — never falls back to the first option',
      okBad === false && doc.getElementById('wd_degree_button_text').textContent === 'Bachelors Degree or Equivalent');
    expect('the dropdown popup is closed (Escape) after a failed match, not left open',
      doc.getElementById('wd_degree_listbox').style.display !== 'block');

    const countryBtn = doc.getElementById('wd_country_button');
    const countryEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-dropdown' && e.button === countryBtn);
    const okCountry = await Scanner.applyFill(countryEntry, 'USA');
    expect('Country dropdown filled by alias "USA" resolves to "United States of America"',
      okCountry === true && doc.getElementById('wd_country_button_text').textContent === 'United States of America');
  }

  // ---- prompt: exact/acronym/startsWith/substring matching, no-blind-first-result,
  //      the Skills LIST (skip-and-continue on a per-term basis) ----
  {
    const scanned = Scanner.scanFields(doc);
    const fosInput = doc.getElementById('wd_fos_input');
    const fosEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-prompt' && e.input === fosInput);
    expect('found the Field of Study wd-prompt registry entry', !!fosEntry);
    const fosSelected = () => Array.from(doc.querySelectorAll('#wd_fos_selected li')).map(li => li.textContent);

    const ok = await Scanner.applyFill(fosEntry, 'Computer Science');
    expect('Field of Study: exact match "Computer Science" is added to the selected item list',
      ok === true && fosSelected().includes('Computer Science'));
    expect('the prompt input is cleared after a successful add', fosInput.value === '');

    const okBad = await Scanner.applyFill(fosEntry, 'Zoology');
    expect('Field of Study: a term with NO confident match among the (non-empty) results selects NOTHING',
      okBad === false && fosSelected().length === 1 && !fosSelected().includes('Zoology'));
    expect('the typed text is cleared (not left sitting in the field) after no confident match', fosInput.value === '');

    const skillsInput = doc.getElementById('wd_skills_input');
    const skillsEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-prompt' && e.input === skillsInput);
    const skillsSelected = () => Array.from(doc.querySelectorAll('#wd_skills_selected li')).map(li => li.textContent);
    const okSkills = await Scanner.applyFill(skillsEntry, ['SQL', 'Excel', 'Python']);
    expect('Skills LIST: "SQL" matches via the acronym form "(SQL)" in "Structured Query Language (SQL)"',
      skillsSelected().includes('Structured Query Language (SQL)'));
    expect('Skills LIST: "Excel" (no matching option) is skipped, never force-matched to anything',
      !skillsSelected().some(t => /excel/i.test(t)));
    expect('Skills LIST: "Python" (exact match) still gets added after the earlier skip (skip-and-continue, not stop-on-first-failure)',
      skillsSelected().includes('Python'));
    expect('Skills LIST applyFill reports overall success once at least one skill matched', okSkills === true);
    expect('Skills rows use checkboxItem automation ids, clicked via the checkbox itself',
      doc.querySelectorAll('#wd_skills_results [data-automation-id^="checkboxItem-"]').length > 0);
  }

  // ---- résumé: duplicate prevention (own isolated JSDOM -- no interference with the shared
  //      test-page.html document, same discipline as the other isolated résumé tests above) ----
  {
    const freshDom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
    freshDom.window.HTMLElement.prototype.getBoundingClientRect = function () {
      return { width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10 };
    };
    const fdoc = freshDom.window.document;

    const container = fdoc.createElement('div');
    container.setAttribute('data-automation-id', 'formField-wd-resume');
    const input = fdoc.createElement('input');
    input.type = 'file';
    input.id = 'dup_resume_input';
    container.appendChild(input);
    fdoc.body.appendChild(container);

    expect('findWorkdayUploadedFilename() finds nothing before any upload', Scanner.findWorkdayUploadedFilename(fdoc) === '');

    const file = new freshDom.window.File(['hello'], 'my-resume.pdf', { type: 'application/pdf' });
    const before = await Scanner.attachResumeFile(fdoc, file);
    expect('attachResumeFile() with no existing upload does not report alreadyAttached (still fails soft — jsdom has no DataTransfer)',
      !!before && !before.alreadyAttached);

    // Simulate Workday's own UI already showing a file (as if a previous attach succeeded).
    const item = fdoc.createElement('div');
    item.setAttribute('data-automation-id', 'file-upload-item');
    const nameEl = fdoc.createElement('div');
    nameEl.setAttribute('data-automation-id', 'file-upload-item-name');
    nameEl.textContent = 'already-attached.pdf';
    item.appendChild(nameEl);
    fdoc.body.appendChild(item);

    expect('findWorkdayUploadedFilename() now finds the already-attached filename',
      Scanner.findWorkdayUploadedFilename(fdoc) === 'already-attached.pdf');

    const dup = await Scanner.attachResumeFile(fdoc, file);
    expect('attachResumeFile() refuses to attach again once a résumé is already shown as attached (duplicate prevention — uploading twice is worse than not uploading)',
      dup && dup.attempted === true && dup.attached === false && dup.alreadyAttached === true);
    expect('the duplicate-prevention reason names the already-attached file',
      dup && /already-attached\.pdf/.test(dup.reason || ''));
  }

  // ---- résumé: the MutationObserver-driven upload-confirmation wait ----
  // The part of the résumé fix jsdom genuinely cannot exercise end-to-end via
  // attachResumeFile() itself (no DataTransfer at all — see the pre-existing "fail-soft guard"
  // test above), so it is tested directly against the exported wait/detection helpers, which
  // only ever READ the DOM and don't care how a file-upload-item-name element got there.
  {
    const freshDom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
    const fdoc = freshDom.window.document;

    const neverAppears = await Scanner.waitForWorkdayUploadSuccess(fdoc, 'resume.pdf', 150);
    expect('waitForWorkdayUploadSuccess() resolves false after its own timeout when nothing ever appears', neverAppears === false);

    setTimeout(() => {
      const nameEl = fdoc.createElement('div');
      nameEl.setAttribute('data-automation-id', 'file-upload-item-name');
      nameEl.textContent = 'resume.pdf (204 KB)';
      fdoc.body.appendChild(nameEl);
    }, 40);
    const found = await Scanner.waitForWorkdayUploadSuccess(fdoc, 'resume.pdf', 2000);
    expect('waitForWorkdayUploadSuccess() resolves true once its MutationObserver sees a matching file-upload-item-name appear (a 40ms-delayed append, well inside its budget)',
      found === true);
  }
  {
    const freshDom = new JSDOM('<!DOCTYPE html><body><div data-automation-id="file-upload-successful"></div></body>', { pretendToBeVisual: true });
    const fdoc = freshDom.window.document;
    const found = await Scanner.waitForWorkdayUploadSuccess(fdoc, 'anything.pdf', 100);
    expect('workdayUploadIndicatesSuccess() also short-circuits true via the explicit file-upload-successful marker alone', found === true);
  }

  // ---- decoy submit button: a REAL trap, and proof nothing above ever reached it ----
  // Runs last, in the same sequential chain, so this checks the shared document's submit
  // counter only AFTER every dropdown/prompt interaction above has fully settled.
  {
    expect('none of the Workday widget fills above ever triggered the real Workday submit button',
      !dom.window.__WD_SUBMIT_COUNT__);

    const wdSubmitBtn = doc.getElementById('wd_submit_btn');
    expect('the Workday decoy submit button is a genuine submit control with a form owner',
      wdSubmitBtn.type === 'submit' && !!wdSubmitBtn.form && wdSubmitBtn.form.id === 'wd-form');
    expect('isWorkdayDropdownOpenerSafe REFUSES it (no aria-haspopup=listbox at all)',
      Scanner.isWorkdayDropdownOpenerSafe(wdSubmitBtn) === false);
    expect('isAddAnotherButtonSafe ALSO refuses it (its text does not read as an add action)',
      Scanner.isAddAnotherButtonSafe(wdSubmitBtn) === false);

    // Bypassing every guard and clicking it directly (simulating "a bug let this through") DOES
    // submit — proving it is a real hazard, exactly like the pre-existing trap-form fixture.
    wdSubmitBtn.click();
    expect('bypassing every guard and clicking the Workday decoy submit button directly DOES fire its form\'s submit handler',
      dom.window.__WD_SUBMIT_COUNT__ === 1);
  }
})());

Promise.all(pending).then(() => {
  let failed = 0;
  console.log('\n--- checks ---');
  for (const c of checks) {
    console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.name}`);
    if (!c.pass) failed++;
  }
  console.log(`\n${checks.length - failed}/${checks.length} checks passed.`);
  process.exit(failed ? 1 : 0);
}, (e) => {
  console.error('An async check threw:', e);
  process.exit(1);
});
