# ApplyPilot Copilot — Friend Installer + On-Device AI: Design

Date: 2026-09-27. Branch: `feat/chrome-extension`. Status: approved direction, detailed plan in
`docs/superpowers/plans/2026-09-27-friend-installer.md`.

## Goal

Let a non-technical friend use ApplyPilot Copilot on their own Windows PC or Apple-Silicon Mac
with a one-time install: no Python, no terminal, no API key. Their data stays on their machine.

## Decisions (from the operator)

| Question | Decision |
|---|---|
| How zero-touch | One-time installer, then invisible. |
| Extension delivery | Phase 1: manual "Load unpacked" (one-time, documented). Force-install policy or Chrome Web Store later, as an addition. |
| Platforms | Windows 10/11 and macOS 13+ on Apple Silicon (M1 or newer). |
| Data model | One person per install, own machine, own `~/.applypilot` data. No multi-tenancy. |
| AI provider for the friend | Run the model inside the extension: Chrome's built-in on-device model (Gemini Nano, Prompt API). No key, nothing leaves the machine. |
| Tailored-résumé PDF | Not in the friend build (needs Playwright plus a bundled Chromium). The Tailor button shows a clear message. |
| Mac build | GitHub Actions workflow; the operator pushes it to a repo they own and runs it. Claude never pushes. |

## What already exists and is reused unchanged

- The extension (MV3): scanning, filling, side panel, options page, résumé import UI, auto-connect
  through the native host (`background.js` `ensureServerViaNativeHost`).
- The pinned extension ID: `extension/manifest.json` carries a public `"key"`, so an unpacked copy
  loaded from any folder has ID `noooclaijfiejnfgabkemnpabcbdnaac`.
- The local service (`applypilot/extension/server.py`, FastAPI + uvicorn) and the native host
  (`applypilot/extension/native_host.py`, Chrome stdio framing, `ensure_server`).
- Per-profile data under `APPLYPILOT_DIR`; the answer bank is the profile's own
  `answer_bank.json` (server passes the path explicitly).

## Problems found while reviewing the code (all must be fixed for a frozen build)

1. `native_host.start_server` launches `sys.executable -m applypilot serve-extension`. In a frozen
   build `sys.executable` is the app itself and has no `-m`. Needs a frozen branch:
   `[sys.executable, "serve-extension", "--port", N]`.
2. On macOS, Chrome kills the native-host process after each one-shot message. The service is
   started with `Popen` and no new session, so it can die with the host. Needs
   `start_new_session=True` on POSIX.
3. The dev installer writes a `.bat` that sets `APPLYPILOT_DIR`. A frozen host is launched by
   Chrome directly, with no env var. The host reads `config.APP_DIR`; the service resolves its
   dir through `bind_profile()`. They agree on a fresh machine only by coincidence (an empty
   `~/.applypilot` counts as a legacy layout). The frozen entry point sets `APPLYPILOT_DIR`
   explicitly before importing `applypilot.config`, for every mode.
4. Chrome launches a native host with the caller's origin as `argv[1]`
   (`chrome-extension://<id>/`, plus `--parent-window=<n>` on Windows). The frozen entry point
   must treat that as native-host mode.
5. `native_install` registers only on Windows (registry). macOS registration is a JSON manifest
   at `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.applypilot.copilot.json`.
   In a frozen install the manifest `"path"` is the executable itself (no launcher script).
6. `/profile/import-resume` is `async def` and calls `resume_import.import_resume` (which calls
   the LLM) synchronously on the event loop. With the on-device bridge, that call waits for the
   extension to answer over HTTP, which the blocked loop can never serve: a deadlock. Wrap the
   call in `run_in_threadpool`.
7. Tailored-résumé PDF rendering imports Playwright (`scoring/pdf.py`). Excluded from the frozen
   build; `/resume/tailor` returns HTTP 501 with a clear message when frozen.
8. The repo-root `answer_bank.json` holds the operator's real answers. It is untracked and never
   imported as a module, so PyInstaller will not bundle it, but the build verification must
   check that no `answer_bank.json`, `profile.json`, `.env`, `resume.*` or `extension_token.txt`
   ends up in any artifact.
9. Unsigned apps: Windows SmartScreen and macOS Gatekeeper will warn once. On macOS the installer
   must also strip the `com.apple.quarantine` attribute from the installed files, or Chrome's
   launch of the host is blocked silently.

## Architecture

```
Friend's machine
┌──────────────────────────── Chrome ────────────────────────────┐
│ Extension (loaded unpacked from the install folder)            │
│  side panel / options page ── llm_bridge.js ── LanguageModel   │
│        │  (long-poll /llm/next, POST /llm/result)  (Gemini Nano)│
│  background.js ── native messaging ──┐                          │
└──────────────────────────────────────│──────────────────────────┘
                                       ▼
   ApplyPilotCopilot(.exe)  <chrome-extension://…>   = native host (stdio)
        │ starts, detached
        ▼
   ApplyPilotCopilot(.exe) serve-extension --port 8787 = FastAPI service
        │ LLM calls go through llm_util.get_llm_client()
        ▼
   BridgeClient ── job queue ── /llm/next ◄── extension page answers with Gemini Nano
   Data: ~/.applypilot (profile.json, answer_bank.json, logs, token)
```

One frozen executable serves every role, chosen by `argv`:

| argv | Mode |
|---|---|
| `chrome-extension://…` (Chrome launching the host) | native host (stdio) |
| `serve-extension [--port N]` | local HTTP service |
| `install-host --extension-dir PATH` | register the native host for this user |
| `uninstall-host` | remove the registration |
| `--version` | print version, exit 0 |

## Component: on-device AI bridge

