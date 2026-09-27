# Friend build: how to build and share ApplyPilot Copilot

The friend build packs the backend (native host + local service) into one executable per OS, so a friend needs no Python, terminal or API key. Design: `docs/superpowers/specs/2026-09-27-friend-installer-design.md`.

## What your friend gets

- Windows: `ApplyPilotCopilot-Setup-<version>.exe` (per-user install, no admin).
- Mac (Apple Silicon only): `ApplyPilotCopilot-macOS-arm64-<version>.zip`.
- The guide `SETUP.txt` (source: `docs/FRIEND_SETUP.md`) is inside both.

## Build Windows on this PC

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

This builds `build\friend\windows\`, runs `scripts/verify_friend_bundle.py` (6 checks, all in throwaway folders), then makes `dist\ApplyPilotCopilot-Setup-<version>.exe` if Inno Setup 6.3 or newer is installed (https://jrsoftware.org/isdl.php). Without it, use the GitHub workflow below.

**Never run the installer on this PC.** It re-registers the `com.applypilot.copilot` native host to the frozen app, replacing your dev launcher. If it happens, uninstall it (Settings > Apps) and restore yours with `python -m applypilot extension install-host`.

## Build both installers on GitHub (needed for the Mac one)

1. Create a new **private** repository on github.com under your account (for example `applypilot-friend`). Leave it empty (no README).
2. Add it as a second remote and push this branch as its first branch (so it becomes the default branch, which the Run workflow button needs):
   ```bash
   git remote add mine https://github.com/<your-user>/applypilot-friend.git
   git push mine feat/chrome-extension
   ```
   Checked 2026-09-27: `.env` is ignored and no API keys appear anywhere in the history.
3. On GitHub: Actions tab > **friend-build** > Run workflow.
4. When both jobs are green, download the two artifacts from the run's summary page.

Private repos get 2,000 free Actions minutes a month; macOS minutes count 10x, and one build uses roughly 10 macOS minutes (about 100 of your minutes).

## Sending it

Send the installer for their OS (email, Drive, etc.). Both OSes warn once because the builds aren't code-signed; `SETUP.txt` tells them what to click.

## What doesn't work in the friend build

- Tailored-résumé PDFs (the service answers 501; needs Playwright + Chromium).
- AI features on computers that don't meet Chrome's on-device model requirements (Chrome 138+, ~22 GB free disk, >4 GB VRAM or 16 GB RAM with 4+ cores). Autofill works regardless.
- Intel Macs.
