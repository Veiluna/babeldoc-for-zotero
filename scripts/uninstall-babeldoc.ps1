<#
.SYNOPSIS
删除 Zotero 中文 PDF 翻译插件使用的 BabelDOC Conda 环境。

.DESCRIPTION
删除 EnvPrefix 指定的目录。默认会先要求确认；只有明确指定 RemoveCache 时才会删除
用户目录下的 BabelDOC 资源缓存（%USERPROFILE%\.cache\babeldoc）。

参数：EnvPrefix 指定环境目录；Force 跳过删除确认；RemoveCache 同时删除 BabelDOC 资源缓存。

.PARAMETER EnvPrefix
要删除的 Conda 环境路径。默认值为 LOCALAPPDATA 下的 zotero-translate-files\babeldoc-env。

.PARAMETER Force
跳过环境目录删除确认。

.PARAMETER RemoveCache
同时删除 %USERPROFILE%\.cache\babeldoc 中的 BabelDOC 字体和布局模型缓存。

.EXAMPLE
.\uninstall-babeldoc.ps1

.EXAMPLE
.\uninstall-babeldoc.ps1 -Force

.EXAMPLE
.\uninstall-babeldoc.ps1 -Force -RemoveCache
#>
#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$EnvPrefix,
    [switch]$Force,
    [switch]$RemoveCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-IsFileSystemRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $pathRoot = [System.IO.Path]::GetPathRoot($Path)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) {
        return $false
    }

    $trimCharacters = [char[]]@([char]'\', [char]'/')
    return $Path.TrimEnd($trimCharacters) -eq $pathRoot.TrimEnd($trimCharacters)
}

try {
    if ([string]::IsNullOrWhiteSpace($EnvPrefix)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw '无法确定 LOCALAPPDATA。请使用 -EnvPrefix 指定环境路径。'
        }
        $EnvPrefix = Join-Path $env:LOCALAPPDATA 'zotero-translate-files\babeldoc-env'
    }
    $EnvPrefix = [System.IO.Path]::GetFullPath($EnvPrefix)

    if (Test-IsFileSystemRoot -Path $EnvPrefix) {
        throw "拒绝删除磁盘根目录：$EnvPrefix"
    }

    $userProfile = $env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($userProfile)) {
        $userProfile = [Environment]::GetFolderPath('UserProfile')
    }
    $cachePath = $null
    if (-not [string]::IsNullOrWhiteSpace($userProfile)) {
        $cachePath = Join-Path $userProfile '.cache\babeldoc'
    }

    $environmentExists = Test-Path -LiteralPath $EnvPrefix
    if ($environmentExists -and -not $Force) {
        $answer = Read-Host "即将递归删除 Conda 环境目录：$EnvPrefix。输入 Y 确认"
        if ($answer -notmatch '^(?i:y|yes)$') {
            Write-Host '已取消；环境和 BabelDOC 资源缓存均未删除。'
            return
        }
    }

    if ($environmentExists) {
        Remove-Item -LiteralPath $EnvPrefix -Recurse -Force
        Write-Host "已删除 Conda 环境：$EnvPrefix" -ForegroundColor Green
    }
    else {
        Write-Host "环境目录不存在，无需删除：$EnvPrefix"
    }

    if ($RemoveCache) {
        if ($cachePath -and (Test-Path -LiteralPath $cachePath)) {
            if (Test-IsFileSystemRoot -Path ([System.IO.Path]::GetFullPath($cachePath))) {
                throw "拒绝删除无效的 BabelDOC 缓存路径：$cachePath"
            }
            Remove-Item -LiteralPath $cachePath -Recurse -Force
            Write-Host "已删除 BabelDOC 资源缓存：$cachePath" -ForegroundColor Green
        }
        else {
            Write-Host '未找到 BabelDOC 资源缓存，无需删除。'
        }
    }
    else {
        Write-Host 'BabelDOC 资源缓存保留在 %USERPROFILE%\.cache\babeldoc。'
        Write-Host '如需删除该缓存，请显式添加 -RemoveCache。'
    }
}
catch {
    Write-Error -Message "BabelDOC 卸载未完成：$($_.Exception.Message)" -ErrorAction Continue
    exit 1
}
