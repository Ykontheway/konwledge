# 工作台只读管理 API

B1 提供真实任务、知识版本和证据的读取契约，B2 已接入 V3 只读工作台。管理端点全部是 GET，只有显式启用并提供密钥时才挂载；现有 U18 上下文协议与 Snapshot V1 不变。当前版本仍为未发布的 `0.1.0`，没有公开部署。

## 显式启用

安装现有 `[api]` extra，并从凭据管理器或进程环境注入非空的 `KNOWLEDGE_API_KEY`。不要把真实密钥写进命令参数、文档、日志或仓库。

```bash
python -m pip install '.[api]'
knowledge serve --enable-management \
  --database .konwledge/knowledge.db --host 127.0.0.1 --port 8000
```

数据库必须已经存在，`serve` 不创建缺失文件。服务仍只绑定 loopback，不自动开启公网访问、CORS、后台调度或多租户能力。开启管理端即使在本机也必须提供有效密钥；不支持通过隐式环境开关开启管理路由。

嵌入式调用如下，engine 生命周期仍由调用者管理：

```python
import os
from konwledge.api import create_app

app = create_app(
    library,
    api_key=os.environ["KNOWLEDGE_API_KEY"],
    enable_management=True,
)
```

请求使用 `Authorization: Bearer <进程环境中的密钥>`。未认证请求在管理路由进入存储前返回 401。默认未启用时管理路径为 404，也不进入 OpenAPI；启用密钥后沿用原有策略关闭公开 `/docs`、`/redoc`、`/openapi.json`，客户端开发者可在受控进程内通过 `app.openapi()` 获取 schema。

## 读取契约

以下路径均以 `/v1/management` 为前缀。

| GET 路径 | 返回内容 |
| --- | --- |
| `/jobs` | 分页任务列表，可按 status、dataset_id、dataset_version、parent_job_id 过滤 |
| `/jobs/{job_id}` | 任务状态、时间、配置与数据集指纹、聚合指标、质量检查、安全错误码和触发 revision 链接 |
| `/revisions/{revision_id}` | revision 类型、变更类型及白名单导航字段，关联 document、unit、lineage IDs |
| `/lineages/{lineage_id}` | 同一稳定 lineage 的分页历史单元元信息 |
| `/documents/{document_id}` | 精确文档版本、checksum、导入时间、前后导入版本以及分页单元 |
| `/documents/{new_document_id}/diff?old_document_id={old_id}` | 同源文档的持久化 lineage Diff、变更计数、分页单元对照和证据链接 |
| `/units/{unit_id}` | 最小单元元信息与导航链接 |
| `/units/{unit_id}/evidence` | 精确版本的原文选段、字符范围、hash、source/document/unit/lineage IDs 及 revision 锚点 |

分页默认 `limit=20`、`offset=0`，范围分别为 1～100、0～100000。分页集合包含 `items`、`total`、`limit`、`offset`。offset 分页可能受并发新增数据影响，前端展示持续变化的列表时应刷新第一页，不能把跨请求分页结果视为冻结快照。

DTO 及嵌套模型均 frozen、extra forbid。前端按 schema 获取字段，不依赖数据库表结构或任意 JSON payload。指标缺少有效结果时可为 null，尚未执行、blocked、superseded 等状态不得显示成通过。

## 从任务回到历史证据

先从任务的 `trigger_revision_ids` 或 `links` 读取 revision，再通过白名单关联读取对应文档、单元与 lineage。文档 Diff 使用已持久化 lineage，包含 added、modified、moved、removed、unchanged，不重新做相似度匹配。

证据必须通过确切 `unit_id` 获取，校验该单元对应的不可变 `document_id`、范围和内容 hash。旧任务不能用 latest 文档替代历史正文；历史证据缺失或损坏会明确报错。证据中的范围按 Python Unicode 字符索引计算，不是字节或 JavaScript UTF-16 偏移，前端高亮时需做对应转换。

