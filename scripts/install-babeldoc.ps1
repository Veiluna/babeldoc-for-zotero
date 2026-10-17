<#
.SYNOPSIS
为 BabelDOC for Zotero 创建隔离的 Miniconda 环境并安装 BabelDOC。

.DESCRIPTION
使用指定的 Conda 安装在 EnvPrefix 下创建 Python 环境，安装 BabelDOC 0.6.4，并默认预下载其字体和布局模型资源。
脚本不会激活环境，也不会修改 PATH 或 shell 配置。可通过 SkipWarmup 跳过资源预下载，或通过 OfflineAssets
从离线资源路径恢复资源。

参数：CondaPath 指定 Conda 路径；EnvPrefix 指定环境目录；PythonVersion 默认为 3.12；BabelDocVersion 默认为 0.6.4，
也可用于指定其他版本；SkipWarmup 跳过资源准备；IndexUrl 指定 pip 镜像；OfflineAssets 指定离线资源文件或目录。

.PARAMETER CondaPath
可选的 conda.exe 路径或 Miniconda 安装根目录。省略时依次检查 PATH、CONDA_EXE 和常见安装目录。

.PARAMETER EnvPrefix
Conda 环境的完整路径。默认值为 LOCALAPPDATA 下的 zotero-translate-files\babeldoc-env。

.PARAMETER PythonVersion
要安装的 Python 版本，默认 3.12。

.PARAMETER BabelDocVersion
要安装的 BabelDOC 版本，默认固定为 0.6.4。

.PARAMETER SkipWarmup
跳过 BabelDOC 字体和布局模型资源的预下载。

.PARAMETER IndexUrl
可选的 pip 包索引地址，例如中国大陆用户可使用的 PyPI 镜像。

.PARAMETER OfflineAssets
离线 BabelDOC 资源文件或目录路径。指定后通过 --restore-offline-assets 恢复资源，而不执行在线 warmup。

.EXAMPLE
.\install-babeldoc.ps1 -CondaPath 'C:\Users\Alice\miniconda3'

.EXAMPLE
.\install-babeldoc.ps1 -BabelDocVersion '0.6.4' -IndexUrl 'https://pypi.tuna.tsinghua.edu.cn/simple'

.EXAMPLE
.\install-babeldoc.ps1 -OfflineAssets 'D:\babeldoc-assets.zip'
#>
#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$CondaPath,
    [string]$EnvPrefix,
    [string]$PythonVersion = '3.12',
    [string]$BabelDocVersion = '0.6.4',
    [switch]$SkipWarmup,
    [string]$IndexUrl,
    [string]$OfflineAssets
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-CondaCandidate {
    param(
        [AllowNull()]
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    $expandedPath = [Environment]::ExpandEnvironmentVariables($Path.Trim('"'))
    if (Test-Path -LiteralPath $expandedPath -PathType Container) {
        $scriptsConda = Join-Path $expandedPath 'Scripts\conda.exe'
        if (Test-Path -LiteralPath $scriptsConda -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($scriptsConda)
        }
        return $null
    }

    if (Test-Path -LiteralPath $expandedPath -PathType Leaf) {
        $resolvedPath = [System.IO.Path]::GetFullPath($expandedPath)
        $fileName = [System.IO.Path]::GetFileName($resolvedPath)

        if ($fileName -ieq 'conda.bat') {
            $parentPath = [System.IO.Path]::GetDirectoryName($resolvedPath)
            $parentName = [System.IO.Path]::GetFileName($parentPath)
            $scriptsConda = $null

            if ($parentName -ieq 'condabin') {
                $condaRoot = [System.IO.Directory]::GetParent($parentPath).FullName
                $scriptsConda = Join-Path $condaRoot 'Scripts\conda.exe'
            }
            elseif ($parentName -ieq 'Scripts') {
                $scriptsConda = Join-Path $parentPath 'conda.exe'
            }

            if ($scriptsConda -and (Test-Path -LiteralPath $scriptsConda -PathType Leaf)) {
                return [System.IO.Path]::GetFullPath($scriptsConda)
            }

            return $null
        }

        if ($fileName -ieq 'conda.exe') {
            return $resolvedPath
        }
    }

    return $null
}

function Resolve-CondaCommand {
    if (-not [string]::IsNullOrWhiteSpace($CondaPath)) {
        $candidate = Get-CondaCandidate -Path $CondaPath
        if ($candidate) {
            return $candidate
        }
        Write-Warning "CondaPath 不存在或目录中没有 conda.exe：$CondaPath；继续查找其他位置。"
    }

    $pathCommand = Get-Command -Name 'conda' -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandType -in @('Application', 'ExternalScript') } |
        Select-Object -First 1
    if ($pathCommand) {
        $candidate = Get-CondaCandidate -Path $pathCommand.Source
        if ($candidate) {
            return $candidate
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($env:CONDA_EXE)) {
        $candidate = Get-CondaCandidate -Path $env:CONDA_EXE
        if ($candidate) {
            return $candidate
        }
    }

    $roots = @()
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $roots += Join-Path $env:USERPROFILE 'miniconda3'
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $roots += Join-Path $env:LOCALAPPDATA 'miniconda3'
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramData)) {
        $roots += Join-Path $env:ProgramData 'miniconda3'
    }
    $roots += 'C:\ProgramData\miniconda3'

    foreach ($drive in [System.IO.DriveInfo]::GetDrives()) {
        if ($drive.DriveType -eq [System.IO.DriveType]::Fixed -and $drive.IsReady) {
            $roots += Join-Path $drive.RootDirectory.FullName 'ProgramData\miniconda3'
            $roots += Join-Path $drive.RootDirectory.FullName 'miniconda3'
        }
    }

    foreach ($root in ($roots | Select-Object -Unique)) {
        $candidate = Get-CondaCandidate -Path $root
        if ($candidate) {
            return $candidate
        }
    }

    return $null
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage,

        [switch]$AllowFailure
    )

    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $nativeExitCode = -1
    $nativeOutput = @()
    try {
        $nativeOutput = @(& $Executable @Arguments 2>&1)
        $nativeExitCode = $LASTEXITCODE
    }
    catch {
        $nativeOutput = @($_.Exception.Message)
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }

    $outputLines = @($nativeOutput | ForEach-Object { [string]$_ })
    foreach ($line in $outputLines) {
        Write-Host $line
    }

    $result = [PSCustomObject]@{
        ExitCode = $nativeExitCode
        Output   = [string]::Join([Environment]::NewLine, [string[]]$outputLines)
    }

    if ($nativeExitCode -ne 0 -and -not $AllowFailure) {
        throw "$FailureMessage（退出码：$nativeExitCode）"
    }

    return $result
}

