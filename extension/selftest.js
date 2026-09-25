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

// capture.js leans on ApplyPilotScanner (getLabel/isVisible/findOpenShadowRoots) exactly like
// content.js loads them together in a real page, so it must be eval'd into the SAME window
// after scanner.js, not tested standalone.
const captureSrc = fs.readFileSync(path.join(__dirname, 'capture.js'), 'utf8');
dom.window.eval(captureSrc);
const Capture = dom.window.ApplyPilotCapture;
if (!Capture) {
  console.error('capture.js did not attach ApplyPilotCapture to the window — aborting.');
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
  // Reviewer round 5, blocker A: this is the EXACT "State/Province" shape the reviewer's own
  // chrome_panel_test.py output already showed reported "Didn't stick" -- a value-attribute
  // select ("1") given a plain profile answer ("Arizona") that can only ever compare equal
  // against the OPTION'S OWN VISIBLE TEXT, never the opaque value attribute a page author chose.
  expect('blocker A: getCurrentValue() returns the option\'s visible TEXT ("Arizona"), never its raw value attribute ("1")',
    Scanner.getCurrentValue(s2Entry) === 'Arizona');
  expect('blocker A: entry._committedText stashes that SAME visible text at commit time',
    s2Entry._committedText === 'Arizona');

  s2Entry.el.selectedIndex = 0; // reset to the placeholder before the next fill
  ok = Scanner.applyFill(s2Entry, 'AZ');
  expect('select2 select filled by 2-letter CODE, applyFill reports success', ok === true);
  expect('select2 select value read back after fill-by-code cross-matched to "Arizona" (value="1")',
    s2Entry.el.value === '1');
  expect('blocker A: getCurrentValue()/entry._committedText agree on "Arizona" even when the SERVICE\'s own fill value was the differently-worded "AZ"',
    Scanner.getCurrentValue(s2Entry) === 'Arizona' && s2Entry._committedText === 'Arizona');

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

  // Every Workday date-part input on the whole page, across every fixture (My, Y, masked, MDY,
  // async, yearclear, hidden, localized) -- none of these may ALSO turn up as a plain 'element'
  // field. This caught a real bug during development: dayEl (Self-Identify's third part) was
  // never added to the consumed-elements set, so it was scanned a second time as an ordinary
  // text field alongside being part of the merged wd-date-mdy field.
  const allWdDatePartIds = [
    'wd_start_month', 'wd_start_year', 'wd_edu_from_year', 'wd_cert_masked',
    'wd_async_month', 'wd_async_year', 'wd_yearclear_month', 'wd_yearclear_year',
    'wd_hidden_month', 'wd_hidden_year', 'wd_selfid_month', 'wd_selfid_day', 'wd_selfid_year',
    'wd_monat_month', 'wd_monat_year'
  ];
  const allWdDatePartEls = allWdDatePartIds.map(id => doc.getElementById(id));
  const strayWd = scanned.fields.filter(f => {
    const e = scanned.registry[f.id];
    return e && e.kind === 'element' && allWdDatePartEls.indexOf(e.el) !== -1;
  });
  expect('Workday spinner/masked date inputs (every fixture, including MDY\'s Day) are never ALSO scanned as ordinary text fields',
    strayWd.length === 0);

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

  // --- Month+Day+Year (Self-Identify "Date") ------------------------------------------------
  const selfidMonth = doc.getElementById('wd_selfid_month');
  const selfidDay = doc.getElementById('wd_selfid_day');
  const selfidYear = doc.getElementById('wd_selfid_year');
  const mdyField = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'wd-date-mdy'
    && scanned.registry[f.id].dayEl === selfidDay);
  expect('Workday Month+Day+Year dateInputWrapper is scanned as ONE field with widget "wd-date-mdy" (never mistaken for Month+Year)',
    !!mdyField && mdyField.widget === 'wd-date-mdy');
  const mdyEntry = mdyField && scanned.registry[mdyField.id];
  expect('the wd-date-mdy registry entry carries month/day/year elements',
    mdyEntry && mdyEntry.monthEl === selfidMonth && mdyEntry.dayEl === selfidDay && mdyEntry.yearEl === selfidYear);

  // --- a localized tenant (aria-label="Monat"/"Jahr") is STILL detected, via data-automation-id ---
  const monatField = scanned.fields.find(f => scanned.registry[f.id] && scanned.registry[f.id].kind === 'wd-date-my'
    && scanned.registry[f.id].monthEl === doc.getElementById('wd_monat_month'));
  expect('a localized aria-label ("Monat") does not block detection -- data-automation-id is the locale-independent hook',
    !!monatField);

  // --- Self-Identify disability CheckboxGroup (CC-305) --------------------------------------
  // Disambiguated by widget "wd-checkbox-group", not just type "checkbox-group" -- the choice-
  // widget driver (Greenhouse/Ashby/Lever, see the "ats-widgets-form" fixtures) legitimately
  // emits the SAME type string for its own, unrelated checkbox groups; only `widget` tells
  // this one apart as Workday's.
  const cgField = scanned.fields.find(f => f.widget === 'wd-checkbox-group');
  expect('the disabilityStatus CheckboxGroup is scanned as ONE field with type "checkbox-group"', !!cgField);
  expect('...labelled from its own <legend>', cgField && cgField.label === 'Please check one of the boxes below:');
  expect('...options are the three CC-305 option texts, in document order', cgField && JSON.stringify(cgField.options) === JSON.stringify([
    'Yes, I have a disability, or have had one in the past',
    'No, I do not have a disability and have not had one in the past',
    'I do not want to answer'
  ]));
  const cgEntry = cgField && scanned.registry[cgField.id];
  const strayDisabilityBoxes = scanned.fields.filter(f => {
    const e = scanned.registry[f.id];
    return e && e.kind === 'element' && cgEntry && cgEntry.boxes.indexOf(e.el) !== -1;
  });
  expect('the group\'s own checkboxes are never ALSO scanned as independent boolean fields', strayDisabilityBoxes.length === 0);

  // --- the terms/consent checkbox is NEVER offered as a fillable field at all ---------------
  const agreementCb = doc.getElementById('wd_agreement_checkbox');
  const agreementScanned = scanned.fields.some(f => scanned.registry[f.id] && scanned.registry[f.id].el === agreementCb);
  expect('agreementCheckbox (terms/consent) is never scanned as a fillable field, not even a plain checkbox (hard-deny)',
    agreementScanned === false);
  expect('hasWorkdayHardDenyAutomationId recognises agreementCheckbox directly',
    Scanner.hasWorkdayHardDenyAutomationId(agreementCb) === true);
})();

// --- spinner dates: the ArrowUp technique, its retry-once path, and the masked fallback ----
// setWorkdaySpinnerValue()/setWorkdayDateValue() are now ASYNCHRONOUS (a real yield is exactly
// the fix for the "hangs on dates" report -- see the project brief), so applyFill() on any
// wd-date-* entry now returns a Promise instead of a plain boolean. All date-value testing
// below therefore lives in the sequential async block further down, alongside the
// dropdown/prompt/résumé tests it already had to run strictly sequentially for the same reason.
(() => {
  const doc = dom.window.document;
  const scanned = Scanner.scanFields(doc);
  const monthEl = doc.getElementById('wd_start_month');

  // Prove the mock is genuinely adversarial: the WRONG (previously-shipped) technique —
  // plain value + input/change — really is ignored, not just assumed to be. This part alone
  // stays synchronous: the mock's revert-on-input/change listener fires (and un-fires) within
  // the same tick, well before any real spinner value could ever legitimately change.
  monthEl.value = '07';
  monthEl.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  monthEl.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  expect('the mock Workday spinner genuinely IGNORES plain value+input/change (proves the mock, not just the fix)',
    monthEl.value !== '07');
  expect('...and the "-display" div never moved off its placeholder either (truth lives only in -display/aria-valuetext)',
    doc.getElementById('wd_start_month-display').textContent === 'MM');

  // An invalid value short-circuits with no DOM side effects at all (see setWorkdayDateValue),
  // so this proves the Promise-returning contract without touching the shared document's state
  // ahead of the sequential async block below.
  const myEntryForShapeCheck = Object.values(scanned.registry).find(e => e.kind === 'wd-date-my' && e.monthEl === monthEl);
  expect('setWorkdaySpinnerValue/setWorkdayDateValue now return a Promise (asynchronous, per the project brief)',
    typeof Scanner.setWorkdayDateValue(myEntryForShapeCheck, 'not-a-date').then === 'function');
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

  // Day is now a RECOGNISED part (Self-Identify and similar Month+Day+Year fields) -- the v6
  // guard refused it outright, which is exactly the bug that would have silently dropped the
  // Day part of an MDY date.
  const dayGuardIn = doc.createElement('input');
  dayGuardIn.setAttribute('aria-label', 'Day');
  wrapper.appendChild(dayGuardIn);
  expect('isWorkdaySpinnerInputSafe allows input[aria-label=Day] inside dateInputWrapper (MDY support)',
    Scanner.isWorkdaySpinnerInputSafe(dayGuardIn) === true);

  const wrongLabel = doc.createElement('input');
  wrongLabel.setAttribute('aria-label', 'Duration');
  wrapper.appendChild(wrongLabel);
  expect('isWorkdaySpinnerInputSafe REFUSES an aria-label/automation-id that names no recognised date part at all',
    Scanner.isWorkdaySpinnerInputSafe(wrongLabel) === false);

  const monatIn = doc.createElement('input');
  monatIn.setAttribute('aria-label', 'Monat'); // localized -- no English aria-label at all
  monatIn.setAttribute('data-automation-id', 'dateSectionMonth-input');
  wrapper.appendChild(monatIn);
  expect('isWorkdaySpinnerInputSafe allows a localized aria-label when data-automation-id names the part (locale-independent hook)',
    Scanner.isWorkdaySpinnerInputSafe(monatIn) === true);

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

  // -- canary-tier Yes/No: reviewer round 5, blocker B (false citizenship claim) --------------
  // A bare "Yes"/"No" guessed BLIND by the service's canary tier (before a combobox/Workday
  // dropdown ever reveals its real options) must land ONLY on an option that is ITSELF nothing
  // but that one word -- never one that bundles a further claim. The live repro: a visa holder's
  // canary "Yes" got committed to "Yes, I am a U.S. citizen or permanent resident" simply
  // because it was the ONLY option starting with "Yes" (never ambiguous by COUNT, so the
  // ordinary loose rule below never refused it). The 4th argument (`canaryStrict`) is what
  // content.js now threads through only for a fill whose `source` is "canary" -- see
  // matchAnswerFamily's own doc comment in scanner.js.
  const CITIZEN_OPTIONS = ['Yes, I am a U.S. citizen or permanent resident', 'No, I will require sponsorship'];
  expect('canary Yes/No: a NON-canary "Yes" keeps today\'s looser behaviour unchanged (regression guard) -- picks the only "Yes, ..." option',
    mco('Yes', CITIZEN_OPTIONS) === 0);
  expect('blocker B: a CANARY "Yes" against the SAME options refuses -- the only "Yes, ..." option bundles a citizenship claim, not a bare "Yes"',
    mco('Yes', CITIZEN_OPTIONS, true) === -1);
  expect('blocker B: a CANARY "No" is refused too, symmetrically, when the only "No" option also bundles a claim',
    mco('No', ['Yes, I am authorized without sponsorship', 'No, I will require sponsorship'], true) === -1);
  expect('blocker B: a CANARY "Yes" still matches a genuinely BARE "Yes" option -- never over-refuses',
    mco('Yes', ['Yes', 'No'], true) === 0);
  expect('blocker B: a CANARY "No" still matches a genuinely BARE "No" option',
    mco('No', ['Yes', 'No'], true) === 1);
  expect('blocker B: bare-word matching ignores trailing punctuation/whitespace ("Yes.", "  No  ")',
    mco('Yes', ['Yes.', '  No  '], true) === 0 && mco('No', ['Yes.', '  No  '], true) === 1);
  expect('blocker B: a CANARY "No" against a sentence-shaped denial ("I have never worked here") also refuses -- that non-canary fallback never applies under canaryStrict',
    mco('No', ['I currently work here', 'I have never worked here'], true) === -1);

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

  // Reviewer round 3: the degree-family step must apply the SAME "exactly one" rule as every
  // other matchChoiceOption tier -- it must never pick the FIRST family member when a tenant
  // lists several SPECIFIC degree titles (a live gap: "Bachelor of Design" was picking
  // "Bachelor of Arts", and "MS"/"MA" were picking "Master of Arts" -- a false degree claim).
  const SPECIFIC_BACHELOR_TITLES = ['Bachelor of Arts', 'Bachelor of Design (BDes)', 'Bachelor of Science'];
  expect('Workday degree family, SEVERAL specific titles: "Bachelor of Design" still resolves to its OWN specific title, never the first ("Bachelor of Arts")',
    mwdo('Bachelor of Design', SPECIFIC_BACHELOR_TITLES) === 1);
  expect('matchChoiceOption has the SAME fix (shared matchDegreeFamily helper, not just the Workday-specific path)',
    Scanner.matchChoiceOption('Bachelor of Design', SPECIFIC_BACHELOR_TITLES) === 1);

  // An acronym is matched as the WHOLE title it abbreviates ("MS" = "Master of Science"), never
  // as letters inside another title ("ma" begins every "Master ..." title) -- so each resolves to
  // its own title, and one no listed title names still resolves to nothing.
  const SPECIFIC_MASTER_TITLES = ['Master of Arts', 'Master of Science', 'Master of Business Administration'];
  expect('Workday degree family, SEVERAL specific titles: a bare "MS" acronym resolves to "Master of Science", never the first ("Master of Arts")',
    mwdo('MS', SPECIFIC_MASTER_TITLES) === 1);
  expect('Workday degree family, SEVERAL specific titles: a bare "MA" acronym resolves to "Master of Arts"',
    mwdo('MA', SPECIFIC_MASTER_TITLES) === 0);
  expect('Workday degree family, SEVERAL specific titles: "M.Eng" (no listed title names it, no generic level listed) is -1, never the first',
    mwdo('M.Eng', SPECIFIC_MASTER_TITLES) === -1);

  // Greenhouse's standard "Degree" list, verbatim from job-boards.greenhouse.io/twitch
  // (2026-09-25). An operator's "M.S." was left unfilled against it: two options belong to the
  // master family ("Master of Business Administration (M.B.A.)", "Master's Degree") and neither
  // contains "ms". When no listed title names the applicant's own degree, the generic level
  // option is the one true answer; a DIFFERENT specific title (the M.B.A.) never is.
  const GH_DEGREES = ["Associate's Degree", "Bachelor's Degree", 'Doctor of Medicine (M.D.)',
    'Doctor of Philosophy (Ph.D.)', "Engineer's Degree", 'High School', 'Juris Doctor (J.D.)',
    'Master of Business Administration (M.B.A.)', "Master's Degree", 'Other'];
  const ghDeg = (v, opts) => { const o = opts || GH_DEGREES; const i = Scanner.matchChoiceOption(v, o); return i === -1 ? null : o[i]; };
  for (const v of ['M.S.', 'MS', 'MSc', 'Master of Science', 'M.A.', 'M.Eng', 'MFA', 'Master of Design', 'MHCI', 'Masters']) {
    expect(`Greenhouse degree list: "${v}" -> "Master's Degree" (no listed title names it; the generic level is true)`,
      ghDeg(v) === "Master's Degree");
  }
  expect('Greenhouse degree list: "MBA" still -> its own listed title', ghDeg('MBA') === 'Master of Business Administration (M.B.A.)');
  expect('Greenhouse degree list: "M.B.A." still -> its own listed title', ghDeg('M.B.A.') === 'Master of Business Administration (M.B.A.)');
  expect('Greenhouse degree list: "Ph.D." -> its own listed title', ghDeg('Ph.D.') === 'Doctor of Philosophy (Ph.D.)');
  expect(`Greenhouse degree list: "B.E." -> "Bachelor's Degree"`, ghDeg('B.E.') === "Bachelor's Degree");
  expect('Greenhouse degree list: a bare "Doctorate" stays unfilled (M.D., Ph.D. and J.D. are all doctorates -- never guessed)',
    ghDeg('Doctorate') === null);
  expect('NEGATIVE CONTROL: "M.S." never lands on the M.B.A. option', ghDeg('M.S.') !== 'Master of Business Administration (M.B.A.)');
  expect('NEGATIVE CONTROL: "Ph.D." never lands on the M.D. or J.D. option',
    ghDeg('Ph.D.') !== 'Doctor of Medicine (M.D.)' && ghDeg('Ph.D.') !== 'Juris Doctor (J.D.)');
  const CURLY_APOS = String.fromCharCode(0x2019);
  const GH_DEGREES_CURLY = GH_DEGREES.map(t => t.replace("'", CURLY_APOS));
  expect('Greenhouse degree list with typographic apostrophes: "M.S." -> the curly-apostrophe "Master' + CURLY_APOS + 's Degree"',
    ghDeg('M.S.', GH_DEGREES_CURLY) === 'Master' + CURLY_APOS + 's Degree');
  expect(`an M.B.A. against a list with no M.B.A. title -> the generic "Master's Degree" (true), never "Master of Science"`,
    ghDeg('MBA', ["Bachelor's Degree", 'Master of Science', "Master's Degree"]) === "Master's Degree");

  expect('Workday degree family: a GENERIC level list ("Bachelor\'s Degree"/"Master\'s Degree"/...) still resolves via the exactly-one-in-family tier (no regression)',
    mwdo('Bachelor of Design', ["High School Diploma", "Associate's Degree", "Bachelor's Degree", "Master's Degree", 'Doctorate']) === 2);
})();

