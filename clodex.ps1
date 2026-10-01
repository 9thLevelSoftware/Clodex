# PowerShell shim for Clodex.
$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-ClodexPython {
    param([string]$Exe, [string[]]$Prefix = @())
    # Probe tolerantly: the Microsoft Store python3.exe stub writes to stderr and
    # exits non-zero, which 'Stop' would otherwise turn into a terminating error.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Find-ClodexPython {
    foreach ($candidate in @('python3', 'python')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd -and (Test-ClodexPython $cmd.Source)) {
            return @{ Exe = $cmd.Source; Prefix = @() }
        }
    }
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @('-3', '-3.14', '-3.13', '-3.12')) {
            if (Test-ClodexPython $py.Source @($version)) {
                return @{ Exe = $py.Source; Prefix = @($version) }
            }
        }
    }
    throw 'Clodex requires Python >= 3.12'
}

$python = Find-ClodexPython
# Stay in the caller's directory so Clodex operates on their repo, not this checkout.
$existing = $env:PYTHONPATH
$env:PYTHONPATH = if ($existing) { "$scriptDir;$existing" } else { $scriptDir }
& $python.Exe @($python.Prefix) -m clodex @args
exit $LASTEXITCODE
