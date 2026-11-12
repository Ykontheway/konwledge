# Changelog

All notable changes to Konwledge are documented in this file.

The project is in alpha. Minor releases may refine public and lower-level APIs as capabilities are added; v0.1.0 does not provide a complete backward-compatibility guarantee.

## Unreleased — U18-U20

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
- Frontend development has been paused at V2 since the 2026-11-06 scope decision. V3/V4, live-page publication, and further deployment are deferred; workflow configuration is not evidence of a live site.
- No v0.1.0 tag, GitHub Release, PyPI package publication, or public-site deployment is part of this prepared candidate.
