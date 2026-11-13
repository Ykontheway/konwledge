# Changelog

All notable changes to Konwledge are documented in this file.

The project is in alpha. Minor releases may refine public and lower-level APIs as capabilities are added; v0.1.0 does not provide a complete backward-compatibility guarantee.

## Unreleased — U14-U16 acceptance and U18-U20

### B2 本机只读工作台

- 已授权恢复前端开发，完成 V3 生命周期与变更视图；保留 React／Vite／Tailwind 静态演示，新增真实任务、revision、document、lineage、Diff 和 exact evidence 的 hash 导航，支持任务状态过滤、分页与刷新。
- 增加 loading、空数据、安全错误和读取重试处理，区分完整任务状态，`null` 不等于零或通过；请求使用 Abort 与 sequence 防止旧响应覆盖，不自动回退到 fixture。单元状态是持久化当前缓存，不冒充任务 as-of。
- 新增独立 management schema／TypeScript 类型及两端 `--check` 漂移检查，DTO 用 Ajv 运行时白名单校验，检查 ID／href，并按 Unicode code point 解释字符偏移；Snapshot V1 与 demo golden 保持不变。
- 增加仅 Vite dev 可用的严格 loopback 代理，配置缺失时合法管理请求返回 503，转发前校验 Origin、Host、路由、GET 方法和 queries。key 仅保存在 backend／Vite Node 进程环境，浏览器同源 GET 不带 key；preview／dist 不含 dev proxy，无公开 live 部署。
- 增加 `scripts/live_workbench.py`：使用已有验收库、随机临时 key，先等待 backend health 再启动 web，退出只清理自己的子进程。backend 沿用已有 additive schema／FTS 启动初始化，HTTP 读取只读，不新增 evaluation 或更新 baseline。启动步骤见 [本机只读工作台](docs/live-workbench.md)。
- 前端 126 tests、类型检查／构建与管理契约漂移检查通过；Python 1320 passed、3 skipped，ruff／格式／mypy 通过。云端浏览器走通失败任务到旧版本证据，任务列表、失败详情、Diff 和证据页静态检查无控制台错误。管理验收确认读取前后 19 张表、137 行及内容摘要不变。
- 任务详情的初步 metrics／checks 不代替 B3 可比结果、用例级诊断与导览；B3 下一批，V4 延后，B4 写交互／B5 MCP 未实现。v0.1.0 仍为 prepared，未创建 tag、PR、main 合并、PyPI 发布或公开部署。

以下旧条目中的“本次”“本轮”均指各自增量完成时的范围，保留历史说明。

### B1 工作台只读管理 API

- 增加明确启用的 `/v1/management` GET 契约：分页任务、revision 导航、稳定 lineage、文档版本与 Diff，以及精确历史证据。响应模型 frozen、extra forbid，报告与 payload 使用白名单投影。
- `knowledge serve --enable-management` 和 `create_app(..., enable_management=True)` 必须提供有效密钥；默认不挂载管理端点。保留 U18 请求协议、Snapshot V1、loopback、已有数据库与关闭 access log 的边界。
- 使用独立 SQLite 只读连接，不写库、不触发评测。历史证据校验版本、范围和 hash；缺失、损坏、跨源 Diff、非法请求和写方法均有明确安全结果。
- 增加管理契约、安全接线与真实库 HTTP 导航验收，并扩展 wheel/sdist 新模块检查和安装后管理 smoke。本次未修改前端、发布或部署。

### 后端验收与自动评测

- U14 增加可选 `[tokens]` 与真实 tiktoken 预算，覆盖完整 context rendering；保留默认字符预算、ContextPacket JSON 和冻结 Demo 不变。
- U15 新增独立 30 条公开基准，通过真实 lifecycle 发布、历史版本和过期干扰验证检索；不替换 6 条 Demo 或人工基线。
- U16 增加 TopK 5、Recall ≥ 0.80、MRR ≥ 0.65、引用覆盖 100%、过期命中 0 和无答案准确率门禁；新增 1,000 active 单元、30 次采样的 P95 ≤ 1.5 秒本机性能验收。
- 增加默认关闭的 `auto-evaluation`。启用后在知识变更事务中持久化任务，提交后同步评测；固定数据集版本/hash 与知识指纹，保留安全错误码、租约恢复和重试审计，不自动更新基线或回滚已提交知识。
- CLI 新增 `quality` 与 `auto-evaluation`，知识变更评测未通过时保留普通／JSON stdout，在 stderr 提示并退出 `3`。扩展 wheel/sdist 独立安装 smoke、真实 tokenizer 验收与 CI 证据。
- 本次不改变 v0.1.0 身份、U18 HTTP 请求协议或 Snapshot V1，不包含 MCP、前端、正式发布或部署。

### 真实文档验收与展示路线

