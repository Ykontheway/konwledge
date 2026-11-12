# 数据库 schema 捕获

U20 把 SQLite 或 PostgreSQL 的结构元数据保存为不可变版本，并把当前版本物化为可检索的 knowledge units。捕获过程只读取 catalog，不复制业务行，也不会调用 LLM。

版本号仍是 `0.1.0`，U20 处于 Unreleased 状态。

## 安装

SQLite 捕获只用核心依赖：

```bash
uv sync --locked --dev
```

PostgreSQL 捕获需要可选的 psycopg 3 驱动：

```bash
uv sync --locked --dev --extra postgres
# 在源码仓库中安装时可用：python -m pip install '.[postgres]'
```

核心安装不会静态导入 psycopg。没有安装 `[postgres]` 时，SQLite 和其他核心功能仍可使用；执行 PostgreSQL 捕获会收到不含 DSN 的提示。

## 用合成 SQLite 文件试用

下面的例子只在临时目录创建合成数据。`source.sqlite` 是待检查的数据库，`knowledge.db` 是 Knowledge Library 元数据库，两者必须是不同文件。示例结束后删除临时目录即可，不要提交生成的数据库。

```bash
workdir="$(mktemp -d)"
python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.executescript("CREATE TABLE accounts(id INTEGER PRIMARY KEY, display_name TEXT); CREATE INDEX ix_accounts_name ON accounts(display_name); CREATE TABLE sentinel(payload TEXT); INSERT INTO sentinel VALUES (\"business-row-is-not-captured\");"); c.close()' "$workdir/source.sqlite"

uv run knowledge database capture-sqlite "$workdir/source.sqlite" \
  --name "Synthetic account schema" --author "local-review" \
  --timeout 5 --json -d "$workdir/knowledge.db"

python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("ALTER TABLE accounts ADD COLUMN active_field TEXT"); c.commit(); c.close()' "$workdir/source.sqlite"

uv run knowledge database capture-sqlite "$workdir/source.sqlite" \
  --name "Synthetic account schema" --json -d "$workdir/knowledge.db"
```

第一次捕获创建 version 1。schema 没变时重复执行会返回同一版本，并把 `created` 设为 `false`。结构变化会追加新版本。若后续结构恢复为历史版本，服务复用已有 document，但仍追加一次捕获记录，例如 version 3；历史不会被覆盖。

## PostgreSQL 凭据只从环境变量读取

CLI 不接受 `--dsn`。先把连接 URL 放入环境变量，再把变量名传给 `--dsn-env`：

```bash
export KNOWLEDGE_CAPTURE_DSN='postgresql+psycopg://capture_role:password@127.0.0.1:5432/app'
uv run knowledge database capture-postgres \
  --dsn-env KNOWLEDGE_CAPTURE_DSN \
  --name "Application public schema" \
  --schema public --json -d .konwledge/knowledge.db
```

环境变量名必须匹配 `[A-Za-z_][A-Za-z0-9_]*`。程序完成其他输入校验后才读取 DSN；输出、错误、来源 URI 和默认 `Source.metadata` 都不保存或打印 URL、用户名、密码及查询参数。请不要把真实凭据写进 shell history、文档、日志或仓库。

PostgreSQL 角色应只具备连接目标数据库、使用目标 schema 和读取 catalog 所需的最低权限。捕获使用 psycopg 3、`REPEATABLE READ READ ONLY` 事务、5 秒连接超时、5 秒 statement timeout 和 lock timeout。它不提供 SSO，也不验证 `--author` 对应的真实身份；`--author` 只是审计归属字段。

## 查看历史、导出和比较

捕获结果里的 `source_id` 用于后续只读命令：

```bash
uv run knowledge database list --json -d .konwledge/knowledge.db
uv run knowledge database history <source-uuid> --json -d .konwledge/knowledge.db
uv run knowledge database show <source-uuid> --version 2 --json \
  -d .konwledge/knowledge.db
uv run knowledge database export <source-uuid> --version 2 \
  --output schema-v2.json -d .konwledge/knowledge.db
uv run knowledge database diff <source-uuid> \
  --from-version 1 --to-version 2 --json -d .konwledge/knowledge.db
```

`export` 默认拒绝覆盖已有文件，只有显式传入 `--overwrite` 才会替换。省略 `--output` 时，规范 JSON 写到 stdout。`show --json` 返回带捕获时间和 document ID 的完整记录；`export` 只返回可移植 snapshot。

`diff` 按 `(namespace, table name, object type)` 识别表，并报告表和列的新增、删除、修改，以及索引、外键、注释和表选项变化。表名保持不变时，knowledge unit lineage 保持稳定。重命名不会被猜测为同一对象，而会显示为删除旧表和新增新表。

成功退出码是 `0`。连接、运行时或文件写入错误返回 `1`；输入校验、未找到版本、系统 schema、冲突及覆盖保护返回 `2`。失败输出会收敛成固定诊断，不回显 DSN、用户名、原始 SQL 或底层驱动错误。

## 会捕获什么

SQLite 捕获表和视图、列、主键位置、生成列标记、索引、外键、`WITHOUT ROWID` 和 `STRICT`。连接使用 `mode=ro`，authorizer 只允许 catalog 读取和一组元数据 PRAGMA，并用进度处理器限制执行时间。CLI 会拒绝缺失文件、目录、Knowledge Library 元数据库及它的 symlink 或 hardlink 别名。

PostgreSQL 只捕获显式指定的一个非系统 schema，包括表、视图及定义、列、主键位置、索引表达式与部分索引谓词、外键及可获得的注释。`postgresql://`、`postgres://` 和 `postgresql+psycopg://` 均归一化为 psycopg 3 驱动；`information_schema`、`pg_catalog` 和 `pg_*` schema 会被拒绝。

每个完整 snapshot 都有规范 JSON 和 SHA-256 content hash。上限是 1,000 张表、10,000 列和 10 MiB；超过任一上限会整体失败，不保存不完整 snapshot。外部 catalog 先完整采集一次，随后才打开本地元数据库事务，因此失败的源捕获不会新建 Knowledge Library 数据库，也不会因二次采集得到并发变化后的另一份内容。

## 安全边界和限制

- 不读取、复制或镜像业务行。触发器、存储过程、函数、角色、权限、扩展、事件及数据库运行指标不在 snapshot 中。
- schema 元数据本身也可能敏感。表名、列名、注释、默认值、索引表达式、谓词及可还原的 DDL 片段可能包含内部信息；导出文件和 Knowledge Library 数据库都应按敏感本地制品管理。
- 捕获只保证本次 catalog 读取使用只读连接或事务。它不管理 PostgreSQL 账号、不轮换凭据、不提供 SSO，也不替代数据库侧审计。
- SQLite 源文件和 Knowledge Library 元数据库不能是同一 inode。请避免在捕获期间替换源文件；CLI 会在持久化前只采集一次，但无法为外部文件系统提供分布式锁。
- PostgreSQL schema 名作为标识符交给 SQLAlchemy inspector，不接受系统 schema。仍应给捕获角色设置数据库侧权限和资源限制。
- `SourceKind.DATABASE` 尚未加入前端使用的 V1 snapshot 合同。V1 导出遇到数据库来源会明确失败，不会改标成其他来源，也不会修改 demo golden、JSON Schema 或前端生成类型。
- U20 没有新增 HTTP 写接口。已有本机 API 仍是只读检索接口，只能查询已经发布的当前 schema units。
