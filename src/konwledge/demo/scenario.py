"""The scripted demo story: "the life of one piece of knowledge".

A fictional *service deployment guide* goes through three versions, while an
on-call FAQ keeps quoting the old port. Every step happens at a fixed time, so
the demo library (and the snapshot exported from it) is identical on every run.

Diff coverage of the story:

* guide v1 -> v2: port changed (modified), canary release added (added),
  "上线验证" heading added (added), health check moved under it (moved),
  rollback script dropped (removed), everything else unchanged;
* guide v2 -> v3: port and alerting rewritten (modified).

Status coverage at the end: active, stale (the FAQ port answer), superseded
(older guide versions), and deleted (the rollback script).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))

GUIDE = "deployment-guide.md"
FAQ = "oncall-faq.md"

DEMO_TITLE = "Knowledge CI 演示：一条知识的一生"


@dataclass(frozen=True, slots=True)
class ImportFile:
    """Write a corpus file to ``target`` and import it."""

    target: str
    corpus: str


@dataclass(frozen=True, slots=True)
class MarkStale:
    """Mark the current unit with ``heading`` in ``target`` as stale."""

    target: str
    heading: str
    reason: str


@dataclass(frozen=True, slots=True)
class Refresh:
    """Confirm the current stale unit with ``heading`` in ``target``."""

    target: str
    heading: str
    reason: str


DemoAction = ImportFile | MarkStale | Refresh


@dataclass(frozen=True, slots=True)
class DemoStep:
    """One narrated, timestamped action of the demo story."""

    at: datetime
    title: str
    description: str
    action: DemoAction


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=SHANGHAI)


DEMO_STEPS: tuple[DemoStep, ...] = (
    DemoStep(
        at=_at(5, 10),
        title="导入《服务部署手册》v1",
        description="手册写明服务端口 8000。每个知识片段都记录了字符区间和章节路径。",
        action=ImportFile(GUIDE, "deployment-guide.v1.md"),
    ),
    DemoStep(
        at=_at(5, 10, 30),
        title="导入《值班 FAQ》",
        description="FAQ 同样写着端口 8000，此时两份资料口径一致。",
        action=ImportFile(FAQ, "oncall-faq.v1.md"),
    ),
    DemoStep(
        at=_at(12, 14),
        title="发布《服务部署手册》v2",
        description=(
            "端口改为 8080，新增灰度发布，健康检查挪到上线验证下，删除旧回滚脚本。"
            "五类变化齐全，只有新增和修改的片段重算向量。"
        ),
        action=ImportFile(GUIDE, "deployment-guide.v2.md"),
    ),
    DemoStep(
        at=_at(15, 9, 30),
        title="标记“监控告警”过期",
        description="告警阈值很久没人复核，先标为 stale，检索时可以降权或提示。",
        action=MarkStale(GUIDE, "监控告警", "告警阈值待 SRE 复核"),
    ),
    DemoStep(
        at=_at(16, 11),
        title="标记“健康检查”过期",
        description="有人怀疑探针路径已经变了，先标为 stale。",
        action=MarkStale(GUIDE, "健康检查", "探针路径待确认"),
    ),
    DemoStep(
        at=_at(17, 15),
        title="确认“健康检查”仍然有效",
        description="确认 /healthz 没变，refresh 后恢复 active，过程留在修订链里。",
        action=Refresh(GUIDE, "健康检查", "已确认探针路径仍为 /healthz"),
    ),
    DemoStep(
        at=_at(19, 16),
        title="发布《服务部署手册》v3",
        description="端口说明补充 PORT 环境变量，告警阈值改写。内容更新自动清除过期标记。",
        action=ImportFile(GUIDE, "deployment-guide.v3.md"),
    ),
    DemoStep(
        at=_at(20, 11),
        title="标记 FAQ 端口答案过期",
        description="FAQ 仍写 8000，与手册 v2 起的 8080 不一致。U12 会把它识别为冲突候选。",
        action=MarkStale(FAQ, "服务端口是多少？", "与《服务部署手册》v2 起的 8080 端口不一致"),
    ),
)
