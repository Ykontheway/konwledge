# Releasing

This document is a maintainer checklist. It separates a release-ready source tree, verified local artifacts, and an actually published release.

## Current v0.1.0 state

As of 2026-11-11, v0.1.0 is prepared as an alpha release candidate in source metadata. U18 and U19 remain Unreleased additions and do not change the version. This work does not merge `main`, create a pull request, tag a commit, create a GitHub Release, publish to PyPI, or deploy/verify a public site. Local artifacts produced during verification are not a published release.

No installation instructions should claim that `konwledge==0.1.0` is available from PyPI until the package index confirms it.

## Release roles and approval boundary

One maintainer may prepare and validate a candidate. A maintainer with release authority must explicitly approve each external side effect:

1. merge to `main`;
2. create and push the version tag;
3. create a GitHub Release and upload artifacts;
4. publish to PyPI;
5. publish the static site.

Approval of one action does not imply approval of the others. CI passing is necessary but is not publication approval. Never run release publication commands from an unreviewed working tree.

Because `.github/workflows/deploy-demo.yml` is configured for pushes to `main`, maintainers must decide explicitly whether a proposed merge is allowed to initiate that workflow. Use a protected GitHub Pages environment or pause the workflow when site publication is not approved.

## 1. Confirm scope and repository state

- Confirm the intended version and alpha classification.
- Confirm release notes describe only implemented behavior.
- Record deferred scope: frontend remains at V2; V3/V4, live-page publication, and further deployment are not part of the candidate.
- Confirm there are no claims of a live page, PyPI availability, tag, or GitHub Release before those actions actually succeed.
- Review all changes and verify that unrelated working-tree changes are not entering the candidate.
- Ensure secrets, local databases, vector indexes, caches, logs, and private runtime reports are absent.

Use normal Git inspection commands such as `git status`, `git diff`, and `git log`; do not clean or reset another contributor's work to manufacture a clean tree.

## 2. Check version truth

The release version must agree in all three locations:

| Source | Expected v0.1.0 value |
| --- | --- |
| `pyproject.toml` | `project.version = "0.1.0"` |
| `src/konwledge/__init__.py` | `__version__ = "0.1.0"` |
| `uv.lock` | editable `konwledge` package record version `0.1.0` |

Also verify:

- `requires-python` remains `>=3.11`;
- the classifier reflects alpha status;
- `knowledge` and legacy `konwledge` entry points both target `konwledge.cli:main`;
- README expected output and the `0.1.0` CHANGELOG heading match the candidate;
- root `KnowledgeLibrary` remains a lazy export.

Run the focused metadata contract:

```bash
uv sync --locked --dev
uv run pytest tests/test_release_metadata.py tests/test_cli.py
```

If the version was intentionally changed, update project metadata and regenerate `uv.lock` with `uv lock`; then review the lock diff. Do not hand-edit only one source of version truth.

## 3. Run local validation

