<#
.SYNOPSIS
  Registers the results-verification-service as a Windows Service, using
  NSSM (https://nssm.cc) as the wrapper. See ADR-024 for why NSSM and not
  a native pywin32 service.

.DESCRIPTION
  NSSM's job is to give the Service Control Manager (SCM) something that
  actually speaks its protocol (start/stop/status), since plain
  `python.exe` does not. NSSM registers ITSELF as the service and, under
  the hood, launches and supervises `python -m src.main --run` as a
  child process - the SCM only ever talks to NSSM, never to Python
  directly.

  This script is idempotent: re-running it against an already-registered
  service reconfigures it in place (via `nssm set`) rather than failing.
  A service that was running before this script touched it is restarted
  afterward; one that was stopped is left stopped unless -Start is
  passed - reconfiguring should never silently leave an always-on
  listener stopped (PR review, session 15).

  Every native command (nssm, python, pip) has its exit code checked
  immediately - PowerShell's $ErrorActionPreference does NOT turn a
  nonzero exit code from a native command into a terminating error on
  its own, so without this, a failed nssm call could leave the service
  missing or half-configured while the script still prints success
  (PR review, session 15).

  Run this ONCE per machine, as Administrator, after:
    1. NSSM is installed (see ADR-024 / README - this is a manual,
       one-time tooling setup step, not something this script installs
       for you).
    2. `python -m src.main --init` has been run at least once (the
       database must already exist - this script does not initialise
       it).

.PARAMETER ServiceName
  Windows service name. Shows up in services.msc and `Get-Service`.

.PARAMETER NssmPath
  Path to nssm.exe. Defaults to assuming it's on PATH.

.PARAMETER Start
  Start the service after registering it, even if it was stopped before
  this script ran (e.g. a first-ever install). Has no effect on whether
  a service that WAS already running gets restarted - that always
  happens regardless of this switch, so a routine reconfigure never
  leaves the service stopped.
#>
param(
    [string]$ServiceName = "ResultsVerificationService",
    [string]$NssmPath = "nssm",
    [switch]$Start
)

$ErrorActionPreference = "Stop"

# Repo root is this script's parent directory (scripts/ lives at the repo
# top level).
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogsDir = Join-Path $RepoRoot "logs"

# -u: run Python unbuffered. NSSM redirects stdout/stderr to a file, not
# a terminal, and Python's stdout is block-buffered (not line-buffered)
# whenever it isn't attached to a real terminal - so without -u, print()
# output (main.py's only form of logging) can sit invisibly in a buffer
# for a long time before it's actually written to the log file, making
# the only live view into the running service unreliable (PR review,
# session 15).
$ServiceArgs = "-u -m src.main --run"

Write-Host "Repo root:    $RepoRoot"
Write-Host "Service name: $ServiceName"

# --- Helpers -------------------------------------------------------------

function Invoke-Nssm {
    # Runs nssm with the given arguments and aborts the whole script if
    # it returns a nonzero exit code. $LASTEXITCODE reflects only the
    # most recently run *native* command, so this check must happen
    # immediately after the call, before any other native command runs.
    & $NssmPath @Args
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($Args -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][scriptblock]$ScriptBlock
    )
    & $ScriptBlock
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

# --- Sanity checks ---------------------------------------------------------

# NSSM 2.24's `version` command prints its banner and then usage text and
# exits nonzero, so Invoke-Nssm (which throws on a nonzero exit) can't be
# used here. Run it directly and check the banner instead.
$nssmOk = $false
try {
    $ErrorActionPreference = "Continue"
    $nssmBanner = (& $NssmPath version 2>&1 | Out-String)
    $nssmOk = ($nssmBanner -match "NSSM")
} catch {
    $nssmOk = $false
} finally {
    $ErrorActionPreference = "Stop"
}
if (-not $nssmOk) {
    $msg = "Could not run '$NssmPath'. Install NSSM (https://nssm.cc) and " +
        "either put nssm.exe on PATH or pass -NssmPath <path-to-nssm.exe>."
    Write-Error $msg
    exit 1
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "No .venv found at $PythonExe - creating one and installing dependencies..."
    Invoke-Checked "python -m venv" { python -m venv (Join-Path $RepoRoot ".venv") }
    Invoke-Checked "pip install" {
        & $PythonExe -m pip install --quiet -r (Join-Path $RepoRoot "requirements.txt")
    }
}

$ConfigPath = Join-Path $RepoRoot "config\local_config.yaml"
if (-not (Test-Path $ConfigPath)) {
    $configMsg = "config\local_config.yaml not found - the service will fall " +
        "back to config\config.yaml. If this machine needs its own local " +
        "overrides (it almost certainly does - see README), create " +
        "local_config.yaml before starting the service."
    Write-Warning $configMsg
}

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

# --- Install or reconfigure ------------------------------------------------

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$wasRunning = $false
$configured = $false
$restoreFailure = $null

try {
    if ($existing) {
        Write-Host "Service '$ServiceName' already exists - reconfiguring in place."
        $wasRunning = ($existing.Status -eq "Running")
        if ($wasRunning) {
            Write-Host "Stopping it first..."
            Invoke-Nssm stop $ServiceName
        }
    } else {
        Write-Host "Installing new service '$ServiceName'..."
        Invoke-Nssm install $ServiceName $PythonExe $ServiceArgs
    }

    # These `nssm set` calls run regardless of whether the service was just
    # installed or already existed, so re-running this script always leaves
    # the service in the same known configuration.
    Invoke-Nssm set $ServiceName AppDirectory $RepoRoot
    Invoke-Nssm set $ServiceName AppParameters $ServiceArgs
    Invoke-Nssm set $ServiceName DisplayName "Countable PCR Results Verification Service"
    Invoke-Nssm set $ServiceName Description "Always-on listener/orchestrator for the PCR regression verification service (ADR-015). See docs/adr in the repo."
    Invoke-Nssm set $ServiceName Start SERVICE_AUTO_START

    # Logs - NSSM redirects the child process's stdout/stderr (main.py's
    # plain print() calls) to these files, with basic rotation so they don't
    # grow forever.
    Invoke-Nssm set $ServiceName AppStdout (Join-Path $LogsDir "service_stdout.log")
    Invoke-Nssm set $ServiceName AppStderr (Join-Path $LogsDir "service_stderr.log")
    Invoke-Nssm set $ServiceName AppRotateFiles 1
    Invoke-Nssm set $ServiceName AppRotateOnline 1
    Invoke-Nssm set $ServiceName AppRotateBytes 10485760   # 10 MB per file

    # Stop behavior: try a Ctrl+C-style console signal first (main.py now
    # catches this cleanly - see the run() docstring) and give it up to 60s,
    # since asyncio.run()'s shutdown waits for any in-flight verify_run() to
    # actually finish before returning. Window-message and thread-message
    # stop methods don't apply to a console app with no window, so leave
    # their (short) defaults - NSSM will fall through them quickly and, if
    # the process still hasn't exited after all methods are tried, terminate
    # it outright as a last resort.
    Invoke-Nssm set $ServiceName AppStopMethodConsole 60000

    # Crash recovery: if the process exits on its own (not via a deliberate
    # `nssm stop`/`Stop-Service`), restart it after a short delay rather than
    # immediately - avoids a tight crash loop if something is wrong at
    # startup (e.g. the pipeline DB is unreachable).
    Invoke-Nssm set $ServiceName AppExit Default Restart
    Invoke-Nssm set $ServiceName AppRestartDelay 15000

    Write-Host ""
    Write-Host "Service '$ServiceName' configured."
    Write-Host "  Logs:   $LogsDir"
    Write-Host "  Review it in services.msc, or:"
    Write-Host "    Get-Service $ServiceName"
    Write-Host "    Stop-Service $ServiceName"

    $configured = $true
} finally {
    # A service that was running before this script touched it must be
    # running again afterwards - on success AND on failure. If a later
    # `nssm set` throws after the stop above, skipping this would leave
    # the always-on listener down and dispatched runs unprocessed (PR
    # review, session 18). Best-effort: never mask the original error.
    if ($wasRunning) {
        try {
            $svc = Get-Service -Name $ServiceName -ErrorAction Stop
            if ($svc.Status -ne "Running") {
                if (-not $configured) {
                    Write-Warning "Configuration failed part-way; restoring the service's previous running state anyway."
                }
                Start-Service $ServiceName -ErrorAction Stop
            }
        } catch {
            # Remembered, not just warned: if configuration succeeded this
            # is the only error, so the success path below must fail on it.
            # If configuration failed, the original error is still in
            # flight and propagates after this finally block untouched.
            $restoreFailure = "Could not restore the running state of '$ServiceName': $_"
            Write-Warning "$restoreFailure - start it manually with: Start-Service $ServiceName"
        }
    }
}

# Success path (only reached when configuration succeeded - a failure
# above propagates its own error). A service that was running before was
# restored in the finally block; if that restore failed, fail here rather
# than report success with the always-on listener stopped (PR review,
# session 18).
if ($restoreFailure) {
    throw $restoreFailure
}

if ($wasRunning -or $Start) {
    if (-not $wasRunning) {
        Write-Host "Starting service..."
        Start-Service $ServiceName
    }
    Start-Sleep -Seconds 2
    # Verify rather than assume: NSSM can report a successful start and
    # the child process still exit immediately (bad config, missing venv).
    $final = Get-Service $ServiceName
    $final
    if ($final.Status -ne "Running") {
        throw "Service '$ServiceName' is not running after start (status: $($final.Status)). Check $LogsDir for service_stderr.log."
    }
} else {
    Write-Host "Service left stopped (it wasn't running before this script ran, and -Start wasn't passed)."
    Write-Host "  Start it with: Start-Service $ServiceName"
}
