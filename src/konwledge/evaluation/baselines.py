"""仅保留数据集快照、谱系排名和指标的可移植评估基线。

不保留检索命中原文、来源 URI、定位信息、文档 ID 或语料文件路径。
数据集中的查询、来源 UUID 和预期谱系 UUID 则按 U15 原样保留。
结构与指标校验不是密码学来源证明；实际基线的健康性由比较边界强制检查。
"""

from __future__ import annotations

import json
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from konwledge.domain.evaluation import EvaluationSet
from konwledge.evaluation.metrics import aggregate_metrics, score_ranked_lineages
from konwledge.evaluation.models import AggregateMetrics, EvaluationReport, ItemMetrics
from konwledge.evaluation.validation import (
    _check_metrics,
    _model_data,
    _reconstruct,
    _same_instant,
    _validate_ids,
    validate_evaluation_report,
)
from konwledge.retrieval.models import RetrievalMode


class BaselineCase(BaseModel):
    """一个 case 的紧凑结果；重复谱系保留排名并占用 top-k 槽位。"""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    item_id: str = Field(min_length=1)
    evaluated_at: AwareDatetime
    ranked_lineage_ids: tuple[UUID, ...]
    missing_vector_ids: tuple[UUID, ...] = ()
    metrics: ItemMetrics

    @field_validator("item_id")
    @classmethod
    def validate_item_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("item_id must not be blank")
        return value

    @field_validator("ranked_lineage_ids", "missing_vector_ids")
    @classmethod
    def validate_uuid_values(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        _validate_ids(value, name="lineage or missing vector IDs")
        return value

    @field_validator("missing_vector_ids")
    @classmethod
    def validate_unique_missing(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        _validate_ids(value, name="missing_vector_ids", unique=True)
        return value

    @field_validator("metrics", mode="before")
    @classmethod
    def validate_strict_metrics(cls, value: Any) -> ItemMetrics:
        checked = ItemMetrics.model_validate(_model_data(value), strict=True)
        aggregate_metrics((checked,))  # 同时拒绝非有限值、越界值和错误指标群组。
        return checked


class EvaluationBaseline(BaseModel):
    """固定数据集和检索配置的不可变紧凑快照。

    允许结构有效的降级快照，以便作为候选参与比较；不能用作健康基线。
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    schema_version: Literal[1] = 1
    dataset: EvaluationSet
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_at: AwareDatetime
    mode: RetrievalMode
    top_k: int = Field(gt=0)
    candidate_limit: int = Field(gt=0)
    rrf_k: int = Field(gt=0)
    cases: tuple[BaselineCase, ...]
    metrics: AggregateMetrics

    @field_validator("dataset")
    @classmethod
    def validate_dataset_instance(cls, value: EvaluationSet) -> EvaluationSet:
        return _reconstruct(value, EvaluationSet)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_strict_schema_version(cls, value: Any) -> Any:
        # Literal[1] 本身把 True 和 1.0 视为相等，需额外禁止这两种输入。
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("metrics", mode="before")
    @classmethod
    def validate_strict_metrics(cls, value: Any) -> AggregateMetrics:
        return AggregateMetrics.model_validate(_model_data(value), strict=True)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        if self.dataset_hash != self.dataset.content_hash:
            raise ValueError("dataset_hash does not match the evaluation dataset")
        if tuple(case.item_id for case in self.cases) != tuple(
            item.id for item in self.dataset.items
        ):
            raise ValueError("baseline cases must exactly match dataset item IDs and order")
        scores: list[ItemMetrics] = []
        for item, case in zip(self.dataset.items, self.cases, strict=True):
            _same_instant(
                case.evaluated_at,
                item.at if item.at is not None else self.evaluated_at,
                name=f"case {item.id}: evaluated_at",
            )
            if len(case.ranked_lineage_ids) > self.top_k:
                raise ValueError(f"case {item.id}: ranked lineage count exceeds top_k")
            if self.mode is RetrievalMode.LEXICAL and case.missing_vector_ids:
                raise ValueError(f"case {item.id}: lexical retrieval cannot have missing vectors")
            score = score_ranked_lineages(
                item.expected_lineage_ids,
                case.ranked_lineage_ids,
                k=self.top_k,
                expect_no_answer=item.expect_no_answer,
            )
            _check_metrics(case.metrics, score, name=f"case {item.id}: metrics")
            scores.append(score)
        _check_metrics(self.metrics, aggregate_metrics(scores), name="baseline metrics")
        return self

    @property
    def degraded(self) -> bool:
        """任意 case 存在缺失向量即降级；不作为 JSON computed field。"""
        return any(case.missing_vector_ids for case in self.cases)


def baseline_from_report(
    report: EvaluationReport, *, allow_degraded: bool = False
) -> EvaluationBaseline:
    """完整验证报告后构建基线，默认拒绝缺失向量。

    allow_degraded=True 只用于候选紧凑快照；不能绕过 compare_baseline 的健康检查。
    """
    if not isinstance(allow_degraded, bool):
        raise ValueError("allow_degraded must be a boolean")
    checked = validate_evaluation_report(report)
    if checked.degraded and not allow_degraded:
        raise ValueError("cannot create a baseline from a degraded evaluation report")
    return EvaluationBaseline(
        dataset=checked.dataset,
        dataset_hash=checked.dataset_hash,
        evaluated_at=checked.evaluated_at,
        mode=checked.mode,
        top_k=checked.top_k,
        candidate_limit=checked.candidate_limit,
        rrf_k=checked.rrf_k,
        cases=tuple(
            BaselineCase(
                item_id=case.item_id,
                evaluated_at=case.retrieval.evaluated_at,
                ranked_lineage_ids=tuple(hit.unit.lineage_id for hit in case.retrieval.hits),
                missing_vector_ids=case.retrieval.missing_vector_ids,
                metrics=case.metrics,
            )
            for case in checked.results
        ),
        metrics=checked.metrics,
    )


def dump_baseline(baseline: EvaluationBaseline) -> str:
    """重新校验后输出确定性 JSON：排序键、UTF-8 字符、缩进 2 和单个尾换行。"""
    checked = _reconstruct(baseline, EvaluationBaseline)
    return (
        json.dumps(
            checked.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
