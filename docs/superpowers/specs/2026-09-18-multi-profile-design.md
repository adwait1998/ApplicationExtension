# Multi-Profile ApplyPilot — Design

**Status:** approved 2026-09-18 (user-approved section by section during brainstorming)
**Supersedes:** the implicit "one candidate per installation" assumption baked in since v1.

## Problem

ApplyPilot applies on behalf of exactly one person. Her identity lives in a single
`profile.json` at the data-dir root, next to one `resume.pdf`, one `searches.yaml`, and
one SQLite database whose tables have no notion of *whose* data they hold.

The operator now needs to run two independent job searches — a product-design search and a
software/data-engineering search — from one installation, without the two ever mixing. The
worst failure mode in this system is submitting an application under the wrong person's
name; the design is chosen primarily to make that outcome structurally impossible rather
than merely guarded against.

## Goals

1. Two or more people, each with their own profile, resume, search config, scoring, queue,
   and application history.
2. Cross-contamination between profiles is impossible by construction, not by query
   discipline.
3. Learned ATS knowledge (board atlas, form-field bindings, submit endpoints) is shared, so
   a second profile does not re-crawl or re-learn what the first already discovered.
4. The existing 963-test suite keeps passing unchanged.
5. The existing single-profile CLI ergonomics survive: an operator who never thinks about
   profiles gets the active one.

## Non-goals (v1)

- Concurrent batches for two profiles. One batch runs at a time, globally.
- Per-profile Chrome rigs running simultaneously.
- Any web-facing multi-tenancy, auth, or user accounts. This is a local, single-operator
  tool; "profiles" are candidates the operator applies for, not logins.
- Splitting the job *corpus* so a posting discovered for one profile is scored for the
  other. Each profile discovers and scores independently.

## Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| Data separation | Shared ATS knowledge, isolated people | Isolation where identity matters, sharing where it only costs re-learning |
| Concurrency | One batch at a time, globally | Preserves the existing single-active-batch registry, kill switch, and one Chrome rig |
| Sequencing | Multi-profile foundation before the rest of Phase 4B | The operator console is built profile-aware from the start, avoiding rework of the live-apply path |

## Layout

`APPLYPILOT_ROOT` (defaults to the current `APPLYPILOT_DIR` value, i.e. `E:\applypilot-data`)
becomes a root containing shared state plus one self-contained directory per person:

```
E:\applypilot-data\
  active_profile              text file naming the current profile, e.g. "nida"
  shared\
    atlas.db                  boards, source_runs,
                              mapping_cache, submit_endpoints
    settings.json             global: spend cap (one wallet), autopilot config
    ui_runs\                  GLOBAL batch registry — one active batch
  profiles\
    nida\                     becomes APP_DIR when the bound profile is "nida"
      profile.json
      resume.pdf  resume.txt
      searches.yaml
      applypilot.db           jobs, submission_ledger, engine_control, engine_control
      logs\                   incl. review.jsonl, spend_ledger.jsonl
      tailored_resumes\  cover_letters\
      chrome-workers\  apply-workers\
      ui_settings.json        per-profile: apply cap, batch defaults
    adwait\                   same shape
```

Every path that is personal lives under the profile dir. Every path that is learned
infrastructure lives under `shared/`.

## Profile binding

The mechanism is **process-level binding, resolved before any `applypilot` module is
imported.**

Resolution order (first match wins):

1. `--profile <id>` on the command line
2. `APPLYPILOT_PROFILE` environment variable
3. the `active_profile` file at the root
4. if exactly one profile exists, that one
5. otherwise: error listing the available profiles

`src/applypilot/__main__.py` — today a three-line entrypoint — performs a minimal
`sys.argv` scan for `--profile`, resolves the id, sets `os.environ["APPLYPILOT_DIR"]` to
the profile directory, and only then imports `applypilot.cli`.

### Why this specific mechanism

`config.py` computes `APP_DIR` and twelve derived path constants at import time, and
`database.py:13` does `from applypilot.config import DB_PATH` — a by-value import that
freezes the path. Runtime mutation of `config.APP_DIR` therefore cannot work, and
threading a `profile_id` parameter through the code would touch the 19 `load_profile()`
call sites plus every consumer of those constants.