- 新增 `scripts/real_knowledge_acceptance.py`，用项目真实使用文档、独立 SQLite 与固定评测集，通过正式 CLI 验证正向变更、质量下降、恢复和重复导入。每次运行保留独立报告，不改原始文档，不接受或更新人工基线。
- 新增 3 个测试，覆盖变更后自动任务、失败不回滚与历史保留、固定期望和安全失败报告；本次不修改生产服务。
- 增加前后端一体化展示路线，按只读管理契约、V3 生命周期与变更视图、V4 评测导览、受控写入和 U22 MCP 安排依赖。本轮仅验收与规划，未开发前端、增加 HTTP 管理接口或执行部署。

### U20

- 增加 SQLite 与 PostgreSQL schema-only 捕获。SQLite 使用 `mode=ro`、catalog authorizer、元数据 PRAGMA 和有界超时；PostgreSQL 使用 psycopg 3、只读 repeatable-read 事务及连接、statement、lock timeout。
- 增加不可变 schema snapshot 历史、规范 JSON/hash、稳定表 lineage、当前结构检索物化和 schema diff。重复捕获未变化结构保持幂等，结构恢复会复用已有 document 并追加捕获版本。
- 增加 `knowledge database capture-sqlite/capture-postgres/list/history/show/export/diff`。PostgreSQL DSN 只从 `--dsn-env` 指定的环境变量读取；CLI 不接受 DSN 值，失败诊断不回显凭据、用户名、原始 SQL 或驱动错误。
- 增加可选 `[postgres]` 依赖，核心安装仍不需要 psycopg。CI 使用 PostgreSQL 16/17 服务和临时测试凭据运行专用集成测试，默认 quality job 不安装该驱动。
- 扩展源码与隔离安装 smoke，覆盖合成 SQLite 增量捕获、业务行不泄漏、只读 list/history、schema diff，以及 API 对当前字段可检索、退役字段不可检索。
- `SourceKind.DATABASE` 不进入 V1 snapshot 合同。V1 导出遇到数据库来源会明确失败，不修改 demo golden、JSON Schema、前端类型或现有前端。

### U19

- 增加不可变对话 schema、10 MiB/UTF-8/重复 key 严格校验、版本历史与规范 JSON 导出；同一 ID/版本内容不可变，重复导入相同内容保持幂等。
- 增加确定性候选提取：只识别 `user`/`assistant` 消息中的显式事实和决策标记；不读取 `system`/`tool`，不调用 LLM，也不自动认定候选真实。
- 增加人工批准/拒绝终态审核与审计归属。批准在一个事务中物化可检索知识，拒绝不写知识；重导入保留决定，新版本不自动撤销旧版本已批准事实。
- 增加 `knowledge conversation` 和顶层 `knowledge candidate` CLI，支持 stdin、latest 默认、状态/对话/版本过滤、稳定 JSON、规范导出和安全覆盖保护。
- 扩展源码与安装后验收，覆盖待审核不可检索、批准可检索、拒绝缺席、重复审核审计、终态冲突和 HTTP 只读查询。U19 不增加运行时依赖，不修改 U18 API 协议，不改前端，也不发布或部署。
- 冻结 snapshot V1 合同中的来源类型，继续保持 `url`、`file`、`repository`、`manual` 四项。批准后的对话仍是 `conversation` 来源；V1 导出遇到这类来源会明确失败，不会省略或改标。demo golden、JSON Schema 与前端生成类型均未重新生成。

### U18

- 增加可选 `[api]`（FastAPI/Uvicorn）本机只读上下文传输：健康检查、模型列表、原生 `ContextPacket` 与严格的 OpenAI chat completion 子集；不执行推理、不返回虚构 usage。
- 增加 `knowledge serve`：仅 loopback，端口 `1..65535`，已有数据库与 SQLite `mode=rw`，密钥只取自 `KNOWLEDGE_API_KEY`（拒绝显式空白），禁用 access log，启动允许 additive schema/FTS 初始化，退出释放 engine。
- 默认 dev 组增加 API 类型/测试依赖、HTTPX 与官方 OpenAI/LangChain SDK；wheel 核心依赖不变，SDK 不作为运行时依赖。
- 增加无需外部模型/网络服务的真实 SDK 验收与 CLI 启动测试；固定查询、`as_of` 和检索控制，验证两个 SDK 的 packet 与原生 JSON 相等，未知选项仍拒绝。
- 分发验证保留 core-only smoke，并为 wheel/sdist 增加仓库外 `[api]` ASGI smoke；检查 API 文件打包、optional marker、无 SDK 运行时依赖、只读请求和 engine 释放。
- 增加 API 文档并更新架构/发布说明；U18 不提升版本、不修改前端、不部署、不发布。CI 触发范围仍为 PR/`main`，不是功能分支 push。

