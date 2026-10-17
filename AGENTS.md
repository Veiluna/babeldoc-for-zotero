# AGENTS.md — 协作与代码编写规则（AstraPilot 分工）

本仓库的所有代码工作按以下分工进行。Codex、Claude 及其他 agent 都必须遵守。

## 角色

| 角色 | 模型 | 职责 | 权限 |
| --- | --- | --- | --- |
| 编写 | Codex `gpt-6-luna`（6luna），思考强度 MAX | 编写全部实现代码：插件代码、Python 包装脚本、PowerShell 脚本、测试、README | workspace-write；**不得** `git commit` / `git push` |
| 审查 | Codex `gpt-6.1-sol`（6.1sol），思考强度 MAX | 只做代码审查，列出必须修改项，最后一行给出 `VERDICT: PASS` 或 `VERDICT: CHANGES` | read-only，不得修改文件 |
| 调度 | Claude | 写任务说明、传递审查意见、运行检查、git 提交与推送、向用户汇报；不编写实现代码 | 全部 |

## 升级规则：6luna 无法胜任时改由 Claude Opus 5.5 编写

在**当前任务**中，满足以下**任一**指标即升级：

1. **同一条** 6.1sol 审查意见，经 6luna **连续 3 轮**修改后，仍被 6.1sol 判定为未解决；
2. 6luna **连续 3 次**调用因非环境原因失败：崩溃、拒绝执行、没有任何文件改动、没有给出最终总结；
3. 6luna 的改动**连续 2 轮**导致已有测试或打包失败，且下一轮仍未修复。

**不计入**：
- 环境原因的失败：内存不足、网络错误、沙箱 EPERM、接口额度用尽。这类情况先修复环境再重试。
- 后续轮次提出的**新的、不同的**审查意见：这属于正常的审查深度。

**升级后**：
- 该任务由 **Claude Opus 5.5** 编写代码；
- 仍由 6.1sol 审查（`PASS` 才算完成），仍由 Claude 验证和提交；
- 提交信息注明 "Implemented by Claude Opus 5.5 (escalated: <触发的指标>)"，并告知用户；
- 下一个任务恢复由 6luna 编写。

## Codex 额度规则：5 小时额度用尽时改由 Claude Opus 5.5 编写

- 每次调用 6luna 之前，Claude 先从 Codex 会话日志查询 5 小时额度：取 `%USERPROFILE%\.codex\sessions\**\rollout-*.jsonl` 中时间戳最新的 `token_count` 事件，读 `rate_limits.primary`（`window_minutes` = 300）的 `used_percent` 和 `resets_at`。
- 剩余额度为 0%（`used_percent` ≥ 100）且 `resets_at` 尚未到达时，直接改由 **Claude Opus 5.5** 编写代码，不再调用 6luna。到达 `resets_at` 后（或日志显示额度已恢复），恢复由 6luna 编写。
- 这种切换不属于升级，也不计入升级指标。提交信息注明 "Implemented by Claude Opus 5.5 (Codex 5h quota exhausted)"。
- 审查仍由 6.1sol 负责，同样消耗 Codex 额度。额度用尽时，审查等额度恢复后再进行，`PASS` 之前不提交。
- context7 规则照常适用。

## 文档查询（context7，强制）

- **编写代码前必须查询**：任何模型（6luna，或升级后的 Claude Opus 5.5）在编写或修改调用库、接口的代码之前，都必须先用 context7 MCP（resolve-library-id → query-docs）查询**实际所用版本**的文档，并按文档实现，不得凭记忆使用参数名、函数签名或默认值。涉及的库和接口包括 Zotero 插件 API、Firefox/Gecko ESR 140、OpenAI Chat Completions 参数（如 `max_tokens` 与 `max_completion_tokens`、`temperature` 是否支持）、BabelDOC 0.6.4、PyMuPDF、Python 标准库等。
- **总结中必须列出**：编写者的最终总结必须包含 "Docs checked (context7)" 一节，逐项写明查询的库或接口、context7 库 ID 和确认的结论。缺少这一节视为任务未完成。
- **审查时必须核对**：6.1sol 审查时要自己用 context7 核对每一处库/接口调用和参数是否符合该版本文档。与文档不符，或缺少 "Docs checked (context7)" 一节，都列为必须修改项。
- **Claude**：写任务说明前，同样先用 context7 查询相关文档，并把结论写进说明。
- 起因：曾凭记忆使用 `max_tokens`，结果服务商的部分后端只接受 `max_completion_tokens`，返回 400。
- 文档与本机实际安装的源码冲突时，**以源码为准**：Zotero 10.0.6 在 `G:\Program Files\Zotero` 的 `omni.ja`，BabelDOC 0.6.4 在 `G:\ProgramData\zotero-translate-files\babeldoc-env`。

## 每个任务的流程

1. **任务说明**：Claude 写一份自包含的说明，包括目标、涉及文件、验收标准，以及已核实的源码事实（如 Zotero 10.0.6、BabelDOC 的内部实现）。
2. **实现**：6luna 按说明实现。
3. **审查**：把仓库全部改动（`git diff`）连同任务说明交给 6.1sol。
4. **循环**：结论为 `CHANGES` 时，把审查意见交回 6luna 修改，再交 6.1sol 复审，直到 `PASS`。审查意见与用户明确的需求冲突时，以用户需求为准，并同步更新任务说明。
5. **验证**：`PASS` 后，Claude 运行全部测试、语法检查和打包。必要时用真实 BabelDOC 加本机模拟接口实测，或在临时的 Zotero 10.0.6 配置中实际加载插件。
6. **提交**：提交到 `main` 并推送。提交信息注明 "Implemented by Codex gpt-6-luna, reviewed by gpt-6.1-sol (N rounds)"，然后向用户汇报。

## 编码约定

- 以 **Zotero 10.0.6** 为主要目标，兼容 Zotero 7+；只使用 10.0.6 中实际存在的 API。`manifest.json` 必须包含 `applications.zotero.id`、`update_url`（https）和 `strict_max_version`。
- 纯 JavaScript，无构建步骤，文件放在 `addon/` 下；界面文字使用 Fluent（`zh-CN` / `en-US`）。
- 插件 ID `zotero-translate-files@veiluna.github.io` 与偏好分支 `extensions.zotero-translate-files.` 不得更改。
- API key 只能通过环境变量传给子进程，不得出现在命令行参数或日志中；所有报错文本统一脱敏。
- 不提交密钥和构建产物（`build/`、`*.xpi`）。
- 不跳过、不禁用测试。新增的非平凡逻辑必须配回归测试：
  - Node：`node tests/<file>.test.js`（逐个运行）
  - Python：`python tests/test_runner_policy.py`、`python tests/test_min_requests.py`
- 打包：`powershell -ExecutionPolicy Bypass -File scripts/build-xpi.ps1`。

## 模型调用方式

```
codex exec -m gpt-6-luna  -c model_reasoning_effort="max" -c 'mcp_servers.context7.url="https://mcp.context7.com/mcp"' -s workspace-write ...   # 编写
codex exec -m gpt-6.1-sol -c model_reasoning_effort="max" -c 'mcp_servers.context7.url="https://mcp.context7.com/mcp"' -s read-only ...         # 审查
```

简写名 "6luna" / "6.1sol" 会被接口拒绝，必须使用完整模型名。本机内存紧张时，加 `--ignore-user-config -c windows.sandbox="unelevated"`，以免启动用户配置中的 MCP 服务。