Run the complete Python quality and behavior suite:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=konwledge --cov-report=term-missing
```

Verify the committed demo contract and U16 gate behavior:

```bash
uv run knowledge demo build --force
uv run knowledge demo export --check
uv run pytest tests/test_evaluation_gate_demo.py
```

If the optional adapter is in release scope:

```bash
uv sync --locked --dev --extra lancedb
uv run pytest tests/test_indexing.py -k lance
```

The default tests and demo gate do not require provider credentials or a network service. SQLite must provide FTS5.

## 4. Build and verify isolated distributions

Run the project verifier, not only `pytest`:

```bash
uv run python scripts/verify_distribution.py
```

The script performs these release checks:

- builds an exact wheel and source distribution with `uv build`;
- validates archive paths, metadata identity, console entry points, the PEP 561 `py.typed` marker, packaged demo resources, and runtime-file exclusions;
- installs each artifact into a fresh environment outside the source checkout;
- proves imports come from that environment's `site-packages`;
- smoke-tests `knowledge`, `konwledge`, and `python -m konwledge`;
- runs the packaged eight-step demo, historical retrieval, context/citation behavior, evaluation, baseline comparison, and both passing and blocking gates.

Preserve an explicit output directory when artifacts are candidates for review:

```bash
uv run python scripts/verify_distribution.py --output-dir release-verification
```

A successful run prepares verified local files such as `release-verification/build/konwledge-0.1.0-py3-none-any.whl` and the matching source archive. These files are not published merely because they exist or were uploaded as CI artifacts.

Review smoke summaries and archive contents before approving publication. Do not publish an artifact rebuilt by an unverified, different path.

## 5. Review release evidence

Before merge or tag approval, confirm:

- pull-request CI passed on supported Python versions;
- the fixed-time candidate passes `examples/evaluation-gate.json` against the committed baseline;
- CI did not regenerate the approved baseline;
- demo snapshot and schema golden files changed only when intentionally reviewed;
- raw reports do not leak local source paths or source text into public artifacts;
- compact baselines are still validated, portable, and free of hit quotes and absolute corpus paths;
- wheel and sdist smoke summaries both passed;
- documentation links resolve within the repository.

A valid blocked gate exits `3` and must stop the release. Invalid evidence exits `2`; runtime or file failures exit `1`. Do not convert these outcomes into an unconditional success.

## 6. Approve merge and identify the release commit

Merge to `main` only after maintainer review and explicit merge approval. After merge:

- wait for `main` CI to pass;
- identify the exact immutable commit to release;
- verify the tag does not already exist locally or remotely;
- ensure release artifacts correspond to that exact commit.

If site publication remains deferred, enforce that decision before a `main` push can reach the Pages deployment job.

## 7. Optional future publication commands

The commands below are examples for a future maintainer session after explicit approval. They were not executed as part of U17.

### Tag

```bash
git tag -s v0.1.0 <approved-main-commit> -m "Konwledge v0.1.0"
git push origin v0.1.0
```

Use the repository's accepted signing policy. If signed tags are not configured, stop and obtain an approved alternative rather than silently changing provenance.

### GitHub Release

After verifying checksums for the exact approved wheel and source archive, a maintainer may create a release through the GitHub UI or an authenticated CLI. One possible command shape is:

```bash
gh release create v0.1.0 \
  release-verification/build/konwledge-0.1.0-py3-none-any.whl \
  release-verification/build/konwledge-0.1.0.tar.gz \
  --verify-tag --title "Konwledge v0.1.0" --notes-file CHANGELOG.md
```

Review the rendered notes; using the whole changelog as notes may not be desirable. A GitHub Release is distinct from a PyPI publication.

### PyPI

Publish only the already verified artifacts, with an approved trusted-publishing or credential flow. For example, in a configured maintainer environment:

```bash
uv publish release-verification/build/konwledge-0.1.0-py3-none-any.whl \
  release-verification/build/konwledge-0.1.0.tar.gz