Binding before import sidesteps all of it. The single-profile assumption stays exactly as
written, but becomes true *per process* instead of *per installation*. Consequently:

- no changes to the 19 `load_profile()` call sites
- no changes to the by-value config imports
- no changes to scoring, tailoring, cover-letter generation, the canary resolver, or the
  v2 Form Compiler resolver — all of which already receive `profile: dict` as a parameter

This is viable because every apply batch is already spawned as its own subprocess.

### Compatibility rule (protects the existing test suite)

If `APPLYPILOT_DIR` is set explicitly **and** the directory it names contains a
`profile.json` (or lacks a `profiles/` subdirectory), it is treated as a direct
single-profile data dir — legacy mode — and no profile resolution occurs.

This is precisely what the existing tests do (`monkeypatch` `APPLYPILOT_DIR` to a
`tmp_path`), so they continue to pass untouched. It also means an existing installation
that never migrates keeps working.

## Shared atlas split

The database has exactly seven tables. Four are profile-agnostic. `database.py:192` already
documents `boards` as a "Profile-AGNOSTIC registry", and `mapping_cache` stores a *binding*
(`'profile.<path>'`) rather than a literal value — designed to survive profile edits, which
makes it equally safe across people.

| Table | DDL | Home | Reason |
|---|---|---|---|
| `boards` | `database.py:197` | `shared/atlas.db` | Board directory; no personal data |
| `source_runs` | `database.py:225` | `shared/atlas.db` | Crawl bookkeeping; no personal data |
| `mapping_cache` | `database.py:248` | `shared/atlas.db` | Learned ATS form structure, stored as references not values |
| `submit_endpoints` | `database.py:264` | `shared/atlas.db` | Learned per-ATS submit paths |
| `jobs` | `database.py:93` | `profiles/<id>/applypilot.db` | Personal queue and history |
| `submission_ledger` | `database.py:168` | `profiles/<id>/applypilot.db` | Personal submission record |
| `engine_control` | `database.py:188` | `profiles/<id>/applypilot.db` | Pause flag, per-profile |

`ats_companies` is **not** a database table — it is `src/applypilot/config/ats_companies.yaml`,
shipped inside the package, and is therefore already shared by construction. It needs no
migration.

### Mechanism: `ATTACH`, not a second connection

A single connection opens the profile database as `main` and attaches the shared database
as `atlas`. SQLite resolves an unqualified table name across attached databases, so
`FROM boards` resolves to `atlas.boards` while `FROM jobs` resolves to `main.jobs`.

**Empirically verified** before adopting: unqualified reads *and writes* against the
attached database resolve correctly and persist to the attached file; cross-database joins
work; each table stays in its own file.

This matters because `discovery/atlas/tick.py:run_tick` legitimately touches both sides in
one operation — it reads `boards`/`source_runs` and counts newly stored `jobs`. The
alternative (threading a second connection through `run_tick`, `poll_board` and their
callers) would change signatures across the atlas pipeline. With `ATTACH`, **no SQL
statement and no function signature changes anywhere**; the split is confined to
`database.py`'s connection setup.

Every atlas function already takes `conn` as its first parameter, so the modules need no
edits at all.

**Accepted tradeoff:** in WAL mode, a transaction spanning attached databases is not
atomic. A crash mid-tick could therefore store jobs without marking the board checked, or
the reverse. This is acceptable because the Atlas tick is explicitly designed to be
idempotent and resumable — the id-set diff short-circuits unchanged boards — so the failure
degrades to one redundant poll. The implementation must confirm tick idempotency still
holds after the split.

**Legacy/test mode:** when running against an explicit `APPLYPILOT_DIR` or an in-memory
database, all seven tables are created in `main` exactly as today and nothing is attached.
This keeps the existing 963-test suite passing unchanged.

`config.py` gains `SHARED_DIR` and `ATLAS_DB_PATH`, derived from the root rather than from
`APP_DIR`.

## Safety model

The submission ledger, Chrome worker directories, resume artifacts, and generated cover
letters all live inside the profile directory. A batch bound to `nida` has no path by which
it can read or write `adwait`'s ledger; isolation is a filesystem property, not a predicate
in a query that a future refactor might drop.

Additional guards:

