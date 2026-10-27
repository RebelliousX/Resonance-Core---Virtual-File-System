# =============================================================================
# capture_procmon.ps1 - MUST BE RUN AS ADMINISTRATOR
#
# Captures every syscall made by the game during ONE cutscene attempt so we
# can see which call fails when videos are served by the virtual drive.
#
# Steps it performs:
#   1. Downloads Process Monitor (Sysinternals) into _vfs_cache_\tools
#      (skipped if already present).
#   2. Starts a full capture into _vfs_cache_\procmon_trace.pml.
#   3. YOU: launch the game normally (comandos_w10 Launcher.bat), get to the
#      main menu, play ONE cutscene, watch it get skipped, then ALT-TAB back
#      here and press ENTER.
#   4. Stops the capture and exports _vfs_cache_\procmon_trace.csv for me.
#
# Keep the capture window SHORT: start the game within ~a minute of pressing
# ENTER below, and come back promptly after the cutscene gets skipped.
# =============================================================================

$ErrorActionPreference = "Stop"

# --- require elevation ------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "This script must be run as ADMINISTRATOR." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> Run as administrator, then re-run this script."
    pause
    exit 1
}

$gameDir = Split-Path -Parent $PSScriptRoot     # parent of _vfs_scripts_
if (-not (Test-Path (Join-Path $gameDir "_vfs_data_"))) {
    # fallback: derive from script location directly
    $gameDir = "G:\Commandos BEL"
}
$cacheDir = Join-Path $gameDir "_vfs_cache_"
$toolsDir = Join-Path $cacheDir "tools"
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

$procmon = Join-Path $toolsDir "Procmon64.exe"
if (-not (Test-Path $procmon)) {
    Write-Host "Downloading Process Monitor from live.sysinternals.com..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "https://live.sysinternals.com/Procmon64.exe" -OutFile $procmon -UseBasicParsing
    Write-Host "Downloaded: $procmon"
}

$pml = Join-Path $cacheDir "procmon_trace.pml"
$csv = Join-Path $cacheDir "procmon_trace.csv"
Remove-Item $pml, $csv -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host " STARTING CAPTURE NOW." -ForegroundColor Yellow
Write-Host " 1. Launch the game via the launcher."                    -ForegroundColor Yellow
Write-Host " 2. Reach the main menu, play ONE cutscene."             -ForegroundColor Yellow
Write-Host " 3. Wait until it is skipped."                          -ForegroundColor Yellow
Write-Host " 4. Alt-Tab here and press ENTER."                      -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow

& $procmon /AcceptEula /Quiet /Minimized /BackingFile $pml | Out-Null
Start-Sleep -Seconds 3

Read-Host "Press ENTER after the cutscene was skipped (this stops the capture)"

# terminate the running procmon instance gracefully (flushes + closes pml)
& $procmon /AcceptEula /Terminate | Out-Null
Start-Sleep -Seconds 6

Write-Host "Exporting CSV (this can take a minute)..." -ForegroundColor Cyan
& $procmon /AcceptEula /OpenLog $pml /SaveAs $csv | Out-Null

if (Test-Path $csv) {
    Write-Host ""
    Write-Host ("DONE. Trace files:" -f "") -ForegroundColor Green
    Write-Host ("  {0}  ({1:N1} MB)" -f $csv, ((Get-Item $csv).Length / 1MB))
    Write-Host ("  {0}  ({1:N1} MB)" -f $pml, ((Get-Item $pml).Length / 1MB))
    Write-Host "You can now close this window."
} else {
    Write-Host "Export did not produce $csv - check for error windows." -ForegroundColor Red
}
pause