```

Do not rebuild between approval and upload. After publishing, verify package-index metadata and install the exact version in a new external environment before changing README availability claims.

### Static site

Site publication is a separate decision. If approved, run `pnpm verify` in `web/`, confirm the snapshot contract and intended base path, and explicitly dispatch or approve the Pages workflow. Record the resulting URL only after it is accessible and its content has been checked. Do not infer a live site from workflow configuration alone.

## 8. Post-publication verification

Only after each action succeeds:

- verify the remote tag resolves to the approved commit;
- verify GitHub Release assets and checksums;
- verify a clean install imports version `0.1.0` from site-packages;
- run `knowledge --version`, `konwledge --version`, and `python -m konwledge --version`;
- verify packaged demo resources and one offline end-to-end smoke path;
- verify the public site if it was separately approved and published;
- update status language to distinguish which channels are actually live.

If any channel fails, report that channel as incomplete. Do not delete or overwrite evidence to make the release appear complete.

## U18 集成检查补充（Unreleased，非发布）

U18 保持 `0.1.0`，新增可选本机上下文 API；v0.1.0 仍然是已准备、未发布候选。没有提交/推送、tag、发布、部署或远端 CI 通过的隐含承诺。工作分支 push 不触发当前默认 CI，触发条件仍为 PR/`main`。

```bash
uv sync --locked --dev
uv run pytest tests/test_api.py tests/test_api_cli.py tests/test_api_clients.py tests/test_distribution.py tests/test_release_metadata.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=konwledge --cov-report=term-missing
uv run python scripts/verify_distribution.py --output-dir dist/u18-verification
```

发布评审需额外确认：

1. wheel 与 sdist 含 `api/__init__.py`、`app.py`、`models.py`、`server.py`；FastAPI/Uvicorn 的 `Requires-Dist` 仅在 `extra == 'api'` 时生效，SDK/HTTPX 不进入运行时依赖。
2. `uv.lock` 通过 `uv lock` 重新生成；默认 dev 含 FastAPI/Uvicorn，保证不显式选 extra 时静态类型和默认测试也能运行。dev 同时包含真实 SDK 验收依赖，但不能把 dev 安装成功当作 core-only 证据。
3. `verify_distribution.py` 分别先安装 wheel/sdist 的 core-only 版本，证明 optional 包缺失时 root/API 导入、help/version 和缺 extra 友好错误仍有效；再安装同一产物的 `[api]`。复制到仓库外的 `installed_api_smoke.py` 用 `python -I` 证明 API 来自 site-packages、不含 OpenAI/LangChain/HTTPX，驱动 ASGI 验证 packet、鉴权、严格错误与请求只读，并替换 `uvicorn.run` 验证 CLI 启动及 engine 释放。
4. 两份 `*-smoke-summary.json` 中新增 `api_extra` 结果，CI 原有上传模式会保存它们；默认 CI 同步 dev 已覆盖 API 类型/SDK 测试，无需把 SDK 加到 wheel 或改变 feature-push 触发条件。
5. [API 文档](api.md) 明确 `serve` 的已有数据库要求、loopback/端口/环境密钥约束及启动 additive schema/FTS 可能写库，与 HTTP 请求只读区分；OpenAI 子集只返回 packet，无真实推理、usage、stream/tools/sampling/Responses/对话历史。

测试不得调用外部 provider，不启动后台服务或部署；本地产物仍须单独取得发布授权，不能因为分发 smoke 成功而标记为已发布。


## U19 集成检查补充（Unreleased，非发布）

U19 保持 `0.1.0`，不增加运行时依赖，也不修改 U18 只读 HTTP schema。候选审核是本地显式操作，不是发布授权、身份认证或自动事实验证。

建议在其他协作者完成安全测试后运行：

```bash
uv run pytest tests/test_conversation_cli.py tests/test_candidate_cli.py \
  tests/test_conversation_safety.py
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts
uv run python scripts/verify_distribution.py --output-dir dist/u19-verification
```

发布评审还要确认以下事项：

- core-only wheel/sdist 都能从仓库外完成对话 stdin 导入、默认 pending 列表、批准与拒绝、重复导入和审计查询；测试不读取源码 checkout。
- 非法 JSON、重复 key、非 UTF-8、超过 10 MiB 和不合 schema 的输入在创建数据库或父目录前失败，诊断不回显原始私密正文。
- 待审核与已拒绝候选不出现在 search/context；批准候选可检索。重复相同决定保留最初审核记录，反向终态操作退出 `2`。
- `[api]` 隔离 smoke 先通过 CLI 准备批准/拒绝记录，再在只读 SQL 监听阶段验证 HTTP 只能检索批准内容，不产生写入。
- `examples/conversation-demo.json` 是合成数据；原始聊天、conversation/candidate JSON、导出和数据库均按私密本地制品处理，不加入公开发布资产。

U19 不要求新增 console entry point、依赖、API 路由、前端或部署。分发验证成功仍只说明本地产物可供评审，不表示 v0.1.0 已发布。