// --- matchWorkdayPromptOption: exact/acronym-only matching (skills), and the tie rule ------
(() => {
  const mwpo = Scanner.matchWorkdayPromptOption;
  expect('exact (case-insensitive) match wins', mwpo('python', ['Python', 'PySpark']) === 0);
  expect('the "(ACRONYM)" form matches', mwpo('sql', ['Structured Query Language (SQL)', 'NoSQL']) === 0);
  expect('NEVER a prefix/startsWith match: "java" does not match "JavaScript"', mwpo('java', ['JavaScript']) === -1);
  expect('NEVER a substring match: "script" does not match "JavaScript"', mwpo('script', ['JavaScript']) === -1);
  expect('a TIE on exact match (two options normalising the same) fails rather than guessing',
    mwpo('python', ['Python', 'python']) === -1);
  expect('a TIE on the acronym form also fails rather than guessing',
    mwpo('sql', ['Structured Query Language (SQL)', 'Some Query Locator (SQL)']) === -1);
  expect('no match at all is -1', mwpo('cobol', ['Python', 'Java']) === -1);
  expect('an empty term is always -1', mwpo('', ['Python']) === -1);
})();

// --- findWorkdayDateSectionDisplay: the id-suffix-swap lookup path (the main fixtures all
//     exercise the OTHER path -- a same-parent data-automation-id sibling -- so this proves the
//     id-based path independently, on an isolated scratch fixture). ------------------------
(() => {
  const doc = dom.window.document;
  const scratch = doc.createElement('div');
  scratch.innerHTML =
    '<div data-automation-id="dateInputWrapper">' +
    '  <input id="idswap-dateSectionMonth-input" data-automation-id="dateSectionMonth-input" aria-label="Month">' +
    '  <div id="idswap-dateSectionMonth-display" data-automation-id="dateSectionMonth-display">MM</div>' +
    '</div>';
  doc.body.appendChild(scratch);
  const input = doc.getElementById('idswap-dateSectionMonth-input');
  const display = Scanner.findWorkdayDateSectionDisplay(input);
  expect('findWorkdayDateSectionDisplay resolves via the id-suffix swap ("-input" -> "-display") when the ids follow that convention',
    display && display.id === 'idswap-dateSectionMonth-display');
  scratch.remove();
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

// --- Reviewer round 5, blocker A ("didn't stick" false negatives): the SAME EEO decline
//     fixtures above, this time through the real applyFill() entry-point (what content.js's
//     runFieldAttempt() actually calls) with a proper `entry`, proving getCurrentValue()
//     returns the committed option's own visible text (never a raw value attribute) and that
//     entry._committedText stashes that exact same text at commit time -- the two signals
//     content.js's post-fill verify sweep now compares against each other instead of against
//     the service's own answer string. Both fixtures' option VALUE attribute ("decline") reads
//     nothing like its visible TEXT/label ("I do not wish to answer" / "I don't wish to
//     answer") -- worded completely differently from the profile's own "Decline to
//     self-identify" too -- exactly the shape that used to compare unequal against the
//     service's answer and get reported "Didn't stick" even though the right option was
//     sitting right there on the page. ------------------------------------------------------
(() => {
  const doc = dom.window.document;

  const genderSelect = doc.getElementById('eeo_gender_select_fixture');
  genderSelect.selectedIndex = 0; // reset to the placeholder before this fresh fill
  const genderEntry = { kind: 'element', el: genderSelect };
  const genderOk = Scanner.applyFill(genderEntry, 'Decline to self-identify');
  expect('blocker A select: applyFill via the decline family still succeeds', genderOk === true);
  expect('blocker A select: getCurrentValue() returns the option\'s visible TEXT ("I do not wish to answer"), never its raw value attribute ("decline")',
    Scanner.getCurrentValue(genderEntry) === 'I do not wish to answer');
  expect('blocker A select: entry._committedText stashes that SAME visible text at commit time, never the service\'s own differently-worded answer',
    genderEntry._committedText === 'I do not wish to answer');

  const hispanicRadios = Array.from(doc.querySelectorAll('input[name="eeo_hispanic_fixture"]'));
  hispanicRadios.forEach((r) => { r.checked = false; }); // reset before this fresh fill
  const hispanicEntry = { kind: 'radio-group', elements: hispanicRadios };
  const hispanicOk = Scanner.applyFill(hispanicEntry, 'Decline to self-identify');
  expect('blocker A radio-group: applyFill via the decline family still succeeds', hispanicOk === true);
  expect('blocker A radio-group: getCurrentValue() returns the checked radio\'s visible LABEL ("I don\'t wish to answer"), never its raw value attribute ("decline")',
    Scanner.getCurrentValue(hispanicEntry) === "I don't wish to answer");
  expect('blocker A radio-group: entry._committedText stashes that SAME visible label at commit time',
    hispanicEntry._committedText === "I don't wish to answer");
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

  // ---- dates: async commit, yield-between-parts, wrapper-visibility gating, MDY, localized --
  {
    const scanned = Scanner.scanFields(doc);
    const byKind = (kind, match) => Object.values(scanned.registry).find(e => e.kind === kind && match(e));

    const myEntry = byKind('wd-date-my', e => e.monthEl === doc.getElementById('wd_start_month'));
    expect('found the Work Experience "From" wd-date-my registry entry', !!myEntry);
    const okMy = await Scanner.applyFill(myEntry, '09/2020');
    expect('applyFill commits "09/2020" into the Month/Year pair via the async set-then-ArrowUp technique', okMy === true);
    expect('month spinner reads back 9', parseInt(doc.getElementById('wd_start_month').value, 10) === 9);
    expect('year spinner reads back 2020', parseInt(doc.getElementById('wd_start_year').value, 10) === 2020);
    expect('the "-display" divs show the committed values (zero-padded for month)',
      doc.getElementById('wd_start_month-display').textContent === '09'
      && doc.getElementById('wd_start_year-display').textContent === '2020');
    expect('getCurrentValue reassembles "09/2020" via -display/aria-valuetext', Scanner.getCurrentValue(myEntry) === '09/2020');

    doc.getElementById('wd_start_month').value = '99'; // a raw write with NO commit at all
    expect('TRUTH-LIVES-ONLY-IN-DISPLAY proof: a raw, uncommitted .value write is never trusted as the field\'s value',
      Scanner.getCurrentValue(myEntry) === '09/2020');
    Scanner.setNativeValue(doc.getElementById('wd_start_month'), '09'); // restore, via the mock's own revert path

    const okBad = await Scanner.applyFill(myEntry, 'not-a-date');
    expect('a non-MM/YYYY value is refused rather than guessed at', okBad === false);

    const yEntry = byKind('wd-date-y', e => e.yearEl === doc.getElementById('wd_edu_from_year'));
    expect('found the Education "From" wd-date-y registry entry', !!yEntry);
    const okY = await Scanner.applyFill(yEntry, '2016');
    expect('setWorkdaySpinnerValue\'s retry-once path commits a year into a spinner needing ArrowUp TWICE per unit', okY === true);
    expect('year-only spinner reads back 2016 via -display', Scanner.getCurrentValue(yEntry) === '2016');
    const okY2 = await Scanner.applyFill(yEntry, '05/2017');
    expect('a "MM/YYYY" value sent to a wd-date-y field uses ONLY the year part', okY2 === true && Scanner.getCurrentValue(yEntry) === '2017');

    const maskedEl = doc.getElementById('wd_cert_masked');
    const maskedEntry = byKind('wd-date-my', e => e.maskedEl === maskedEl);
    expect('found the masked single-input wd-date-my registry entry', !!maskedEntry);
    maskedEl.value = '11/2025';
    maskedEl.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    expect('the mock masked field genuinely IGNORES plain value+input with no preceding keydown', maskedEl.value !== '11/2025');
    const okMasked = await Scanner.applyFill(maskedEntry, '03/2021');
    expect('typeMaskedTextField types "03/2021" character by character', okMasked === true && maskedEl.value === '03/2021');

    // NEGATIVE CONTROL: asynchronous commit (setTimeout 0) -- the actual "hangs on dates" fix.
    const asyncMonthEl = doc.getElementById('wd_async_month');
    asyncMonthEl.value = '5';
    asyncMonthEl.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }));
    expect('ASYNC-COMMIT proof: a synchronous read-back right after ArrowUp does NOT yet see a setTimeout(0)-deferred commit',
      asyncMonthEl.value !== '6');
    await new Promise(resolve => setTimeout(resolve, 30)); // let the deferred mock commit settle first
    const asyncEntry = byKind('wd-date-my', e => e.monthEl === asyncMonthEl);
    const okAsync = await Scanner.applyFill(asyncEntry, '08/2019');
    expect('a real fill still succeeds against an asynchronously-committing spinner',
      okAsync === true && Scanner.getCurrentValue(asyncEntry) === '08/2019');

    // NEGATIVE CONTROL: "one model per wrapper" -- committing Year right after Month clears Month.
    const yearclearMonthEl = doc.getElementById('wd_yearclear_month');
    const yearclearYearEl = doc.getElementById('wd_yearclear_year');
    yearclearMonthEl.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }));
    yearclearYearEl.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true })); // no yield at all
    expect('YEAR-CLEARS-MONTH proof: committing Year immediately after Month (no yield) genuinely clears Month back out',
      yearclearMonthEl.value === '');
    const yearclearEntry = byKind('wd-date-my', e => e.monthEl === yearclearMonthEl);
    const okYearclear = await Scanner.applyFill(yearclearEntry, '07/2025');
    expect('a real fill YIELDS between parts and survives the adversarial clear -- Month is still 7 after Year is set',
      okYearclear === true && Scanner.getCurrentValue(yearclearEntry) === '07/2025');

    // NEGATIVE CONTROL: the spinbutton input itself is invisible behind its own visible display.
    const hiddenMonthEl = doc.getElementById('wd_hidden_month');
    expect('HIDDEN-INPUT proof: the spinbutton input is genuinely invisible via isVisible()', Scanner.isVisible(hiddenMonthEl) === false);
    const hiddenEntry = byKind('wd-date-my', e => e.monthEl === hiddenMonthEl);
    const okHidden = await Scanner.applyFill(hiddenEntry, '04/2018');
    expect('a fill still succeeds when the input is invisible but its dateInputWrapper is visible (gate on the wrapper, not the input)',
      okHidden === true && Scanner.getCurrentValue(hiddenEntry) === '04/2018');

    const mdyEntry = byKind('wd-date-mdy', e => e.dayEl === doc.getElementById('wd_selfid_day'));
    expect('found the Self-Identify Month+Day+Year registry entry', !!mdyEntry);
    const okMdy = await Scanner.applyFill(mdyEntry, '07/04/2026');
    expect('Month+Day+Year fills all three parts and reassembles "07/04/2026" (Day is never dropped)',
      okMdy === true && Scanner.getCurrentValue(mdyEntry) === '07/04/2026');

    const monatEntry = byKind('wd-date-my', e => e.monthEl === doc.getElementById('wd_monat_month'));
    const okMonat = await Scanner.applyFill(monatEntry, '03/2022');
    expect('LOCALIZED-TENANT proof: a German aria-label ("Monat"/"Jahr") still fills correctly via data-automation-id',
      okMonat === true && Scanner.getCurrentValue(monatEntry) === '03/2022');
  }

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
    // This build: a failed wd-dropdown fill also carries the option texts it saw as STRUCTURED
    // DATA on entry._lastOptions (not only baked into the reason string) -- content.js's
    // second-chance /resolve round trip reads this to give the service the real options.
    expect('Degree wd-dropdown: entry._lastOptions carries the actual rendered option texts as structured data',
      JSON.stringify(degreeEntry._lastOptions) === JSON.stringify([
        'Bachelors Degree or Equivalent', 'Masters Degree or Equivalent', 'Doctorate',
        'Associates Degree', 'High School or Equivalent'
      ]));

    const countryBtn = doc.getElementById('wd_country_button');
    const countryEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-dropdown' && e.button === countryBtn);
    const okCountry = await Scanner.applyFill(countryEntry, 'USA');
    expect('Country dropdown filled by alias "USA" resolves to "United States of America"',
      okCountry === true && doc.getElementById('wd_country_button_text').textContent === 'United States of America');

    // The country listbox only renders a window of 5 at a time out of 57 -- "United States of
    // America" sits at index 51, well past what's visible on open, so the assertion above only
    // passed because the scroll/ArrowDown collection loop actually ran and worked. Reopen it
    // fresh (a click toggles __scrollIndex back to the initial window) to prove that directly.
    doc.getElementById('wd_country_button').click(); // open, back to the INITIAL render window
    const initialCountryRender = Array.from(doc.getElementById('wd_country_listbox').querySelectorAll('[role="option"]')).map(li => li.textContent);
    doc.getElementById('wd_country_button').click(); // close again
    expect('...confirmed: the window that renders on open does not include the target at all',
      !initialCountryRender.includes('United States of America'));
  }

  // ---- prompt: Field of Study -- portalled popup, loose fixed-list "live search" -----------
  {
    const scanned = Scanner.scanFields(doc);
    const fosInput = doc.getElementById('wd_fos_input');
    const fosEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-prompt' && e.input === fosInput);
    expect('found the Field of Study wd-prompt registry entry', !!fosEntry);
    const fosSelected = () => Array.from(doc.querySelectorAll('#wd_fos_selected li')).map(li => li.textContent);

    const ok = await Scanner.applyFill(fosEntry, 'Computer Science');
    expect('Field of Study: exact match is added, found in the PORTALLED popup (not inside the field)',
      ok === true && fosSelected().includes('Computer Science'));
    expect('the prompt input is cleared after a successful add', fosInput.value === '');
    expect('the popup is closed (Escape) after a successful add', doc.getElementById('wd_fos_popup').style.display !== 'block');

    const fosBadStart = Date.now();
    const okBad = await Scanner.applyFill(fosEntry, 'Zoology');
    const fosBadMs = Date.now() - fosBadStart;
    expect('Field of Study: a term with NO confident match among the (non-empty) results selects NOTHING',
      okBad === false && fosSelected().length === 1 && !fosSelected().includes('Zoology'));
    expect('the typed text is cleared after no confident match', fosInput.value === '');
    // Regression guard: a non-virtualized prompt (this fixed 3-option "live search") whose
    // ArrowDown walk never sees anything change must stop within ~2 stale rounds, NOT burn the
    // full 40-try/150ms budget (~6s) on every single no-match term -- an earlier version of
    // this rewrite did exactly that, which is worse than the ORIGINAL "hangs" bug, just moved.
    expect('a no-match term on a non-virtualized prompt fails within ~1s, not ~6s (stale-round early exit)',
      fosBadMs < 2000);
  }

  // ---- prompt: Skills -- the adversarial suite from the project brief §2.3 (the most likely
  //      cause of the reported "hangs on ... skills" bug) --------------------------------------
  {
    const scanned = Scanner.scanFields(doc);
    const skillsInput = doc.getElementById('wd_skills_input');
    const skillsEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-prompt' && e.input === skillsInput);
    expect('found the Skills wd-prompt registry entry', !!skillsEntry);
    const skillsSelected = () => Array.from(doc.querySelectorAll('#wd_skills_selected li')).map(li => li.textContent);

    // Pre-existing decoys (seeded directly in test-page.html, present since page load):
    // a DIFFERENT field's own already-open wd-popup, and a DIFFERENT field's own pill --
    // both listing/containing text Skills will also search for.
    expect('DECOY setup sanity: the unrelated field\'s pill list already has exactly one pre-existing "Python"',
      doc.querySelectorAll('#wd_decoy_other_selected li').length === 1
      && doc.getElementById('wd_decoy_other_selected').textContent.trim() === 'Python');

    const okPython = await Scanner.applyFill(skillsEntry, 'Python');
    expect('Skills: exact match "Python" is added -- the decoy field\'s pre-existing "Python" pill never short-circuited this',
      okPython === true && skillsSelected().filter(t => t === 'Python').length === 1);

    const okSql = await Scanner.applyFill(skillsEntry, 'SQL');
    expect('Skills: "SQL" matches via the acronym form "(SQL)" in "Structured Query Language (SQL)"',
      okSql === true && skillsSelected().includes('Structured Query Language (SQL)'));
    expect('the popup is closed (Escape) between terms, not left open for the next one to stumble into',
      doc.getElementById('wd_skills_popup').style.display !== 'block');

    const t0 = Date.now();
    const okExcel = await Scanner.applyFill(skillsEntry, 'Excel');
    expect('Skills: "No Items." is detected and fails FAST, never waiting out the full result budget',
      okExcel === false && (Date.now() - t0) < 3000);
    expect('...and nothing Excel-shaped was ever added', !skillsSelected().some(t => /excel/i.test(t)));

    expect('the OLD prefix-matching bug is gone: matchWorkdayPromptOption("java", ["JavaScript"]) no longer matches',
      Scanner.matchWorkdayPromptOption('java', ['JavaScript']) === -1);
    const okJava = await Scanner.applyFill(skillsEntry, 'Java');
    expect('Skills: VIRTUALIZED list -- "Java" (one ArrowDown away) is added exactly, never the rendered "JavaScript" prefix',
      okJava === true && skillsSelected().includes('Java') && !skillsSelected().includes('JavaScript'));

    const okDocker = await Scanner.applyFill(skillsEntry, 'Docker');
    expect('Skills: AUTO-COMMIT -- an exact term can commit straight to a pill with NO popup ever appearing',
      okDocker === true && skillsSelected().includes('Docker'));

    const okK8s = await Scanner.applyFill(skillsEntry, 'Kubernetes');
    expect('Skills: LATENCY -- results arriving ~900ms after Enter are still caught, not failed prematurely',
      okK8s === true && skillsSelected().includes('Kubernetes'));

    const okFigma = await Scanner.applyFill(skillsEntry, 'Figma');
    expect('Skills: PICKY CHECKBOX -- a checkbox that ignores a bare click still gets added via the pointer-sequence fallback',
      okFigma === true && skillsSelected().includes('Figma'));

    expect('DECOY popup: the unrelated pre-existing wd-popup was never read as Skills\' own results (still shows its untouched row)',
      doc.querySelector('#wd_decoy_popup [data-automation-checked]').getAttribute('data-automation-checked') === 'Not Checked');
    expect('DECOY pill: the unrelated field\'s "Python" pill is still exactly one, never touched by any Skills interaction above',
      doc.querySelectorAll('#wd_decoy_other_selected li').length === 1);

    // "First 2 terms produce no popup at all" -- short resultTimeoutMs override so this stays
    // fast in the suite; the production default (8s) is for a real, possibly-slow tenant.
    const abortResult = await Scanner.fillWorkdayPromptValue(skillsEntry,
      ['totally-unrecognized-term-one', 'totally-unrecognized-term-two', 'Python'],
      { resultTimeoutMs: 200 });
    expect('Skills LIST: 2 terms in a row producing no popup at all stops the rest of the list with the operator-facing message',
      abortResult.results.length === 3 && abortResult.results[2].result.reason === 'Workday skills results not found — please send a Report page');
    expect('...and the 3rd term was never actually attempted once the abort fired', abortResult.results[2].result.ok === false);

    // Cap and budget are exercised as pure logic (see below) rather than by actually running 16
    // real terms or waiting out a real 60s here -- keeps the suite fast and deterministic.
  }

  // ---- Self-Identify disability CheckboxGroup (CC-305): single-choice via the shared
  //      answer-family matcher; "exactly one" is enforced by unchecking every other box ------
  {
    const scanned = Scanner.scanFields(doc);
    const cgEntry = Object.values(scanned.registry).find(e => e.kind === 'wd-checkbox-group');
    expect('found the disabilityStatus wd-checkbox-group registry entry', !!cgEntry);

    const okYes = Scanner.applyFill(cgEntry, 'Yes, I have a disability, or have had one in the past');
    expect('checkbox-group: exact option text checks exactly that box, none of the others',
      okYes === true && doc.getElementById('wd_disability_yes').checked === true
      && doc.getElementById('wd_disability_no').checked === false
      && doc.getElementById('wd_disability_decline').checked === false);

    const okDecline = Scanner.applyFill(cgEntry, 'Decline to self-identify');
    expect('checkbox-group: the shared decline answer-family matches "I do not want to answer"',
      okDecline === true && doc.getElementById('wd_disability_decline').checked === true);
    expect('checkbox-group: "exactly one" holds -- switching answers unchecks the previous one',
      doc.getElementById('wd_disability_yes').checked === false && doc.getElementById('wd_disability_no').checked === false);

    const okBad = Scanner.applyFill(cgEntry, 'Something Unrelated Entirely');
    expect('checkbox-group: no confident match leaves the group completely untouched', okBad === false);
    expect('...the previously-checked answer is still checked (nothing was cleared by the failed attempt)',
      doc.getElementById('wd_disability_decline').checked === true);
    // Reviewer round 3: the reason must NEVER quote the attempted (here, self-identification)
    // value -- an exported fill report must not carry EEO/disability answers.
    expect('wd-checkbox-group failure reason never quotes the attempted self-identification value',
      !String(cgEntry._lastReason || '').includes('Something Unrelated Entirely'));

    const agreementCb = doc.getElementById('wd_agreement_checkbox');
    expect('agreementCheckbox is refused by the checkbox-group option guard even if somehow targeted directly',
      Scanner.isWorkdayCheckboxGroupOptionSafe(agreementCb, doc.getElementById('wd_disability_group')) === false);
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

// ===========================================================================================
// Coordinator addendum -- reviewer-found gaps: matchAnswerFamily ambiguity in EVERY family
// tier (not only plain containment), and honest read-back verification for every applyFill()
// path that previously returned true unconditionally.
// ===========================================================================================

// --- matchChoiceOption: plain containment tier ambiguity (the fix accompanying this addendum
//     applies everywhere, including the tier already covered above) -------------------------
(() => {
  const mco = Scanner.matchChoiceOption;
  expect('containment tier: two options BOTH contain the value ("Engineer") -- ambiguous, never the first ("Software Engineer")',
    mco('Engineer', ['Software Engineer', 'Site Engineer']) === -1);
  expect('containment tier: with only ONE containing option, it still matches normally',
    mco('Engineer', ['Software Engineer', 'Product Designer']) === 0);
  // Identical duplicates are not ambiguity (this build): the page rendering the SAME option text
  // twice is never something the applicant could have told apart either -- pick the first. Two
  // DIFFERENT texts (immediately above) still refuse -- this is the negative control for that.
  expect('containment tier: two options with the IDENTICAL text both contain the value -- not real ambiguity, picks the first',
    mco('Engineer', ['Software Engineer', 'Software Engineer']) === 0);
})();

// --- matchAnswerFamily: ambiguity must hold in EVERY family tier, not only containment ------
(() => {
  const mco = Scanner.matchChoiceOption;

  // (a) yes/no family: two "Yes, ..." options are materially different legal statements for a
  // sponsorship-needing applicant -- must never pick the first.
  expect('yes/no family: value "Yes" with TWO "Yes, ..." options (different legal statements) is ambiguous, never the first',
    mco('Yes', ['Yes, I am a U.S. citizen or permanent resident', 'Yes, I am authorized but will require sponsorship', 'No']) === -1);
  expect('yes/no family: value "Yes" with only ONE "Yes, ..." option still resolves normally',
    mco('Yes', ['Yes, I am authorized to work', 'No']) === 0);

  // (b) decline family: an option that ALSO asserts a veteran/identity claim is not a genuine
  // decline option, even though it mentions decline-ish phrasing ("choose not", "self-identify"
  // is literally part of DECLINE_RE).
  expect('decline family: an option that ALSO asserts "I am a protected veteran" is rejected, not picked for "Decline to self-identify"',
    mco('Decline to self-identify', ['I am a protected veteran, but I choose not to self-identify the classifications to which I belong']) === -1);
  expect('decline family: an option that ALSO asserts "Yes, I self-identify as..." is rejected, not picked for "Decline to self-identify"',
    mco('Decline to self-identify', ['Yes, I self-identify as LGBTQ+']) === -1);
  expect('decline family: with BOTH assertion-laden decline-ish decoys present together, still refuses (never guesses between them)',
    mco('Decline to self-identify', [
      'I am a protected veteran, but I choose not to self-identify the classifications to which I belong',
      'Yes, I self-identify as LGBTQ+',
      'No'
    ]) === -1);
  expect('decline family: a genuine plain decline option is still picked once assertion-laden decoys are filtered out',
    mco('Decline to self-identify', [
      'I am a protected veteran, but I choose not to self-identify the classifications to which I belong',
      "I don't wish to answer"
    ]) === 1);
  expect('decline family: two DIFFERENT plain decline phrasings together are ambiguous, never the first',
    mco('Decline to self-identify', ["I don't wish to answer", 'Prefer not to say']) === -1);
})();

// --- applyFill: honest read-back for text/number/textarea, radio, and checkbox -------------
(() => {
  const doc = dom.window.document;

  // Plain text field -- genuine success still reports true (the fix must not create false
  // NEGATIVES on ordinary fields).
  {
    const el = doc.getElementById('full_name');
    const entry = { kind: 'element', el };
    const ok = Scanner.applyFill(entry, 'Jordan Quill');
    expect('applyFill on a plain text field: genuine success still reports true and reads back the value',
      ok === true && el.value === 'Jordan Quill');
  }

  // NEGATIVE CONTROL: a React-style input that reverts the typed value synchronously within
  // its own 'input' listener must report FAILURE, never a blind "true".
  {
    const el = doc.getElementById('revert_on_input_field');
    const entry = { kind: 'element', el };
    const ok = Scanner.applyFill(entry, 'this will be reverted');
    expect('applyFill on a reverting text field reports FAILURE (read-back caught the revert), not a blind true',
      ok === false && el.value === '');
    expect('a failed text fill records a human-readable reason', /stick/i.test(entry._lastReason || ''));
  }

  // NEGATIVE CONTROL: "120000 USD" into input[type=number] -- the browser's own number-input
  // setter rejects the non-numeric string (value becomes ""), which must be reported honestly.
  {
    const el = doc.getElementById('revert_on_input_number');
    const entry = { kind: 'element', el };
    const ok = Scanner.applyFill(entry, '120000 USD');
    expect('applyFill on input[type=number] with "120000 USD": the invalid string is REJECTED by the number input itself, and that is now reported as failure, not success',
      ok === false && el.value === '');
    const okGood = Scanner.applyFill(entry, '120000');
    expect('applyFill on input[type=number] with a genuinely valid number still reports success',
      okGood === true && el.value === '120000');
  }

  // Textarea -- same generic path as text/number; genuine success still reports true.
  {
    const el = doc.getElementById('cover_note');
    const entry = { kind: 'element', el };
    const ok = Scanner.applyFill(entry, 'Looking forward to this role.');
    expect('applyFill on a textarea: genuine success reads back correctly',
      ok === true && el.value === 'Looking forward to this role.');
  }

  // Checkbox -- genuine success still reports true, now verified via the NEW read-back.
  {
    const el = doc.querySelector('input[name="workExperience-1--currentlyWorkHere"]');
    const entry = { kind: 'element', el };
    const ok = Scanner.applyFill(entry, true);
    expect('applyFill on a checkbox: genuine success is verified via .checked, not just a click return value',
      ok === true && el.checked === true);
    el.checked = false; // reset for hygiene
  }

  // NEGATIVE CONTROL: a checkbox whose own click handler reverts the check synchronously.
  {
    const el = doc.getElementById('revert_on_click_checkbox');
    const entry = { kind: 'element', el };
    const ok = Scanner.applyFill(entry, true);
    expect('applyFill on a reverting checkbox reports FAILURE (read back .checked === false), not a blind true',
      ok === false && el.checked === false);
    expect('a failed checkbox fill records a human-readable reason', /checked state/i.test(entry._lastReason || ''));
  }

  // Radio group -- genuine success still reports true, now verified via .checked.
  {
    const scanned = Scanner.scanFields(doc);
    const radioField = scanned.fields.find(f => f.type === 'radio');
    const radioEntry = scanned.registry[radioField.id];
    const ok = Scanner.applyFill(radioEntry, 'Yes');
    expect('applyFill on a radio group: genuine success is verified via .checked, not just a click return value',
      ok === true && radioEntry.elements.some(r => r.checked && r.value === 'yes'));
  }
})();

// ===========================================================================================
// Choice widgets (2026-09-24 live probe): react-select/generic-ARIA combobox, Ashby button
// groups, checkbox groups, Lever's location type-ahead and card labels. Fixtures added to
// test-page.html's new "ats-widgets-form" / "lever_label_fixtures" sections.
// ===========================================================================================

// --- checkbox groups: scanning shape + label resolution -------------------------------------
(() => {
  const doc = dom.window.document;
  const scanned = Scanner.scanFields(doc);
  // Excludes Workday's own "wd-checkbox-group" widget -- it legitimately shares the same
  // type "checkbox-group" string (see src/applypilot/extension/matcher.py's is_choice_field),
  // but is a separate detector with its own dedicated test coverage above.
  const cgFields = scanned.fields.filter(f => f.type === 'checkbox-group' && f.widget !== 'wd-checkbox-group');

  expect('checkbox groups detected: Greenhouse (shared name[]), Ashby (fieldset, no shared name), Lever (cards[..][fieldN])',
    cgFields.length === 3);

  const gh = cgFields.find(f => f.name === 'question_lang[]');
  expect('Greenhouse checkbox group labelled from its <fieldset><legend>, required marker stripped',
    gh && gh.label === 'What language(s) are you fluent in?');
  expect('Greenhouse checkbox group options are each checkbox\'s own label',
    gh && gh.options.join(',') === 'English,Spanish,French');
  expect('the decoy standalone checkbox sharing the word "fluent" stays its OWN separate (non-group) field',
    !!byName.fluent_decoy && byName.fluent_decoy.type === 'checkbox');
  expect('the decoy standalone checkbox is never folded into the language group\'s options',
    gh && !gh.options.some(o => /sign language/i.test(o)));

  const lever = cgFields.find(f => f.name && f.name.indexOf('cards[92a51f92') === 0);
  expect('Lever "cards[<uuid>][field0]" checkbox group gets its real question text from .application-question .application-label .text',
    lever && lever.label === 'Have you previously worked at this company as an intern?');
  expect('Lever checkbox group options are the option labels, never the raw opaque name',
    lever && lever.options.join(',') === 'No,Yes - Intern,Yes - Full Time Employment');

  const ashby = cgFields.find(f => f.name === 'Mountain View, CA');
  expect('Ashby checkbox group (no shared name -- grouped by its <fieldset>) labelled from the preceding question title',
    ashby && ashby.label === 'Please select your preferred working location.');
  expect('Ashby checkbox group options are the three location labels',
    ashby && ashby.options.join(',') === 'Mountain View, CA,San Francisco, CA,Los Angeles, CA');
})();

// --- checkbox groups: fill / verify, including decline family and ambiguous negative control -
(() => {
  const doc = dom.window.document;
  const scanned = Scanner.scanFields(doc);
  // Excludes Workday's own "wd-checkbox-group" widget -- it legitimately shares the same
  // type "checkbox-group" string (see src/applypilot/extension/matcher.py's is_choice_field),
  // but is a separate detector with its own dedicated test coverage above.
  const cgFields = scanned.fields.filter(f => f.type === 'checkbox-group' && f.widget !== 'wd-checkbox-group');

  const gh = cgFields.find(f => f.name === 'question_lang[]');
  const ghEntry = scanned.registry[gh.id];

  const ok1 = Scanner.applyFill(ghEntry, 'Spanish');
  expect('checkbox-group: a single value ticks the ONE matching option',
    ok1 === true && doc.getElementById('gh_lang_es').checked === true);
  expect('checkbox-group: the other options in the group stay unticked',
    doc.getElementById('gh_lang_en').checked === false && doc.getElementById('gh_lang_fr').checked === false);

  const ok2 = Scanner.applyFill(ghEntry, ['English', 'French']);
  expect('checkbox-group: a LIST value ticks each matching option',
    ok2 === true && doc.getElementById('gh_lang_en').checked === true && doc.getElementById('gh_lang_fr').checked === true);
  expect('checkbox-group: ticking more options NEVER unticks a box already ticked by an earlier fill',
    doc.getElementById('gh_lang_es').checked === true);

  const okBad = Scanner.applyFill(ghEntry, 'Klingon');
  expect('checkbox-group: no confident match among the options -> false', okBad === false);
  expect('checkbox-group failure reason never quotes the attempted value, only the options it saw',
    !String(ghEntry._lastReason || '').includes('Klingon') && /saw: /.test(ghEntry._lastReason));

  const okListBad = Scanner.applyFill(ghEntry, ['English', 'Klingon', 'Vulcan']);
  expect('checkbox-group LIST value: the two unmatched terms never block the one that DOES match',
    okListBad === true && doc.getElementById('gh_lang_en').checked === true);
  expect('checkbox-group LIST failure reason reports a COUNT of unmatched terms, never the terms themselves',
    /2 of 3/.test(ghEntry._lastReason) &&
    !String(ghEntry._lastReason).includes('Klingon') && !String(ghEntry._lastReason).includes('Vulcan'));

  // Ambiguous negative control: "CA" contains-matches all three California cities.
  const ashby = cgFields.find(f => f.name === 'Mountain View, CA');
  const ashbyEntry = scanned.registry[ashby.id];
  const okAmbiguous = Scanner.applyFill(ashbyEntry, 'CA');
  expect('checkbox-group: a value matching MULTIPLE options ("CA" -> 3 California cities) never guesses',
    okAmbiguous === false &&
    !doc.getElementById('ashby_loc_mv').checked && !doc.getElementById('ashby_loc_sf').checked && !doc.getElementById('ashby_loc_la').checked);

  // Decline family: reuses matchChoiceOption's existing family dispatch -- no separate
  // detection logic needed for checkbox groups.
  const declineWrap = doc.createElement('div');
  const declineFieldset = doc.createElement('fieldset');
  declineWrap.appendChild(declineFieldset);
  doc.body.appendChild(declineWrap);
  function mkCb(id, label) {
    const l = doc.createElement('label');
    const cb = doc.createElement('input');
    cb.type = 'checkbox'; cb.id = id;
    l.appendChild(cb);
    l.appendChild(doc.createTextNode(label));
    declineFieldset.appendChild(l);
    return cb;
  }
  const raceAsian = mkCb('race_asian_cb', 'Asian');
  const raceWhite = mkCb('race_white_cb', 'White');
  const raceDecline = mkCb('race_decline_cb', "I don't wish to answer");
  const declineGroupScan = Scanner.findCheckboxGroups(declineWrap, doc);
  expect('synthetic race checkbox group detected via its <fieldset>', declineGroupScan.groups.length === 1);
  const declineEntry = { kind: 'checkbox-group', elements: [raceAsian, raceWhite, raceDecline] };
  const okDecline = Scanner.applyCheckboxGroupValue(declineEntry, 'Decline to self-identify');
  expect('checkbox-group: a decline-shaped value picks the decline-family option, never "Asian"/"White"',
    okDecline === true && raceDecline.checked === true && !raceAsian.checked && !raceWhite.checked);
})();

// --- Lever card labels: the RADIO-group half of the fix, via a hidden fixture tested by
//     DIRECT function call (never through scanFields()'s whole-page scan, so it can never
//     perturb the sitewide "radio group captured as ONE field" count above) ------------------
(() => {
  const doc = dom.window.document;
  const radios = Array.from(doc.querySelectorAll('input[name="cards[d090becd-07b8-4536-ac84-dbf3bc07e03d][field0]"]'));
  expect('Lever radio card fixture: 2 radio options found', radios.length === 2);
  expect('Lever radio group label resolved from .application-question .application-label .text, not its own opaque "cards[...]" name',
    Scanner.getGroupLabel(radios) === 'Role requires candidate to be based in London or Stockholm');
})();

// --- Lever location: extractLocationName / splitCityState (pure functions) -----------------
(() => {
  expect('extractLocationName: a plain "City, State" string passes through unchanged',
    Scanner.extractLocationName('Seattle, Washington') === 'Seattle, Washington');
  expect('extractLocationName: a resolved-suggestion JSON object unwraps to its "name"',
    Scanner.extractLocationName('{"name":"Seattle, WA, USA","id":"f93b25a3"}') === 'Seattle, WA, USA');
  expect('extractLocationName: malformed JSON-looking text falls back to the raw string, never throws',
    Scanner.extractLocationName('{not valid json') === '{not valid json');

  expect('splitCityState: plain "City, State" splits in two',
    JSON.stringify(Scanner.splitCityState('Seattle, Washington')) === JSON.stringify({ city: 'Seattle', state: 'Washington' }));
  expect('splitCityState: "City, State, Country" ignores the trailing country part',
    JSON.stringify(Scanner.splitCityState('Seattle, WA, USA')) === JSON.stringify({ city: 'Seattle', state: 'WA' }));
  expect('splitCityState: a JSON-object value is unwrapped before splitting',
    JSON.stringify(Scanner.splitCityState('{"name":"Austin, Texas, United States"}')) === JSON.stringify({ city: 'Austin', state: 'Texas' }));
  expect('splitCityState: a bare city with no comma has an empty state',
    JSON.stringify(Scanner.splitCityState('Seattle')) === JSON.stringify({ city: 'Seattle', state: '' }));
})();

// --- Ashby button groups: scanning shape + isChoiceButtonSafe guard -------------------------
(() => {
  const doc = dom.window.document;
  const scanned = Scanner.scanFields(doc);
  const bgFields = scanned.fields.filter(f => f.widget === 'button-group');
  expect('button groups detected: work-authorization, sponsorship, the decoy-adjacent relocation question, and the no-form real-shape group',
    bgFields.length === 4);

  const workauth = bgFields.find(f => /permanently authorized/i.test(f.label));
  const sponsor = bgFields.find(f => /require sponsorship/i.test(f.label));
  expect('work-authorization button group has options [Yes, No]', workauth && workauth.options.join(',') === 'Yes,No');
  expect('sponsorship button group has options [Yes, No], a SEPARATE field from work-authorization',
    sponsor && sponsor.options.join(',') === 'Yes,No' && sponsor.id !== workauth.id);

  const yesBtn = doc.getElementById('ashby_workauth_yes');
  const container = doc.getElementById('ashby_workauth_entry');
  expect('isChoiceButtonSafe allows a real type=button option inside its own question container',
    Scanner.isChoiceButtonSafe(yesBtn, container) === true);
  expect('isChoiceButtonSafe REFUSES a button from a DIFFERENT question\'s container',
    Scanner.isChoiceButtonSafe(yesBtn, doc.getElementById('ashby_sponsor_entry')) === false);
  expect('isChoiceButtonSafe REFUSES null', Scanner.isChoiceButtonSafe(null, container) === false);

  // Reviewer round 3: isChoiceButtonSelected must match "active"/"selected" as a whole class
  // TOKEN (a real word-break, or the "_"/"-" a hashed CSS-module class glues pieces with),
  // never a bare substring -- "_inactive_..."/"_interactive_..." must NOT read as selected.
  const scratchBtn = doc.createElement('button');
  scratchBtn.className = '_inactive_1234_56';
  expect('isChoiceButtonSelected: a class containing "active" as part of "inactive" is NOT selected',
    Scanner.isChoiceButtonSelected(scratchBtn) === false);
  scratchBtn.className = '_interactive_1234_56';
  expect('isChoiceButtonSelected: a class containing "active" as part of "interactive" is NOT selected either',
    Scanner.isChoiceButtonSelected(scratchBtn) === false);
  scratchBtn.className = '_option_1svni_32--selected_9xk2'; // the REAL Ashby hashed shape
  expect('isChoiceButtonSelected: the real Ashby hashed "--selected_" class STILL matches (no regression)',
    Scanner.isChoiceButtonSelected(scratchBtn) === true);
  scratchBtn.className = 'active';
  expect('isChoiceButtonSelected: a plain "active" class still matches', Scanner.isChoiceButtonSelected(scratchBtn) === true);

  // Reviewer round 3: the radio-group dispatcher reason must never quote the attempted value
  // either (an EEO/disability answer is exactly the kind of value that must never land in an
  // exported report) -- only the radio group's own option LABELS (page furniture) are safe.
  const radioA = doc.createElement('input');
  radioA.type = 'radio';
  radioA.name = 'scratch_radio_leak_test';
  const radioLabelA = doc.createElement('label');
  radioLabelA.appendChild(radioA);
  radioLabelA.appendChild(doc.createTextNode('Yes'));
  const radioB = doc.createElement('input');
  radioB.type = 'radio';
  radioB.name = 'scratch_radio_leak_test';
  const radioLabelB = doc.createElement('label');
  radioLabelB.appendChild(radioB);
  radioLabelB.appendChild(doc.createTextNode('No'));
  doc.body.appendChild(radioLabelA);
  doc.body.appendChild(radioLabelB);
  const radioEntry = { kind: 'radio-group', elements: [radioA, radioB] };
  const radioOk = Scanner.applyFill(radioEntry, 'A Very Specific Unmatched Radio Value');
  expect('radio-group: no confident match -> false', radioOk === false);
  expect('radio-group failure reason never quotes the attempted value, only the options it saw',
    !String(radioEntry._lastReason || '').includes('A Very Specific Unmatched Radio Value') && /saw: /.test(radioEntry._lastReason));
  radioLabelA.remove();
  radioLabelB.remove();

  const decoySubmit = doc.getElementById('ashby_decoy_submit');
  const decoyContainer = doc.getElementById('ashby_decoy_entry');
  expect('the decoy submit button really is type=submit (no type="" attribute -- the same HTML trap isAddAnotherButtonSafe guards against)',
    decoySubmit.type === 'submit');
  expect('isChoiceButtonSafe REFUSES the decoy submit button (not type=button)',
    Scanner.isChoiceButtonSafe(decoySubmit, decoyContainer) === false);
  expect('the decoy submit button was never even scanned as one of the choice group\'s own options',
    !bgFields.some(f => f.options.some(o => /submit/i.test(o))));

  const submitTypeBtn = doc.createElement('button');
  submitTypeBtn.type = 'submit';
  submitTypeBtn.textContent = 'Yes';
  container.appendChild(submitTypeBtn);
  expect('isChoiceButtonSafe REFUSES a type=submit button even with matching option TEXT ("Yes")',
    Scanner.isChoiceButtonSafe(submitTypeBtn, container) === false);
  container.removeChild(submitTypeBtn);

  // --- real Ashby shape: no <form> anywhere, every button (including the decoy) is type-less
  //     (el.type reports "submit" by default, el.form is null) ---
  const noFormYes = doc.getElementById('ashby_noform_yes');
  const noFormNo = doc.getElementById('ashby_noform_no');
  const noFormContainer = doc.getElementById('ashby_noform_entry');
  const noFormDecoy = doc.getElementById('ashby_noform_submit');
  expect('fixture sanity: the no-form option buttons really are type-less (type reports "submit")',
    noFormYes.type === 'submit' && noFormNo.type === 'submit');
  expect('fixture sanity: the no-form option buttons truly have no form owner', noFormYes.form === null);
  expect('fixture sanity: the decoy "Submit Application" is ALSO type-less with no form owner',
    noFormDecoy.type === 'submit' && noFormDecoy.form === null);

  expect('isChoiceButtonSafe allows a type-less (submit-by-default), form-less Ashby option button whose text matches a known option',
    Scanner.isChoiceButtonSafe(noFormYes, noFormContainer, ['Yes', 'No']) === true);
  expect('isChoiceButtonSafe REFUSES the type-less, form-less DECOY "Submit Application" button -- its text is not a known option',
    Scanner.isChoiceButtonSafe(noFormDecoy, noFormContainer, ['Yes', 'No']) === false);
  expect('isChoiceButtonSafe REFUSES a type-less, form-less button when no optionTexts are supplied at all (never relaxes blind)',
    Scanner.isChoiceButtonSafe(noFormYes, noFormContainer) === false);
})();

// --- matchCityStateOption: direct unit checks (edge cases the end-to-end combobox fixtures
//     below don't otherwise exercise) -------------------------------------------------------
(() => {
  const CATALOG = ['Seattle, Washington, United States', 'Seattle Hill-Silver Firs, Washington, United States',
    'South Seattle, Washington, United States', 'Seattle Bar, Oregon, United States'];
  expect('matchCityStateOption: exact city+state resolves uniquely, never a same-state neighbour that merely contains the word',
    Scanner.matchCityStateOption('Seattle, Washington', CATALOG) === 0);
  expect('matchCityStateOption: a 2-letter state code resolves the same option as the full name',
    Scanner.matchCityStateOption('Seattle, WA', CATALOG) === 0);
  expect('matchCityStateOption: a plain value with no comma at all is never mistaken for "City, State" (-1, not a crash)',
    Scanner.matchCityStateOption('Seattle', CATALOG) === -1);
  expect('matchCityStateOption: a comma-bearing value whose second part is not a real US state never unlocks this tier',
    Scanner.matchCityStateOption('Seattle, Mars', CATALOG) === -1);
  expect('matchCityStateOption: wrong state for an otherwise-matching city -> -1, never guesses the city alone',
    Scanner.matchCityStateOption('Seattle, Oregon', CATALOG) === -1);

  // Identical duplicates are not ambiguity (this build, live probe 2026-09-24,
  // job-boards.greenhouse.io/twilio: the geocoder returned "Seattle, Washington, United States"
  // TWICE for the same query) -- the applicant could never have told the two apart on the page
  // either, so the first is picked rather than refusing outright.
  const DUPLICATE_CATALOG = ['Seattle, Washington, United States', 'Seattle, Washington, United States'];
  expect('matchCityStateOption: two options with the IDENTICAL rendered text -- not real ambiguity, picks the first',
    Scanner.matchCityStateOption('Seattle, Washington', DUPLICATE_CATALOG) === 0);

  // Negative control: two options that both match on city+state but render DIFFERENT full text
  // (a genuinely different third segment) are still real ambiguity -- never guessed between.
  const DIFFERENT_TEXT_CATALOG = ['Seattle, Washington, United States', 'Seattle, Washington, USA'];
  expect('matchCityStateOption: two options matching city+state but with DIFFERENT full text still refuse (-1), never the first',
    Scanner.matchCityStateOption('Seattle, Washington', DIFFERENT_TEXT_CATALOG) === -1);
})();

// --- matchChoiceOption: US state cross-match, degree bare-stem duplicates, and the "no
//     literal Yes/No option" screening fallback -- pure-function unit checks (live probe,
//     2026-09-24: job-boards.greenhouse.io/oura's 'State*' and boards.greenhouse.io/robinhood's
//     'Degree*'/"Have you ever worked..." fields). The end-to-end combobox fixtures below also
//     exercise these through the full open/type/clear flow; these are the fast, direct checks. -
(() => {
  expect('matchChoiceOption: US state cross-match -- a full state name resolves against a bare 2-letter-code list',
    Scanner.matchChoiceOption('Washington', ['AL', 'AK', 'AZ', 'WA']) === 3);
  expect('matchChoiceOption: US state cross-match works in the OTHER direction too -- a 2-letter code resolves against full state names',
    Scanner.matchChoiceOption('WA', ['Alabama', 'Alaska', 'Arizona', 'Washington']) === 3);
  expect('matchChoiceOption: US state cross-match never fires for a value that is not a recognised state at all',
    Scanner.matchChoiceOption('Wakanda', ['AL', 'AK', 'AZ', 'WA']) === -1);

  // Ground truth: Greenhouse's own standard "Degree" field lists BOTH a bare level noun
  // ("Bachelors") AND its fuller phrasing ("Bachelor's Degree") as separate options -- neither
  // contains "Bachelor of Design"'s own specific token, so the existing "exactly one specific
  // match" tier finds nothing; treated as the same level (bare stems collapse identically) and
  // resolved to the fuller, unambiguous "...Degree" phrasing.
  const ROBINHOOD_DEGREE_OPTIONS = ['High School', 'Associates', "Associate's Degree", 'Bachelors',
    "Bachelor's Degree", 'Masters', "Master's Degree", 'Doctorate'];
  expect('matchChoiceOption: "Bachelors" vs "Bachelor\'s Degree" -- same level under two spellings, resolves to the fuller "...Degree" phrasing',
    Scanner.matchChoiceOption('Bachelor of Design', ROBINHOOD_DEGREE_OPTIONS) === 4);
  expect('matchChoiceOption: the SAME duplicate-spelling tie-break applies to a different family too ("Masters" vs "Master\'s Degree")',
    Scanner.matchChoiceOption('Master of Design', ROBINHOOD_DEGREE_OPTIONS) === 6);
  // Negative control (no regression): several GENUINELY DIFFERENT specific titles must still
  // refuse rather than being swept up by the new bare-stem tie-break (their stems differ).
  const SPECIFIC_MASTER_TITLES = ['Master of Arts', 'Master of Science', 'Master of Business Administration'];
  // (A value one of them DOES name -- "MS" = "Master of Science" -- resolves to that title in
  // the whole-title tier before this tie-break is ever reached; see the Workday block above.)
  expect('matchChoiceOption: bare-stem tie-break never fires for genuinely DIFFERENT specific titles -- "M.Eng" (named by none of them) still refuses (no regression)',
    Scanner.matchChoiceOption('M.Eng', SPECIFIC_MASTER_TITLES) === -1);
  expect('matchChoiceOption: "MS" resolves to its OWN title ("Master of Science") among different specific titles',
    Scanner.matchChoiceOption('MS', SPECIFIC_MASTER_TITLES) === 1);

  // Ground truth: a "have you ever worked here" screening question can render with NO literal
  // "Yes"/"No" option at all -- every choice is a full first-person sentence.
  const ROBINHOOD_WORKED_HERE_OPTIONS = [
    'I currently work at Robinhood as a full-time employee or intern',
    'I have previously worked at Robinhood as a full-time employee or intern (Hoodie Alumni)',
    'I currently work at Robinhood in a contractor role',
    'I have previously worked at Robinhood in a contractor role',
    'I have never worked at Robinhood'
  ];
  expect('matchChoiceOption: a plain "No" resolves to the one flat-denial sentence when no option literally starts with "No"',
    Scanner.matchChoiceOption('No', ROBINHOOD_WORKED_HERE_OPTIONS) === 4);
  expect('matchChoiceOption: the same fallback never fires for "Yes" (unchanged, no fitting option to guess at)',
    Scanner.matchChoiceOption('Yes', ROBINHOOD_WORKED_HERE_OPTIONS) === -1);
  // Negative control: a "never"-shaped fallback must still refuse when it would be ambiguous.
  const TWO_NEVER_OPTIONS = ['I have never worked here', 'I have never lived here'];
  expect('matchChoiceOption: "no" -> "never" fallback still refuses when TWO options both read as a flat denial (never guesses)',
    Scanner.matchChoiceOption('No', TWO_NEVER_OPTIONS) === -1);
})();

// --- combobox / button-group / Lever-location: async fill + verify behavior, run STRICTLY
//     SEQUENTIALLY (same reasoning as the Workday dropdown/prompt block above: shared timers
//     and MutationObservers on one document behave most predictably one step at a time). -----
pending.push((async () => {
  const doc = dom.window.document;
  const scanned = Scanner.scanFields(doc);
  const byId = id => doc.getElementById(id);
  const fieldByLabel = label => scanned.fields.find(f => f.label === label);
  const entryFor = field => scanned.registry[field.id];

  // ---- combobox: static option list (Country) ----
  {
    const field = fieldByLabel('Country*');
    expect('Country combobox scanned as ONE field with widget "combobox", never a plain text field',
      !!field && field.widget === 'combobox');
    const entry = entryFor(field);

    // NEGATIVE CONTROL: typed-but-not-selected text is never read back as a committed value --
    // react-select drops it on blur; the mock does too (see test-page.html).
    Scanner.setNativeValue(entry.input, 'United States of America');
    expect('typed-but-not-selected combobox text is NOT reported as a committed value',
      Scanner.getComboboxCommittedValue(entry) === '');

    const ok = await Scanner.applyFill(entry, 'United States of America');
    expect('applyFill on a combobox resolves to a boolean', typeof ok === 'boolean');
    expect('Country combobox filled and committed (menu opened via mouseup, option selected via mousedown+click)',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'United States of America');
    expect('the search input is EMPTY after a genuine commit (never leftover search text)',
      byId('gh_country_input').value === '');

    const okBad = await Scanner.applyFill(entry, 'Atlantis');
    expect('Country combobox: no confident match -> false, the earlier commit is untouched',
      okBad === false && Scanner.getComboboxCommittedValue(entry) === 'United States of America');
    expect('Country combobox failure reason never quotes the attempted value ("Atlantis")',
      !String(entry._lastReason || '').includes('Atlantis'));
    // "Atlantis" filters the rendered catalog down to ZERO rows ("no options rendered while
    // filtering") -- a DIFFERENT failure than "Can" below (which filters to a single
    // non-matching option, "no confident match AMONG the filtered options"). This build's
    // clear-and-recheck-the-unfiltered-list recovery (see fillComboboxOne) means the "zero
    // rows" case no longer gives up empty-handed either: it re-reads the real (small, static)
    // catalog after clearing the search text and carries THAT as optionsSeen, same contract as
    // every other failed-match tier -- content.js's second-chance /resolve round trip reads this
    // to give the service the real options it never saw at first.
    expect('Country combobox: even the "zero rows while filtering" failure now carries the recovered unfiltered catalog as optionsSeen (this build)',
      JSON.stringify(entry._lastOptions) === JSON.stringify(['Canada', 'United States of America', 'Mexico']));
    const okBad2 = await Scanner.applyFill(entry, 'Can');
    expect('Country combobox: a second no-confident-match value (filters to a single non-matching option) also fails, commit still untouched',
      okBad2 === false && Scanner.getComboboxCommittedValue(entry) === 'United States of America');
    expect('Country combobox: entry._lastOptions carries the actual rendered (filtered) option texts as structured data',
      JSON.stringify(entry._lastOptions) === JSON.stringify(['Canada']));
    // Ground truth (live, 2026-09-25, job-boards.greenhouse.io/twitch): Greenhouse's Escape only
    // clears the search text, so a failed fill used to leave the real menu OPEN over the form.
    // The mock now behaves the same way (see test-page.html's makeReactSelect).
    expect('Country combobox: a failed fill leaves its menu CLOSED, never open over the form',
      byId('gh_country_menu').style.display !== 'block');
  }

  // ---- combobox: ambiguous negative control (two rendered options both contain "Engineer") ----
  {
    const field = fieldByLabel('Role*');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'Engineer');
    expect('combobox ambiguous negative control: "Engineer" matches BOTH "Software Engineer" and "Site Engineer" -> stays unfilled',
      ok === false && Scanner.getComboboxCommittedValue(entry) === '');
    expect('combobox ambiguous negative control: the refused fill leaves the menu CLOSED',
      byId('gh_ambiguous_menu').style.display !== 'block');
    // Negative control for optionsSeen too: two GENUINELY DIFFERENT option texts both matching
    // is still real ambiguity -- entry._lastOptions is still populated (so a second-chance
    // /resolve round trip can still try), but the fill itself still correctly refuses above.
    expect('combobox ambiguous negative control: entry._lastOptions still carries both (different) option texts',
      JSON.stringify(entry._lastOptions) === JSON.stringify(['Software Engineer', 'Site Engineer']));
  }

  // ---- combobox: Greenhouse "State", static list rendered as bare 2-letter codes, first read
  // forced empty (live probe, 2026-09-24, job-boards.greenhouse.io/oura's 'State*' field) --
  // proves BOTH halves of the fix together: the US state cross-match (matchChoiceOption), and
  // the clear-and-recheck-the-unfiltered-list recovery after a typed filter renders nothing
  // (fillComboboxOne) -- `emptyOnFirstOpen` means the recovery path is the ONLY way to ever see
  // the real list here. ----
  {
    const field = fieldByLabel('State*');
    expect('State combobox scanned as a combobox field', !!field && field.widget === 'combobox');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'Washington');
    expect('State combobox: "Washington" resolves to "WA" -- typing it first filters the real react-select mock to nothing, then the clear-and-recheck fallback finds it in the restored unfiltered list',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'WA');

    // NEGATIVE CONTROL, same fixture: a value that matches nothing at all -- not a state, not a
    // code -- must still fail honestly even after the recovery attempt, with a reason listing
    // the real (recovered) option texts, never the attempted value.
    const okBad = await Scanner.applyFill(entry, 'Mars');
    expect('State combobox negative control: "Mars" is not a state -> false even after recovery, the earlier commit is untouched',
      okBad === false && Scanner.getComboboxCommittedValue(entry) === 'WA');
    expect('State combobox negative control: the failure reason names real recovered option texts, never quotes the attempted "Mars"',
      /saw: /.test(entry._lastReason) && !entry._lastReason.includes('Mars'));
    // reasonWithOptions() truncates the human-readable SENTENCE to 8 options, but
    // entry._lastOptions (structured data for content.js's second-chance /resolve round trip)
    // carries the actual FULL recovered list -- proof the recovery reached the real "WA" option,
    // not just whichever 8 happened to be first alphabetically.
    expect('State combobox negative control: entry._lastOptions carries the full RECOVERED unfiltered list (all 51 codes), including "WA"',
      Array.isArray(entry._lastOptions) && entry._lastOptions.length === 51 && entry._lastOptions.includes('WA'));
  }

  // ---- combobox: Greenhouse "Degree", simple generic-level shape (one option per family, e.g.
  // SoFi's real embed field) -- resolves via the degree-family match on the very first
  // unfiltered read, no typing ever needed. ----
  {
    const field = fieldByLabel('Degree*');
    expect('Degree (simple) combobox scanned as a combobox field', !!field && field.widget === 'combobox');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'Bachelor of Design');
    expect('Degree combobox (simple generic levels): "Bachelor of Design" resolves to "Bachelor\'s Degree" via the degree-family match, no typing needed',
      ok === true && Scanner.getComboboxCommittedValue(entry) === "Bachelor's Degree");
  }

  // ---- combobox: Greenhouse "Degree", the REAL Robinhood shape -- BOTH "Bachelors" and
  // "Bachelor's Degree" render as separate options (live probe, 2026-09-24,
  // boards.greenhouse.io/robinhood's 'Degree*' field). Proves the bare-stem duplicate-spelling
  // tie-break end to end, not just as a pure-function check. ----
  {
    const field = fieldByLabel('Degree (Robinhood shape)*');
    expect('Degree (Robinhood shape) combobox scanned as a combobox field', !!field && field.widget === 'combobox');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'Bachelor of Design');
    expect('Degree combobox (Robinhood shape): "Bachelor of Design" resolves to the fuller "Bachelor\'s Degree", never the bare "Bachelors" duplicate',
      ok === true && Scanner.getComboboxCommittedValue(entry) === "Bachelor's Degree");
  }

  // ---- combobox: Greenhouse screening question with NO literal Yes/No option -- the real
  // Robinhood shape (live probe, 2026-09-24, boards.greenhouse.io/robinhood's "Have you ever
  // worked for Robinhood...?" field): every option is a full first-person sentence. ----
  {
    const field = fieldByLabel('Have you ever worked for Robinhood as an employee, intern or contractor?*');
    expect('Robinhood-shape screening combobox scanned as a combobox field', !!field && field.widget === 'combobox');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'No');
    expect('screening combobox: "No" resolves to the one flat-denial sentence ("I have never worked at Robinhood"), never fails just because no option literally starts with "No"',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'I have never worked at Robinhood');
  }

  // ---- combobox: async/filtered catalog (School) -- ground truth: "No options" is shown
  // until the debounced request resolves; never blind-pick the first alphabetical row. ----
  {
    const field = fieldByLabel('School*');
    const entry = entryFor(field);
    const okBad = await Scanner.applyFill(entry, 'Underwater Basket Weaving University');
    expect('School combobox (async catalog): no options ever match -> false, never the first alphabetical row',
      okBad === false && Scanner.getComboboxCommittedValue(entry) === '');

    const ok = await Scanner.applyFill(entry, 'University of Washington');
    expect('School combobox (async catalog): types to filter, waits for the debounced results, and commits the exact match',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'University of Washington');
  }

  // ---- combobox: multi-select (chips) ----
  {
    const field = fieldByLabel('Languages spoken*');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, ['English', 'French']);
    expect('multi-select combobox: a LIST value adds each option as its own chip',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'English, French');
    const ok2 = await Scanner.applyFill(entry, 'Spanish');
    expect('multi-select combobox: adding one more value keeps the earlier chips (never replaces them)',
      ok2 === true && Scanner.getComboboxCommittedValue(entry) === 'English, French, Spanish');
  }

  // ---- generic ARIA combobox (no react-select classes at all) ----
  {
    const field = fieldByLabel('Preferred office');
    const entry = entryFor(field);
    const input = byId('generic_combo_input');
    const highlightOnly = byId('generic_combo_opt_2'); // "Remote" -- never actually chosen below

    // Reviewer round 3, negative control FIRST: aria-activedescendant tracks the HIGHLIGHTED
    // row during ArrowDown navigation per the real ARIA combobox pattern, not a completed
    // choice -- a page that only moves focus (hover, or ArrowDown with no Enter/click) must
    // never be read as a committed value just because activedescendant now names an option.
    input.setAttribute('aria-activedescendant', highlightOnly.id);
    expect('generic ARIA combobox: aria-activedescendant ALONE (option not aria-selected) is never read as a committed value',
      Scanner.getComboboxCommittedValue(entry) === '');
    highlightOnly.setAttribute('aria-selected', 'true'); // now aria-selected too, but...
    input.setAttribute('aria-expanded', 'true'); // ...the listbox is still open -- still not a commit
    expect('generic ARIA combobox: aria-selected="true" while the listbox is STILL OPEN is also never read as committed',
      Scanner.getComboboxCommittedValue(entry) === '');
    input.setAttribute('aria-expanded', 'false');
    expect('generic ARIA combobox: only once aria-selected="true" AND the listbox is closed does it read as committed',
      Scanner.getComboboxCommittedValue(entry) === 'Remote');
    // restore untouched fixture state for the real fill immediately below
    input.removeAttribute('aria-activedescendant');
    highlightOnly.removeAttribute('aria-selected');
    input.setAttribute('aria-expanded', 'false');

    const ok = await Scanner.applyFill(entry, 'Austin');
    expect('generic ARIA combobox (opened via ArrowDown, no "Toggle flyout" button) filled and verified via aria-activedescendant',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'Austin');
  }

  // Looks a scanned combobox field up by its OWN input element rather than by label text --
  // several fixtures on this page are deliberately labelled just "Country" (the phone-country
  // widget matches the REAL Greenhouse markup, which carries no distinguishing label of its
  // own beyond "Country" -- see gh_phone_country_label), so fieldByLabel() alone would resolve
  // to whichever one happens to scan first.
  const fieldForInputId = id => {
    const el = byId(id);
    return scanned.fields.find(f => { const e = scanned.registry[f.id]; return e && e.input === el; });
  };

  // ---- combobox: Greenhouse phone "Country" (dial-code widget) ----
  // Ground truth (live probe, 2026-09-24, job-boards.greenhouse.io/gitlab and /twilio): once
  // committed, the combobox's OWN chip collapses to just the dial code ("+1") -- ONLY the
  // paired intl-tel-input button's aria-label still names the country. Before this fix,
  // verifyComboboxSelection compared the matched option text ("United States +1") against
  // that bare "+1" chip and always reported "selection did not commit".
  {
    const field = fieldForInputId('gh_phone_country_input');
    expect('phone Country combobox scanned as its own combobox field', !!field && field.widget === 'combobox');
    const entry = entryFor(field);
    const itiBtn = byId('gh_phone_iti_button');
    expect('fixture sanity: the paired intl-tel-input button starts with nothing selected',
      itiBtn.getAttribute('aria-label') === 'Select country');

    // "United" alone types down to a filtered set containing BOTH "United States +1" and
    // "United Kingdom +44" -- ambiguous, so this also exercises the "among filtered options"
    // reason enrichment (problem 3) with the real rendered option texts.
    const okBad = await Scanner.applyFill(entry, 'United');
    expect('phone Country combobox: "United" is ambiguous (States vs Kingdom) -> false, reason names the options it saw',
      okBad === false && /saw: /.test(entry._lastReason) &&
      /United States \+1/.test(entry._lastReason) && /United Kingdom \+44/.test(entry._lastReason));

    const ok = await Scanner.applyFill(entry, 'United States');
    expect('phone Country combobox: "United States" resolves against the rendered "United States +1" and reports success',
      ok === true);
    expect('phone Country combobox: the paired intl-tel-input button now names the committed country (not just its dial code)',
      itiBtn.getAttribute('aria-label') === 'Change country, selected United States (+1)');
    expect('phone Country combobox: its OWN chip only ever shows the dial code, never the country name -- the real bug this guards against',
      byId('gh_phone_country_value').querySelector('.select__single-value').textContent === '+1');
    // Reviewer round 6 (live probe, 2026-09-24/25 — every Greenhouse posting checked): round 5's
    // own fix left entry._committedText as the FULL matched option text ("United States +1")
    // while a later getCurrentValue() re-read this SAME widget via its chip/generic combobox
    // path -- still just "+1", never "United States +1" -- so content.js's verify sweep compared
    // two honestly-different strings for the SAME correct fill and reported "didn't stick" on
    // every single one. Both sides must now defer to the SAME paired intl-tel-input button (via
    // phoneCountrySelectedName/phoneCountryOptionName), landing on the bare country NAME with no
    // dial-code suffix on either side.
    expect('blocker A round 6: entry._committedText now carries the bare country NAME ("United States"), not "United States +1"',
      entry._committedText === 'United States');
    expect('blocker A round 6: getCurrentValue() reads the SAME paired-button NAME as entry._committedText -- the exact equality content.js\'s verify sweep checks',
      Scanner.getCurrentValue(entry) === entry._committedText);

    // NEGATIVE CONTROL: a GENUINE revert of this exact widget shape (both signals reset -- the
    // chip cleared AND the paired button back to "Select country") must still read back empty,
    // never silently keep reporting the stale committed country -- proving getCurrentValue()
    // re-reads the button's CURRENT state every time rather than caching/trusting the commit
    // forever, and never defaults an unselected combobox to "the applicant's country" just
    // because that happens to be what was last filled.
    byId('gh_phone_country_value').querySelector('.select__single-value').remove();
    itiBtn.setAttribute('aria-label', 'Select country');
    expect('blocker A round 6 NEGATIVE CONTROL: a genuinely reverted phone-country widget reads back empty, never the stale committed country, and no longer equals entry._committedText',
      Scanner.getCurrentValue(entry) === '' && Scanner.getCurrentValue(entry) !== entry._committedText);
  }

  // ---- combobox: Greenhouse phone "Country", ALREADY showing the applicant's country ----
  // Per the project brief: never overwrite an already-correct value. Proven here by asserting
  // the mock's menu was never even opened, not just that the end state looks right.
  {
    const field = fieldForInputId('gh_phone_prefilled_country_input');
    const entry = entryFor(field);
    const itiBtn = byId('gh_phone_prefilled_iti_button');
    expect('fixture sanity: the paired intl-tel-input button already names "United States" before any fill',
      itiBtn.getAttribute('aria-label') === 'Change country, selected United States (+1)');
    const opensBefore = dom.window.__gh_phone_country_prefilled__.openCount();

    const ok = await Scanner.applyFill(entry, 'United States');
    expect('phone Country combobox already correct: reports success without being touched',
      ok === true);
    expect('phone Country combobox already correct: the menu was NEVER opened (never reopen/re-click an already-correct selection)',
      dom.window.__gh_phone_country_prefilled__.openCount() === opensBefore);
    expect('phone Country combobox already correct: the iti button label is unchanged',
      itiBtn.getAttribute('aria-label') === 'Change country, selected United States (+1)');
    // "Kept, not filled" still ends up with entry._committedText/getCurrentValue() agreeing on
    // the SAME representation ("United States", not "United States +1") -- the "never overwrite"
    // path is never a second, differently-shaped code path from the ordinary commit above.
    expect('phone Country combobox already correct: entry._committedText is the bare country NAME, same representation as a fresh commit',
      entry._committedText === 'United States');
    expect('phone Country combobox already correct: getCurrentValue() agrees with entry._committedText',
      Scanner.getCurrentValue(entry) === entry._committedText);
  }

  // ---- combobox: Greenhouse "Location (City)", geocoded "City, State, Country" catalog ----
  // Ground truth (live probe, 2026-09-24, job-boards.greenhouse.io/twilio): typing "Seattle"
  // also returns same-state near-misses ("Seattle Hill-Silver Firs", "South Seattle") that
  // legitimately contain "Seattle" as a whole word -- matchCityStateOption must still resolve
  // uniquely to the option whose OWN city segment is EXACTLY "Seattle".
  {
    const field = fieldByLabel('Location (City)*');
    expect('Greenhouse Location (City) combobox scanned as a combobox field', !!field && field.widget === 'combobox');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'Seattle, Washington');
    expect('Location (City) combobox: "City, State" resolves against "City, State, Country" options, never a same-state near-miss',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'Seattle, Washington, United States');
  }

  // ---- combobox: Location (City), two geocode results render identically -> not real
  //      ambiguity (this build): the applicant could never tell the two apart either, so the
  //      first is committed rather than refusing outright. See matchCityStateOption's own direct
  //      unit checks above for the negative control (two DIFFERENT texts still refuse). ----------
  {
    const field = fieldByLabel('Location (City) duplicate*');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'Seattle, Washington');
    expect('Location (City) combobox: two identically-rendered options -> not real ambiguity, the first is committed',
      ok === true && Scanner.getComboboxCommittedValue(entry) === 'Seattle, Washington, United States');
    expect('Location (City) duplicate case: entry._lastOptions is cleared (null) on success, never a stale list from some earlier attempt',
      entry._lastOptions == null);
  }

  // ---- Ashby Yes/No button groups: scoped to their OWN question ----
  {
    const workauthEntry = entryFor(fieldByLabel('Are you permanently authorized to work in the United States without visa sponsorship?'));
    const sponsorEntry = entryFor(fieldByLabel('Will you now or in the future require sponsorship to work in the US?'));

    const okNo = await Scanner.applyFill(workauthEntry, 'No');
    expect('Ashby work-authorization button group: "No" clicked and verified selected',
      okNo === true && byId('ashby_workauth_no').getAttribute('aria-pressed') === 'true');
    const okYes = await Scanner.applyFill(sponsorEntry, 'Yes');
    expect('Ashby sponsorship button group: "Yes" clicked and verified selected -- a SEPARATE question, unaffected by the one above',
      okYes === true && byId('ashby_sponsor_yes').getAttribute('aria-pressed') === 'true');
    expect('clicking the sponsorship question never touched the work-authorization question\'s own selection',
      byId('ashby_workauth_no').getAttribute('aria-pressed') === 'true' &&
      byId('ashby_workauth_yes').getAttribute('aria-pressed') !== 'true');
    expect('the hidden checkbox mirror also reflects each button-group\'s own choice',
      byId('ashby_workauth_entry').querySelector('input[type="checkbox"]').checked === false &&
      byId('ashby_sponsor_entry').querySelector('input[type="checkbox"]').checked === true);
  }

  // ---- Ashby Yes/No decoy: the group fills correctly, the decoy submit is NEVER clicked ----
  {
    const decoyEntry = entryFor(fieldByLabel('Are you comfortable relocating?'));
    const before = dom.window.__ATS_WIDGETS_SUBMIT_COUNT__ || 0;
    const ok = await Scanner.applyFill(decoyEntry, 'Yes');
    expect('button group beside a decoy submit button still fills correctly',
      ok === true && byId('ashby_decoy_yes').getAttribute('aria-pressed') === 'true');
    expect('the decoy submit button next to the group was NEVER clicked (zero submissions)',
      (dom.window.__ATS_WIDGETS_SUBMIT_COUNT__ || 0) === before);
  }

  // Reviewer round 3: fillButtonGroup's verification must require the state CHANGED because
  // of OUR click. A hidden-checkbox mirror that was ALREADY checked before anything happened
  // (e.g. still reflecting some earlier, unrelated state) must never be trusted as evidence
  // that a click the page's own handler completely ignores actually selected anything.
  {
    const container = doc.createElement('div');
    const yesBtn = doc.createElement('button');
    yesBtn.type = 'button';
    yesBtn.textContent = 'Yes';
    const noBtn = doc.createElement('button');
    noBtn.type = 'button';
    noBtn.textContent = 'No';
    const mirror = doc.createElement('input');
    mirror.type = 'checkbox';
    mirror.checked = true; // already checked before the click -- and NO listener ever changes it
    container.appendChild(yesBtn);
    container.appendChild(noBtn);
    container.appendChild(mirror);
    doc.body.appendChild(container);

    const brokenEntry = { buttons: [yesBtn, noBtn], container: container, label: 'Broken button group (negative control)' };
    const r = await Scanner.fillButtonGroup(brokenEntry, 'No', doc);
    expect('fillButtonGroup: a stale, already-checked hidden-mirror checkbox is NOT trusted as evidence that a click the page ignores actually selected "No"',
      r.ok === false);
    container.remove();
  }

  // ---- button-group: "no confident match" also carries optionsSeen as structured data (this
  //      build) -- not only baked into the reason string -- so content.js's second-chance
  //      /resolve round trip can give the service the real options it never saw at first. ------
  {
    const container = doc.createElement('div');
    const visaBtn = doc.createElement('button');
    visaBtn.type = 'button';
    visaBtn.textContent = 'Yes, I will require H-1B sponsorship';
    const otherVisaBtn = doc.createElement('button');
    otherVisaBtn.type = 'button';
    otherVisaBtn.textContent = 'Yes, I will require TN visa support';
    const noBtn2 = doc.createElement('button');
    noBtn2.type = 'button';
    noBtn2.textContent = 'No, I will not require sponsorship';
    container.appendChild(visaBtn);
    container.appendChild(otherVisaBtn);
    container.appendChild(noBtn2);
    doc.body.appendChild(container);

    const sponsorBgEntry = { buttons: [visaBtn, otherVisaBtn, noBtn2], container: container, label: 'Sponsorship button group (optionsSeen check)' };
    const rBg = await Scanner.fillButtonGroup(sponsorBgEntry, 'Yes', doc);
    expect('button-group: a short "Yes" against two "Yes, ..." options is ambiguous -> false, never the first',
      rBg.ok === false);
    expect('button-group: optionsSeen carries the actual rendered button texts as structured data',
      JSON.stringify(rBg.optionsSeen) === JSON.stringify([
        'Yes, I will require H-1B sponsorship', 'Yes, I will require TN visa support', 'No, I will not require sponsorship'
      ]));
    container.remove();
  }

  // ---- button-group: reviewer round 5, blocker B (false citizenship claim), end-to-end
  //      through the real applyFill() entry-point -- proves fillOptions.canary threads all the
  //      way down to fillButtonGroup's own matchChoiceOption call. Unlike the sponsorship group
  //      above, this fixture has only ONE "Yes, ..." option -- never ambiguous by COUNT, which
  //      is exactly the gap a canary-tier value must refuse on its own wording instead. ---------
  {
    function makeCitizenGroup() {
      const groupContainer = doc.createElement('div');
      const yesBtn = doc.createElement('button');
      yesBtn.type = 'button';
      yesBtn.textContent = 'Yes, I am a U.S. citizen or permanent resident';
      yesBtn.setAttribute('aria-pressed', 'false');
      const noBtn = doc.createElement('button');
      noBtn.type = 'button';
      noBtn.textContent = 'No, I will require sponsorship';
      noBtn.setAttribute('aria-pressed', 'false');
      // A real click -> aria-pressed toggle (same observable contract isChoiceButtonSelected()
      // reads), so fillButtonGroup's own post-click verification has something genuine to see —
      // without this, a bare <button> with no listener at all would make even a CORRECT match
      // report "button click did not register as selected", which is not what this test is
      // about (see the panel-test tab / matchChoiceOption unit checks above for the matching
      // logic itself; this block is purely about the fillOptions.canary threading).
      [yesBtn, noBtn].forEach((btn) => {
        btn.addEventListener('click', () => {
          yesBtn.setAttribute('aria-pressed', String(btn === yesBtn));
          noBtn.setAttribute('aria-pressed', String(btn === noBtn));
        });
      });
      groupContainer.appendChild(yesBtn);
      groupContainer.appendChild(noBtn);
      doc.body.appendChild(groupContainer);
      return { kind: 'button-group', buttons: [yesBtn, noBtn], container: groupContainer, label: 'Citizenship (blocker B fixture)', yesBtn: yesBtn };
    }

    const nonCanaryEntry = makeCitizenGroup();
    const nonCanaryOk = await Scanner.applyFill(nonCanaryEntry, 'Yes');
    expect('blocker B regression guard: a NON-canary "Yes" keeps today\'s behaviour unchanged -- commits the only "Yes, ..." option',
      nonCanaryOk === true && Scanner.isChoiceButtonSelected(nonCanaryEntry.yesBtn) === true);
    nonCanaryEntry.container.remove();

    const canaryEntry = makeCitizenGroup();
    const canaryOk = await Scanner.applyFill(canaryEntry, 'Yes', { canary: true });
    expect('blocker B: applyFill(entry, "Yes", {canary: true}) REFUSES to commit the bundled citizenship claim',
      canaryOk === false);
    expect('blocker B: the citizenship button was never actually selected',
      Scanner.isChoiceButtonSelected(canaryEntry.yesBtn) === false);
    expect('blocker B: the refusal still carries optionsSeen (structured data), so content.js\'s EXISTING second-chance /resolve path picks this field up exactly like any other "no confident match"',
      JSON.stringify(canaryEntry._lastOptions) === JSON.stringify([
        'Yes, I am a U.S. citizen or permanent resident', 'No, I will require sponsorship'
      ]));
    canaryEntry.container.remove();
  }

  // ---- real Ashby shape: no <form> at all, every button (including the decoy) type-less ----
  {
    const field = fieldByLabel('Are you legally authorized to work in this country?');
    expect('the no-form Ashby button group is scanned like any other button group', !!field && field.widget === 'button-group');
    const entry = entryFor(field);
    const ok = await Scanner.applyFill(entry, 'No');
    expect('real-shape (type-less, no <form>) Ashby button group: "No" clicked and verified selected via its hashed "selected" class',
      ok === true && byId('ashby_noform_no').className.indexOf('selected') !== -1);
    expect('the type-less DECOY "Submit Application" beside it (also no form) was NEVER clicked',
      !dom.window.__ASHBY_NOFORM_SUBMIT_COUNT__);
  }

  // ---- Lever location type-ahead ----
  {
    const field = fieldByLabel('Current location');
    expect('Lever location field scanned with a CLEAN label (no dropdown/status text pollution from the wrapping <label>)',
      !!field);
    const entry = entryFor(field);

    const okAmbiguous = await Scanner.applyFill(entry, 'Seattle');
    expect('Lever location: city alone matches suggestions in TWO different states -- ambiguous, never guesses',
      okAmbiguous === false && byId('lever_selected_location').value === '');

    const ok = await Scanner.applyFill(entry, 'Seattle, Washington');
    expect('Lever location: city + full state name resolves to the ONE matching suggestion, committed via mousedown',
      ok === true && byId('lever_selected_location').value === 'Seattle, Washington');

    byId('lever_selected_location').value = '';
    const okCode = await Scanner.applyFill(entry, 'Austin, TX');
    expect('Lever location: city + 2-letter state CODE also resolves (via the existing US state table)',
      okCode === true && byId('lever_selected_location').value === 'Austin, Texas');

    byId('lever_selected_location').value = '';
    const okBad = await Scanner.applyFill(entry, 'Nowhereville, Idaho');
    expect('Lever location: no suggestion matches -> false, selectedLocation stays empty',
      okBad === false && byId('lever_selected_location').value === '');

    // A live probe (2026-09-24) showed the service can send a resolved-suggestion JSON object
    // string instead of a plain "City, State" string -- must still resolve correctly.
    byId('lever_selected_location').value = '';
    const okJson = await Scanner.applyFill(entry, '{"name":"Austin, Texas, United States","id":"abc123"}');
    expect('Lever location: a JSON-object value ({"name": "City, State, Country", ...}) is unwrapped and still resolves',
      okJson === true && byId('lever_selected_location').value === 'Austin, Texas');

    // Reviewer round 6, live probe (2026-09-25, jobs.lever.co/zerohomes): the REAL widget's own
    // hidden `selectedLocation` field commits a JSON-encoded object itself
    // ({"name":"Seattle, WA, USA","id":"f93b..."}), not the plain display string this mock's own
    // commit path happens to write above -- getCurrentValue() must unwrap it down to the SAME
    // human name fillLeverLocation()'s own matchedText already stashes onto entry._committedText
    // (applyFill()'s lever-location branch), or a genuinely-correct commit would forever compare
    // a name against its own raw JSON and misreport "didn't stick" -- the exact live bug this
    // build fixes.
    byId('lever_selected_location').value = '{"name":"Seattle, WA, USA","id":"f93b1234-5678-90ab-cdef-1234567890ab"}';
    expect('blocker A round 6: Lever location getCurrentValue() unwraps a JSON-encoded hidden value down to its plain "name", never the raw JSON',
      Scanner.getCurrentValue(entry) === 'Seattle, WA, USA');

    // NEGATIVE CONTROL: a genuinely EMPTY hidden field (a real revert — Lever clears it as soon
    // as the search query is edited again, or the widget wipes it on its own re-render) must
    // still read back empty, never some stale leftover.
    byId('lever_selected_location').value = '';
    expect('blocker A round 6 NEGATIVE CONTROL: Lever location getCurrentValue() reads back empty once the hidden field is genuinely cleared',
      Scanner.getCurrentValue(entry) === '');
  }

  // ---- final safety check: none of the choice-widget interactions above ever submitted the
  // ats-widgets-form (the decoy submit button check above covers the SPECIFIC decoy; this
  // covers the whole form as one last sweep) ----
  expect('none of the choice-widget fills above ever submitted the ats-widgets-form',
    !(dom.window.__ATS_WIDGETS_SUBMIT_COUNT__ > 0));

  // ---- proof the decoy is a REAL trap: bypassing every guard and clicking it directly DOES
  // submit (same convention as the pre-existing trap-form / Workday decoy fixtures) ----
  {
    const before = dom.window.__ATS_WIDGETS_SUBMIT_COUNT__ || 0;
    byId('ashby_decoy_submit').click();
    expect('bypassing every guard and clicking the decoy submit button directly DOES fire its form\'s submit handler',
      (dom.window.__ATS_WIDGETS_SUBMIT_COUNT__ || 0) === before + 1);
  }
  {
    const before = dom.window.__ASHBY_NOFORM_SUBMIT_COUNT__ || 0;
    byId('ashby_noform_submit').click();
    expect('bypassing every guard and clicking the type-less, form-less decoy directly DOES fire its own click handler (a genuine trap, not vacuous)',
      (dom.window.__ASHBY_NOFORM_SUBMIT_COUNT__ || 0) === before + 1);
  }
})());

