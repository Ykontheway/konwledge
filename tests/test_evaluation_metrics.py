from __future__ import annotations

import math
from uuid import UUID

import pytest
from pydantic import ValidationError

from konwledge.evaluation.metrics import aggregate_metrics, score_ranked_lineages
from konwledge.evaluation.models import AggregateMetrics, ItemMetrics

A = UUID("10000000-0000-0000-0000-000000000001")
B = UUID("20000000-0000-0000-0000-000000000002")
C = UUID("30000000-0000-0000-0000-000000000003")
D = UUID("40000000-0000-0000-0000-000000000004")
X = UUID("90000000-0000-0000-0000-000000000009")
NIL = UUID(int=0)


def score(gold: list[UUID], returned: list[UUID], k: int = 3) -> ItemMetrics:
    return score_ranked_lineages(gold, returned, k=k)


def test_exact_k_unfilled_slots_and_top_k_cutoff() -> None:
    exact = score([A], [A, X, B], k=3)
    assert exact.recall == 1.0
    assert exact.precision == pytest.approx(1 / 3)
    assert exact.reciprocal_rank == 1.0
    assert exact.ndcg == 1.0
    assert exact.hit_rate == 1.0
    assert exact.no_answer_success is None

    # A hit after k is not evaluated; missing result slots still count as misses
    # in the fixed Precision@k denominator.
    cutoff = score([A], [X, X, X, A], k=3)
    short = score([A], [A], k=3)
    assert cutoff == ItemMetrics(
        recall=0.0,
        precision=0.0,
        reciprocal_rank=0.0,
        ndcg=0.0,
        hit_rate=0.0,
        no_answer_success=None,
    )
    assert short.precision == pytest.approx(1 / 3)


def test_no_hits_are_zero_not_undefined() -> None:
    result = score([A, B], [X], k=5)
    assert result == ItemMetrics(
        recall=0.0,
        precision=0.0,
        reciprocal_rank=0.0,
        ndcg=0.0,
        hit_rate=0.0,
        no_answer_success=None,
    )


def test_multiple_gold_partial_binary_ndcg_values() -> None:
    # Relevant first occurrences are at ranks 2 and 4.
    result = score([A, B, C], [X, B, X, A], k=4)
    expected_dcg = 1 / math.log2(3) + 1 / math.log2(5)
    ideal_dcg = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert result.recall == pytest.approx(2 / 3)
    assert result.precision == 0.5
    assert result.reciprocal_rank == 0.5
    assert result.ndcg == pytest.approx(expected_dcg / ideal_dcg)
    assert result.hit_rate == 1.0


def test_perfect_ndcg_with_more_gold_than_k() -> None:
    result = score([A, B, C, D], [D, B], k=2)
    assert result.recall == 0.5
    assert result.precision == 1.0
    assert result.reciprocal_rank == 1.0
    assert result.ndcg == 1.0


def test_duplicate_returns_consume_slots_and_only_first_relevance_is_credited() -> None:
    result = score([A, B], [A, A, B], k=3)
    expected_dcg = 1 + 1 / math.log2(4)
    ideal_dcg = 1 + 1 / math.log2(3)
    assert result.recall == 1.0
    assert result.precision == pytest.approx(2 / 3)
    assert result.reciprocal_rank == 1.0
    assert result.ndcg == pytest.approx(expected_dcg / ideal_dcg)

    # If duplicates were collapsed, B would incorrectly move into the cutoff.
    cutoff = score([A, B], [A, A, B], k=2)
    assert cutoff.recall == 0.5
    assert cutoff.precision == 0.5
    assert cutoff.ndcg == pytest.approx(1 / (1 + 1 / math.log2(3)))


def test_no_answer_cases_have_only_the_no_answer_metric() -> None:
    success = score_ranked_lineages([], [], k=5, expect_no_answer=True)
    failure = score_ranked_lineages([], [X], k=5, expect_no_answer=True)
    for result, expected_success in ((success, True), (failure, False)):
        assert result.recall is None
        assert result.precision is None
        assert result.reciprocal_rank is None
        assert result.ndcg is None
        assert result.hit_rate is None
        assert result.no_answer_success is expected_success


