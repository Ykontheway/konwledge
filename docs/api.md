# 本机上下文 API（U18，Unreleased）

U18 为已有知识库提供可选 HTTP 传输，不改变 `0.1.0` 版本号。v0.1.0 仍是已准备、未发布的 alpha 候选；本文不代表 PyPI 发布、远端 CI 通过或服务部署。

## 安装与启动

从仓库或已验证的本地 wheel 安装（不要假定 PyPI 已有该版本）：

```bash
python -m pip install '.[api]'
# 或
python -m pip install 'dist/konwledge-0.1.0-py3-none-any.whl[api]'
knowledge import examples --database .konwledge/knowledge.db
knowledge serve --database .konwledge/knowledge.db --host 127.0.0.1 --port 8000
```

- `[api]` 仅增加 FastAPI、Uvicorn；核心安装不需要它们，`knowledge --version`、`--help`、`serve --help` 可正常运行。
- 默认库为 `.konwledge/knowledge.db`，可用 `--database`/`-d` 改变；必须是**已有文件**。`serve` 不创建父目录或缺失数据库。SQLite URI 使用 `mode=rw`，校验后文件被移除也不会自动创建空库。
- `--host` 仅允许 `127.0.0.1`、`::1`、`localhost`。`localhost` 明确映射到 `127.0.0.1`，不通过 DNS 决定绑定地址。拒绝 `0.0.0.0`、其他 IP/主机名；端口范围 `1..65535`。这些校验在打开库之前完成。
- API 密钥只从环境变量 `KNOWLEDGE_API_KEY` 读取，**没有密钥值 CLI 参数**。建议通过本机凭据管理器/进程环境注入，不把真实值写到 shell 历史、日志或仓库。未设置表示本机无鉴权；显式设置为空或仅含空白时拒绝启动，不会降级为无鉴权。非空值原样匹配，不裁剪。
- Uvicorn 使用传入的 app 对象阻塞运行，关闭 access log，不自动启动后台任务、reload 或多 worker；退出或失败时释放 engine。参数错误退出 `2`，缺少 extra 或启动/运行失败退出 `1`。
- `serve` 启动会调用 `create_schema()`：允许为**已有文件**创建缺失表、执行已有 additive upgrade 和 FTS 初始化/回填，故启动阶段可能写库。HTTP 请求本身只读，不导入资料、更新状态、写引用、修复向量或产生模型推理。
- 默认只配置 lexical。vector/hybrid 需要调用者向 `KnowledgeLibrary` 注入已配置的 `IndexingService`，再用 `create_app(library)` 组合；`serve` 不自动配置向量 provider，不调用付费 API。

API 只适用于可信本机环境，不是 TLS 服务、多租户隔离、托管模型或生产 SLA。不要通过反向代理或隧道把知识库无意暴露到公网。返回的引用可能含原文、文件路径与标识符，应按本地敏感数据处理。

## 路由与鉴权

| 路由 | 返回 | 鉴权 |
| --- | --- | --- |
| `GET /health` | `{"status":"ok","version":"0.1.0"}` | 始终公开，仅健康/版本 |
| `GET /v1/models` | 一个模型 `knowledge-context` | 设置密钥时需 Bearer |
| `POST /v1/context` | 原生 `ContextPacket` JSON | 设置密钥时需 Bearer |
| `POST /v1/chat/completions` | OpenAI chat completion 外壳 | 设置密钥时需 Bearer |

鉴权头为 `Authorization: Bearer <本机密钥>`。无密钥模式提供 `/docs`、`/redoc`、`/openapi.json`；启用密钥时关闭这些文档路由，避免意外公开。嵌入式调用者可用 `app.openapi()` 获取 schema，且由调用者管理注入库的 engine 生命周期。`create_app()` 不创建数据库、不关闭 engine，FastAPI 仅在实例化时导入。

## 原生请求：`ContextRequest`

请求为 `query` 加平铺的 `ContextControls`，不是嵌套 `context`：

```json
{
  "query": "deployment",
  "source_ids": [],
  "as_of": "2026-11-11T12:00:00Z",
  "mode": "lexical",
  "top_k": 5,
  "candidate_limit": 50,
  "rrf_k": 60,
  "max_chars": 8000,
  "max_units": 10,
  "include_stale": false
}
```

| 字段 | 默认值与约束 |
| --- | --- |
| `query` | 必填字符串，1..100000 字符，不得仅为空白；按字面检索而非执行 FTS/SQL |
| `source_ids` | `null`；UUID 数组最多 **1 项**，空数组/`null` 不过滤。核心目前仅支持单个 `source_id`，不是多来源 OR |
| `as_of` | `null` 表示当前时间；提供时必须为带时区的 ISO 8601 字符串，不接受 naive 时间、Unix 数字或布尔值 |
| `mode` | `lexical`；也可指定 `vector`/`hybrid`，但必须有调用者注入的向量配置 |
| `top_k` | `5`；严格整数 `1..100` |
| `candidate_limit` | `null`，核心使用 `max(50, top_k)`；提供时严格整数 `1..1000` 且不小于 `top_k` |
| `rrf_k` | `60`；严格整数 `1..100000` |
| `max_chars` | `8000`；严格整数 `0..100000`，以 Python Unicode code point 衡量 `context_text`（非模型 token） |
| `max_units` | `10`；严格整数 `1..100` |
| `include_stale` | `false`；严格布尔值，仅显式启用才包含 stale head 单元 |

