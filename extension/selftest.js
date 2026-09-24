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

  // Remove every remaining file input to test the "nothing to attach to" case.
  doc.getElementById('attach_upload').remove();
  doc.getElementById('cover_letter_upload').remove();
  expect('no eligible file input on the page -> null target', Scanner.findResumeFileTarget(doc) === null);

  const noTargetResult = Scanner.attachResumeFile(doc, new dom.window.File(['x'], 'resume.pdf', { type: 'application/pdf' }));
  expect('attachResumeFile() reports attempted:false when there is nothing to attach to',
    noTargetResult && noTargetResult.attempted === false);
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
  let result;
  try {
    result = Scanner.attachResumeFile(doc, file);
  } catch (e) {
    threw = true;
  }
  expect('attachResumeFile() never throws even when DataTransfer is unavailable', threw === false);
  expect('attachResumeFile() reports a clear, non-silent failure when DataTransfer is unavailable (jsdom has none)',
    !!result && result.attempted === true && result.attached === false && /DataTransfer/.test(result.reason || ''));
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

let failed = 0;
console.log('\n--- checks ---');
for (const c of checks) {
  console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.name}`);
  if (!c.pass) failed++;
}
console.log(`\n${checks.length - failed}/${checks.length} checks passed.`);
process.exit(failed ? 1 : 0);
