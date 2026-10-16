# Translate Files for Zotero

当前版本：**0.1.2**。主要支持 **Zotero 10.0.6（64 位）**，兼容 Zotero 7–10。这是一个 Windows 插件。它调用本机安装的 BabelDOC 和用户配置的 OpenAI 兼容接口，生成保持版式的简体中文 PDF，并把译本作为新的文件附件导入 Zotero。

## 功能简介

- 只在用户从条目或 PDF 附件的右键菜单选择“翻译成中文 PDF”后运行，不会自动翻译。
- 按 PDF 的文字层进行翻译，包括可编辑的矢量图表文字和题注；v1 不对图片中的文字做 OCR。
- 只输出单语中文 PDF，不生成双语 PDF；原 PDF 保持不变。
- 结果作为新的已存储文件附件加入 Zotero。同一父条目下的译本标题为“原标题（中文译本）”。
- 插件没有设置页数上限。多个任务按 FIFO 顺序排队，同一时间处理一个；失败后提示错误并继续下一个任务，不自动重试。
- 如果本次进入段落翻译的内容没有任何段落成功，任务会按失败处理。部分段落失败时，任务仍可完成，进度窗口会显示失败段数，失败段落保留原文。

## 系统要求

- Windows 和 Windows PowerShell 5.1。
- Zotero 7–10。当前主要支持 Zotero 10.0.6（64 位）；插件清单声明的版本范围为 6.999 至 10.*。
- Miniconda。安装脚本会创建独立的 Python 3.12 环境，不会激活环境，也不会修改 PATH。
- 一个可用的 OpenAI 兼容 Chat Completions 接口、API key 和模型名。

## 安装

### 1. 安装 Miniconda

如果尚未安装，请先安装 Windows 版 Miniconda。安装时无需将 Conda 加入 PATH；安装脚本会查找常见位置，也可以通过 `-CondaPath` 指定 Miniconda 安装目录或 `conda.exe`。

### 2. 安装 BabelDOC

在仓库根目录打开 Windows PowerShell，运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-babeldoc.ps1
```

`-ExecutionPolicy Bypass` 只对这次 PowerShell 命令生效。常用参数如下：

| 参数 | 作用 |
| --- | --- |
| `-CondaPath` | Conda 安装目录或 `conda.exe` 路径；省略时自动查找。 |
| `-EnvPrefix` | 环境目录；默认是 `%LOCALAPPDATA%\zotero-translate-files\babeldoc-env`。 |
| `-PythonVersion` | Python 版本，默认 `3.12`。 |
| `-BabelDocVersion` | 可选的 BabelDOC 版本，例如 `0.6.4`；省略时安装与 Python 版本兼容的最新版。 |
| `-SkipWarmup` | 跳过 BabelDOC 字体和布局模型资源预下载。 |
| `-IndexUrl` | 指定 pip 包索引。国内用户可按需使用镜像，例如清华 PyPI 镜像；该参数只传给 pip。 |
| `-OfflineAssets` | 从已有的 BabelDOC 资源文件或目录恢复字体和模型资源。它不离线安装 Conda/Python 依赖。 |

例如，指定 Miniconda 路径和 pip 镜像：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-babeldoc.ps1 `
  -CondaPath 'G:\ProgramData\miniconda3' `
  -IndexUrl 'https://pypi.tuna.tsinghua.edu.cn/simple'
```

使用已有离线资源时，可添加 `-OfflineAssets 'D:\babeldoc-assets.zip'`。安装脚本默认会预下载资源；若添加 `-SkipWarmup`，则会跳过在线预下载，也会跳过指定的离线资源恢复。

安装脚本使用 [scripts/babeldoc-constraints.txt](scripts/babeldoc-constraints.txt) 固定依赖约束 `bitstring<5`，以兼容 BabelDOC 对 `bitstring.BitStream` 的使用。安装完成后，脚本会输出 BabelDOC 可执行文件路径。

### 3. 打包插件

在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-xpi.ps1
```

脚本会把插件打包到 `build\zotero-translate-files-<版本>.xpi`。也可以直接使用已构建的 `.xpi` 文件。

打包时也会根据 `addon/manifest.json` 生成 `build/updates.json`。发布自动更新时，将该文件作为 `release` 版本的 `updates.json` 资源发布，并将对应版本的 XPI 发布到 `v<版本>` 标签下。清单中的自动更新地址为 `https://github.com/Veiluna/zotero-translate-files/releases/download/release/updates.json`。由于此 GitHub 仓库目前是私有仓库，Zotero 在仓库或发布资源公开前无法读取更新清单，自动更新检查会静默失败；这不影响从本地 XPI 安装和使用。

### 4. 在 Zotero 安装插件

打开 Zotero 的 **工具 → 插件**，选择 **从文件安装**，然后选中 `.xpi` 文件。

### 5. 填写插件设置

打开 Zotero **设置 → Translate Files for Zotero**，填写：

