[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $PSScriptRoot "runtime"
$JavaDir = Join-Path $RuntimeDir "java"
$PlatformToolsDir = Join-Path $RuntimeDir "platform-tools"
$TempDir = Join-Path ([IO.Path]::GetTempPath()) ("quest-apk-renamer-" + [guid]::NewGuid())

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

try {
    $HashLines = @()

    if ($Force -or -not (Test-Path (Join-Path $JavaDir "bin\java.exe"))) {
        $JavaArchive = Join-Path $TempDir "temurin-jdk.zip"
        $JavaExtract = Join-Path $TempDir "java"
        $LinkedJava = Join-Path $TempDir "java-runtime"
        $JavaUrl = "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jdk/hotspot/normal/eclipse"
        Write-Host "Downloading Eclipse Temurin JDK 21 to create a smaller runtime..."
        Invoke-WebRequest -UseBasicParsing -Uri $JavaUrl -OutFile $JavaArchive
        Expand-Archive -Path $JavaArchive -DestinationPath $JavaExtract
        $ExtractedJdk = Get-ChildItem -Path $JavaExtract -Directory | Select-Object -First 1
        if (-not $ExtractedJdk) {
            throw "The Temurin archive did not contain a JDK directory."
        }
        $Jlink = Join-Path $ExtractedJdk.FullName "bin\jlink.exe"
        if (-not (Test-Path $Jlink)) {
            throw "The Temurin archive did not contain jlink.exe."
        }
        & $Jlink `
            --add-modules "java.base,java.desktop,java.logging" `
            --strip-debug `
            --no-header-files `
            --no-man-pages `
            --compress=2 `
            --output $LinkedJava
        if ($LASTEXITCODE -ne 0) {
            throw "jlink failed to create the bundled Java runtime."
        }
        & (Join-Path $LinkedJava "bin\java.exe") -jar (Join-Path $ProjectDir "tools\apktool.jar") --version
        if ($LASTEXITCODE -ne 0) {
            throw "The trimmed Java runtime could not start Apktool."
        }
        & (Join-Path $LinkedJava "bin\java.exe") -jar (Join-Path $ProjectDir "tools\uber-apk-signer.jar") --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "The trimmed Java runtime could not start the APK signer."
        }
        & (Join-Path $LinkedJava "bin\keytool.exe") -help 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "The trimmed Java runtime does not provide a working keytool."
        }
        if (Test-Path $JavaDir) {
            Remove-Item -LiteralPath $JavaDir -Recurse -Force
        }
        Move-Item -LiteralPath $LinkedJava -Destination $JavaDir
        $HashLines += "Temurin JDK archive SHA256: $((Get-FileHash $JavaArchive -Algorithm SHA256).Hash)"
        $HashLines += "Temurin source: $JavaUrl"
        $HashLines += "jlink modules: java.base,java.desktop,java.logging"
    }
    if (-not (Test-Path (Join-Path $JavaDir "bin\keytool.exe"))) {
        throw "The bundled Java runtime does not include keytool.exe, which is required to create signing keys."
    }

    if ($Force -or -not (Test-Path (Join-Path $PlatformToolsDir "adb.exe"))) {
        $PlatformArchive = Join-Path $TempDir "platform-tools.zip"
        $PlatformExtract = Join-Path $TempDir "platform-tools"
        $PlatformUrl = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
        Write-Host "Downloading Android SDK Platform-Tools..."
        Invoke-WebRequest -UseBasicParsing -Uri $PlatformUrl -OutFile $PlatformArchive
        Expand-Archive -Path $PlatformArchive -DestinationPath $PlatformExtract
        $ExtractedTools = Join-Path $PlatformExtract "platform-tools"
        if (-not (Test-Path (Join-Path $ExtractedTools "adb.exe"))) {
            throw "The Platform-Tools archive did not contain adb.exe."
        }
        if (Test-Path $PlatformToolsDir) {
            Remove-Item -LiteralPath $PlatformToolsDir -Recurse -Force
        }
        Move-Item -LiteralPath $ExtractedTools -Destination $PlatformToolsDir
        $HashLines += "Android Platform-Tools archive SHA256: $((Get-FileHash $PlatformArchive -Algorithm SHA256).Hash)"
        $HashLines += "Platform-Tools source: $PlatformUrl"
    }

    $JavaVersion = & (Join-Path $JavaDir "bin\java.exe") -version 2>&1
    $AdbVersion = & (Join-Path $PlatformToolsDir "adb.exe") version 2>&1
    $HashLines += ""
    $HashLines += "Resolved Java:"
    $HashLines += $JavaVersion
    $HashLines += ""
    $HashLines += "Resolved ADB:"
    $HashLines += $AdbVersion
    $HashLines | Set-Content -Encoding UTF8 (Join-Path $RuntimeDir "DEPENDENCY-HASHES.txt")

    Write-Host "Windows runtime dependencies are ready in $RuntimeDir"
}
finally {
    if (Test-Path $TempDir) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force
    }
}
