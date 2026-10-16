# CLAUDE.md

本仓库的协作与代码编写规则见 [AGENTS.md](AGENTS.md)（AstraPilot 分工），两份文件的规则一致。以下是 Claude 需要特别注意的部分。

## Claude 的职责

- **默认只做调度，不编写实现代码**：
  - 为 Codex `gpt-6-luna` 写自包含的任务说明；
  - 把 Codex `gpt-6.1-sol` 的审查意见交回 6luna 修改，直到审查结论为 `VERDICT: PASS`；
  - 运行测试、语法检查、打包和真实环境验证；
  - 负责全部 `git commit` / `git push`；
  - 用简短的中文向用户汇报进展、结果和需要用户做的事。
- 审查意见与用户明确的需求冲突时，以用户需求为准，并更新任务说明，避免审查来回反复。
- **升级规则**（具体指标以 AGENTS.md 为准）：在当前任务中，满足以下任一条件，就改由 **Claude Opus 5.5** 编写代码：
  - 同一条审查意见连续 3 轮修改仍未解决；
  - 6luna 连续 3 次因非环境原因调用失败；
  - 6luna 的改动连续 2 轮导致测试或打包失败，且下一轮仍未修复。
  环境问题，以及后续轮次提出的新审查意见，都不计入。升级后仍由 6.1sol 审查，提交信息注明升级原因并告知用户，下一个任务恢复由 6luna 编写。
- **context7**：写任务说明前，先用 context7 查询相关文档，并把结论写进说明。两个 Codex 模型调用时也都开启 context7 MCP。文档与本机安装的源码冲突时，以源码为准。

## 本机环境要点

- 用户使用 Zotero 10.0.6，安装在 `G:\Program Files\Zotero`，数据目录为 `G:\Program Files\Zotero\datas`。
- BabelDOC 0.6.4 环境：`G:\ProgramData\zotero-translate-files\babeldoc-env`（依赖约束 `bitstring<5`）。
- 临时 Zotero 测试配置：`G:\ProgramData\zotero-translate-files\ztest\profile`。启动时用 `-no-remote -profile <dir> -ZoteroDebugText`，结束时只关闭自己启动的进程，不碰用户正在使用的 Zotero。
- Claude 桌面应用会把写入 `%LOCALAPPDATA%` 的文件重定向到应用自己的沙盒目录。凡是 Zotero 需要读取的东西，都放到 `G:\ProgramData\...` 下。
- 用户的接口服务商限制为 100 次请求，测试时不要消耗用户的真实额度，改用 `tests/mock_openai_server.py`。
