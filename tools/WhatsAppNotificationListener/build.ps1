[CmdletBinding()]
param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"

$vsWherePath = Join-Path `
    ${env:ProgramFiles(x86)} `
    "Microsoft Visual Studio\Installer\vswhere.exe"

if (-not (Test-Path -LiteralPath $vsWherePath)) {
    throw "Visual Studio locator was not found."
}

$msBuildPath = & $vsWherePath `
    -latest `
    -products * `
    -requires Microsoft.Component.MSBuild `
    -find "MSBuild\**\Bin\MSBuild.exe" |
    Select-Object -First 1

if (-not $msBuildPath) {
    throw "MSBuild was not found."
}

$windowsKitsRoot = Join-Path `
    ${env:ProgramFiles(x86)} `
    "Windows Kits\10\UnionMetadata"

$windowsSdkDirectory = Get-ChildItem `
    -LiteralPath $windowsKitsRoot `
    -Directory |
    Where-Object {
        $_.Name -match "^\d+\.\d+\.\d+\.\d+$"
    } |
    Sort-Object Name -Descending |
    Where-Object {
        Test-Path -LiteralPath (
            Join-Path $_.FullName "Windows.winmd"
        )
    } |
    Select-Object -First 1

if (-not $windowsSdkDirectory) {
    throw "Windows SDK WinRT metadata was not found."
}

$windowsMetadataPath = Join-Path `
    $windowsSdkDirectory.FullName `
    "Windows.winmd"
$windowsSdkVersion = $windowsSdkDirectory.Name
$windowsReferencesRoot = Join-Path `
    ${env:ProgramFiles(x86)} `
    "Windows Kits\10\References\$windowsSdkVersion"
$foundationContractPath = Get-ChildItem `
    -LiteralPath (
        Join-Path `
            $windowsReferencesRoot `
            "Windows.Foundation.FoundationContract"
    ) `
    -Directory |
    Sort-Object Name -Descending |
    ForEach-Object {
        Join-Path `
            $_.FullName `
            "Windows.Foundation.FoundationContract.winmd"
    } |
    Where-Object {
        Test-Path -LiteralPath $_
    } |
    Select-Object -First 1

if (-not $foundationContractPath) {
    throw "Windows SDK API contract metadata was not found."
}

$windowsRuntimePath = Join-Path `
    $env:WINDIR `
    "Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll"

if (-not (Test-Path -LiteralPath $windowsRuntimePath)) {
    throw ".NET Framework Windows Runtime support was not found."
}

$projectPath = Join-Path `
    $PSScriptRoot `
    "WhatsAppNotificationListener.csproj"

& $msBuildPath `
    $projectPath `
    /nologo `
    /m:1 `
    /t:Rebuild `
    /p:Configuration=$Configuration `
    /p:Platform=AnyCPU `
    /p:WindowsSdkUnionMetadataPath="$windowsMetadataPath" `
    /p:WindowsFoundationContractPath="$foundationContractPath" `
    /p:WindowsRuntimeAssemblyPath="$windowsRuntimePath" `
    /v:minimal

if ($LASTEXITCODE -ne 0) {
    throw "Companion build failed."
}
