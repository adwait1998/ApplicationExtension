# Copilot Extension v2 — fill everything, and own the profile

**Status:** design, 2026-09-24
**Builds on:** `2026-09-23-copilot-extension-design.md` (v1: the ladder, the never-submit rule)

## What v1 proved, and where it stopped

v1 was validated on two real ATS forms — the first live ATS run of this feature. On a
Tessera Labs (Ashby-style) form it filled 6 fields correctly, including **both** canary
questions (work authorisation → Yes, sponsorship → Yes), which is the answer that matters
most and the one we refuse to let a model near.

Two real gaps showed up:

1. **A Workday work-experience step filled 0 of 10 fields.** Every skip said "no
   deterministic match". That diagnosis was wrong in an important way: the fields (Job Title,
   Company, Location, From, To, Role Description) are perfectly recognisable — **the profile
   simply has no work history to put in them.** This is a DATA gap, not a matching gap, and
   no amount of smarter classification fixes it.
2. `"First and Last Legal Name"` was not matched, though `personal.full_name` was right
   there. A pattern gap.

## Goal

Fill the fields v1 leaves amber — including the non-deterministic ones — and let the operator
create and edit profiles from the extension itself, without editing JSON by hand.

The never-submit rule is unchanged and non-negotiable.

## The insight: most of this is already built

The autonomous pipeline solved "answer an arbitrary application question" in Reliability v2
Phase D. That machinery is sitting in the repo, unused by the extension:

- **`apply/answer_cache.py`** — `AnswerCache(profile, bank_path, threshold)` with
  `.answer(question, context=...) -> AnswerResult(answer, source, similarity, llm_called,
  matched_q)`. It embeds the question, finds the nearest previously-answered one, and only
  calls an LLM on a miss. Crucially it **scrubs canary entries before building embeddings**,
  so a canary answer can never come from the cache.
- **`answer_bank.json`** — **98 real question/answer pairs already harvested from Nida's
  actual applications.** Questions this extension is about to be asked again.
- **`llm.py`** — `get_client()`, local Ollama or the Claude CLI.
- **`profile.resume_facts`** — `preserved_companies`, `preserved_projects`, `real_metrics`:
  the existing anti-fabrication guardrail from resume tailoring.

So the non-deterministic tier is mostly wiring, not invention.

## The ladder, v2

| # | Tier | Source | Auto-fills? |
|---|---|---|---|
| 0 | secret guard | — | never |
| 1 | canary | exact profile paths | only on an exact hit |
| 2 | deterministic | `autocomplete`, name/id/label | yes |
| 3 | **structured** *(new)* | `work_history[]`, `education[]`, indexed by section | yes |
| 4 | laya | semantic classification, ≥0.75 | yes |
| 5 | **answer bank** *(new)* | `AnswerCache` seed/cache hit | yes |
| 6 | **draft** *(new)* | `AnswerCache` LLM miss | yes, but marked DRAFT |
| 7 | unresolved | — | no |

Tiers 5 and 6 are one call: `AnswerCache.answer()` returns `source` = `seed` / `cache` /
`llm`, and the tier splits on it.

### Tier 6 is different from every other tier

It is the only tier that puts *generated* text under the operator's name. It therefore:

- is **marked `draft: true`** in the FillPlan and rendered distinctly (blue, not green) with
  "review this — drafted, not from your profile";
- is **grounded**: the prompt carries `resume_facts` and is instructed to use only those
  companies, projects and metrics, and to say nothing it cannot support;
- is **never used for a canary** (tier 1 already terminal) and never for anything the secret
  guard covers;
- is **off by default**, behind `APPLYPILOT_DRAFTS=1`, exactly like Laya.

A fabricated employment claim is the failure mode here, and it is worse than an unfilled box.
The human-in-the-loop review is what makes this acceptable at all — which is precisely why
drafts must *look* different from facts in the UI.

## Structured profile data (tier 3)

New optional profile sections, additive — nothing existing changes shape:

```jsonc
"work_history": [
  { "title": "Senior Product Designer", "company": "Acme",
    "location": "Seattle, WA", "start": "03/2022", "end": "",
    "current": true, "description": "..." }
],
"education": [
  { "school": "...", "degree": "...", "field": "...",
    "start": "08/2015", "end": "05/2019" }
]
```

### Repeating sections

Workday renders "Work Experience 1", "Work Experience 2", … and Greenhouse/Ashby do similar
for education. The scanner must therefore report, per field, the **section context**: the
nearest enclosing heading/legend text and a 1-based `section_index` parsed from it (or from a
Workday-style field name such as `workExperience-2--jobTitle`).

Tier 3 then maps `section_index` → `work_history[index - 1]`. Out of range ⇒ skip with
"no N-th position in your profile", never wrap around and never reuse position 1.

## Profile management in the extension

The operator should not hand-edit `profile.json`. New endpoints on the local service:

| Method | Path | Purpose |
|---|---|---|
| GET | `/profile/full` | current profile values, **secret paths stripped** |
| POST | `/profile` | write a profile (validated, atomic, backed up first) |
| GET | `/profiles` | list profile ids + active (multi-profile is already on `main`) |
| POST | `/profiles/{id}/activate` | switch active profile |
| POST | `/profile/import-resume` | parse a résumé into a draft profile for review |

`/profile/full` returns values, which `/profile` (key names only) deliberately did not. That
is a considered relaxation: the service is localhost-only, token-gated, and the data is the
operator's own — but the secret denylist still applies, so `personal.password` never leaves
the service even here.

The extension's options page grows a profile editor: sections for personal / work auth /
compensation / experience, a repeatable editor for `work_history` and `education`, a profile
switcher, and a "import from résumé" button that prefills fields for review. Nothing is saved
without an explicit click.

## Scanner additions

- `section` (nearest heading/legend text) and `section_index` per field.
- Better label resolution for split-name fields (`"First and Last Legal Name"` →
  `personal.full_name`).
- Date inputs: report `MM/YYYY`-style placeholders so tier 3 can format correctly.

## Out of scope (still)

- Submitting or navigating. Unchanged.
- Résumé file upload (OS-level picker).
- Answering anything the secret guard or the canary tier owns, by any generative means.
