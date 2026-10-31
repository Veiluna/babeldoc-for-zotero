# T5 分栏合成重写 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: CHANGES

## 阻塞项

1. tests/acceptance_split_compose.py:22 仍导入已删除的 ztf_split_plan，实跑即 ModuleNotFoundError；还依赖旧 PagePlan 接口与已移除的 fixture 方法，需一并迁移（§6、§12）。

## 建议

- ztf_split_compose.py:815 入口 docstring 补全 pages、variants、paragraphs 字段契约（§2）。

## 已核对

- 单页安全网记录页号与固定原因码，无正文或原始异常消息外泄。
- runner 复制清单与 helper-manifest 一致；临时目录位于 build/t5。
- ZTFSplitLayout v1 字段、实际尺寸与左下原点与 readerSelection.js 兼容。
- Node 16/16、git diff --check 通过；mock 证据 Chen 305.5s、FoBa 286.3s 均 PASS（归档证据）。