// --- Shadow DOM: scanAll/scanFields and capture.js must traverse OPEN shadow roots
//     (recursively), with fills/guards working on elements inside them. See test-page.html's
//     "Shadow DOM" fixture block for the exact shapes covered here. -------------------------
pending.push((async () => {
  const doc = dom.window.document;
  const shadowHost = doc.getElementById('shadow_dom_host');
  const shadowRoot = shadowHost.shadowRoot;
  const nameInput = shadowRoot.getElementById('shadow_name_input');
  const submitBtn = shadowRoot.getElementById('shadow_submit_btn');
  const nestedInput = shadowRoot.getElementById('shadow_nested_host').shadowRoot.getElementById('shadow_nested_input');
  const hiddenInput = doc.getElementById('shadow_dom_hidden_host').shadowRoot.getElementById('shadow_hidden_input');

  expect('fixture sanity: the shadow host really does carry an open shadow root', !!shadowRoot);
  expect('a plain scanFields() over the whole document (no shadow-DOM awareness) does NOT see into an open shadow root',
    !Scanner.scanFields(doc).fields.some(f => f.name === 'shadow_name' || f.name === 'shadow_nested'));

  const scanned = Scanner.scanAll(doc);
  const shadowField = scanned.fields.find(f => f.name === 'shadow_name');
  const nestedField = scanned.fields.find(f => f.name === 'shadow_nested');
  expect('scanAll() finds the field inside the open shadow root, tagged shadow:true',
    !!shadowField && shadowField.shadow === true);
  expect('scanAll() also finds the field inside the NESTED open shadow root (recursion)',
    !!nestedField && nestedField.shadow === true);
  expect('scanAll() reports zero fields for the CLOSED shadow root (unreachable, by design)',
    !scanned.fields.some(f => f.name === 'shadow_closed_unreachable'));
  expect('isVisible() crosses the shadow boundary via .host: a field inside an open shadow root is still hidden by an aria-hidden LIGHT-DOM ancestor OUTSIDE the host',
    Scanner.isVisible(hiddenInput) === false);
  expect('...and that hidden shadow field never turns up in scanAll() at all',
    !scanned.fields.some(f => f.name === 'shadow_hidden'));
  expect('fixture sanity: the genuinely visible shadow field IS reported visible', Scanner.isVisible(nameInput) === true);

  const entry = scanned.registry[shadowField.id];
  const ok = await Scanner.applyFill(entry, 'Ada Lovelace');
  expect('applyFill() fills a text input living inside an open shadow root, and it reads back',
    ok === true && nameInput.value === 'Ada Lovelace');

  expect('none of the scanning/filling above ever clicked the submit button living inside the SAME shadow root',
    (dom.window.__SHADOW_FORM_SUBMIT_COUNT__ || 0) === 0);

  // Genuine trap, not vacuous: a direct click (bypassing every guard) DOES submit -- same
  // discipline as every other decoy submit button on this page.
  submitBtn.click();
  expect('bypassing every guard and clicking the shadow-DOM submit button directly DOES fire its form\'s submit handler',
    (dom.window.__SHADOW_FORM_SUBMIT_COUNT__ || 0) === 1);

  // ---- capture.js: structure-only capture also reaches into open shadow roots ----
  const structure = Capture.captureStructure(doc);
  const shadowNodes = structure.nodes.filter(n => n.shadow === true);
  expect('captureStructure() includes nodes captured from inside open shadow roots, tagged shadow:true',
    shadowNodes.some(n => n.id === 'shadow_name_input') && shadowNodes.some(n => n.id === 'shadow_submit_btn'));
  expect('captureStructure() also reaches the NESTED open shadow root',
    shadowNodes.some(n => n.id === 'shadow_nested_input'));
  expect('captureStructure() never captures anything from the CLOSED shadow root (unreachable)',
    !structure.nodes.some(n => n.id === 'shadow_closed_input'));
  expect('captureStructure() never records a real field VALUE for the shadow text input (structure only, per its own privacy invariant)',
    !structure.nodes.some(n => n.id === 'shadow_name_input' && JSON.stringify(n).includes('Ada Lovelace')));
})());

