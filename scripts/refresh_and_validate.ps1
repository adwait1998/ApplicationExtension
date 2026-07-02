# ApplyPilot: refresh queue + validate apply path (everything except live submit).
# Run:  powershell -ExecutionPolicy Bypass -File E:\auto-apply-pipeline\scripts\refresh_and_validate.ps1
# Then, if the dry-run looks good, the ONE live command (your call, never automated):
#   & $PY -m applypilot apply --limit 5 --workers 1 --headless --model claude-haiku-4-5-20251001

$PY = "C:\Users\adwai\AppData\Local\Programs\Python\Python312\python.exe"
$env:APPLYPILOT_USE_SKILLS = "1"

Write-Host "`n=== 1/5 prune dead links (170+ stale expected to park) ===" -ForegroundColor Cyan
& $PY -m applypilot prune-expired --min-score 7

Write-Host "`n=== 2/5 discover fresh jobs (ats_boards, ~20s) ===" -ForegroundColor Cyan
& $PY -m applypilot run discover enrich --source ats_boards

Write-Host "`n=== 3/5 score new jobs (local Ollama) ===" -ForegroundColor Cyan
& $PY -m applypilot run score

Write-Host "`n=== 4/5 dry-run apply x1 (validates gates + adapter, no submission) ===" -ForegroundColor Cyan
& $PY -m applypilot apply --dry-run --limit 1 --workers 1 --headless --model claude-haiku-4-5-20251001

Write-Host "`n=== 5/5 queue status ===" -ForegroundColor Cyan
& $PY -m applypilot status
Write-Host "`nIf the dry-run reached dry_run:applied, run the live batch (see header comment)." -ForegroundColor Green
