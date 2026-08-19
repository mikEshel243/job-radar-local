[CmdletBinding()]
param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",

    [ValidatePattern("^[0-9A-Fa-f]{40}$")]
    [string]$CertificateThumbprint,

    [ValidatePattern("^[0-9A-Za-z][0-9A-Za-z.-]{0,63}$")]
    [string]$ArtifactSuffix
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "build.ps1") `
    -Configuration $Configuration

if ($LASTEXITCODE -ne 0) {
    throw "Companion build failed before packaging."
}

& (Join-Path $PSScriptRoot "build-native-listener.ps1")

if ($LASTEXITCODE -ne 0) {
    throw "Native listener build failed before packaging."
}

$artifactsRoot = Join-Path $PSScriptRoot "artifacts"
$stagingName = if ($ArtifactSuffix) {
    "package-$ArtifactSuffix"
}
else {
    "package"
}
$stagingPath = Join-Path $artifactsRoot $stagingName
$resolvedToolRoot = [System.IO.Path]::GetFullPath(
    $PSScriptRoot
)
$resolvedStagingPath = [System.IO.Path]::GetFullPath(
    $stagingPath
)

if (-not $resolvedStagingPath.StartsWith(
    $resolvedToolRoot + [System.IO.Path]::DirectorySeparatorChar,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Refusing to clean a staging path outside the tool folder."
}

if (Test-Path -LiteralPath $resolvedStagingPath) {
    Remove-Item `
        -LiteralPath $resolvedStagingPath `
        -Recurse `
        -Force
}

New-Item `
    -ItemType Directory `
    -Path (Join-Path $resolvedStagingPath "Assets") `
    -Force |
    Out-Null

$buildOutput = Join-Path `
    $PSScriptRoot `
    "bin\$Configuration"

Copy-Item `
    -LiteralPath (
        Join-Path `
            $buildOutput `
            "WhatsAppNotificationListener.exe"
    ) `
    -Destination $resolvedStagingPath
Copy-Item `
    -LiteralPath (
        Join-Path `
            $PSScriptRoot `
            "native\bin\Release\WhatsAppNotificationListenerNative.exe"
    ) `
    -Destination $resolvedStagingPath
Copy-Item `
    -LiteralPath (
        Join-Path $PSScriptRoot "Package.appxmanifest"
    ) `
    -Destination (
        Join-Path $resolvedStagingPath "AppxManifest.xml"
    )

Add-Type -AssemblyName System.Drawing

function New-PackageLogo {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [int]$Size
    )

    $bitmap = [System.Drawing.Bitmap]::new($Size, $Size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)

    try {
        $graphics.Clear(
            [System.Drawing.Color]::FromArgb(32, 80, 120)
        )
        $font = [System.Drawing.Font]::new(
            "Segoe UI",
            [Math]::Max(8, [Math]::Floor($Size / 4)),
            [System.Drawing.FontStyle]::Bold
        )
        $brush = [System.Drawing.Brushes]::White

        try {
            $graphics.DrawString("JR", $font, $brush, 2, 2)
        }
        finally {
            $font.Dispose()
        }

        $bitmap.Save(
            $Path,
            [System.Drawing.Imaging.ImageFormat]::Png
        )
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

New-PackageLogo `
    -Path (
        Join-Path $resolvedStagingPath "Assets\StoreLogo.png"
    ) `
    -Size 50
New-PackageLogo `
    -Path (
        Join-Path `
            $resolvedStagingPath `
            "Assets\Square44x44Logo.png"
    ) `
    -Size 44
New-PackageLogo `
    -Path (
        Join-Path `
            $resolvedStagingPath `
            "Assets\Square150x150Logo.png"
    ) `
    -Size 150

$windowsSdkBin = Get-ChildItem `
    -LiteralPath (
        Join-Path `
            ${env:ProgramFiles(x86)} `
            "Windows Kits\10\bin"
    ) `
    -Directory |
    Where-Object {
        $_.Name -match "^\d+\.\d+\.\d+\.\d+$"
    } |
    Sort-Object Name -Descending |
    Where-Object {
        Test-Path -LiteralPath (
            Join-Path $_.FullName "x64\makeappx.exe"
        )
    } |
    Select-Object -First 1

if (-not $windowsSdkBin) {
    throw "Windows SDK packaging tools were not found."
}

$makeAppxPath = Join-Path `
    $windowsSdkBin.FullName `
    "x64\makeappx.exe"
$signToolPath = Join-Path `
    $windowsSdkBin.FullName `
    "x64\signtool.exe"
$packageName = if ($ArtifactSuffix) {
    "JobRadar.WhatsAppNotificationListener.$ArtifactSuffix.msix"
}
else {
    "JobRadar.WhatsAppNotificationListener.msix"
}
$packagePath = Join-Path $artifactsRoot $packageName

& $makeAppxPath `
    pack `
    /d $resolvedStagingPath `
    /p $packagePath `
    /o

if ($LASTEXITCODE -ne 0) {
    throw "Unsigned MSIX packaging failed."
}

if ($CertificateThumbprint) {
    & $signToolPath `
        sign `
        /fd SHA256 `
        /sha1 $CertificateThumbprint `
        $packagePath

    if ($LASTEXITCODE -ne 0) {
        throw "MSIX signing failed."
    }
}
else {
    Write-Output (
        "Created an unsigned local MSIX. It was not installed. " +
        "Sign it with a trusted local certificate whose subject " +
        "is CN=JobRadarLocal before explicit installation."
    )
}