## 0.1.0 - 2026-11-11 (prepared, not published)

This release candidate closes local readiness for the first Knowledge CI workflow. It has not been merged to `main`, tagged, published as a GitHub Release, uploaded to PyPI, or used to verify a public demo deployment.

### Core capabilities delivered through U01-U16

- Local UTF-8 Markdown and text ingestion with normalization, SHA-256 identity, heading-aware splitting, exact character ranges, stable source/document/unit traceability, and idempotent imports.
- SQLite metadata persistence with explicit transactions, foreign keys, additive local upgrades, synchronized FTS5, immutable document versions, append-only revisions, lineage-aware diffs, reverts, and as-of lifecycle state.
- Freshness policies for source-scoped TTL and known source-update times, active-only publication defaults, and atomic dry-run/apply scans.
- Conservative conflict candidates with frozen two-sided evidence, deduplication, human review states, and an append-only audit chain.
- Lexical, vector, and hybrid retrieval with eligibility-first lifecycle filtering, deterministic reciprocal-rank fusion, historical/source scopes, and explicit missing-vector degradation.
- Whole-unit context assembly with exact citations, deterministic citation identity, duplicate/overlap controls, bounded character/unit budgets, and source text rendered as untrusted evidence.
- Immutable, versioned evaluation sets; deterministic retrieval metrics and read-only reports; compact healthy baselines; validated comparisons; and policy-driven fail-closed gates with stable process exit codes.
- Deterministic hash embeddings for offline plumbing tests, an in-memory exact vector index, and an optional LanceDB adapter.
- Reproducible packaged demo data, a versioned JSON snapshot/schema contract, golden drift tests, a V2 read-only static site, and CI for Python, the site build, the optional adapter, demo drift, and the committed regression gate.

### U17 release-readiness additions

- Synchronized v0.1.0 identity across `pyproject.toml`, `konwledge.__version__`, and `uv.lock`, with the package classified as alpha and Python 3.11+ retained.
- Added a lazy package-root `KnowledgeLibrary` export and the packaged PEP 561 `py.typed` marker while retaining the established domain and evaluation import surfaces.
- Added wheel and source-distribution inspection plus isolated installed-package verification for metadata, entry points, packaged resources, CLI aliases, module execution, retrieval/context, evaluation, and gate behavior.
- Added architecture, contribution, release, installation, end-to-end demo, API-boundary, and release-status documentation.
- Documented that CI runs for pull requests and pushes to `main`, not an ordinary push only to a feature branch, and that CI artifacts are not published packages.

### Known limitations

- `HashEmbeddingProvider` and `--demo-vectors` validate deterministic pipeline wiring; they do not provide semantic embeddings or production-quality retrieval scores.
- Evaluation matches expected lineages. It does not assess whether source facts are correct, whether retrieved text fully answers a question, or whether a generated answer is faithful. Konwledge does not automatically call an LLM or generate answers.
- Vector retrieval for a small local library copies eligible vectors into an exact in-memory index and scans them in O(N) time. This is a single-machine design, not a distributed or large-scale ANN service.
- The metadata store uses local SQLite with additive upgrades. It does not provide destructive migration orchestration, high availability, multi-tenant isolation, or a production support SLA.
- Reproducible reports require callers to control corpus versions, evaluation times, retrieval settings, and provider/index identity. Evaluation does not freeze the entire corpus, vector backend, or embedding provider/model.
- Regression gates cannot be bypassed by an average improvement: the committed strict policy checks aggregate and per-case metrics, blocks degraded candidates, and fails closed on invalid or uncovered evidence.
- Raw evaluation reports may include source text, locators, IDs, and local paths. Compact baselines omit hit quotes, source locators, document/unit IDs, and absolute corpus paths, but still contain dataset-authored queries and identifiers.
- v0.1.0 prepared 当时沿用 2026-11-06 的范围决定，前端停在 V2。后续 B2 已获授权恢复并实现 V3，V4（B3）仍延后；公开 live 部署未执行，workflow 配置不代表站点已上线。
- No v0.1.0 tag, GitHub Release, PyPI package publication, or public-site deployment is part of this prepared candidate.

<!-- B2 文稿复核：使用 dedicated grep 扫描，中文半角标点 0、中文长破折号 0、元话语 0、对比句式 0；标题模式命中 1，原有 0.1.0 版本索引标题豁免，不改英文格式。事实回读保留旧条目的“本次”范围，核对 B2／V3 已实现、B3／V4 下一批、B4／B5 未实现、126／85／29／4 已知结果，以及无 tag／PR／main 合并／发布／公开部署；完整 Python 1320／3 与云端最短导航已通过，不写成公开部署或全部人工点验。主线是追加 B2 记录，不否认历史范围。 -->
