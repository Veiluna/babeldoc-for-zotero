const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const installerPath = path.resolve(__dirname, "../scripts/install-babeldoc.ps1");
const constraintsPath = path.resolve(__dirname, "../scripts/babeldoc-constraints.txt");
const installerSource = fs.readFileSync(installerPath, "utf8");

assert.ok(fs.existsSync(constraintsPath), "BabelDOC pip constraints file must exist");
const constraintsSource = fs.readFileSync(constraintsPath, "utf8");
assert.match(constraintsSource, /^#.*BabelDOC imports `?bitstring\.BitStream`?.*bitstring 5/m);
assert.match(constraintsSource, /^bitstring<5\s*$/m);
assert.match(installerSource, /Join-Path \$PSScriptRoot 'babeldoc-constraints\.txt'/);
assert.match(installerSource, /Test-Path -LiteralPath \$constraintsPath -PathType Leaf/);
assert.match(
  installerSource,
  /\$installArguments = @\('-m', 'pip', 'install', '--no-warn-script-location'\)/,
);
assert.match(
  installerSource,
  /\$installArguments \+= @\('--upgrade', '-c', \$constraintsPath, 'bitstring<5', \$babelDocRequirement\)/,
);
assert.match(
  installerSource,
  /\$installArguments \+= @\('-c', \$constraintsPath, 'bitstring<5', \$babelDocRequirement\)/,
);
assert.equal(
  (installerSource.match(/'--no-warn-script-location'/g) || []).length,
  2,
  "both pip install commands should suppress script-location warnings",
);

const importChecks = [
  ...installerSource.matchAll(
    /Invoke-NativeCommand -Executable \$pythonExe -Arguments @\('-c', 'import babeldoc\.main'\)/g,
  ),
];
assert.equal(importChecks.length, 2, "babeldoc.main should be checked before and after warmup");
const warmupBlockIndex = installerSource.indexOf("if (-not $SkipWarmup)");
assert.ok(warmupBlockIndex > importChecks[0].index, "the first import check must run before warmup");
assert.ok(warmupBlockIndex < importChecks[1].index, "the final import check must remain after warmup");

const powershellProbe = String.raw`
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:ZTF_TEST_INSTALLER_PATH, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Installer parse failed.' }

$functionNames = @('Get-CondaCandidate', 'Resolve-CondaCommand')
foreach ($functionName in $functionNames) {
    $functionAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
    if (-not $functionAst) { throw "Function not found: $functionName" }
    Invoke-Expression $functionAst.Extent.Text
}

function Assert-ResolvedPath {
    param([string]$Actual, [string]$Expected, [string]$Context)
    if ($Actual -ne $Expected) { throw ($Context + ": expected '" + $Expected + "', got '" + $Actual + "'") }
}

$testRoot = Join-Path $env:TEMP ('ZTF_REVIEW&ver-' + [guid]::NewGuid().ToString('N'))
$savedPath = $env:PATH
$savedUserProfile = $env:USERPROFILE
$savedCondaExe = $env:CONDA_EXE
try {
    $scriptsDir = Join-Path $testRoot 'Scripts'
    $condabinDir = Join-Path $testRoot 'condabin'
    $null = New-Item -Path $scriptsDir -ItemType Directory -Force
    $null = New-Item -Path $condabinDir -ItemType Directory -Force
    $expected = Join-Path $scriptsDir 'conda.exe'
    $null = New-Item -Path $expected -ItemType File -Force
    $expected = [System.IO.Path]::GetFullPath($expected)
    $batchPath = Join-Path $condabinDir 'conda.bat'
    Set-Content -LiteralPath $batchPath -Value '@echo off'

    $CondaPath = $testRoot
    Assert-ResolvedPath (Resolve-CondaCommand) $expected 'explicit root'

    $CondaPath = $expected
    Assert-ResolvedPath (Resolve-CondaCommand) $expected 'explicit conda.exe'

    $CondaPath = $batchPath
    Assert-ResolvedPath (Resolve-CondaCommand) $expected 'explicit conda.bat'

    $CondaPath = $null
    $env:PATH = $condabinDir
    Assert-ResolvedPath (Resolve-CondaCommand) $expected 'PATH conda.bat'

    $env:PATH = ''
    $env:CONDA_EXE = $batchPath
    Assert-ResolvedPath (Resolve-CondaCommand) $expected 'CONDA_EXE conda.bat'

    $userProfile = Join-Path $testRoot 'user'
    $discoveredRoot = Join-Path $userProfile 'miniconda3'
    $discoveredScripts = Join-Path $discoveredRoot 'Scripts'
    $null = New-Item -Path $discoveredScripts -ItemType Directory -Force
    $discoveredExe = Join-Path $discoveredScripts 'conda.exe'
    $null = New-Item -Path $discoveredExe -ItemType File -Force
    $env:CONDA_EXE = ''
    $env:USERPROFILE = $userProfile
    Assert-ResolvedPath (Resolve-CondaCommand) ([System.IO.Path]::GetFullPath($discoveredExe)) 'common root discovery'
}
finally {
    $env:PATH = $savedPath
    $env:USERPROFILE = $savedUserProfile
    $env:CONDA_EXE = $savedCondaExe
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
`;

const result = spawnSync(
  "powershell.exe",
  ["-NoProfile", "-EncodedCommand", Buffer.from(powershellProbe, "utf16le").toString("base64")],
  {
    encoding: "utf8",
    env: { ...process.env, ZTF_TEST_INSTALLER_PATH: installerPath },
  },
);

assert.ifError(result.error);
assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
process.stdout.write("BabelDOC Conda path regression cases passed\n");
