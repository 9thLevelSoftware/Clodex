# PowerShell shim for Clodex.
$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-ClodexPython {
    foreach ($candidate in @('python3', 'python')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            & $cmd.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $cmd.Source
            }
        }
    }
    throw 'Clodex requires Python >= 3.12'
}

$python = Find-ClodexPython
Push-Location $scriptDir
try {
    & $python -m clodex @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
