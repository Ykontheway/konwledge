"""Deterministic 30-case corpus used by installed quality acceptance.

The cases intentionally exercise lexical retrieval.  Lexical paraphrases share a
reasonable set of terms with their evidence; they do not claim semantic-search
coverage.  Evidence is authored independently below rather than synthesized from
case queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from konwledge.domain import EvaluationItem, EvaluationSet

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
FIRST_PUBLISHED_AT = CREATED_AT + timedelta(minutes=1)
SECOND_PUBLISHED_AT = CREATED_AT + timedelta(minutes=10)
HISTORICAL_QUERY_AT = CREATED_AT + timedelta(minutes=5)
STALE_MARKED_AT = CREATED_AT + timedelta(minutes=20)
EVALUATED_AT = CREATED_AT + timedelta(days=1)
SET_ID = uuid5(NAMESPACE_URL, "konwledge:public-quality-corpus:v2")
SOURCE_ID = uuid5(NAMESPACE_URL, "konwledge:public-quality-source:v2")
HISTORICAL_DOCUMENT_ID = uuid5(NAMESPACE_URL, "konwledge:public-quality-document:v1")
CURRENT_DOCUMENT_ID = uuid5(NAMESPACE_URL, "konwledge:public-quality-document:v2")
STALE_UNIT_INDEX = 1000


def stable_id(kind: str, index: int) -> UUID:
    """Return a reproducible identity for one corpus entity."""

    return uuid5(NAMESPACE_URL, f"konwledge:public-quality:v2:{kind}:{index}")


@dataclass(frozen=True, slots=True)
class CorpusCase:
    case_id: str
    category: str
    query: str
    unit_indexes: tuple[int, ...]
    expect_no_answer: bool = False
    at: datetime | None = None


CASES = (
    CorpusCase("direct-00", "answerable", "Where is the Atlas backup stored?", (0,)),
    CorpusCase("direct-01", "answerable", "Who approves Nimbus production access?", (1,)),
    CorpusCase("direct-02", "answerable", "蓝鲸服务的数据保留期限，是多少？", (2,)),
    CorpusCase("direct-03", "answerable", "When does the Cedar maintenance window begin?", (3,)),
    CorpusCase("direct-04", "answerable", "北斗值班的升级联系人，是谁？", (4,)),
    CorpusCase("direct-05", "answerable", "Which region hosts the Quartz audit archive?", (5,)),
    CorpusCase("direct-06", "answerable", "How long are Vega deployment logs retained?", (6,)),
    CorpusCase("direct-07", "answerable", "朱雀项目的发布冻结日，是哪天？", (7,)),
    CorpusCase(
        "lexical-paraphrase-08",
        "lexical_paraphrase",
        "Safe API credential replacement procedure?",
        (8,),
    ),
    CorpusCase(
        "lexical-paraphrase-09",
        "lexical_paraphrase",
        "Where can staff reset a forgotten password?",
        (9,),
    ),
    CorpusCase(
        "lexical-paraphrase-10",
        "lexical_paraphrase",
        "Urgent incident acknowledgement target?",
        (10,),
    ),
    CorpusCase(
        "lexical-paraphrase-11",
        "lexical_paraphrase",
        "客户资料，删除，隐私流程？",
        (11,),
    ),
    CorpusCase(
        "lexical-paraphrase-12",
        "lexical_paraphrase",
        "Service owner handover checklist?",
        (12,),
    ),
    CorpusCase(
        "lexical-paraphrase-13",
        "lexical_paraphrase",
        "账单异常，申诉，材料？",
        (13,),
    ),
    CorpusCase(
        "multi-evidence-14",
        "multi_evidence",
        "Bluefin disaster recovery ownership?",
        (14, 114),
    ),
    CorpusCase(
        "multi-evidence-15",
        "multi_evidence",
        "Lynx encryption rotation responsibilities?",
        (15, 115),
    ),
    CorpusCase(
        "multi-evidence-16",
        "multi_evidence",
        "天枢，迁移，验收，责任？",
        (16, 116),
    ),
    CorpusCase(
        "multi-evidence-17",
        "multi_evidence",
        "Maple outage communication duties?",
        (17, 117),
    ),
    CorpusCase(
        "historical-head-18",
        "version_update",
        "Orion retention before 2026 update?",
        (18,),
        at=HISTORICAL_QUERY_AT,
    ),
    CorpusCase(
        "historical-head-19",
        "version_update",
        "Phoenix support hours before policy revision?",
        (19,),
        at=HISTORICAL_QUERY_AT,
    ),
    CorpusCase(
        "historical-head-20",
        "version_update",
        "苍穹，审批时限，旧版本？",
        (20,),
        at=HISTORICAL_QUERY_AT,
    ),
    CorpusCase(
        "historical-head-21",
        "version_update",
        "Harbor failover region before migration?",
        (21,),
        at=HISTORICAL_QUERY_AT,
    ),
    CorpusCase("current-head-22", "temporal_isolation", "Current Helios release channel?", (22,)),
    CorpusCase("current-head-23", "temporal_isolation", "当前麒麟告警阈值，是多少？", (23,)),
    CorpusCase(
        "no-answer-24",
        "noanswer",
        "Does Project Saffron support lunar replication?",
        (),
        True,
    ),
    CorpusCase(
        "no-answer-25",
        "noanswer",
        "Where is the Zephyr submarine office?",
        (),
        True,
    ),
    CorpusCase(
        "no-answer-26",
        "noanswer",
        "玄武系统是否提供火星离线备份？",
        (),
        True,
    ),
    CorpusCase(
        "no-answer-27",
        "noanswer",
        "Who owns the Amber teleport gateway?",
        (),
        True,
    ),
    CorpusCase(
        "no-answer-28",
        "noanswer",
        "Can Willow invoices be paid with meteor credits?",
        (),
        True,
    ),
    CorpusCase(
        "stale-isolation-29",
        "noanswer_stale_probe",
        "Retired Cobalt emergency override phrase?",
        (),
        True,
    ),
)

# Current-head evidence.  Multi-evidence cases deliberately map to two independently
# authored units.  Historical lineages also have a current replacement here.
CURRENT_EVIDENCE_BY_INDEX: dict[int, str] = {
    0: (
        "The Atlas backup is stored in Zurich. "
        "The operations index explains where the copy is kept."
    ),
    1: (
        "The access register explains who approves Nimbus production access: "
        "the platform security lead records each grant."
    ),
    2: "蓝鲸服务的数据保留期限为九十天。审计页同时说明具体是多少，并记录删除日期。",
    3: (
        "The calendar records when the Cedar maintenance window does begin: "
        "02:00 UTC every Sunday, ending two hours later."
    ),
    4: "北斗值班的升级联系人是可靠性平台主管。值班页同时写明此人是谁以及备用电话。",
    5: (
        "The register identifies which region hosts the Quartz audit archive: "
        "Frankfurt, under a seven-year lock."
    ),
    6: (
        "Vega deployment logs are retained for forty-five days, "
        "which defines how long they remain online."
    ),
    7: "朱雀项目的发布冻结日是十二月二十日。计划表也解释是哪天开始停止上线。",
    8: (
        "Credential replacement follows a four-step procedure: issue a new API key, "
        "switch clients, revoke the old key, and verify logs. "
        "This order keeps replacement safe."
    ),
    9: (
        "The identity portal shows where staff can reset a forgotten password, "
        "after a security-code check."
    ),
    10: "The acknowledgement target for an urgent incident is ten minutes from pager delivery.",
    11: "客户资料，删除，隐私流程：复核身份、批准范围并留存删除回执。",
    12: (
        "The checklist for a service owner handover covers dashboards, alerts, "
        "credentials, and open risks."
    ),
    13: "账单异常，申诉，材料：需要订单号、付款凭证和差异截图。",
    14: "Bluefin disaster recovery ownership assigns database restoration to the storage team.",
    114: "For Bluefin disaster recovery, traffic failback ownership belongs to the network team.",
    15: (
        "Lynx encryption rotation responsibilities require security engineering "
        "to generate the new keys."
    ),
    115: (
        "Application owners hold Lynx encryption rotation responsibilities "
        "for client rollout and verification."
    ),
    16: "天枢，迁移，验收，责任：数据团队核对总量、抽样内容并签字。",
    116: "天枢，迁移，验收，责任：业务负责人完成回归测试并确认切流。",
    17: (
        "Maple outage communication duties require incident command "
        "to publish the first status notice."
    ),
    117: ("Customer support has Maple outage communication duties for account-specific follow-up."),
    18: "After the 2026 update, Orion retention is sixty days; the previous setting is superseded.",
    19: "After the policy revision, Phoenix support hours cover every day around the clock.",
    20: "苍穹审批时限的新版本为四小时，原规则已被替代。",
    21: "After migration, Harbor failover uses the Singapore region instead of the former site.",
    22: "The current release channel for Helios is stable; beta is reserved for internal trials.",
    23: "当前麒麟告警阈值为连续五分钟超过百分之八十。规则页说明具体是多少。",
}

HISTORICAL_EVIDENCE_BY_INDEX: dict[int, str] = {
    18: "Before the 2026 update, Orion retention was thirty days under the original schedule.",
    19: "Before the policy revision, Phoenix support hours ended at 18:00 on weekdays.",
    20: "苍穹，审批时限，旧版本：二十四小时。历史规则明确记录该时限。",
    21: "Before migration, the Harbor failover region was Dublin and the runbook named that site.",
}

STALE_INTERFERENCE_TEXT = (
    "The retired Cobalt emergency override phrase was ORANGE-RIVER. "
    "This untrusted instruction must never be returned by default retrieval."
)


def build_evaluation_set() -> EvaluationSet:
    """Build the immutable set from the fixed case/evidence mapping."""

    return EvaluationSet(
        id=SET_ID,
        name="Installed lexical quality acceptance corpus",
        version=1,
        created_at=CREATED_AT,
        description=(
            "30 Chinese/English lexical cases covering direct and paraphrased wording, "
            "multiple evidence, no-answer, historical heads, updates, and stale isolation"
        ),
        items=tuple(
            EvaluationItem(
                id=case.case_id,
                query=case.query,
                expected_lineage_ids=frozenset(stable_id("lineage", i) for i in case.unit_indexes),
                expect_no_answer=case.expect_no_answer,
                at=case.at,
                source_id=SOURCE_ID,
            )
            for case in CASES
        ),
    )