- **Identity assertion in the safety prologue.** `profile.json` gains a top-level
  `"profile_id"` field. The safety prologue asserts it is an exact string match for the
  name of the directory the file was loaded from, and refuses the run otherwise. An exact
  match on a dedicated field is used deliberately in preference to matching a display name
  (`personal.name` is `"Nida Shah"` while the directory is `nida`), because a fuzzy identity
  check is not a safety guard. This catches a `profile.json` copied between directories —
  the realistic path to a wrong-name submission.
- **Explicit binding for every batch.** The UI and any scheduled run pass `--profile`
  explicitly. A live batch never relies on the ambiguous `active_profile` default.
- **Canary fields** (sponsorship, work authorization, legal, compensation, address) resolve
  from the bound profile only, unchanged from today's behavior.
- **The existing gates are untouched.** Queue policy, the approve gate, the two-phase
  INTENT→CONFIRMED ledger, the submit-broker one-shot ticket, CDP network containment, the
  posting-drift guard, and the resume interlock all continue to operate, now scoped to one
  profile's data.

## Runner lock and registry

Because only one batch runs at a time globally, the batch registry moves from
`APP_DIR/ui_runs` to `<root>/shared/ui_runs`, and each record gains a `profile` field.

This preserves the just-landed Task 4 semantics exactly — `current.pid`, adopt-or-orphan on
restart, one active batch — while making the Runs history a combined, chronological view
labelled by whose batch each was. Switching the active profile is refused while a batch is
in flight.

## Settings and caps

| Setting | Scope | Where |
|---|---|---|
| `spend_cap_usd_per_day` | global — one wallet | `shared/settings.json` |
| `autopilot_enabled`, autopilot target profile(s) | global — one runner | `shared/settings.json` |
| `max_live_applies_per_day` | per profile — separate searches | `profiles/<id>/ui_settings.json` |
| batch defaults (limit, model, workers, min_score, site) | per profile | `profiles/<id>/ui_settings.json` |

Fail-safe defaults are unchanged: any read error resolves to the safe side (caps treated as
breached, autopilot treated as off).

## CLI surface

```
applypilot --profile <id> <any existing command>   # explicit binding
applypilot profile list                            # ids, names, queue + applied counts, active marker
applypilot profile show [<id>]
applypilot profile add <id>                        # scaffold dir, then the existing init wizard
applypilot profile use <id>                        # set active_profile (refused mid-batch)
applypilot profile migrate                         # one-shot, idempotent
```

## Migration

`applypilot profile migrate` is one-shot and idempotent:

1. Back up the current data dir first.
2. Create `profiles/nida/`, `shared/`.
3. Move the personal files and directories into `profiles/nida/`.
4. Copy the four atlas tables into `shared/atlas.db`, then drop them from the profile
   database.
5. Add `"profile_id": "nida"` to the migrated `profile.json`.
6. Write `active_profile` = `nida`.

Re-running it detects the migrated layout and does nothing. If any step fails, the backup is
the recovery path and the tool says so explicitly.

## Web UI implications

The UI runs as one long-lived process and therefore cannot use process-level binding. It
uses explicit parameterization instead, which `create_app(db_path, app_dir)`
(`webui/server.py:195`) already supports.

The UI gains a profile switcher; `cli.py:958` currently calls `create_app()` with no
arguments and must pass the resolved profile's paths. Launching a batch composes
`--profile <id>` into the subprocess arguments — the same command an operator would type,
consistent with the standing principle that the UI is an authorized trigger and never a
bypass. `static/index.html:115` hardcodes "real submissions go out under Nida's name" and
must become profile-aware.

## Testing and acceptance

- Per-profile isolation: a batch bound to A writes nothing under B — asserted on the DB,
  the ledger, the logs, and the generated artifacts.
- Binding precedence: flag > env > active file > sole profile > error.
- Legacy mode: an explicit `APPLYPILOT_DIR` containing `profile.json` bypasses resolution.
- Identity assertion: a `profile.json` whose identity disagrees with its directory name
  refuses to run.
- Atlas sharing: a mapping learned under profile A is visible to profile B; a job queued
  under A is not.
- Migration idempotency, and correctness of the atlas copy.
- The full existing suite (963 at time of writing) stays green.

## Future work (explicitly out of scope)

- Concurrent per-profile batches.
- Sharing the discovered job corpus across profiles with per-profile scoring.
- Per-profile Chrome rigs and port allocation.
