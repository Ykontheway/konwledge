# 知识变更自动评测

自动评测默认关闭。显式启用后，知识变更在原事务中创建持久化任务，提交成功后立即同步执行质量检查。首版用于本机、单库的变更后回归，不提供发布前审批或自动回滚。

## 先绑定自己的评测集

先按现有 `EvaluationSet` 格式制作并导入项目自己的评测集，期望答案使用稳定的 lineage ID。公开的 30 条验收基准用于验证工具，不替代业务评测集；冻结的 6 条 Demo 包含主动查询过期知识的用例，也不能直接当作默认严格质量策略的业务基线。

```bash
knowledge evaluation import your-evaluation.json -d .konwledge/knowledge.db
knowledge auto-evaluation enable --set-id <set-id> --version 1 \
  -d .konwledge/knowledge.db
knowledge auto-evaluation status -d .konwledge/knowledge.db
knowledge auto-evaluation run -d .konwledge/knowledge.db
```

`enable` 固定数据集 ID、版本、内容 hash、检索参数和策略，仅保存配置，不认定已有知识合格。可用 `--policy-json` 指定质量策略，用 `--baseline-json` 和 `--regression-policy-json` 指定经人工审核的回归基线与策略。缺少基线时不能启用回归策略；新评测集版本需要显式重新绑定。

默认门禁要求 TopK 为 5、Recall 至少 0.80、MRR 至少 0.65、检索证据引用覆盖率为 100%、过期或退役命中为 0；有无答案用例时，其准确率要求为 100%。默认拒绝向量降级、启用 `include_stale` 的评测用例和没有可评估答案的空指标。可通过策略显式调整门槛，但调整后的结果不能再宣称满足默认验收。

自动评测首版只支持 lexical 检索。向量和混合检索需要持久化、明确配置的 provider；系统会拒绝启用这些模式，不用临时 hash 向量代替真实语义评测。独立质量 SDK 可以使用调用方注入的检索服务。

## 哪些变更会触发

通过正式 SDK 或 CLI 写入以下 revision 的事务会触发：

- 文件首次发布、内容更新，以及重新发布历史版本。
- 对话候选人工批准；待审核导入和拒绝不触发。
- SQLite／PostgreSQL Schema 新版本发布；捕获不读取业务行。
- 知识标记过期、刷新，以及来源时效策略更新。

同一外层事务合并为一个任务；嵌套事务提交后等待外层提交，回滚不留下任务。原样重导、重复终态审批、查询、评测集导入，以及只读 HTTP 请求不触发评测。直接改 SQLite 行、绕过 revision 的写入不属于自动触发接口。

```bash
knowledge import ./knowledge -d .konwledge/knowledge.db
knowledge auto-evaluation history -d .konwledge/knowledge.db
knowledge auto-evaluation show <job-id> -d .konwledge/knowledge.db
knowledge auto-evaluation retry <job-id> -d .konwledge/knowledge.db
knowledge auto-evaluation recover -d .konwledge/knowledge.db
knowledge auto-evaluation disable -d .konwledge/knowledge.db
```

变更命令把新增任务摘要写到 stderr，普通结果或 JSON 仍写到 stdout。任一新增任务未通过时，CLI 返回 `3`，同时明确提示 `knowledge committed; evaluation did not pass`。这个退出码表示知识已提交但评测未通过，不能当作导入未发生而盲目重试。SDK 保持正常返回，通过 `library.automatic_evaluation.history()` 查看评测结果。

## 结果和恢复

| 状态 | 含义 |
| --- | --- |
| `queued` | 已持久化，尚未执行 |
| `running` | 已领取，持有租约 |
| `passed` | 当前任务对应的知识状态通过所有检查 |
| `failed` | 门禁失败或运行异常，保留安全错误码 |
| `blocked` | 数据集或配置完整性不满足要求 |
| `superseded` | 配置或知识已变化，旧结果不能继续代表当前状态 |

任务保存评测时间、配置版本、数据集 hash、知识 revision 指纹和触发 revision ID。运行前后检查绑定，最终写入结果时在 SQLite 写事务中再次验证；租约 CAS 防止重复领取和旧 worker 迟写。进程中断后，可用 `recover` 回收超过默认 10 分钟租约的任务；尝试次数和恢复记录保留。`retry` 新增关联任务，不覆盖已有终态记录。

任务报告只保存指标、检查状态和安全错误码，不保存源文、来源路径、评测 query 或原始 provider 异常。评测集和人工基线本身仍可能含有 query，按私密数据管理。基线从不自动更新；已有 `evaluation baseline` 命令仍是人工审核后显式执行的操作。

## 当前边界

同步评测会增加变更命令耗时。首版不包含后台调度服务、跨库并行、增量用例选择或自动重新评估因时间流逝产生的过期状态；需要显式运行时效扫描或评测。一次通过只证明该任务的固定评测时间与知识指纹，不保证未来变更仍然通过。

引用覆盖只验证命中证据能构造并校验精确引用，不判断事实正确或生成答案忠实度。本次保持 U18 HTTP 请求协议和 Snapshot V1 不变，不新增 HTTP 写路由。1,000 单元性能验收独立运行，不把小库业务变更耗时当作该性能基准。更多验收口径见 [后端验收](acceptance.md)。

<!-- 文稿检查：半角中文标点、标题编号、元话语、B2 句式和长破折号均为 0 处；触发范围、失败不回滚、基线人工更新和状态语义与实现逐项核对。 -->
