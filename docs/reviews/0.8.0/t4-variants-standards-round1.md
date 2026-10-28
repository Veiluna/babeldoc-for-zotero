# T4 中文排版变体 — 规范轴评审第 1 轮

评审者：Codex gpt-6.1-sol（全新会话，只读）
结论：VERDICT: CHANGES

## 阻塞项

1. tests/test_split_variants.py:151：`-m unittest tests.test_split_variants` 时 `from mock_openai_server import ...` 报 ModuleNotFoundError（模块在 tests/），8 通过 1 错误，集成回归未执行（§6）。须不靠额外 PYTHONPATH 即可运行。

## 已核对

- runner 安装 helper 前严格校验 BabelDOC 0.6.4；PDFCreater、FontMapper.add_font 签名与本机源码一致；渲染前再深复制 IL，不污染原文档。
- 部署导入可靠；无新增模型请求；生产日志无正文；页级异常用白名单原因码或异常类型。
- 性能：2 个源页产出 6 个变体页耗时 18.94 秒，整篇性能待 T8 实测。

## 建议（不阻塞）

- ztf_split_variants.py:252 保留 source_char_order_missing 等固定原因码与源页号，不折叠成 ValueError。
- ztf_split_variants.py:295 渲染或移动失败后清理临时 PDF。
- tests/test_split_variants.py:158 减少对本机绝对样本路径依赖，补开关前后请求数断言；耗时与测量口径归档到票据。
