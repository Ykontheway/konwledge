# 后端验收

本次补齐 U14～U16 的默认验收，保留现有 Demo、JSON 基线和 Snapshot V1。新增内容仍是 Unreleased，不提升版本，也不发布、部署或开发 MCP。

## Token 预算按真实 tokenizer 计数

核心安装仍使用 `max_chars`、`max_units`。启用 Token 预算需要安装可选 `[tokens]`，默认编码为 `cl100k_base`：

```bash
uv sync --locked --dev --extra tokens
uv run knowledge context "deployment" --max-tokens 2048 \
  --token-encoding cl100k_base --json -d .konwledge/knowledge.db
```

SDK 使用 `library.build_context(query, max_tokens=2048)`，也可向 `assemble_context` 或 `library.context.build` 传入符合 `TokenCounter` 协议的精确计数器。SDK 自定义计数器需要同时填写准确的 `token_encoding` 标签，不应把字符或字节计数用于模型 Token 预算。

计数对象是完整 `context_text`，包含信任边界提示、标签、引用元数据、JSON 转义和换行。查询、外层 Packet JSON、调用方 system prompt、消息封装和输出预留不在预算内，调用方应另行预留。字符、单元和 Token 限制同时生效；超限证据整块跳过，继续尝试后续较小证据，不截断原文。未启用 Token 预算时返回原 `ContextPacket`，其默认 JSON 字段不变；启用时返回 `TokenContextPacket`，增加 `max_tokens`、`used_tokens`、`token_encoding`。

tiktoken 首次加载编码表可能下载公开分词表。可预热 `TIKTOKEN_CACHE_DIR`，离线运行前确认指定 encoding 已缓存。文本不会提交给外部模型。不同模型需要选择匹配的 tokenizer，`cl100k_base` 不代表所有模型。

## 独立基准覆盖 30 条用例

```bash
uv run knowledge quality acceptance --report .konwledge/quality-acceptance.json
# CI 也可调用以下脚本；与 CLI 使用同一份打包语料和 runner。
uv run python scripts/quality_acceptance.py
```

命令在临时库中建立独立公开语料，运行 30 条固定用例和性能验收，结束后删除临时库。原有 `examples/evaluation-demo.json` 的 6 条用例、基线、门禁和 Demo 快照不变。

用例包括自然语言问题、共享关键词的不同问法、多证据、无答案、历史版本以及实际过期知识隔离。答案与 query 独立编写，使用固定身份和时间，经真实 lifecycle 发布。检索模式是 lexical，近义用例验证词面可检索性，不证明模型语义能力；业务需自行补充相应的向量、Agent 或生成答案评测。

## 默认质量门槛

| 检查 | 默认要求 |
| --- | --- |
| 检索 TopK | 5 |
| Recall@5 | ≥ 0.80 |
| MRR | ≥ 0.65 |
| 检索证据引用覆盖 | 100% |
| 过期或退役命中 | 0 |
| 无答案准确率 | 有对应用例时要求 100% |
| 降级与时效配置 | 不允许降级或 `include_stale` |

引用覆盖由真实命中的精确引用构造和校验得出，只证明证据可引用。没有可计算 Recall／MRR、没有命中证据、错误 TopK 或基线不兼容，均不能通过。参数和数值严格校验，拒绝 NaN、无穷值及 bool 冒充整数。

现有 `evaluation gate` 保留原契约。新增 `quality run` 对业务评测集做绝对验收，可选叠加人工基线回归：

```bash
knowledge quality run --set-id <set-id> --version 1 \
  --baseline-json approved-baseline.json \
  --regression-policy-json regression-policy.json \
  --report .konwledge/quality-report.json -d .konwledge/knowledge.db
```

没有回归基线时可省略两个参数。质量命令通过返回 `0`，有效门禁失败返回 `1`，非法输入返回 `2`；自动评测变更命令的未通过退出码为 `3`。报告文件默认拒绝覆盖。基线和策略从不被 runner 自动改写。

## 性能门槛有固定测试条件

性能 runner 使用 1,000 个 active 单元，加一个实际 stale 的干扰单元，文件型 SQLite、lexical 检索、TopK 5、固定时间和 query，预热 3 次后计时 30 次真实 `RetrievalService.search`。P95 使用 nearest-rank，阈值为 1.5 秒；超限让整体验收失败。

报告记录 Python、SQLite、平台、活跃单元数、采样数、预热数和耗时统计。这个结果是固定条件下的本机验收，不是线上 SLA，也不涵盖网络调用、向量 provider、Agent 执行或端到端自动评测总耗时。

## 交付检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=konwledge --cov-report=term-missing
uv run knowledge demo export --check
uv run python scripts/verify_distribution.py
```

分发检查在仓库外分别安装 wheel 和 sdist，验证 core-only 的 30 条基准、自动评测真实文件更新及独立 `[tokens]` 的真实编码计数。CI 保留旧 Demo 门禁，并运行新增独立验收与上传证据。功能分支单独 push 不触发当前 CI；本地通过不能替代远端 CI 状态。

启用业务知识变更回归的步骤和状态说明见 [知识变更自动评测](auto-evaluation.md)。

<!-- 文稿检查：半角中文标点、标题编号、元话语、B2 句式和长破折号均为 0 处；数字、命令、预算范围、退出码和发布边界与实现逐项核对。 -->
