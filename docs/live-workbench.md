# 本机只读工作台

B2 已完成 V3 生命周期与变更视图。现有 React／Vite／Tailwind 静态演示保留，新增实时工作台通过 B1 管理 API 读取已有数据库。两种数据来源明确区分；实时读取失败会显示错误，不自动回退到 fixture。当前没有公开 live 部署，版本仍为 `0.1.0` prepared，未创建 tag、PR、main 合并或 PyPI 发布。

## 用真实验收库启动

需要 Python 3.11+、支持 FTS5 的 SQLite、uv、Node 和 pnpm。首次运行在仓库根目录安装锁定依赖：

```bash
uv sync --locked --dev
(cd web && pnpm install --frozen-lockfile)
uv run python scripts/real_knowledge_acceptance.py \
  --output-dir .konwledge/real-knowledge-acceptance
```

验收脚本每次建立唯一运行目录。确认输出 `status` 为 `passed`，从输出的 `report` 路径找到这次 `report.json` 的父目录，再用其中已有的 `knowledge.db`。不要把输出根目录当作运行目录，也不要复用某台机器的固定目录或凭据。将下面的占位路径替换为实际运行目录后执行：

```bash
uv run python scripts/live_workbench.py \
  --database "<real-acceptance-run-directory>/knowledge.db" \
  --web-port 5175 --api-port 8015
```

启动器检查已有 SQLite 文件及两个不同的空闲 loopback 端口，自动生成随机临时 key，先等待 backend `/health` 就绪，再启动 Vite。打开 [本机任务列表](http://127.0.0.1:5175/#lifecycle/jobs)。按 `Ctrl+C` 只终止启动器自己创建的子进程组；启动失败也会清理这些进程，不按端口杀其它服务。

启动器不创建缺失数据库，但 backend 启动仍可能执行已有 additive schema／FTS 初始化。因此请使用隔离验收库，不能宣称启动完全不写库。HTTP 读取只读，页面不新增 evaluation、不更新 baseline，也不重跑任务。

## 页面与数据边界

- 静态入口仍是 `#overview`、`#trace`、`#diff`；实时入口是 `#lifecycle/jobs`，支持状态过滤、分页和手动刷新。切换状态回到第一页，刷新重读当前页；查看新增任务时先回第一页。跨请求分页不作为冻结快照。
- 实时 hash 导航覆盖任务详情、revision、document、lineage、版本 Diff 和 exact evidence。具体 ID 从页面链接获取，历史选段按确切 unit／document 版本读取，并显示字符范围、content hash 和 document checksum。缺失或损坏的历史证据明确报错，不用最新正文替换。
- 任务保留评测时间、数据集版本及配置／知识指纹；单元 status 展示的是持久化的当前缓存状态，不是任务当时的 as-of 状态。文档前后版本表示导入顺序，也不等于发布 head。
- 页面区分 `queued`、`running`、`passed`、`failed`、`blocked`、`superseded`，处理 loading、空数据、权限拒绝、资源不存在、历史资源不可用、服务错误和读取重试。这里的“重试”只重发 GET。缺少报告或指标为 `null` 时不显示成零或通过。
- 请求使用 `AbortController` 和 sequence 检查，切换路由、筛选或刷新后，旧响应不能覆盖新请求。DTO 通过 Ajv 运行时白名单校验，并检查 ID 与 href 的一致性。字符偏移按 Unicode code point 解释，不当作 JavaScript UTF-16 下标；原文选段按未受信文本展示，不执行 HTML。

任务详情已有初步聚合 metrics／checks，帮助查看持久化结果。B3 下一批才补可比结果对照、用例级诊断和演示导览，当前导航只提供定位线索，不宣称变更与指标下降的自动因果归因。B4 写交互与 B5 MCP 均未实现。

## 密钥只留在进程中

启动器仅向 backend 和 Vite 的 Node 子进程注入 `KNOWLEDGE_API_KEY`；Vite 另收 `KNOWLEDGE_MANAGEMENT_ORIGIN=http://127.0.0.1:8015`。手动接线也只在这两个进程环境中配置，不写入仓库、命令参数或 `VITE_*`，不把 key 交给浏览器。

浏览器只向同源 `/v1/management` 发 GET，不带 key 或 cookie；dev proxy 校验后再添加 backend Bearer 认证。代理未配置或配置无效时，合法管理请求返回 503。upstream 只接受带显式有效端口的 HTTP loopback，主机限 `127.0.0.1`、`localhost` 或 `[::1]`，拒绝公网地址、URL 凭据、额外路径或 query。转发前校验 Origin、Host、route、method 及 queries，拒绝未知／重复参数和写方法，不透传浏览器认证或任意请求头。

代理仅用于 Vite dev；`pnpm preview` 和 `dist/` 不含 dev proxy。静态构建保留演示，不能据此宣称 live 已部署，也不应为联调放开公网或任意 CORS。完整 HTTP 契约见 [只读管理 API](management-api.md)。

## 验证命令与已知结果

在仓库根目录检查 management schema／类型漂移及专项测试：

```bash
uv run python scripts/export_management_schema.py --check
node web/scripts/gen-management-types.mjs --check
uv run pytest tests/test_live_workbench.py tests/test_management_schema.py
(cd web && pnpm verify)
```

Management schema 从 Python DTO／query 模型导出，与 Snapshot V1 独立，不修改 demo golden。`pnpm verify` 检查前端类型、Snapshot 类型漂移、Vitest 和构建；management 的两条 `--check` 需单独执行，不以生成命令代替漂移检查。

完整 Python 回归取得 1320 passed、3 skipped，ruff、格式检查与 mypy 均通过。前端 `pnpm verify` 取得 126 tests、类型检查、Snapshot 类型漂移及构建通过；management schema／类型漂移也通过。proxy 专项 85 属于前端 126，launcher 专项 29、schema 专项 4 属于 Python 回归，不另行相加。

已用云端浏览器在隔离验收工作台走通失败任务 → 触发 revision → 历史 Diff → 旧版本证据。页面中的证据确切指向旧 document／unit，字符范围为 `[995, 1885)`，选段为 890 code points，未用最新正文替换。任务列表、失败详情、Diff 和证据页的静态布局检查均无控制台错误，列表显示 passed → failed → passed，Diff 为 1 modified／4 unchanged。管理 API 验收再次通过，读取前后 19 张表、137 行及内容摘要不变，评测任务仍为 3 条。

筛选、分页、读取重试、前进后退及竞态通过自动测试覆盖，本轮云端浏览器只做最短导航检查，没有逐项人工点验，也没有验证全部移动端尺寸或公开部署。构建仍有主 bundle 超过 500 kB 的非阻断提示，后续可按页面拆包。

<!-- B2 文稿复核：按 haohao workflow 分别回读事实、行文和节奏；使用 dedicated grep 扫描，中文半角标点、中文长破折号、编号／量词标题、元话语及对比句式均为 0。事实核对启动命令与实际唯一目录、进程密钥、严格 dev proxy、启动初始化与 HTTP 只读、持久化当前状态、Snapshot V1 独立，以及 126／85／29／4 已知结果；补充 1320／3 的完整 Python 结果、云端最短导航与只读摘要验收，区分自动测试覆盖、实际浏览器点验和未验证范围。主线为安全启动和只读边界，无无关背景扩写。 -->