Python never talks to the model. Every LLM call already goes through
`llm_util.get_llm_client().chat(messages, max_tokens=…, temperature=…) -> str`. A new
`BridgeClient` implements that same `chat` signature by queuing a job and waiting for an
extension page to answer it.

- Provider order in `get_llm_client()`: an explicit env provider (`GEMINI_API_KEY`,
  `OPENAI_API_KEY`, `LLM_URL`, `LLM_PROVIDER=claude`) → **on-device bridge, when an extension page
  has reported the model `available` within the last 60 s** → Claude CLI → raise. The operator's
  own setup (`LLM_URL` = Ollama) is unchanged.
- The bridge is a local provider: `provider_info()["local"]` is true, so `cloud_block_reason`
  never blocks it.
- Which pages run the bridge loop: the side panel and the options page, while open. They are the
  pages that start every AI action (Fill with drafts, Cover letter, Import résumé). The service
  worker does not run it (its lifetime is unreliable for long-polls). Fill started by the
  Alt+Shift+G shortcut with the panel closed gets no drafts — acceptable, documented.
- The loop: `GET /llm/next?status=<availability>` (server holds up to 20 s), run the job with
  `LanguageModel`, `POST /llm/result {id, text}` or `{id, error}`. Jobs are only taken when
  `LanguageModel.availability()` is `"available"`.
- Timeouts: a job must be picked up within 10 s and answered within 180 s; otherwise
  `BridgeClient.chat` raises `RuntimeError`, which every existing caller already treats as
  "no LLM" (fail-soft).
- Mapping messages to the Prompt API: `system` and earlier turns become `initialPrompts`; the last
  `user` message is sent with `session.prompt()`. `temperature` is passed with `topK` (the API
  requires both), clamped to `LanguageModel.params()` maxima. `max_tokens` is ignored.
- First use: the model download needs a user click. Options → Settings → Smart fill gets an
  "On-device AI" row: status, and a "Download on-device model" button with progress.
- Requirements (Chrome docs, 2026-09): Chrome 138+, Windows 10/11 or macOS 13+, ≥ 22 GB free on
  the drive holding the Chrome profile, and a GPU with > 4 GB VRAM or 16 GB RAM with 4+ cores.
  When unavailable, AI features say so; autofill works regardless.

## Component: packaging

- `packaging/applypilot_copilot.spec` (PyInstaller, onedir, console app, name `ApplyPilotCopilot`).
  Entry: `src/applypilot/extension/frozen_entry.py`. Excludes Playwright, pandas, numpy, torch,
  laya, mcp, typer, bs4.
- `packaging/requirements-friend.txt`: the runtime set (fastapi, uvicorn, pydantic,
  python-multipart, httpx, python-dotenv, pyyaml, rich, pypdf, python-docx) plus pyinstaller.
- `packaging/extension_files.txt`: allow-list of extension files to ship (no `selftest.js`,
  `test-page.html`, fixtures, README).
- Windows: `packaging/build_windows.ps1` → PyInstaller → Inno Setup (`packaging/windows/installer.iss`),
  per-user install (no admin) to `%LOCALAPPDATA%\Programs\ApplyPilotCopilot`, post-install runs
  `ApplyPilotCopilot.exe install-host --extension-dir "{app}\extension"`, uninstall runs
  `uninstall-host`. Output: `ApplyPilotCopilot-Setup-<version>.exe`.
- macOS: `packaging/build_macos.sh` → PyInstaller → zip with `ApplyPilotCopilot/`, `extension/`,
  `Install ApplyPilot Copilot.command`, `Uninstall ApplyPilot Copilot.command`, `SETUP.md`.
  The install script copies to `~/Library/Application Support/ApplyPilotCopilot`, strips
  quarantine, and runs `install-host`.
- `.github/workflows/friend-build.yml`: `workflow_dispatch`; jobs on `windows-latest` and
  `macos-14` (arm64); each uploads its artifact.
- `scripts/verify_friend_bundle.py`: runs against a built folder — no PII files; `--version`
  works; the native host answers `hello` over stdio; `serve-extension` starts on a temp
  `APPLYPILOT_DIR` and `/health` answers with the token.

## Friend's setup (documented in `docs/FRIEND_SETUP.md`, shipped as `SETUP.md`)

1. Run the installer (Windows: "More info → Run anyway" once; Mac: right-click → Open once).
2. `chrome://extensions` → Developer mode → Load unpacked → the `extension` folder the installer
   opened.
3. Options → Settings → Smart fill → "Download on-device model" (optional; needs capable hardware).
4. Options → Import résumé to create their profile; review and save.
5. On a job page, click the toolbar icon → Fill this page.

## Out of scope (phase 1)

Force-install policy, Chrome Web Store, code signing, Intel Macs, tailored-résumé PDF, auto-update
of the backend, multi-profile management in the friend build.

## Testing

- Unit (pytest): frozen entry dispatch; native-host service command (frozen/dev/POSIX session);
  cross-platform `native_install` with injected platform/home/registry; bridge queue, timeouts,
  endpoints; `get_llm_client` order; `/resume/tailor` 501 when frozen; import-resume runs off the
  event loop.
- JS (node, no browser): `llm_bridge.js` with fake `fetch` and fake `LanguageModel`.
- Existing suites stay green: pytest, jsdom selftest, chrome widgets/load/panel.
- Build verification: `scripts/verify_friend_bundle.py` on the Windows build (here) and on the
  macOS artifact (in CI).
- Never run the real installer on the operator's machine: it would repoint the
  `com.applypilot.copilot` registration away from the dev launcher. If it happens, restore with
  `applypilot extension install-host`.
- Not testable here: Gemini Nano itself (this PC has 15.7 GB RAM and too little free space on C:),
  and the Mac installer by hand (no Mac). Both are covered by fakes plus CI, and the final check is
  the friend's own run.