try {
    $constraintsPath = Join-Path $PSScriptRoot 'babeldoc-constraints.txt'
    if (-not (Test-Path -LiteralPath $constraintsPath -PathType Leaf)) {
        throw "找不到 BabelDOC pip 约束文件：$constraintsPath"
    }

    if ([string]::IsNullOrWhiteSpace($EnvPrefix)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw '无法确定 LOCALAPPDATA。请使用 -EnvPrefix 指定环境路径。'
        }
        $EnvPrefix = Join-Path $env:LOCALAPPDATA 'zotero-translate-files\babeldoc-env'
    }
    $EnvPrefix = [System.IO.Path]::GetFullPath($EnvPrefix)

    if ([string]::IsNullOrWhiteSpace($PythonVersion)) {
        throw 'PythonVersion 不能为空。'
    }
    if ($OfflineAssets -and -not (Test-Path -LiteralPath $OfflineAssets)) {
        throw "找不到离线资源路径：$OfflineAssets"
    }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $condaExe = Resolve-CondaCommand
    if (-not $condaExe) {
        Write-Host '未找到 Conda。请先从 Miniconda 官网下载并安装 Windows 版 Miniconda，然后重新运行本脚本。' -ForegroundColor Red
        Write-Host "示例：.\scripts\install-babeldoc.ps1 -CondaPath 'C:\Users\<用户名>\miniconda3'"
        Write-Host '也可以将 -CondaPath 设置为 conda.exe 的完整路径。'
        exit 1
    }

    $pythonExe = Join-Path $EnvPrefix 'python.exe'
    $babelDocExe = Join-Path $EnvPrefix 'Scripts\babeldoc.exe'

    if (Test-Path -LiteralPath $EnvPrefix -PathType Leaf) {
        throw "EnvPrefix 已存在但不是目录：$EnvPrefix"
    }

    if (Test-Path -LiteralPath $pythonExe -PathType Leaf) {
        Write-Host "复用已有 Python 环境：$EnvPrefix"
    }
    else {
        $parentPath = [System.IO.Path]::GetDirectoryName($EnvPrefix)
        if ($parentPath -and -not (Test-Path -LiteralPath $parentPath -PathType Container)) {
            $null = New-Item -Path $parentPath -ItemType Directory -Force
        }

        Write-Host "正在创建 Python $PythonVersion 环境：$EnvPrefix"
        $createArguments = @('create', '-y', '-p', $EnvPrefix, "python=$PythonVersion")
        $createResult = Invoke-NativeCommand -Executable $condaExe -Arguments $createArguments -FailureMessage 'Conda 创建环境失败' -AllowFailure

        if ($createResult.ExitCode -ne 0) {
            $termsOfServiceFailure = $createResult.Output -match '(?is)terms of service.*(not been accepted|must be accepted|accept)'
            if (-not $termsOfServiceFailure) {
                throw "Conda 创建环境失败（退出码：$($createResult.ExitCode)）。请查看上方 Conda 输出。"
            }

            Write-Host '检测到 defaults channels 的服务条款尚未接受，改用 conda-forge 创建环境。'
            $fallbackArguments = @(
                'create', '-y', '-p', $EnvPrefix, "python=$PythonVersion",
                '--override-channels', '-c', 'conda-forge'
            )
            $null = Invoke-NativeCommand -Executable $condaExe -Arguments $fallbackArguments -FailureMessage '使用 conda-forge 创建环境失败'
        }
    }

    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        throw "环境中的 python.exe 不存在：$pythonExe"
    }

    $pipIndexArguments = @()
    if (-not [string]::IsNullOrWhiteSpace($IndexUrl)) {
        $pipIndexArguments = @('-i', $IndexUrl)
    }

    Write-Host '正在升级环境内的 pip。'
    $upgradePipArguments = @('-m', 'pip', 'install', '--upgrade', '--no-warn-script-location', 'pip') + $pipIndexArguments
    $null = Invoke-NativeCommand -Executable $pythonExe -Arguments $upgradePipArguments -FailureMessage '升级 pip 失败'

    $installArguments = @('-m', 'pip', 'install', '--no-warn-script-location')
    if ([string]::IsNullOrWhiteSpace($BabelDocVersion)) {
        $babelDocRequirement = 'BabelDOC'
        Write-Host '正在升级到与当前 Python 兼容的最新版 BabelDOC。'
        $installArguments += @('--upgrade', '-c', $constraintsPath, 'bitstring<5', $babelDocRequirement)
    }
    else {
        $babelDocRequirement = "BabelDOC==$BabelDocVersion"
        Write-Host "正在安装固定版本 $babelDocRequirement。"
        $installArguments += @('-c', $constraintsPath, 'bitstring<5', $babelDocRequirement)
    }
    $installArguments += $pipIndexArguments
    $null = Invoke-NativeCommand -Executable $pythonExe -Arguments $installArguments -FailureMessage '安装 BabelDOC 失败'

    if (-not (Test-Path -LiteralPath $babelDocExe -PathType Leaf)) {
        throw "安装后找不到 BabelDOC 命令：$babelDocExe"
    }

    Write-Host '正在预下载前验证 Python 模块导入。'
    $null = Invoke-NativeCommand -Executable $pythonExe -Arguments @('-c', 'import babeldoc.main') -FailureMessage '预下载前导入 babeldoc.main 失败'

    if (-not $SkipWarmup) {
        if ($OfflineAssets) {
            Write-Host "正在从离线资源恢复 BabelDOC 资源：$OfflineAssets"
            $warmupArguments = @('--restore-offline-assets', $OfflineAssets)
        }
        else {
            Write-Host '正在预下载 BabelDOC 字体和布局模型资源。'
            $warmupArguments = @('--warmup')
        }
        $null = Invoke-NativeCommand -Executable $babelDocExe -Arguments $warmupArguments -FailureMessage 'BabelDOC 资源准备失败'
    }
    elseif ($OfflineAssets) {
        Write-Host '已指定 OfflineAssets，但 SkipWarmup 会跳过资源恢复。'
    }
    else {
        Write-Host '已按要求跳过 BabelDOC 资源预下载。'
    }

    Write-Host '正在验证 BabelDOC 版本。'
    $null = Invoke-NativeCommand -Executable $babelDocExe -Arguments @('--version') -FailureMessage 'BabelDOC 版本验证失败'

    Write-Host '正在验证 Python 模块导入。'
    $null = Invoke-NativeCommand -Executable $pythonExe -Arguments @('-c', 'import babeldoc.main') -FailureMessage '导入 babeldoc.main 失败'

    Write-Host ''
    Write-Host 'BabelDOC 安装和验证已完成。' -ForegroundColor Green
    Write-Host '请将以下路径粘贴到插件的“BabelDOC 路径”设置：'
    Write-Host $babelDocExe -ForegroundColor Cyan
}
catch {
    Write-Error -Message "BabelDOC 安装未完成：$($_.Exception.Message)" -ErrorAction Continue
    exit 1
}