1. **接口地址**：例如 `https://api.example.com/v1`。插件会把根地址归一化为 Chat Completions endpoint，例如 `https://api.example.com/v1/chat/completions`。主机根地址会补成 `/v1/chat/completions`；版本路径（如 `/v1`）和 `/v1beta/openai` 会补上 `/chat/completions`；已填写完整的 `/chat/completions` endpoint 保持不变。
2. **API key**：接口服务提供的密钥。
3. **模型名**：接口服务支持的模型名称。
4. **BabelDOC 路径**：粘贴安装脚本输出的路径。可填写环境中的 `Scripts\babeldoc.exe`，也可填写环境根目录下的 `python.exe`。

这些设置分别使用 Zotero 偏好键 `extensions.zotero-translate-files.baseURL`、`extensions.zotero-translate-files.apiKey`、`extensions.zotero-translate-files.model` 和 `extensions.zotero-translate-files.babeldocPath`。

点击 **测试连接** 后，插件会向接口发送一次简短的 `ping` 请求，并检查 BabelDOC 可执行文件及对应的 Python 路径是否存在。它不会运行一次完整的 PDF 翻译。

## 使用

1. 在 Zotero 中右键点击普通条目或 PDF 附件，选择 **翻译成中文 PDF**。
2. 插件会显示入队提示。队列按顺序逐个处理，当前任务会打开进度窗口，显示阶段、进度和排队数量。
3. 成功后，译本 PDF 会作为新附件导入对应的父条目，标题为“原标题（中文译本）”；原始 PDF 文件不变。
4. 如果所有进入翻译的段落都失败，任务会显示失败提示且不会导入译本。部分段落失败时，进度窗口的完成状态会提示失败段数。

## API key 的处理

插件通过环境变量 `ZTF_OPENAI_API_KEY` 把 API key 交给本机 Python runner。runner 读取后会从环境变量中移除该值，并在 Python 进程内部将它加入 BabelDOC 的参数；密钥不会出现在操作系统可见的进程命令行参数中。

API key 保存在 Zotero 的 `prefs.js` 偏好设置文件中，**以明文存储**。设置页使用密码输入框只会遮挡屏幕显示，不会加密该文件。请保护好自己的 Windows 用户账户和 Zotero 数据目录。

## 常见问题

### 接口地址应该怎么填？

通常填写接口的 API 根地址，例如 `https://api.example.com/v1`。测试连接会按归一化后的完整地址发送请求。完整的 `/chat/completions` endpoint 也可直接填写；带 `?query` 的地址会原样用于测试连接。

翻译任务由 BabelDOC 的 OpenAI 客户端构造请求，它会在 base URL 后追加 `/chat/completions`。因此，未被识别为主机根地址或版本路径的自定义地址会按原样当作 base URL 使用；如果它其实是另一个完整 endpoint，翻译可能无法连接。BabelDOC 不支持在 base URL 中保留查询参数，带 `?query` 的地址可以测试连接，但翻译任务会在启动前报错；请将必要参数改由接口服务配置提供，并移除 URL 查询部分。

### HTTP 401、404 或 429 怎么办？

- **401/403**：检查 API key 是否正确，以及该 key 是否有调用权限。
- **404**：检查接口地址路径和模型名是否符合服务商要求。
- **429**：检查调用频率限制、账户额度和服务端配额。

插件会在失败提示中给出脱敏后的技术详情；不会在失败时自动重试。

### 为什么第一次翻译较慢？

首次运行可能需要下载 BabelDOC 的字体、布局模型等资源，PDF 页面分析和翻译也需要时间。资源缓存通常位于 `%USERPROFILE%\.cache\babeldoc`。

### 如何卸载？

在 Zotero **工具 → 插件** 中移除本插件。BabelDOC 的 Conda 环境可以单独卸载：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-babeldoc.ps1
```

脚本默认删除 `%LOCALAPPDATA%\zotero-translate-files\babeldoc-env` 前要求确认，并保留 BabelDOC 资源缓存。添加 `-Force` 可跳过环境删除确认；只有明确添加 `-RemoveCache` 才会删除 `%USERPROFILE%\.cache\babeldoc`。例如，确认后同时删除环境和缓存：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-babeldoc.ps1 -Force -RemoveCache
```

## 测试

仓库中的 JavaScript 回归测试需逐个运行，例如：

```powershell
node tests/config-redaction.test.js
node tests/manifest.test.js
node tests/translation-queue.test.js
node tests/translation-runner.test.js
```

其他 `tests/*.test.js` 文件也可用 `node tests/<file>.test.js` 逐个运行。Python runner 策略测试运行：

```powershell
python tests/test_runner_policy.py
```

## 许可证与致谢

本项目采用 GNU Affero General Public License v3.0 only（AGPL-3.0-only）；许可证全文见 [LICENSE](LICENSE)（亦可参阅 [GNU 官方文本](https://www.gnu.org/licenses/agpl-3.0.txt)）。感谢开源项目 BabelDOC 提供 PDF 版面解析与翻译引擎。