文档的前后版本表示导入顺序，不等于某时刻的发布 head。单元 status 是持久化缓存状态，读取不运行时效扫描或状态修复；它不能冒充任务评测当时的 as-of 状态。任务的知识指纹和评测时间保留，用于说明报告范围。

自动评测报告只有聚合结果与检查项，当前不提供用例级失败诊断。因此“任务 → 变更 → Diff → 证据”提供定位线索，不自动断言某段改动就是指标下降的原因。用例级诊断应在后续单独设计授权与隐私契约。

## 只读与隐私边界

普通任务、revision、文档与单元元信息不包含源全文、query、绝对 locator、metadata、author/reviewer 或原始异常。报告和 revision payload 使用白名单投影，不透传任意 JSON。证据端点是唯一明确返回原文选段的管理响应，仍需同样的 Bearer 认证；原文可能自身含有敏感内容，不能视为自动脱敏，也不能把未受信正文当作 HTML 直接执行。

管理读取使用独立 SQLite 只读连接，不调用写入服务、commit 或评测 hook，不修改原连接的 query_only 设置。匿名内存库通过独立备份快照读取，快照不可用时安全返回 503。进程内存快照与文件型库有不同运行约束，生产部署仍需另行设计。

`serve` 启动阶段沿用原有 additive schema／FTS 初始化，可能写入已有数据库；“只读”指 HTTP 请求，不宣称整个启动过程零写入。

| HTTP 状态 | 含义 |
| --- | --- |
| 401 | 缺少或错误认证，尚未进入管理数据读取 |
| 404 | 管理接口未启用，或资源不存在 |
| 405 | 不允许对管理端点执行写方法 |
| 409 | 跨源 Diff 或证据一致性校验失败 |
| 410 | 所需历史证据或锚点不可用 |
| 422 | UUID、分页、过滤条件等请求无效，不回显敏感输入 |
| 500／503 | 安全内部错误或读取快照不可用，不返回 SQL、路径或原始异常 |

## 实际验收与复现

先运行真实文档验收脚本，或使用已有隔离验收目录中的库：

```bash
uv run python scripts/real_knowledge_acceptance.py \
  --output-dir .konwledge/real-knowledge-acceptance
uv run python scripts/management_api_acceptance.py \
  --database "<run-directory>/knowledge.db" \
  --report .konwledge/b1-management-acceptance-new.json
```

第二个脚本通过 HTTPX ASGITransport 实际调用 app，不监听公网，也不把内存调用表述为已部署服务。报告默认拒绝覆盖；复跑需换报告文件名。脚本是针对前一个真实验收场景的导航验收，不是任意生产库的通用检查工具。

本次读取成功库包含 3 个任务：正向 passed、故障 failed、恢复 passed。故障任务 Recall@5 为 0.666667；Diff 为 1 个 modified、4 个 unchanged；读取了同一 lineage 的 3 个历史证据版本。请求前后 19 张表共 137 行的内容摘要一致，没有新增 job、revision 或其它数据。

验收覆盖默认关闭、认证、分页、任务详情、revision 导航、版本 Diff、历史证据，以及未知 UUID、非法参数、跨源 Diff 和拒绝写方法。任务报告不泄漏 query 或源路径；原文只通过明确的 evidence 请求读取。安装后验证同时检查新模块打包及管理开关，核心安装仍不需要 API extra。

B2 已将这些契约接入现有前端，保留静态演示并新增 V3 实时只读视图。启动、安全 dev proxy 和浏览器验收结果见 [本机只读工作台](live-workbench.md)；B3 评测导览为下一批，未公开部署。

<!-- 文稿复核：使用 dedicated grep 扫描，中文半角标点、中文长破折号、编号／量词标题、元话语及对比句式均为 0。事实回读保留 B1 的 8 条 GET 路由、1～100／0～100000 分页、安全行为及 3 个任务、19 张表共 137 行读取前后不变；启动初始化与 HTTP 只读分开说明，ASGI 验收不表述为部署。B2／V3 接入已实现，链接到本机启动文档，真实浏览器最短导航已通过，自动测试覆盖与未人工验证范围见启动文档。 -->
