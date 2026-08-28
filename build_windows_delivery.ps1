[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$DeliveryHead = $env:DELIVERY_HEAD,

    [Parameter(Mandatory = $false)]
    [string]$Version = $env:VERSION,

    [Parameter(Mandatory = $false)]
    [string]$PythonExecutable = $env:AIDRAMA_BUILD_PYTHON,

    [Parameter(Mandatory = $false)]
    [string]$ArtifactRoot,

    [Parameter(Mandatory = $false)]
    [string]$InnoCompiler = $env:AIDRAMA_INNO_COMPILER,

    [switch]$KeepStaging,
    [switch]$SkipInstaller
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$DeliveryHead = ("$DeliveryHead").Trim().ToLowerInvariant()
$Version = ("$Version").Trim()

function Fail-Closed([string]$Message) {
    throw "FAIL CLOSED: $Message"
}

if ($DeliveryHead -notmatch '^[0-9a-f]{40}$') {
    Fail-Closed "DELIVERY_HEAD must be an exact 40-character hexadecimal commit SHA"
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Fail-Closed "VERSION must be a numeric Windows version such as 1.0.0"
}

function Invoke-Git([string[]]$Arguments, [string]$WorkingDirectory = $RepoRoot) {
    $output = & git -C $WorkingDirectory @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail-Closed ("git {0} failed: {1}" -f ($Arguments -join ' '), ($output -join "`n"))
    }
    return @($output)
}

# The caller's packaging worktree must be clean.  The staging worktree below is
# the only tree that is ever handed to PyInstaller, so a dirty developer tree
# can never leak into a customer package.
$status = @(Invoke-Git @('status', '--porcelain'))
if ($status.Count -gt 0) {
    Fail-Closed "packaging source worktree is dirty; commit or stash changes before packaging"
}
$resolvedDelivery = @(Invoke-Git @('rev-parse', "$DeliveryHead^{commit}"))
if ($resolvedDelivery[0].Trim().ToLowerInvariant() -ne $DeliveryHead) {
    Fail-Closed "DELIVERY_HEAD does not resolve to the requested commit"
}

