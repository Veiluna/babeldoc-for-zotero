# T6 断句组拆片段再译 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: CHANGES

## 阻塞项

1. GLOSSARY.md:26、ztf_babeldoc_runner.py 正式入口约 6836 行固定 `max_requests = None`：新请求经 RequestBudget 计数，但生产路径没有有限上限，"超限不发"永远不触发；与 #56 验收"计入每篇请求上限"不符。需要 Lead 明确上限来源；若维持无限，先同步票据与规格。

## 已核对

- `except Exception: applied = False` 后有 restore_failed、quality_failure、item_outcome、policy.record_failure 与 failed_items 计数，不是静默吞异常。
- `on_batch_error=lambda error: None` 后网络异常由 create_completion_adapting 记录，批次转失败项并结算降级计数。
- 新请求由 runner 发起；日志不含正文与 API key。

## 建议（不阻塞）

- runner 约 5531 行保留安全的异常类型或固定原因码。
- 补经过真实 create_completion_adapting 的 503/参数拒绝只尝试一次用例；补拆片段仍失败时的 PDF 导出与 ZTF_WARNING e2e。

## Lead 处理

上限来源属于用户决定（规格写"计入用户设置的每篇最多请求数"，但当前插件没有该设置，规格同时规定"不新增设置项"），已交用户裁决。
