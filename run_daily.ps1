$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundledPython = "C:\Users\49182\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (Test-Path -LiteralPath $bundledPython) {
    $pythonExecutable = $bundledPython
} else {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $pythonExecutable = $pythonCommand.Source
}

& $pythonExecutable (Join-Path $projectRoot "collector.py")
if ($LASTEXITCODE -ne 0) {
    throw "Global trends collection failed with exit code $LASTEXITCODE"
}

& $pythonExecutable (Join-Path $projectRoot "collector.py") --validate-only
if ($LASTEXITCODE -ne 0) {
    throw "Global trends validation failed with exit code $LASTEXITCODE"
}
