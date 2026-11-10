from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from konwledge.domain import EvaluationItem, EvaluationSet, dump_evaluation_set

ID = "case-1"
SET_ID = UUID("10000000-0000-0000-0000-000000000001")
LINEAGE_ID = UUID("20000000-0000-0000-0000-000000000002")
NIL_UUID = UUID(int=0)
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


def make_item(**changes: object) -> EvaluationItem:
    values: dict[str, object] = {
        "id": ID,
        "query": "What is the port?",
        "expected_lineage_ids": frozenset({LINEAGE_ID}),
        "expect_no_answer": False,
    }
    values.update(changes)
    return EvaluationItem.model_validate(values)


def make_set(**changes: object) -> EvaluationSet:
    values: dict[str, object] = {
        "id": SET_ID,
        "name": "Base Eval",
        "version": 1,
        "created_at": NOW,
        "items": (make_item(),),
    }
    values.update(changes)
    return EvaluationSet.model_validate(values)


def test_evaluation_item_validation() -> None:
    # Blank ID or query
    with pytest.raises(ValidationError, match="id must not be blank"):
        make_item(id=" ")
    with pytest.raises(ValidationError, match="query must not be blank"):
        make_item(query=" \n")

    # Nil UUID in gold
    with pytest.raises(ValidationError, match="must not contain the nil UUID"):
        make_item(expected_lineage_ids=frozenset({NIL_UUID}))

    # Timezone aware at
    with pytest.raises(ValidationError, match="must include timezone"):
        make_item(at=datetime(2026, 11, 10))

    # Answerability consistency
    with pytest.raises(ValidationError, match="no-answer items must have empty"):
        make_item(expect_no_answer=True, expected_lineage_ids=frozenset({LINEAGE_ID}))
    with pytest.raises(ValidationError, match="answerable items require"):
        make_item(expect_no_answer=False, expected_lineage_ids=frozenset())


def test_evaluation_set_validation() -> None:
    # Nonblank name
    with pytest.raises(ValidationError, match="at least 1 character"):
        make_set(name=" ")

    # Strictly positive version
    with pytest.raises(ValidationError, match="greater than 0"):
        make_set(version=0)

    # Empty items
    with pytest.raises(ValidationError, match="at least 1 item"):
        make_set(items=())

    # Unique item IDs
    with pytest.raises(ValidationError, match="item IDs must be unique"):
        make_set(items=(make_item(id="1"), make_item(id="1")))


def test_deterministic_content_hash_and_dump() -> None:
    ds1 = make_set(description="Test")
    ds2 = make_set(description="Test")
    assert ds1.content_hash == ds2.content_hash

    # Gold ID order shouldn't affect hash
    item_a = make_item(expected_lineage_ids=frozenset({UUID(int=1), UUID(int=2)}))
    item_b = make_item(expected_lineage_ids=frozenset({UUID(int=2), UUID(int=1)}))
    assert item_a.model_dump_json() == item_b.model_dump_json()

    dump = dump_evaluation_set(ds1)
    assert dump.endswith("\n")
    assert EvaluationSet.model_validate_json(dump) == ds1


def test_query_preserves_whitespace() -> None:
    query = "  exact  \n  query  "
    item = make_item(query=query)
    assert item.query == query


def test_duplicate_gold_ids_are_rejected_before_set_coercion() -> None:
    duplicate = str(LINEAGE_ID)
    payload = {
        "id": "duplicate-gold",
        "query": "query",
        "expected_lineage_ids": [duplicate, duplicate.upper()],
    }
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        EvaluationItem.model_validate_json(json.dumps(payload))


def test_same_query_with_different_scopes_is_allowed() -> None:
    first = make_item(id="source-a", source_id=UUID(int=10))
    second = make_item(id="source-b", source_id=UUID(int=11))
    dataset = make_set(items=(first, second))
    assert dataset.items == (first, second)
