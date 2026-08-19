[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$vsWherePath = Join-Path `
    ${env:ProgramFiles(x86)} `
    "Microsoft Visual Studio\Installer\vswhere.exe"

if (-not (Test-Path -LiteralPath $vsWherePath)) {
    throw "Visual Studio locator was not found."
}

$installationPath = & $vsWherePath `
    -latest `
    -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath

if (-not $installationPath) {
    throw "The Visual C++ toolchain was not found."
}

$msBuildPath = Join-Path `
    $installationPath `
    "MSBuild\Current\Bin\MSBuild.exe"
$projectPath = Join-Path `
    $PSScriptRoot `
    "native\WhatsAppNotificationListenerNative.vcxproj"

& $msBuildPath `
    $projectPath `
    /nologo `
    /m:1 `
    /t:Build `
    /p:Configuration=Release `
    /p:Platform=x64 `
    /v:minimal

if ($LASTEXITCODE -ne 0) {
    throw "Native notification listener build failed."
}