if ([string]::IsNullOrWhiteSpace($ArtifactRoot)) {
    $shortSha = $DeliveryHead.Substring(0, 12)
    $ArtifactRoot = Join-Path $RepoRoot ("dist\delivery-{0}-{1}" -f $Version, $shortSha)
}
$ArtifactRoot = [System.IO.Path]::GetFullPath($ArtifactRoot)
if (Test-Path -LiteralPath $ArtifactRoot) {
    Fail-Closed "artifact directory already exists (refusing to overwrite): $ArtifactRoot"
}
$null = New-Item -ItemType Directory -Path $ArtifactRoot -Force
$installerOut = Join-Path $ArtifactRoot 'installer'
$shortSha = $DeliveryHead.Substring(0, 12)

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $candidates = @(
        (Join-Path $RepoRoot '.venv\Scripts\python.exe'),
        (Join-Path $RepoRoot 'build-env\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { $PythonExecutable = $candidate; break }
    }
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    Fail-Closed "no dedicated build Python found in the packaging worktree; pass -PythonExecutable or set AIDRAMA_BUILD_PYTHON"
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path

& $PythonExecutable -c "import importlib.metadata as m; import PyInstaller; print(PyInstaller.__version__); print(m.version('pywebview'))" 2>&1 | Out-String | Write-Verbose
if ($LASTEXITCODE -ne 0) {
    Fail-Closed "build Python lacks PyInstaller and/or pinned PyWebView 6.2.1; install desktop/requirements.txt and PyInstaller in an isolated environment"
}

$stageRoot = Join-Path $env:TEMP ("aidrama-delivery-{0}-{1}" -f $shortSha, ([guid]::NewGuid().ToString('N')))
$stageCreated = $false
try {
    Invoke-Git @('worktree', 'add', '--quiet', '--detach', $stageRoot, $DeliveryHead) | Out-Null
    $stageCreated = $true
    $stageSha = (@(Invoke-Git @('rev-parse', 'HEAD') $stageRoot))[0].Trim().ToLowerInvariant()
    if ($stageSha -ne $DeliveryHead) {
        Fail-Closed "staging worktree SHA mismatch: expected $DeliveryHead, got $stageSha"
    }
    $stageStatus = @(Invoke-Git @('status', '--porcelain') $stageRoot)
    if ($stageStatus.Count -gt 0) {
        Fail-Closed "staging worktree is not clean"
    }

    $buildArgs = @(
        '-m', 'desktop.build',
        '--output-dir', $ArtifactRoot,
        '--version', $Version,
        '--delivery-head', $DeliveryHead,
        '--source-root', $stageRoot
    )
    # Keep the packaging-only launcher out of the staged product tree. The
    # temporary tooling directory contains no product modules, so PyInstaller
    # resolves aidrama_studio/app/webui exclusively from SOURCE_ROOT.
    $toolingRoot = Join-Path $ArtifactRoot '_packaging_tooling'
    $toolingDesktop = Join-Path $toolingRoot 'desktop'
    $null = New-Item -ItemType Directory -Path $toolingDesktop -Force
    foreach ($toolingFile in @('launcher.py', 'background.py', '__init__.py')) {
        Copy-Item -LiteralPath (Join-Path $RepoRoot "desktop\$toolingFile") -Destination (Join-Path $toolingDesktop $toolingFile)
    }
    # The packaging branch owns the build/installer tooling, while every
    # product file consumed by PyInstaller is resolved from the immutable
    # stage through --source-root. This permits a noon DELIVERY_HEAD that
    # predates today's packaging helpers without mutating that commit.
    $oldSourceRoot = $env:AIDRAMA_SOURCE_ROOT
    $oldEntrypoint = $env:AIDRAMA_DESKTOP_ENTRYPOINT
    $oldMptConfig = $env:MPT_CONFIG_DIR
    $oldAidramaData = $env:AIDRAMA_DATA_DIR
    $buildConfigRoot = Join-Path $ArtifactRoot '_build-config'
    $null = New-Item -ItemType Directory -Path $buildConfigRoot -Force
    Copy-Item -LiteralPath (Join-Path $stageRoot 'config.example.toml') -Destination (Join-Path $buildConfigRoot 'config.toml') -Force
    $env:AIDRAMA_SOURCE_ROOT = $stageRoot
    $env:AIDRAMA_DESKTOP_ENTRYPOINT = Join-Path $toolingDesktop 'launcher.py'
    $env:MPT_CONFIG_DIR = $buildConfigRoot
    $env:AIDRAMA_DATA_DIR = $buildConfigRoot
    Push-Location $RepoRoot
    try {
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $PythonExecutable @buildArgs 2>&1 | ForEach-Object { Write-Host $_ }
            $buildExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorAction
        }
    }
    finally {
        Pop-Location
        if ($oldSourceRoot) { $env:AIDRAMA_SOURCE_ROOT = $oldSourceRoot }
        else { Remove-Item Env:AIDRAMA_SOURCE_ROOT -ErrorAction SilentlyContinue }
        if ($oldEntrypoint) { $env:AIDRAMA_DESKTOP_ENTRYPOINT = $oldEntrypoint }
        else { Remove-Item Env:AIDRAMA_DESKTOP_ENTRYPOINT -ErrorAction SilentlyContinue }
        if ($oldMptConfig) { $env:MPT_CONFIG_DIR = $oldMptConfig }
        else { Remove-Item Env:MPT_CONFIG_DIR -ErrorAction SilentlyContinue }
        if ($oldAidramaData) { $env:AIDRAMA_DATA_DIR = $oldAidramaData }
        else { Remove-Item Env:AIDRAMA_DATA_DIR -ErrorAction SilentlyContinue }
    }
    if ($buildExitCode -ne 0) {
        Fail-Closed "PyInstaller build failed"
    }

    $packageRoot = Join-Path $ArtifactRoot 'AIDramaStudio'
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot 'AIDramaStudio.exe'))) {
        Fail-Closed "PyInstaller did not produce AIDramaStudio.exe"
    }
    $buildInfoPath = Join-Path $packageRoot 'build-info.json'
    if (-not (Test-Path -LiteralPath $buildInfoPath)) {
        Fail-Closed "build-info.json is missing from the package"
    }
    $buildInfo = Get-Content -LiteralPath $buildInfoPath -Raw | ConvertFrom-Json
    if ($buildInfo.delivery_head -ne $DeliveryHead -or $buildInfo.product_version -ne $Version) {
        Fail-Closed "embedded build provenance does not match DELIVERY_HEAD/VERSION"
    }

    $ffmpeg = @(Get-ChildItem -LiteralPath $packageRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^ffmpeg(?:[-_].*)?\.exe$' })
    if ($ffmpeg.Count -eq 0) {
        Fail-Closed "FFmpeg binary is not bundled; refusing a package that depends on global PATH"
    }
    $ffprobe = @(Get-ChildItem -LiteralPath $packageRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^ffprobe(?:[-_].*)?\.exe$' })

    $secretPattern = '(?i)(sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}|api[_-]?key\s*=\s*["''][^"'']{12,})'
    $textExtensions = @('.py', '.toml', '.json', '.txt', '.md', '.yaml', '.yml', '.ini', '.cfg')
    $secretHits = @()
    foreach ($file in Get-ChildItem -LiteralPath $packageRoot -Recurse -File) {
        $ownedSource = $file.FullName -match '\\_internal\\(aidrama_studio|app)\\' -or
            $file.DirectoryName -eq $packageRoot
        if ($ownedSource -and $textExtensions -contains $file.Extension.ToLowerInvariant()) {
            $matches = Select-String -LiteralPath $file.FullName -Pattern $secretPattern -AllMatches -ErrorAction SilentlyContinue
            if ($matches) {
                $secretHits += @($matches | Where-Object { $_.Line -notmatch '(?i)example|placeholder|hardcoded|your[_ -]?api' })
            }
        }
    }
    if ($secretHits.Count -gt 0) {
        Fail-Closed "credential-like literal detected in packaged text files"
    }

    if (-not $SkipInstaller) {
        $iscc = $null
        $isccCandidates = @()
        if ($InnoCompiler) { $isccCandidates += $InnoCompiler }
        $isccCandidates += @('iscc.exe', 'D:\environment\inno-setup\installed\ISCC.exe', 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe', 'C:\Program Files\Inno Setup 6\ISCC.exe')
        foreach ($candidate in $isccCandidates) {
            if ([System.IO.Path]::IsPathRooted($candidate)) {
                if (Test-Path -LiteralPath $candidate) { $iscc = (Resolve-Path -LiteralPath $candidate).Path; break }
            } else {
                $command = Get-Command $candidate -ErrorAction SilentlyContinue
                if ($null -ne $command) { $iscc = $command.Source; break }
            }
        }
        if ([string]::IsNullOrWhiteSpace($iscc)) {
            Fail-Closed "Inno Setup compiler (ISCC.exe) is not available"
        }
        $null = New-Item -ItemType Directory -Path $installerOut -Force
        $iss = Join-Path $RepoRoot 'installer\AIDramaStudio.iss'
        $installerName = "AIDramaStudio-{0}-Windows-x64-{1}-Setup" -f $Version, $shortSha
        $issArgs = @(
            "/DMyAppVersion=$Version",
            "/DDeliveryHead=$DeliveryHead",
            "/DSourceDir=$packageRoot",
            "/DOutputDir=$installerOut",
            "/DOutputBaseFilename=$installerName",
            $iss
        )
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $iscc @issArgs 2>&1 | ForEach-Object { Write-Host $_ }
            $isccExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorAction
        }
        if ($isccExitCode -ne 0) {
            Fail-Closed "Inno Setup compilation failed"
        }
    }

    $installer = @(Get-ChildItem -LiteralPath $installerOut -Filter '*.exe' -File -ErrorAction SilentlyContinue)
    if (-not $SkipInstaller -and $installer.Count -ne 1) {
        Fail-Closed "expected exactly one installer executable"
    }

    $manifest = [ordered]@{
        product = 'AIDrama Studio'
        version = $Version
        delivery_head = $DeliveryHead
        staging_sha = $stageSha
        package_directory = $packageRoot
        installer = if ($installer.Count -eq 1) { $installer[0].Name } else { $null }
        installer_sha256 = if ($installer.Count -eq 1) { (Get-FileHash -LiteralPath $installer[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
        ffmpeg_discovery = 'PASS'
        ffprobe_discovery = if ($ffprobe.Count -gt 0) { 'PASS' } else { 'NOT_SHIPPED_APPLICATION_DOES_NOT_REQUIRE_FFPROBE' }
        credentials_bundled = $false
        built_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ArtifactRoot 'delivery-manifest.json') -Encoding UTF8
    if ($installer.Count -eq 1) {
        "$($manifest.installer_sha256)  $($installer[0].Name)" | Set-Content -LiteralPath (Join-Path $ArtifactRoot 'SHA256SUMS') -Encoding ASCII
        Write-Host "INSTALLER=$($installer[0].FullName)"
        Write-Host "SHA256=$($manifest.installer_sha256)"
    }
    Write-Host "VERSION=$Version"
    Write-Host "EMBEDDED_BUILD_SHA=$DeliveryHead"
}
finally {
    if ($stageCreated -and -not $KeepStaging) {
        & git -C $RepoRoot worktree remove --force $stageRoot 2>&1 | Out-Null
    } elseif ($stageCreated) {
        Write-Host "STAGING_WORKTREE=$stageRoot"
    }
}
