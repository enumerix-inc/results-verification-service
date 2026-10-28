<#
.SYNOPSIS
  Assembles installer\payload\ (bundled Python + dependencies + service
  code + NSSM) and, if Inno Setup is installed, compiles the installer.

.DESCRIPTION
  Run on a BUILD machine with internet access. The resulting installer
  needs no Python, no pip and no internet on the target machine - the
  regression machines are often offline instruments.

  Layout produced (and installed under {app}):
    python\     official embeddable Python + site-packages
    service\    src\, schema\, scripts\, annotations\, config\config.yaml (defaults only)
    tools\      nssm.exe

  Windows PowerShell 5.1 compatible. Keep this file ASCII / BOM-less.

.PARAMETER NssmExe
  Path to the 64-bit nssm.exe (win64 folder of the NSSM download). Required;
  nssm.cc is not downloaded automatically.

.PARAMETER PythonVersion
  Embeddable Python to bundle. Must match what the service is developed and
  tested on; must be a 3.10+ release with an embeddable amd64 zip.

.PARAMETER Version
  Installer version number (shown in Add/Remove Programs).

.PARAMETER IsccPath
  Full path to ISCC.exe (the Inno Setup compiler). When omitted, Inno Setup 7
  then 6 are looked for under Program Files, Program Files (x86), the
  per-user Programs folder, and PATH.

.PARAMETER SkipCompile
  Build the payload only; do not run the Inno Setup compiler.
#>
param(
    [Parameter(Mandatory)][string]$NssmExe,
    [string]$PythonVersion = "3.10.11",
    [string]$Version = "0.1.0",
    [string]$IsccPath = "",
    [switch]$SkipCompile
)

$ErrorActionPreference = "Stop"

$InstallerDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $InstallerDir
$Payload = Join-Path $InstallerDir "payload"
$Cache = Join-Path $InstallerDir "cache"

function Invoke-Checked {
    param([string]$Description, [scriptblock]$ScriptBlock)
    & $ScriptBlock
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path $NssmExe)) { throw "NssmExe not found: $NssmExe" }

New-Item -ItemType Directory -Force -Path $Cache | Out-Null
if (Test-Path $Payload) { Remove-Item -Recurse -Force $Payload }
New-Item -ItemType Directory -Force -Path $Payload | Out-Null

# --- 1. Embeddable Python -----------------------------------------------
$PyDir = Join-Path $Payload "python"
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $Cache $zipName
if (-not (Test-Path $zipPath)) {
    Write-Host "Downloading $zipName ..."
    Invoke-WebRequest -UseBasicParsing `
        -Uri "https://www.python.org/ftp/python/$PythonVersion/$zipName" -OutFile $zipPath
}
Expand-Archive -Path $zipPath -DestinationPath $PyDir -Force

# The embeddable distribution ignores PYTHONPATH and the current directory;
# sys.path comes only from its ._pth file. Add the service folder (so
# `-m src.main` resolves), site-packages, and turn on `import site` (needed
# for pip-installed packages).
$pth = Get-ChildItem -Path $PyDir -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "No python*._pth found in $PyDir" }
$zipStem = [System.IO.Path]::GetFileNameWithoutExtension($pth.Name) -replace "\._pth$", ""
$pthLines = @(
    "$zipStem.zip",
    ".",
    "..\service",
    "Lib\site-packages",
    "import site"
)
[System.IO.File]::WriteAllLines($pth.FullName, $pthLines, (New-Object System.Text.UTF8Encoding($false)))

# --- 2. pip + dependencies ----------------------------------------------
$PyExe = Join-Path $PyDir "python.exe"
$getPip = Join-Path $Cache "get-pip.py"
if (-not (Test-Path $getPip)) {
    Invoke-WebRequest -UseBasicParsing -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
}
Invoke-Checked "get-pip" { & $PyExe $getPip --no-warn-script-location }
Invoke-Checked "pip install" {
    & $PyExe -m pip install --no-warn-script-location -r (Join-Path $RepoRoot "requirements.txt")
}

# --- 3. Service code -------------------------------------------------------
$Svc = Join-Path $Payload "service"
New-Item -ItemType Directory -Force -Path (Join-Path $Svc "config") | Out-Null
foreach ($dir in @("src", "schema", "scripts", "annotations")) {
    # robocopy exit codes 0-7 are success; 8+ are failures.
    & robocopy (Join-Path $RepoRoot $dir) (Join-Path $Svc $dir) /E /XD "__pycache__" /XF "*.pyc" /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $dir failed with exit code $LASTEXITCODE" }
}
# Defaults only. local_config.yaml is NEVER bundled: the installer generates
# the machine's own copy outside the program folder.
Copy-Item (Join-Path $RepoRoot "config\config.yaml") (Join-Path $Svc "config\config.yaml")
Copy-Item (Join-Path $RepoRoot "requirements.txt") $Svc

# --- 4. NSSM -----------------------------------------------------------------
New-Item -ItemType Directory -Force -Path (Join-Path $Payload "tools") | Out-Null
Copy-Item $NssmExe (Join-Path $Payload "tools\nssm.exe")

# --- 5. Smoke test the payload as the service will run it ----------------
Write-Host "Smoke-testing the payload ..."
Push-Location $Svc
try {
    Invoke-Checked "dependency import" {
        & $PyExe -c "import pandas, numpy, asyncpg, yaml, watchdog; print('dependencies import ok')"
    }
    Invoke-Checked "entry point" { & $PyExe -m src.main --help | Out-Null }
} finally {
    Pop-Location
}

Write-Host "Payload ready: $Payload"

# --- 6. Compile --------------------------------------------------------------
if ($SkipCompile) { return }
if (-not $IsccPath) {
    # Inno Setup installs to "Inno Setup <major>", system-wide or per-user.
    $candidates = @()
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if ($root) {
            foreach ($major in @("7", "6")) {
                $candidates += (Join-Path $root "Inno Setup $major\ISCC.exe")
            }
        }
    }
    $IsccPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $IsccPath) {
        $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($onPath) { $IsccPath = $onPath.Source }
    }
}
$iscc = $IsccPath
if (-not $iscc) {
    Write-Warning "ISCC.exe (Inno Setup compiler) not found - payload built, installer not compiled. Pass -IsccPath <full path to ISCC.exe>."
    return
}
Invoke-Checked "ISCC" { & $iscc "/DAppVersion=$Version" (Join-Path $InstallerDir "rvs.iss") }
Write-Host "Installer written to $(Join-Path $InstallerDir 'Output')"
