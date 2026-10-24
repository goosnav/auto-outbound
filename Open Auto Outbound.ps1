$ErrorActionPreference = 'Stop'
try {
    $nativeArchitecture = $env:PROCESSOR_ARCHITEW6432
    if (-not $nativeArchitecture) { $nativeArchitecture = $env:PROCESSOR_ARCHITECTURE }
    switch ($nativeArchitecture) {
        'ARM64' { $architecture = 'arm64' }
        'AMD64' { $architecture = 'x64' }
        default { throw 'LAUNCH-PLATFORM: Use Windows x64 or ARM64.' }
    }
    $launcher = Join-Path $PSScriptRoot "app\launcher\bin\windows-$architecture\launcher.exe"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw 'LAUNCH-ROOT: Extract the complete desktop ZIP, then open this file again.'
    }
    $env:GOOSNAV_RELEASE_ROOT = $PSScriptRoot
    & $launcher
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
