param([Parameter(ValueFromRemainingArguments=$true)][string[]]$BridgeArguments)
$ErrorActionPreference = 'Stop'
$candidates = @()
if ($env:SERIALIZD_PYTHON) { $candidates += $env:SERIALIZD_PYTHON }
foreach ($name in @('python.exe', 'python3.exe')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
}
# Python's Windows launcher can locate an interpreter that isn't on PATH.
$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyLauncher) {
    try {
        $launchedPython = & $pyLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $launchedPython) { $candidates += $launchedPython.Trim() }
    } catch { }
}
$pythonPath = $null
foreach ($candidate in $candidates) {
    if (!(Test-Path -LiteralPath $candidate)) { continue }
    if ($candidate -like '*\Microsoft\WindowsApps\*') { continue }
    & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonPath = $candidate; break }
}
if (!$pythonPath) { throw 'Python 3.10+ was not found. Set SERIALIZD_PYTHON to a Python executable. No pip packages are needed.' }
if (!$BridgeArguments) { $BridgeArguments = @('--help') }
& $pythonPath -X utf8 (Join-Path $PSScriptRoot 'serializd_mpv.py') @BridgeArguments
exit $LASTEXITCODE
