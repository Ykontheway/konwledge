# 真实文档变更验收

本次已经用项目自身真实文档跑通“知识变更 → 自动评测 → 结果检查”。这是一项隔离的项目使用文档验收，不是生产业务库接入；无需外部存储服务。

## 使用方式

在仓库根目录执行：

```bash
uv sync --locked --dev
uv run python scripts/real_knowledge_acceptance.py \
  --output-dir .konwledge/real-knowledge-acceptance
```

runner 每次创建独立目录和文件型 SQLite，不复用旧库、不覆盖旧报告。运行目录包含 `knowledge.db`、固定 `evaluation.json`、原始备份、可变副本、各阶段报告及汇总 `report.json`。全部运行数据位于 gitignore 范围，不进入 Git。

源文档为 `docs/auto-evaluation.md` 与 `docs/acceptance.md`。它们先复制到隔离目录，后续变更只改副本，不改真实文档。首次导入后按稳定章节 lineage 建立评测集，冻结后保持 query、期望、版本和默认策略不变。没有用 6 条 Demo 或 30 条合成验收基准冒充业务数据，也没有为了让失败变通过而调整门槛。

## 本次实际结果

执行来源提交为 `acd1c2adf1eba3d29cde2413cd8feecc1d3428fc`。成功运行目录是 `.konwledge/real-knowledge-acceptance/20261113T062525019181Z-11d25751/`。具体运行标识仅用于定位本次记录，重新执行会生成新的目录、UUID 与时间。

初次导入 2 份文档、11 个知识单元。评测集包含 4 条用例，3 条有答案，其中一条要求两份证据；另有一条无答案问题。

| 阶段 | 触发方式 | 结果与退出码 | Recall@5 | MRR | 引用覆盖率 | 无答案准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 初始检查 | 显式 `quality run` | passed／0 | 1.0 | 1.0 | 1.0 | 1.0 |
| 正向变更 | CLI 导入修改后的文档副本，事务 hook 自动评测 | passed／0 | 1.0 | 1.0 | 1.0 | 1.0 |
| 故障注入 | 保留章节标题、移除该章节的关键证据正文，再导入 | failed／3 | 0.666667 | 0.666667 | 1.0 | 1.0 |
| 恢复 | 导入原始知识副本，自动重新评测 | passed／0 | 1.0 | 1.0 | 1.0 | 1.0 |

三次变更任务均为 `stale_hits=0`、`degraded=false`。故障错误码为 `QUALITY_GATE_FAILED`，Recall 低于默认 0.80 门槛。故障所需证据在普通检索和包含过期知识的检索中均为 0 hit，旧知识没有替当前版本补答案。

Precision@5 在正向和恢复阶段为 0.266667，故障时为 0.2。这些值未被省略为“全部质量指标满分”；当前默认绝对门禁不以 Precision@5 为下限，后续需用更丰富的实际问题检查检索噪声。

初始 `quality run` 是手动检查。之后三次任务由正式导入自动触发，没有手动调用 `auto-evaluation run`、`retry` 或 `drain` 替代变更触发。

## 验收检查了什么

- 正向变更的文档 checksum 和知识内容 hash 实际变化；query 仍命中首次冻结的预期 lineage。
- 故障阶段虽然返回退出码 3，知识已提交，当前文档 checksum 与故障副本一致。这个退出码不能按导入失败盲目重试。
- 恢复新增 passed 任务，保留原 failed 任务及报告；恢复发布原始知识版本，不伪造新版本号。
- 目标章节保留原始、正向与故障三个知识版本，恢复后原始版本重新 active，其余 superseded。
- 启用后原样导入的任务数保持 0 → 0，恢复后原样导入保持 3 → 3；知识快照、任务 ID 列表均未变化。
- 数据集 v1 hash 在三次任务间固定为 `1267746d99bf9114891b7667762d25056c6932a110b55db61093eb13df906787`，配置与策略也保持一致。
- 每个任务均检查触发 revision、知识指纹、检查项与指标，CLI `history/show` 的 JSON 与持久化结果一致。

## 证据与复现

`report.json` 是验收汇总；`positive-job.json`、`fault-job.json`、`restore-job.json` 保存三次自动任务；`history.json` 与 `lineages.json` 用于检查历史留存和版本变化。`initial-quality.json` 保留初始检查，`config.json` 与用例摘要用于核对固定配置。

对保存的库也可执行：

```bash
knowledge auto-evaluation history -d <run-directory>/knowledge.db --json
knowledge auto-evaluation show <job-id> -d <run-directory>/knowledge.db --json
```

源码与测试分别是 `scripts/real_knowledge_acceptance.py`、`tests/test_real_knowledge_acceptance.py`。新增 3 个测试在本次执行中通过，检查完整链路、报告隐私与来源不被修改，以及失败阶段使用固定期望的行为。

调试期间的未通过运行没有覆盖，仍保存在同一输出根目录；其中一次 runner 对知识快照 revision 的读取方式有误，已改为 repository 读取。这是验收脚本问题，本次没有修改生产服务。

## 结果的适用范围

这次通过默认绝对质量门禁。初始结果尚未得到人工明确接受，没有绑定回归 baseline，也不宣称完成 delta 回归。后续应先审核代表性问题和基线，再明确回归策略。

本次只验证 SQLite、本机单库与 lexical 检索。4 条用例足以检查这条执行链路，不能替代完整业务评测集。引用覆盖率只说明命中的证据可引用，不能证明事实正确或生成答案忠实。

验收汇总与任务报告不保存 query、源全文、绝对来源路径或数据库 URL。`evaluation.json`、SQLite 和文档副本仍属于完整运行数据，不应默认公开。没有连接用户生产数据库，也没有读取私密业务文件。

后续开发顺序和展示特色见 [前后端一体化演示路线](integrated-demo-roadmap.md)。

<!-- 文稿检查：机械扫描半角中文标点、长破折号、编号标题、元话语和 B2 句式均为 0 处。事实回读核对来源提交、固定 4 条用例、1.0／0.666667／1.0 指标、0／3／0 退出码和失败历史留存；项目文档验收不表述为生产业务验收。 -->