所有未知字段拒绝；不会默默忽略或转换不支持的选项。原生返回包含 `query`、`evaluated_at`、`mode`、`context_text`、`blocks[].citation`、`skipped`、预算与缺失向量诊断等。每块保留完整单元及精确引文/字符偏移；不会为满足预算截断引文。无匹配或预算为零时返回有效空 packet，不编造答案。原文是未受信证据，不构成下游 prompt injection 防护承诺。

## OpenAI 兼容上下文接口

客户端 `base_url` 必须为本机 `/v1`，例如 `http://127.0.0.1:8000/v1`。请求固定模型 `knowledge-context`，只允许**恰好一条 `user` 字符串消息**；该内容就是 query。检索控制放在 `context`：

```json
{
  "model": "knowledge-context",
  "messages": [{"role": "user", "content": "deployment"}],
  "stream": false,
  "context": {"as_of": "2026-11-11T12:00:00Z", "max_units": 2}
}
```

`context` 使用上述 `ContextControls`，**没有 `query` 字段**。缺省或 `null` 使用默认控制。响应 `choices[0].message.role` 为 `assistant`，其 `content` 是序列化的 **完整 `ContextPacket` JSON 字符串**，不是自然语言答案。`finish_reason` 为 `stop`，不返回或伪造 `usage`/token 统计。

不支持 `stream=true`、tools/tool calls、sampling（包括 `temperature`、`top_p`）、`max_tokens`、`response_format`、Responses API、system/assistant 消息、对话历史、多消息、图像/多模态、真实模型推理。`stream=false` 可省略或显式提供。未知选项返回错误，不能靠客户端适配器删除真实正文来伪装兼容。

### 官方 OpenAI Python SDK

SDK 是客户端工具，不是 Konwledge 运行时依赖。另行安装 `openai`，按本机环境取密钥：

```python
import os
from openai import OpenAI
from konwledge.context import ContextPacket

with OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key=os.environ.get("KNOWLEDGE_API_KEY", "local-no-auth"),
    max_retries=0,
) as client:
    completion = client.chat.completions.create(
        model="knowledge-context",
        messages=[{"role": "user", "content": "deployment"}],
        stream=False,
        extra_body={"context": {"as_of": "2026-11-11T12:00:00Z", "max_units": 2}},
    )
    packet = ContextPacket.model_validate_json(completion.choices[0].message.content)
```

### LangChain `ChatOpenAI`

另行安装 `langchain-openai`；显式省略 temperature 并关闭 streaming/Responses API：

```python
import os
from langchain_openai import ChatOpenAI
from konwledge.context import ContextPacket

chat = ChatOpenAI(
    model="knowledge-context",
    base_url="http://127.0.0.1:8000/v1",
    api_key=os.environ.get("KNOWLEDGE_API_KEY", "local-no-auth"),
    temperature=None,
    streaming=False,
    use_responses_api=False,
    max_retries=0,
    extra_body={"context": {"as_of": "2026-11-11T12:00:00Z", "max_units": 2}},
)
message = chat.invoke("deployment")
packet = ContextPacket.model_validate_json(message.content)
```

不要叠加 agent、历史消息、工具绑定或采样参数。SDK 版本升级后应再次执行验收测试，不应放宽服务端 schema 来迁就默认参数。开发锁包含实际验收所用 SDK；wheel 运行时依赖不包含 OpenAI、LangChain 或 HTTPX。

## 错误与验证

HTTP 错误为统一 `{"error":{"message":...,"type":...,"param":...,"code":...}}`：

- `400`：非法输入/不支持字段（`validation_error`、`unsupported_option`），或未配置向量模式（`retrieval_mode_unavailable`）。
- `401`：缺失/错误 Bearer 密钥（`invalid_api_key`）。
- `503`：vector/hybrid 运行时不可用（`vector_retrieval_unavailable`）。
- `500`：一般内部失败；不反射异常原文、SQL、路径或请求内容。

默认 dev 组安装 FastAPI/Uvicorn（静态类型和默认测试需要）、HTTPX、官方 OpenAI SDK、LangChain SDK。全部验收无需监听端口、外部模型或 provider 凭据：

```bash
uv sync --locked --dev
uv run pytest tests/test_api.py tests/test_api_cli.py tests/test_api_clients.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run python scripts/verify_distribution.py --output-dir dist/u18-verification
```

`test_api_clients.py` 用真实 SDK 和 HTTPX `MockTransport` 将完整 method/path/query/body/headers 转发给 FastAPI `TestClient`；固定 `as_of` 和所有检索控制，验证原生 JSON 与两个客户端 `assistant.content` 解析出的 packet 相等，并验证不支持选项确实被拒绝、没有使用量伪造。`test_api_cli.py` 替换 `uvicorn.run`，验证默认/自定义库、loopback/端口、密钥与缺依赖边界、错误不落盘、engine 释放。

分发验证对 wheel、sdist 分别先进行 core-only 隔离 smoke，再安装同一产物的 `[api]`。API smoke 复制到仓库外、通过 `python -I` 执行，证明 API 模块来自隔离环境 `site-packages`、没有 SDK/HTTPX，直接驱动 ASGI 并替换 `uvicorn.run`，验证只读请求和启动释放。summary 的 `api_extra` 保存结果。CI 仍只在 PR/`main` 触发；功能分支 push 不代表远端 CI 已运行。
