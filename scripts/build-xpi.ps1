#requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$addonPath = Join-Path $repositoryRoot 'addon'
$manifestPath = Join-Path $addonPath 'manifest.json'
$buildPath = Join-Path $repositoryRoot 'build'
$outputPath = $null
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

    $null = New-Item -Path $buildPath -ItemType Directory -Force
    $outputPath = Join-Path $buildPath "zotero-translate-files-$version.xpi"

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

    Write-Output "Created $outputPath"
}
catch {
    if ($archive) {
        $archive.Dispose()
        $archive = $null
    }
    if ($outputPath -and (Test-Path -LiteralPath $outputPath)) {
        Remove-Item -LiteralPath $outputPath -Force -ErrorAction SilentlyContinue
    }
    Write-Error "XPI build failed: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($archive) {
        $archive.Dispose()
    }
}
