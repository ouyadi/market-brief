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

$realPython = 'C:\Users\ouyad\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe'
$venvSitePackages = 'C:\Users\ouyad\hermes-agent\.venv\Lib\site-packages'

if (-not (Test-Path $realPython)) {
    Write-Error "uv-managed Python missing at $realPython. Re-run: uv venv --python 3.11 C:\Users\ouyad\hermes-agent\.venv"
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