// ---- Greenhouse (job-boards) Education blocks: section heading + zero-based block ids ----
// Markup mirrored from job-boards.greenhouse.io/twitch (2026-09-25): each block is a
// div.education--form holding its OWN div.education--header "Education" (not an h1-h6), and
// its fields' ids end "--0", "--1", ... (zero-based). Before this, no field in the block had a
// section, so "Start date year"/"End date year" (scanned apart from the comboboxes) were never
// recognised as education dates, and a second block's "school--1" read as the FIRST entry.
{
  const ghDom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
  ghDom.window.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10 };
  };
  const gdoc = ghDom.window.document;
  const combo = (slot, n, label) => `<div class="select"><div class="select__container"><label id="${slot}--${n}-label" for="${slot}--${n}" class="label select__label">${label}</label><div class="select-shell"><div><div class="select__control"><div class="select__value-container"><div class="select__input-container"><input id="${slot}--${n}" class="select__input" role="combobox" aria-labelledby="${slot}--${n}-label" type="text" autocomplete="off"></div></div><div class="select__indicators"><button type="button" aria-label="Toggle flyout" class="icon-button">v</button></div></div></div></div></div></div>`;
  const yearBox = (slot, n, label) => `<div class="text-input-wrapper"><div class="input-wrapper"><label id="${slot}--${n}-label" for="${slot}--${n}" class="label">${label}</label><input id="${slot}--${n}" type="number" class="input"></div></div>`;
  // The live nesting matters: each month combobox's input sits 9 levels below the level where
  // the "Education" header is a sibling (education--half-width-container > education--date-
  // container > div.select > ...), deeper than the general heading search reaches.
  const dates = (n) => `<div class="education--half-width-container"><div class="education--date-container">${combo('start-month', n, 'Start date month')}${yearBox('start-year', n, 'Start date year')}</div><div class="education--date-container">${combo('end-month', n, 'End date month')}${yearBox('end-year', n, 'End date year')}</div></div>`;
  const eduBlock = (n) => `<div class="education--form"><hr><div class="education--header">Education</div>${combo('school', n, 'School')}${combo('degree', n, 'Degree')}${combo('discipline', n, 'Discipline')}${dates(n)}${n ? '<button type="button">Remove education</button>' : ''}</div>`;
  const scanById = () => {
    const sc = Scanner.scanFields(gdoc);
    const m = {};
    sc.fields.forEach(f => { const e = sc.registry[f.id]; const el = (e && (e.input || e.el)) || null; if (el && el.id) m[el.id] = f; });
    return m;
  };
  gdoc.body.innerHTML = `<form id="application-form"><div class="application--questions"><div class="question--header">Why Twitch?</div><label for="why">Tell us why</label><textarea id="why"></textarea><label for="first_name">First Name</label><input id="first_name" type="text"></div><div class="education--container">${eduBlock(0)}${eduBlock(1)}<button type="button" class="add-another-button">Add another</button></div><div><label for="linkedin">LinkedIn Profile</label><input id="linkedin" type="text"></div></form>`;
  const byElId = scanById();
  for (const n of [0, 1]) {
    for (const slot of ['school', 'degree', 'discipline', 'start-month', 'start-year', 'end-month', 'end-year']) {
      const f = byElId[`${slot}--${n}`];
      expect(`Greenhouse Education block ${n + 1}: "${slot}--${n}" has section "Education" and 1-based section_index ${n + 1}`,
        !!f && f.section === 'Education' && f.section_index === n + 1);
    }
  }
  expect('the Education header never leaks onto a field AFTER its block (LinkedIn Profile)',
    !!byElId.linkedin && byElId.linkedin.section === '' && byElId.linkedin.section_index === null);
  expect('NEGATIVE CONTROL: a header-styled element whose text is not a repeating-section name ("Why Twitch?") is never a section',
    !!byElId.why && byElId.why.section === '');
  expect('countSectionBlocks counts both Greenhouse Education blocks (1-based, so 2 -- not 1)',
    Scanner.countSectionBlocks(gdoc, 'education') === 2);
  const ghAddBtn = Scanner.findAddButtonForKind(gdoc, 'education');
  expect('findAddButtonForKind finds the "Add another" button after the Education blocks',
    !!ghAddBtn && ghAddBtn.textContent === 'Add another');

  // NEGATIVE CONTROL: a form that numbers its blocks from 1 ("school--1", no "--0" twin
  // anywhere) keeps its own index -- only a real zero-based sequence is shifted.
  gdoc.body.innerHTML = `<form><div class="education--container">${eduBlock(1)}</div></form>`;
  const oneBased = scanById();
  expect('NEGATIVE CONTROL: ids numbered from 1 (no "--0" twin) keep section_index 1, never shifted to 2',
    !!oneBased['school--1'] && oneBased['school--1'].section_index === 1
    && !!oneBased['start-year--1'] && oneBased['start-year--1'].section_index === 1);
}

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
