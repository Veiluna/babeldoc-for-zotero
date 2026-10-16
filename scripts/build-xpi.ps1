#requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$addonPath = Join-Path $repositoryRoot 'addon'
$manifestPath = Join-Path $addonPath 'manifest.json'
$buildPath = Join-Path $repositoryRoot 'build'
$outputPath = $null
$updatesOutputPath = $null
$archive = $null

try {
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Plugin manifest not found: $manifestPath"
    }

    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $version = [string]$manifest.version
    if ([string]::IsNullOrWhiteSpace($version)) {
        throw 'The plugin manifest has no version.'
    }
    if ($version -notmatch '^[0-9A-Za-z][0-9A-Za-z.+-]*$') {
        throw "Manifest version cannot be used in an output filename: $version"
    }

    $zoteroApplication = $manifest.applications.zotero
    $addonID = [string]$zoteroApplication.id
    $strictMinVersion = [string]$zoteroApplication.strict_min_version
    $strictMaxVersion = [string]$zoteroApplication.strict_max_version
    $updateURL = [string]$zoteroApplication.update_url
    $updateURLSuffix = '/releases/download/release/updates.json'
    if ([string]::IsNullOrWhiteSpace($addonID) -or
        [string]::IsNullOrWhiteSpace($strictMinVersion) -or
        [string]::IsNullOrWhiteSpace($strictMaxVersion)) {
        throw 'The Zotero application entry must include id and version bounds.'
    }
    if (-not $updateURL.StartsWith('https://', [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $updateURL.EndsWith($updateURLSuffix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The Zotero update_url must end with $updateURLSuffix and use HTTPS."
    }
    $releaseRoot = $updateURL.Substring(0, $updateURL.Length - $updateURLSuffix.Length)
    $updateLink = "$releaseRoot/releases/download/v$version/zotero-translate-files-$version.xpi"

    $null = New-Item -Path $buildPath -ItemType Directory -Force
    $outputPath = Join-Path $buildPath "zotero-translate-files-$version.xpi"
    $updatesOutputPath = Join-Path $buildPath 'updates.json'

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    if (Test-Path -LiteralPath $outputPath) {
        Remove-Item -LiteralPath $outputPath -Force
    }

    $archive = [System.IO.Compression.ZipFile]::Open(
        $outputPath,
        [System.IO.Compression.ZipArchiveMode]::Create
    )

    $files = @(Get-ChildItem -LiteralPath $addonPath -Recurse -File -Force | Sort-Object FullName)
    if ($files.Count -eq 0) {
        throw "No plugin files found in: $addonPath"
    }

    foreach ($file in $files) {
        $relativePath = $file.FullName.Substring($addonPath.Length)
        $relativePath = $relativePath.TrimStart([char]'\', [char]'/')
        $entryName = $relativePath.Replace('\', '/')
        if ([string]::IsNullOrWhiteSpace($entryName)) {
            throw "Could not calculate ZIP entry name for: $($file.FullName)"
        }

        $entry = $archive.CreateEntry(
            $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        )
        $inputStream = [System.IO.File]::OpenRead($file.FullName)
        $outputStream = $entry.Open()
        try {
            $inputStream.CopyTo($outputStream)
        }
        finally {
            $outputStream.Dispose()
            $inputStream.Dispose()
        }
    }

    $archive.Dispose()
    $archive = $null

    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        throw "XPI was not created: $outputPath"
    }

    $updatesManifest = [ordered]@{ addons = [ordered]@{} }
    $updatesManifest.addons[$addonID] = [ordered]@{
        updates = @(
            [ordered]@{
                version = $version
                update_link = $updateLink
                applications = [ordered]@{
                    zotero = [ordered]@{
                        strict_min_version = $strictMinVersion
                        strict_max_version = $strictMaxVersion
                    }
                }
            }
        )
    }
    $updatesJson = ConvertTo-Json -InputObject $updatesManifest -Depth 8
    [System.IO.File]::WriteAllText(
        $updatesOutputPath,
        $updatesJson + [Environment]::NewLine,
        (New-Object System.Text.UTF8Encoding($false))
    )

    Write-Output "Created $outputPath"
    Write-Output "Created $updatesOutputPath"
}
catch {
    if ($archive) {
        $archive.Dispose()
        $archive = $null
    }
    if ($outputPath -and (Test-Path -LiteralPath $outputPath)) {
        Remove-Item -LiteralPath $outputPath -Force -ErrorAction SilentlyContinue
    }
    if ($updatesOutputPath -and (Test-Path -LiteralPath $updatesOutputPath)) {
        Remove-Item -LiteralPath $updatesOutputPath -Force -ErrorAction SilentlyContinue
    }
    Write-Error "XPI build failed: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($archive) {
        $archive.Dispose()
    }
}
