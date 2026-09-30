# Windows Defender scan of a build artifact (GitHub Actions windows-latest).
#
# ASCII ONLY IN THIS FILE. Windows PowerShell 5.1 reads a .ps1 as ANSI unless it
# has a UTF-8 BOM, so any non-ASCII character turns into mojibake and the parser
# dies on an unterminated string. This already broke one CI run.
#
# Why this exists: customer 2309842 downloaded the v1.0.4 --onefile exe and
# Windows refused to launch it ("cannot access the specified device, path, or
# file"), which is what a Defender quarantine looks like from the user's side.
# We do not guess. We scan the bytes we are about to publish with the real
# engine and print the verdict.
#
# MpCmdRun exit codes: 0 = no threats, 2 = threats found. -DisableRemediation
# keeps the scanner from deleting the artifact out from under the build.
param(
  [Parameter(Mandatory = $true)][string] $Path,
  [string] $Label = "artifact",
  [switch] $MustBeClean
)

$ErrorActionPreference = "Continue"
$full = (Resolve-Path $Path).Path
Write-Host "=== Defender scan: $Label"
Write-Host "path: $full"

$mp = Join-Path $env:ProgramFiles "Windows Defender\MpCmdRun.exe"
if (-not (Test-Path $mp)) { throw "MpCmdRun.exe not found at $mp" }

$before = @(Get-MpThreatDetection -ErrorAction SilentlyContinue)
Write-Host "threat detections before scan: $($before.Count)"

# ScanType 3 = custom scan of the given file or directory (recursive).
$out = & $mp -Scan -ScanType 3 -File $full -DisableRemediation 2>&1
$code = $LASTEXITCODE
$out | ForEach-Object { Write-Host "  MpCmdRun| $_" }
Write-Host "MpCmdRun exit code: $code"

$after = @(Get-MpThreatDetection -ErrorAction SilentlyContinue)
$new = @($after | Where-Object { $_.DetectionID -notin ($before | ForEach-Object { $_.DetectionID }) })
Write-Host "new threat detections: $($new.Count)"
foreach ($d in $new) {
  Write-Host ("  DETECTED {0} | {1} | resources: {2}" -f $d.ThreatID, $d.ActionSuccess, ($d.Resources -join ","))
  $t = Get-MpThreat -ThreatID $d.ThreatID -ErrorAction SilentlyContinue
  if ($t) { Write-Host ("  THREAT NAME: {0} severity {1}" -f $t.ThreatName, $t.SeverityID) }
}

$clean = ($code -eq 0 -and $new.Count -eq 0)
if ($clean) {
  Write-Host "VERDICT $($Label): CLEAN (no threats found)"
} else {
  Write-Host "VERDICT $($Label): FLAGGED (exit $code, $($new.Count) new detections)"
}

if ($MustBeClean -and -not $clean) {
  throw "Defender flagged $Label. Do not publish this artifact."
}
