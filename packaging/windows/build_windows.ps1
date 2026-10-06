<#
.SYNOPSIS
    Builds the Windows application and its installer (docs/windows-distribution.md).

.DESCRIPTION
    From the repository root, in Windows PowerShell 5.1 or later:

        packaging\windows\build_windows.ps1

    1. creates or reuses the build environment .venv-build from requirements-build.txt
       (the desktop runtime and PyInstaller only -- no server package can be packaged);
    2. runs the development test suite (skip with -SkipTests);
    3. builds dist\ScheduleMaxing\ScheduleMaxing.exe with packaging\windows\ScheduleMaxing.spec;
    4. checks the built folder (check_bundle.py) and runs the packaged self-test (smoke_test.py);
    5. compiles dist\installer\ScheduleMaxing-Setup-<version>.exe with Inno Setup 6
       (skip with -SkipInstaller) and writes SHA256SUMS.txt beside it;
    6. signs the executable and the installer when signing is configured (otherwise: unsigned).

    The version comes from app\version.py and nowhere else. Any failing step stops the build.

.PARAMETER SkipTests
    Do not run the test suite first.
.PARAMETER SkipInstaller
    Stop after the application folder is built and smoke-tested.
.PARAMETER Clean
    Delete build\, dist\ and the build environment first.
.PARAMETER Python
    The Python used to create the build environment (default: the "python" on PATH; Python 3.10+ x64).
.PARAMETER TestPython
    The Python that has the test tools installed (default: .venv\Scripts\python.exe if present).
.PARAMETER InnoSetupPath
    ISCC.exe, when it is neither on PATH nor in a default install folder.
.PARAMETER SignThumbprint
    SHA-1 thumbprint of a code-signing certificate in the Windows certificate store. Default: $env:SM_SIGN_THUMBPRINT.
.PARAMETER SignPfx
    A .pfx code-signing certificate file (its password in $env:SM_SIGN_PFX_PASSWORD). Default: $env:SM_SIGN_PFX.
.PARAMETER TimestampUrl
    RFC 3161 timestamp server used when signing.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$Clean,
    [string]$Python = "python",
    [string]$TestPython = "",
    [string]$InnoSetupPath = "",
    [string]$SignThumbprint = $env:SM_SIGN_THUMBPRINT,
    [string]$SignPfx = $env:SM_SIGN_PFX,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path
$BuildEnv = Join-Path $Root ".venv-build"
$BuildPython = Join-Path $BuildEnv "Scripts\python.exe"
$DistDir = Join-Path $Root "dist"
$WorkDir = Join-Path $Root "build"
$AppDir = Join-Path $DistDir "ScheduleMaxing"
$AppExe = Join-Path $AppDir "ScheduleMaxing.exe"
$InstallerDir = Join-Path $DistDir "installer"

function Step([string]$Message) { Write-Host ""; Write-Host "==> $Message" -ForegroundColor Cyan }

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Failed (exit code $LASTEXITCODE): $Program $($Arguments -join ' ')" }
}

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$script:SigningConfigured = [bool]($SignThumbprint -or $SignPfx)

# The signtool arguments that select the certificate (never echoed: they may hold a password).
function Get-SignArguments {
    $arguments = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")
    if ($SignThumbprint) { return $arguments + @("/sha1", $SignThumbprint) }
    $arguments += @("/f", $SignPfx)
    if ($env:SM_SIGN_PFX_PASSWORD) { $arguments += @("/p", $env:SM_SIGN_PFX_PASSWORD) }
    return $arguments
}

# Signs a file when a certificate is configured; otherwise says so and does nothing.
function Invoke-CodeSign([string]$File) {
    if (-not $script:SigningConfigured) {
        Write-Host "Not signed (no certificate configured): $File" -ForegroundColor Yellow
        return
    }
    $signTool = Find-SignTool
    if (-not $signTool) { throw "Signing is configured but signtool.exe was not found (install the Windows SDK)." }
    & $signTool @(Get-SignArguments) $File
    if ($LASTEXITCODE -ne 0) { throw "signtool failed for $File" }
    Write-Host "Signed: $File" -ForegroundColor Green
}

function Find-InnoSetup {
    if ($InnoSetupPath) {
        if (Test-Path $InnoSetupPath) { return (Resolve-Path $InnoSetupPath).Path }
        throw "ISCC.exe was not found at -InnoSetupPath $InnoSetupPath"
    }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if ($base) {
            $candidate = Join-Path $base "Inno Setup 6\ISCC.exe"
            if (Test-Path $candidate) { return $candidate }
        }
    }
    throw ("Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup), " +
           "pass -InnoSetupPath <path to ISCC.exe>, or use -SkipInstaller to build only the application folder.")
}

