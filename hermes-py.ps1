# Wrapper that runs a Python script with the hermes-agent venv's site-packages
# on PYTHONPATH, while invoking the *real* uv-managed CPython directly
# (bypasses the broken venv Scripts\python.exe shim seen on this host).
#
# Usage:
#   .\hermes-py.ps1 <script.py> [args...]

param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$Script,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = "Stop"

# Override-able via env so other hosts with a different uv toolchain or venv
# location can re-use this wrapper. Defaults reflect a typical uv install
# under the current user's profile.
if (-not $env:HERMES_UV_PYTHON) {
    $env:HERMES_UV_PYTHON = Join-Path $env:USERPROFILE `
        'AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe'
}
if (-not $env:HERMES_VENV_SITE_PACKAGES) {
    $env:HERMES_VENV_SITE_PACKAGES = Join-Path $env:USERPROFILE `
        'hermes-agent\.venv\Lib\site-packages'
}
$realPython       = $env:HERMES_UV_PYTHON
$venvSitePackages = $env:HERMES_VENV_SITE_PACKAGES

if (-not (Test-Path $realPython)) {
    Write-Error "uv-managed Python missing at $realPython. Re-run: uv venv --python 3.11 $env:USERPROFILE\hermes-agent\.venv"
    exit 2
}
if (-not (Test-Path $venvSitePackages)) {
    Write-Error "venv site-packages missing at $venvSitePackages."
    exit 2
}

# Strip env vars that can confuse a fresh interpreter session.
foreach ($v in 'PYTHONHOME','PYTHONSTARTUP','__PYVENV_LAUNCHER__','PYTHONPATH') {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}

$env:PYTHONPATH = $venvSitePackages

& $realPython $Script @ScriptArgs
exit $LASTEXITCODE
