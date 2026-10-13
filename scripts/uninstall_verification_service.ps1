<#
.SYNOPSIS
  Stops and removes the results-verification-service Windows Service
  (see install_verification_service.ps1 / ADR-024).

  Does NOT touch the repo, the venv, the database, or any manifests/
  reports - only the NSSM-registered service entry itself.

.DESCRIPTION
  Checks nssm's exit code after both stop and remove and aborts before
  claiming success if either fails - PowerShell's $ErrorActionPreference
  does not turn a native command's nonzero exit code into a terminating
  error on its own, so without this a failed `nssm stop` would not have
  stopped a failed `nssm remove` from being attempted, and a failed
  `nssm remove` would not have stopped this script from printing "Done"
  with the service still registered (PR review, session 15).
#>
param(
    [string]$ServiceName = "ResultsVerificationService",
    [string]$NssmPath = "nssm"
)

$ErrorActionPreference = "Stop"

function Invoke-Nssm {
    & $NssmPath @Args
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($Args -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "No service named '$ServiceName' found - nothing to do."
    exit 0
}

if ($existing.Status -eq "Running") {
    Write-Host "Stopping '$ServiceName'..."
    Invoke-Nssm stop $ServiceName
    Start-Sleep -Seconds 2
}

Write-Host "Removing '$ServiceName'..."
Invoke-Nssm remove $ServiceName confirm

Write-Host "Done. Service '$ServiceName' removed. Logs under the repo's logs\ directory were left in place."