Push-Location $Root
try {
    if ($Clean) {
        Step "Cleaning build outputs"
        foreach ($folder in @($WorkDir, $DistDir, $BuildEnv)) {
            if (Test-Path $folder) { Remove-Item -Recurse -Force $folder }
        }
    }

    Step "Build environment ($BuildEnv)"
    if (-not (Test-Path $BuildPython)) { Invoke-Checked $Python @("-m", "venv", $BuildEnv) }
    Invoke-Checked $BuildPython @("-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--upgrade", "pip")
    Invoke-Checked $BuildPython @("-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-r", "requirements-build.txt")
    $bits = (& $BuildPython -c "import struct; print(struct.calcsize('P') * 8)").Trim()
    if ($bits -ne "64") { throw "The build needs a 64-bit Python (found $bits-bit)." }

    $Version = (& $BuildPython (Join-Path $Here "version_info.py")).Trim()
    if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') { throw "Could not read the version from app\version.py." }
    Write-Host "Schedule Maxing $Version"

    if (-not $SkipTests) {
        Step "Tests (development suite)"
        if (-not $TestPython) { $TestPython = Join-Path $Root ".venv\Scripts\python.exe" }
        if (-not (Test-Path $TestPython)) {
            throw "No test environment at $TestPython. Pass -TestPython <python with requirements.txt installed> or -SkipTests."
        }
        Invoke-Checked $TestPython @("-m", "pytest", "-m", "dev", "-q")
    }

    Step "PyInstaller"
    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
    Invoke-Checked $BuildPython @("-m", "PyInstaller", (Join-Path $Here "ScheduleMaxing.spec"), "--noconfirm",
                                  "--distpath", $DistDir, "--workpath", $WorkDir, "--log-level", "WARN")
    if (-not (Test-Path $AppExe)) { throw "PyInstaller did not produce $AppExe" }

    Step "Checking the application folder"
    Invoke-Checked $BuildPython @((Join-Path $Here "check_bundle.py"), $AppDir, "--pyz",
                                  (Join-Path $WorkDir "ScheduleMaxing\PYZ-00.pyz"))

    Step "Smoke test of the packaged application"
    Invoke-Checked $BuildPython @((Join-Path $Here "smoke_test.py"), $AppExe)

    Invoke-CodeSign $AppExe

    if ($SkipInstaller) {
        Write-Host ""
        Write-Host "Application folder: $AppDir (installer skipped)" -ForegroundColor Green
        return
    }

    Step "Inno Setup installer"
    $iscc = Find-InnoSetup
    $Publisher = (& $BuildPython -c "import sys; sys.path.insert(0, r'$Here'); import version_info; print(version_info.read_metadata()['APP_PUBLISHER'])").Trim()
    $isccArguments = @("/Qp", "/DAppVersion=$Version", "/DAppPublisher=$Publisher", "/DSourceDir=$AppDir", "/DOutputDir=$InstallerDir")
    if ($script:SigningConfigured) {
        # Inno Setup signs the installer and the uninstaller it embeds with this command ($f is the file).
        $signTool = Find-SignTool
        if (-not $signTool) { throw "Signing is configured but signtool.exe was not found (install the Windows SDK)." }
        $quoted = (Get-SignArguments | ForEach-Object { '$q' + $_ + '$q' }) -join " "
        $isccArguments += @("/DSignedBuild=1", ('/Ssmsign=$q' + $signTool + '$q ' + $quoted + ' $f'))
    }
    $isccArguments += (Join-Path $Here "ScheduleMaxing.iss")
    & $iscc @isccArguments
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit code $LASTEXITCODE)." }

    $Installer = Join-Path $InstallerDir "ScheduleMaxing-Setup-$Version.exe"
    if (-not (Test-Path $Installer)) { throw "Inno Setup did not produce $Installer" }
    if (-not $script:SigningConfigured) { Write-Host "Not signed (no certificate configured): $Installer" -ForegroundColor Yellow }

    Step "Checksums"
    Invoke-Checked $BuildPython @((Join-Path $Here "checksums.py"), $Installer)

    Write-Host ""
    Write-Host "Installer: $Installer" -ForegroundColor Green
    Write-Host "Checksums: $(Join-Path $InstallerDir 'SHA256SUMS.txt')" -ForegroundColor Green
}
finally {
    Pop-Location
}