def test_no_answer_observes_only_first_k_slots() -> None:
    # Ordinary finite sequences cannot have an item after an empty prefix; this
    # assertion captures the specified prefix semantics directly.
    assert score_ranked_lineages([], (), k=1, expect_no_answer=True).no_answer_success is True


def test_invalid_k_and_answerability_inputs() -> None:
    for bad_k in (True, False, 1.0, "1", None):
        with pytest.raises(TypeError, match="k must be an integer"):
            score_ranked_lineages([A], [A], k=bad_k)  # type: ignore[arg-type]
    for bad_k in (0, -1):
        with pytest.raises(ValueError, match="k must be positive"):
            score_ranked_lineages([A], [A], k=bad_k)
    with pytest.raises(TypeError, match="expect_no_answer must be a boolean"):
        score_ranked_lineages([], [], k=1, expect_no_answer=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no-answer cases require empty"):
        score_ranked_lineages([A], [], k=1, expect_no_answer=True)
    with pytest.raises(ValueError, match="answerable cases require"):
        score_ranked_lineages([], [], k=1)


def test_duplicate_gold_is_rejected_before_set_conversion() -> None:
    with pytest.raises(ValueError, match="must not contain duplicates"):
        score_ranked_lineages([A, A], [A], k=1)


def test_wrong_uuid_and_nil_values_are_rejected_even_beyond_cutoff() -> None:
    with pytest.raises(TypeError, match=r"expected_lineage_ids.*UUID"):
        score_ranked_lineages([str(A)], [A], k=1)  # type: ignore[list-item]
    with pytest.raises(TypeError, match=r"returned_lineage_ids.*UUID"):
        score_ranked_lineages([A], [A, None], k=1)  # type: ignore[list-item]
    with pytest.raises(ValueError, match=r"expected_lineage_ids.*nil UUID"):
        score_ranked_lineages([NIL], [], k=1)
    with pytest.raises(ValueError, match=r"returned_lineage_ids.*nil UUID"):
        score_ranked_lineages([A], [A, NIL], k=1)
    with pytest.raises(TypeError, match="expected_lineage_ids must be an iterable"):
        score_ranked_lineages(None, [A], k=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="returned_lineage_ids must be an iterable"):
        score_ranked_lineages([A], None, k=1)  # type: ignore[arg-type]


def test_aggregate_empty_and_single_cohort_absence_semantics() -> None:
    empty = aggregate_metrics([])
    assert empty == AggregateMetrics(
        case_count=0,
        answerable_count=0,
        no_answer_count=0,
        mean_recall=None,
        mean_precision=None,
        mean_reciprocal_rank=None,
        mean_ndcg=None,
        hit_rate=None,
        no_answer_accuracy=None,
    )

    answerable = aggregate_metrics([score([A], [A], k=2)])
    assert answerable.case_count == answerable.answerable_count == 1
    assert answerable.no_answer_count == 0
    assert answerable.no_answer_accuracy is None

    negative = aggregate_metrics([score_ranked_lineages([], [], k=2, expect_no_answer=True)])
    assert negative.case_count == negative.no_answer_count == 1
    assert negative.answerable_count == 0
    assert negative.mean_recall is None
    assert negative.mean_precision is None
    assert negative.mean_reciprocal_rank is None
    assert negative.mean_ndcg is None
    assert negative.hit_rate is None
    assert negative.no_answer_accuracy == 1.0


def test_aggregate_macro_means_include_misses_and_exclude_negative_cohort() -> None:
    perfect = score([A], [A], k=2)
    miss = score([B], [X], k=2)
    negative_success = score_ranked_lineages([], [], k=2, expect_no_answer=True)
    negative_failure = score_ranked_lineages([], [X], k=2, expect_no_answer=True)
    result = aggregate_metrics([perfect, miss, negative_success, negative_failure])

    assert (result.case_count, result.answerable_count, result.no_answer_count) == (4, 2, 2)
    assert result.mean_recall == 0.5
    assert result.mean_precision == 0.25
    assert result.mean_reciprocal_rank == 0.5
    assert result.mean_ndcg == 0.5
    assert result.hit_rate == 0.5
    assert result.no_answer_accuracy == 0.5


def test_aggregate_is_macro_not_micro() -> None:
    many_gold = score([A, B, C, D], [A, B, C, D], k=4)
    one_gold_miss = score([A], [X], k=4)
    result = aggregate_metrics([many_gold, one_gold_miss])

    assert result.mean_recall == 0.5
    assert result.mean_recall != pytest.approx(4 / 5)  # pooled/micro recall
    assert result.mean_precision == 0.5


def test_models_are_frozen_forbid_extras_and_round_trip_json() -> None:
    item = score([A, B], [X, B], k=2)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ItemMetrics.model_validate({**item.model_dump(), "unknown": 1})
    with pytest.raises(ValidationError, match="frozen"):
        item.recall = 0.0
    assert ItemMetrics.model_validate_json(item.model_dump_json()) == item

    aggregate = aggregate_metrics([item])
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AggregateMetrics.model_validate({**aggregate.model_dump(), "unknown": 1})
    with pytest.raises(ValidationError, match="frozen"):
        aggregate.case_count = 2
    assert AggregateMetrics.model_validate_json(aggregate.model_dump_json()) == aggregate


def test_models_reject_nonfinite_out_of_bounds_and_inconsistent_shapes() -> None:
    valid_item = score([A], [A], k=1).model_dump()

    # extra="forbid" and frozen are inherited from KonwledgeModel
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ItemMetrics.model_validate({**valid_item, "unknown": 1})

    item = ItemMetrics.model_validate(valid_item)
    with pytest.raises(ValidationError, match="frozen"):
        item.recall = 0.0

    valid_aggregate = aggregate_metrics([item]).model_dump()
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AggregateMetrics.model_validate({**valid_aggregate, "unknown": 1})

    aggregate = AggregateMetrics.model_validate(valid_aggregate)
    with pytest.raises(ValidationError, match="frozen"):
        aggregate.case_count = 2


def test_aggregate_defensively_rejects_constructed_malformed_models() -> None:
    partial = ItemMetrics.model_construct(
        recall=1.0,
        precision=None,
        reciprocal_rank=1.0,
        ndcg=1.0,
        hit_rate=1.0,
        no_answer_success=None,
    )
    with pytest.raises(ValueError, match="all five ranking metrics"):
        aggregate_metrics([partial])

    mixed = ItemMetrics.model_construct(
        recall=0.0,
        precision=None,
        reciprocal_rank=None,
        ndcg=None,
        hit_rate=None,
        no_answer_success=False,
    )
    with pytest.raises(ValueError, match="all five ranking metrics to be None"):
        aggregate_metrics([mixed])

    nonfinite = ItemMetrics.model_construct(
        recall=math.nan,
        precision=0.0,
        reciprocal_rank=0.0,
        ndcg=0.0,
        hit_rate=0.0,
        no_answer_success=None,
    )
    with pytest.raises(ValueError, match="finite and between 0 and 1"):
        aggregate_metrics([nonfinite])

    with pytest.raises(TypeError, match="only ItemMetrics"):
        aggregate_metrics([object()])  # type: ignore[list-item]


def test_scored_and_aggregated_values_are_finite_and_bounded() -> None:
    items = [
        score([A, B], [A, A, B], k=3),
        score([A], [X], k=3),
        score([A, B, C], [X, C], k=5),
    ]
    for item in items:
        values = (
            item.recall,
            item.precision,
            item.reciprocal_rank,
            item.ndcg,
            item.hit_rate,
        )
        assert all(
            value is not None and math.isfinite(value) and 0 <= value <= 1 for value in values
        )

    aggregate = aggregate_metrics(items)
    values = (
        aggregate.mean_recall,
        aggregate.mean_precision,
        aggregate.mean_reciprocal_rank,
        aggregate.mean_ndcg,
        aggregate.hit_rate,
    )
    assert all(value is not None and math.isfinite(value) and 0 <= value <= 1 for value in values)
