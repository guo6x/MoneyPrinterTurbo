[CmdletBinding()]
param(
    [ValidateSet("FAST_GATE", "MEDIUM_GATE", "FULL_GATE")]
    [string]$Suite = "FULL_GATE",
    [switch]$PlanOnly,
    [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"

$testRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $testRoot "..\..\..\..")).Path
$manifestPath = Join-Path $testRoot "matrix.json"

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "MATRIX_MANIFEST_MISSING=$manifestPath"
}

try {
    $matrix = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
}
catch {
    throw "MATRIX_MANIFEST_INVALID=$manifestPath; $($_.Exception.Message)"
}

if ($matrix.phase -ne "AIDRAMA_FORGE_MAX_WAVE_1_INTEGRATED_FULL_AI_E2E_TEST_MATRIX") {
    throw "MATRIX_MANIFEST_PHASE_MISMATCH=$($matrix.phase)"
}
if ([int]$matrix.live_provider_calls -ne 0) {
    throw "MATRIX_MANIFEST_LIVE_PROVIDER_CALLS_MUST_BE_ZERO=$($matrix.live_provider_calls)"
}

$relativeTargets = @($matrix.suites.$Suite)
if ($relativeTargets.Count -eq 0) {
    throw "MATRIX_SUITE_EMPTY=$Suite"
}

$missingFiles = @(
    @($matrix.required_test_files) |
        Where-Object { -not (Test-Path -LiteralPath (Join-Path $testRoot $_) -PathType Leaf) }
)

if ($PlanOnly) {
    [pscustomobject]@{
        phase = $matrix.phase
        reference_base = $matrix.reference_base
        suite = $Suite
        live_provider_calls = [int]$matrix.live_provider_calls
        required_test_files_missing = $missingFiles
        pytest_targets = $relativeTargets
        status = if ($missingFiles.Count -eq 0) { "READY_TO_RUN" } else { "MATRIX_INCOMPLETE" }
    } | ConvertTo-Json -Depth 4
    if ($missingFiles.Count -ne 0) {
        exit 2
    }
    exit 0
}

if ($missingFiles.Count -ne 0) {
    throw "MATRIX_INCOMPLETE: required acceptance tests are absent: $($missingFiles -join ', ')"
}

$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "PYTHON_NOT_FOUND: expected $python or python on PATH"
    }
    $python = $pythonCommand.Source
}

$pytestTargets = foreach ($relativeTarget in $relativeTargets) {
    $parts = $relativeTarget -split "::", 2
    $absoluteFile = Join-Path $testRoot $parts[0]
    if ($parts.Count -eq 2) { "$absoluteFile`::$($parts[1])" } else { $absoluteFile }
}

$runRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("aidrama-wave1-matrix-" + [Guid]::NewGuid().ToString("N"))
$dataRoot = Join-Path $runRoot "explicit-aidrama-data"
$blockedLocalAppData = Join-Path $runRoot "default-localappdata-must-remain-empty"

New-Item -ItemType Directory -LiteralPath $runRoot | Out-Null

$oldDataDir = $env:AIDRAMA_DATA_DIR
$oldWal = $env:AIDRAMA_SQLITE_WAL
$oldNoNetwork = $env:AIDRAMA_TEST_NO_NETWORK
$oldLocalAppData = $env:LOCALAPPDATA
$exitCode = 1
$sentinelTouched = $false

try {
    $env:AIDRAMA_DATA_DIR = $dataRoot
    $env:AIDRAMA_SQLITE_WAL = "0"
    $env:AIDRAMA_TEST_NO_NETWORK = "1"
    # Do not create this directory. Any AIDrama data here means a test escaped
    # its explicit temporary DatabasePaths/AIDRAMA_DATA_DIR contract.
    $env:LOCALAPPDATA = $blockedLocalAppData

    Push-Location $repositoryRoot
    try {
        & $python -m pytest -q @pytestTargets
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    $sentinelTouched = Test-Path -LiteralPath (Join-Path $blockedLocalAppData "AIDramaStudio")
    if ($sentinelTouched) {
        Write-Error "DEFAULT_LOCALAPPDATA_DB_OPENED=1 path=$blockedLocalAppData"
        $exitCode = 1
    }

    [pscustomobject]@{
        phase = $matrix.phase
        reference_base = $matrix.reference_base
        suite = $Suite
        live_provider_calls = 0
        explicit_data_root = $dataRoot
        default_localappdata_db_opened = [int]$sentinelTouched
        pytest_exit_code = $exitCode
        status = if ($exitCode -eq 0) { "PASS" } else { "FAIL" }
    } | ConvertTo-Json -Depth 3
}
finally {
    $env:AIDRAMA_DATA_DIR = $oldDataDir
    $env:AIDRAMA_SQLITE_WAL = $oldWal
    $env:AIDRAMA_TEST_NO_NETWORK = $oldNoNetwork
    $env:LOCALAPPDATA = $oldLocalAppData

    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $runRoot -PathType Container)) {
        $resolvedRunRoot = (Resolve-Path -LiteralPath $runRoot).Path
        $resolvedTempRoot = (Resolve-Path -LiteralPath ([System.IO.Path]::GetTempPath())).Path.TrimEnd("\\")
        if ($resolvedRunRoot.StartsWith($resolvedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedRunRoot -Recurse -Force
        }
    }
}

exit $exitCode
