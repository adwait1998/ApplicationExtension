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

const { fields } = Scanner.scanFields(dom.window.document);

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

let failed = 0;
console.log('\n--- checks ---');
for (const c of checks) {
  console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.name}`);
  if (!c.pass) failed++;
}
console.log(`\n${checks.length - failed}/${checks.length} checks passed.`);
process.exit(failed ? 1 : 0);
