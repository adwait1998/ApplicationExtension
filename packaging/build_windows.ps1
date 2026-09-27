# Builds the Windows friend installer.
#   build\friend\windows\              ApplyPilotCopilot\ (app), extension\, SETUP.txt
#   dist\ApplyPilotCopilot-Setup-<version>.exe   (only when Inno Setup 6 is installed)
#
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -Python C:\path\to\python.exe
param([string]$Python = '')

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Invoke-Checked([string]$Exe, [string[]]$ArgList) {
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $Exe $($ArgList -join ' ')" }
}

if (-not $Python) {
    $known = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path $known) { $Python = $known } else { $Python = 'python' }
}

$Version = (Select-String -Path 'src\applypilot\__init__.py' -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "ApplyPilot Copilot friend build $Version"

$Venv = Join-Path $Root 'packaging\.venv-win'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) { Invoke-Checked $Python @('-m', 'venv', $Venv) }
Invoke-Checked $VenvPy @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-Checked $VenvPy @('-m', 'pip', 'install', '-r', 'packaging\requirements-friend.txt')

$Out = Join-Path $Root 'build\friend\windows'
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
Invoke-Checked $VenvPy @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $Out,
    '--workpath', (Join-Path $Root 'build\pyinstaller-win'), 'packaging\applypilot_copilot.spec')
Invoke-Checked $VenvPy @('packaging\stage_extension.py', '--dest', (Join-Path $Out 'extension'))
Copy-Item 'docs\FRIEND_SETUP.md' (Join-Path $Out 'SETUP.txt')

Invoke-Checked $VenvPy @('scripts\verify_friend_bundle.py', '--bundle', $Out)

$Iscc = $null
foreach ($candidate in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'))) {
    if ($candidate -and (Test-Path $candidate)) { $Iscc = $candidate; break }
}
if (-not $Iscc) {
    Write-Warning "Inno Setup 6.3+ not found: the app is built and verified in $Out, but no installer .exe was made. Install Inno Setup 6.3 or newer (https://jrsoftware.org/isdl.php) or build through the GitHub workflow."
    exit 0
}
$env:APC_VERSION = $Version
New-Item -ItemType Directory -Force (Join-Path $Root 'dist') | Out-Null
Invoke-Checked $Iscc @('packaging\windows\installer.iss')
Write-Host "Built dist\ApplyPilotCopilot-Setup-$Version.exe"
