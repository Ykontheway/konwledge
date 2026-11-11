# 对话导入与人工审核

U19（Unreleased）把原始对话作为不可变版本保存。导入只做保守、确定性的候选提取；候选必须经过人工审核，才可能进入检索。系统不会调用 LLM，也不会把整段聊天自动当成事实。

## JSON schema

导入文件必须是 UTF-8 JSON，根对象字段如下：

| 字段 | 约束 |
| --- | --- |
| `schema_version` | 当前固定为 `1` |
| `conversation_id` | 非空字符串，最长 256 个字符 |
| `version` | 从 `1` 开始的正整数；同一 ID 与版本内容不可变 |
| `created_at` | 可选，带时区的 ISO 8601 时间 |
| `messages` | 1 到 10,000 条消息，消息 ID 在该版本内唯一 |
| `metadata` | 可选、有限深度和数量的 JSON 对象 |

每条消息包含 `id`、`role`、`content`，可选 `timestamp` 和 `metadata`。`role` 只能是 `user`、`assistant`、`system`、`tool`。原始 `content` 不做空白归一化，导出时保持内容和字符偏移。

输入上限为 10 MiB。CLI 在打开数据库前检查大小、UTF-8、重复 JSON key 和 schema；错误消息不会回显整份私有对话。

完整合成示例见 [`examples/conversation-demo.json`](../examples/conversation-demo.json)。示例内容只用于演示，不代表系统已经验证其中事实。

## 导入、查看与导出

```bash
knowledge conversation import examples/conversation-demo.json -d .konwledge/knowledge.db
cat examples/conversation-demo.json | knowledge conversation import - --json \
  -d .konwledge/knowledge.db
knowledge conversation list --json -d .konwledge/knowledge.db
knowledge conversation show synthetic-demo-001 --json -d .konwledge/knowledge.db
knowledge conversation show synthetic-demo-001 --version 1 -d .konwledge/knowledge.db
knowledge conversation history synthetic-demo-001 --json -d .konwledge/knowledge.db
knowledge conversation export synthetic-demo-001 --version 1 \
  --output exported-conversation.json -d .konwledge/knowledge.db
```

`show` 和 `export` 省略 `--version` 时取最新版本。`export` 输出可再次导入的规范 JSON；目标文件已存在时必须显式加 `--overwrite`。同一 ID 与版本重复导入相同内容是幂等操作，换成不同内容会失败。

## 候选提取与审核

提取器只读取 `user` 和 `assistant` 消息中以 `Fact:`、`事实：`、`Decision:` 或 `决策：` 开头的单行。空内容和问句会跳过，`system` 与 `tool` 消息不会生成候选。这个规则只识别显式标记，不判断内容真假。

```bash
# 默认只列 pending；--status all 取消状态过滤。
knowledge candidate list --json -d .konwledge/knowledge.db
knowledge candidate list --status approved --conversation-id synthetic-demo-001 \
  --version 1 -d .konwledge/knowledge.db
knowledge candidate list --status all -d .konwledge/knowledge.db
knowledge candidate show <candidate-uuid> --json -d .konwledge/knowledge.db
knowledge candidate history <candidate-uuid> --json -d .konwledge/knowledge.db

knowledge candidate approve <candidate-uuid> --reviewer alice@example.com \
  --reason "已对照来源" -d .konwledge/knowledge.db
knowledge candidate reject <candidate-uuid> --reviewer alice@example.com \
  --reason "内容不准确" -d .konwledge/knowledge.db
```

`--reviewer` 是调用者填写的审计归属，不是身份认证。批准理由可省略；拒绝理由必填且不能是空白。命令不会交互式询问缺失参数。

审核决定是终态：已批准候选不能改为拒绝，已拒绝候选也不能改为批准。重复提交相同决定返回原审计记录，不会重写最初的审核人和理由。导入新版本不会自动撤销旧版本里已经批准的知识；拒绝操作在当前切片也没有撤销入口。

批准会在一个事务中保存来源、文档、精确候选文本、发布记录与审核审计。待审核和已拒绝候选不进入 `search` 或 `context`。因此，原始聊天可能包含私密内容；`conversation show --json`、导出文件、候选详情和本地数据库都应按私密数据管理。

`V1 demo snapshot` 是提供给 V2 前端的冻结合同，来源类型仍只有 `url`、`file`、`repository` 和 `manual`。待审核与已拒绝候选不创建来源，不影响现有 demo 导出。批准后的知识保留 `conversation` 来源和完整 provenance；V1 snapshot 导出会明确失败，不会省略或改标为 `manual`。这类知识请通过 context API 读取，或等待有独立版本号的 snapshot 扩展。本次不重新生成 demo golden、JSON Schema 或前端类型。

## 进程退出码

| 代码 | 含义 |
| --- | --- |
| `0` | 操作成功 |
| `1` | 文件 I/O、存储或其他运行时失败 |
| `2` | 参数、JSON/schema、未找到、版本冲突或终态冲突 |

JSON 模式只在成功时向 stdout 输出完整模型；诊断写入 stderr，不打印 traceback、SQL 或交互提示。
