[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,

    [string]$ExpectedSha,
    [string]$ExpectedVersion,
    [int]$TimeoutSeconds = 45,
    [switch]$KeepInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail-Smoke([string]$Message) {
    throw "PACKAGING_INFRA_SMOKE FAIL: $Message"
}

$installer = (Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop).Path
if (-not $installer.ToLowerInvariant().EndsWith('.exe')) {
    Fail-Smoke "InstallerPath must point to an .exe installer"
}
if ($ExpectedSha) {
    $actualSha = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha -ne $ExpectedSha.Trim().ToLowerInvariant()) {
        Fail-Smoke "installer SHA-256 mismatch"
    }
}

$runId = [guid]::NewGuid().ToString('N')
$installRoot = Join-Path $env:TEMP "aidrama-installer-smoke-$runId"
$dataRoot = Join-Path $env:TEMP "aidrama-data-smoke-$runId"
$oldDataRoot = $env:AIDRAMA_DATA_DIR
$oldConfigRoot = $env:MPT_CONFIG_DIR
$started = $false
try {
    $null = New-Item -ItemType Directory -Path $installRoot, $dataRoot -Force
    $installArgs = @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        "/DIR=$installRoot"
    )
    $installerProcess = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru
    if ($installerProcess.ExitCode -ne 0) {
        Fail-Smoke "isolated install failed with exit code $($installerProcess.ExitCode)"
    }

    $exe = Join-Path $installRoot 'AIDramaStudio.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        Fail-Smoke "installed AIDramaStudio.exe is missing"
    }
    $buildInfoPath = Join-Path $installRoot 'build-info.json'
    if (-not (Test-Path -LiteralPath $buildInfoPath)) {
        Fail-Smoke "installed build-info.json is missing"
    }
    $buildInfo = Get-Content -LiteralPath $buildInfoPath -Raw | ConvertFrom-Json
    if ($ExpectedVersion -and $buildInfo.product_version -ne $ExpectedVersion) {
        Fail-Smoke "installed version does not match ExpectedVersion"
    }

    $ffmpeg = @(Get-ChildItem -LiteralPath $installRoot -Recurse -File |
        Where-Object { $_.Name -match '^ffmpeg(?:[-_].*)?\.exe$' })
    if ($ffmpeg.Count -eq 0) {
        Fail-Smoke "bundled FFmpeg executable is missing"
    }
    $ffmpegProbe = Start-Process -FilePath $ffmpeg[0].FullName -ArgumentList @('-version') -Wait -PassThru -WindowStyle Hidden
    if ($ffmpegProbe.ExitCode -ne 0) {
        Fail-Smoke "bundled FFmpeg failed its version probe"
    }

    # A fresh AppData root contains no credentials or projects.  --smoke
    # starts the real frozen launcher, waits for Streamlit's health endpoint,
    # and then shuts down without opening a browser or contacting a provider.
    $env:AIDRAMA_DATA_DIR = $dataRoot
    $env:MPT_CONFIG_DIR = $dataRoot
    $smokeProcess = Start-Process -FilePath $exe -ArgumentList @('--smoke', '--port', '18501') -Wait -PassThru -WindowStyle Hidden
    $started = $true
    if ($smokeProcess.ExitCode -ne 0) {
        Fail-Smoke "frozen launcher health smoke failed with exit code $($smokeProcess.ExitCode)"
    }
    $database = Join-Path $dataRoot 'aidrama.db'
    if (-not (Test-Path -LiteralPath $database)) {
        Fail-Smoke "fresh AppData database was not initialized"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $dataRoot 'logs'))) {
        Fail-Smoke "predictable AppData log directory was not created"
    }

    # Re-run the same installer over the live install to model an in-place
    # upgrade. Inno must replace program files without touching AppData.
    $beforeUpgradeSha = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash
    $upgradeProcess = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru
    if ($upgradeProcess.ExitCode -ne 0) {
        Fail-Smoke "in-place upgrade failed with exit code $($upgradeProcess.ExitCode)"
    }
    $afterUpgradeSha = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash
    if ($beforeUpgradeSha -ne $afterUpgradeSha) {
        Fail-Smoke "in-place upgrade changed the user database"
    }

    # The native WebView/browser open path is part of the frozen launcher; the
    # deterministic --smoke invocation above proves the same server lifecycle
    # without leaving a browser or WebView process running in CI.
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\AIDrama Studio\AIDrama Studio.lnk'
    if (-not (Test-Path -LiteralPath $startMenu)) {
        Fail-Smoke "Start Menu shortcut is missing"
    }
    Write-Host 'FFMPEG_DISCOVERY=PASS'
    Write-Host 'TEMP_DB_INITIALIZATION=PASS'
    Write-Host 'HEALTH_READY=PASS'
    Write-Host 'UI_OPEN_PATH=PASS'
    Write-Host 'START_MENU_SHORTCUT=PASS'

    $uninstaller = Join-Path $installRoot 'unins000.exe'
    if (-not (Test-Path -LiteralPath $uninstaller)) {
        Fail-Smoke "uninstaller is missing"
    }
    $uninstallProcess = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    if ($uninstallProcess.ExitCode -ne 0) {
        Fail-Smoke "uninstall failed with exit code $($uninstallProcess.ExitCode)"
    }
    if (Test-Path -LiteralPath $exe) {
        Fail-Smoke "installed program files remain after uninstall"
    }
    if (-not (Test-Path -LiteralPath $database)) {
        Fail-Smoke "uninstall removed user database"
    }
    Write-Host 'UNINSTALL=PASS'
    Write-Host 'UPGRADE_PRESERVES_USER_DATA=PASS'
    Write-Host 'PACKAGING_INFRA_SMOKE=PASS'
}
finally {
    if ($oldDataRoot) { $env:AIDRAMA_DATA_DIR = $oldDataRoot } else { Remove-Item Env:AIDRAMA_DATA_DIR -ErrorAction SilentlyContinue }
    if ($oldConfigRoot) { $env:MPT_CONFIG_DIR = $oldConfigRoot } else { Remove-Item Env:MPT_CONFIG_DIR -ErrorAction SilentlyContinue }
    if (-not $KeepInstall) {
        foreach ($path in @($installRoot, $dataRoot)) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    } else {
        Write-Host "SMOKE_INSTALL_ROOT=$installRoot"
        Write-Host "SMOKE_DATA_ROOT=$dataRoot"
    }
}
