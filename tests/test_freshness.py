"""U11: knowledge freshness policies and deterministic stale scans."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from typer.testing import CliRunner

from konwledge.application import KnowledgeLibrary
from konwledge.cli import app
from konwledge.domain import (
    Document,
    DocumentContentType,
    FreshnessPolicy,
    KnowledgeUnit,
    KnowledgeUnitStatus,
    Revision,
    RevisionChangeType,
    RevisionEntityType,
    Source,
    SourceKind,
)
from konwledge.lifecycle import FreshnessService, SourceNotFoundError
from konwledge.storage import (
    SessionFactory,
    UnitOfWork,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_sqlite_engine("sqlite+pysqlite:///:memory:")
    create_schema(database)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


class FreshnessCorpus:
    """Helper to manage a source and its freshness lifecycle."""

    def __init__(self, session_factory: SessionFactory, *, uri: str = "manual://test") -> None:
        self.session_factory = session_factory
        self.source_id = uuid4()
        self.tick: float = 0.0
        with UnitOfWork(session_factory) as uow:
            uow.sources.add(
                Source(
                    id=self.source_id,
                    kind=SourceKind.MANUAL,
                    uri=uri,
                    created_at=NOW,
                )
            )
            uow.commit()
        self.library = KnowledgeLibrary(session_factory, clock=self.clock)
        self.service = self.library.freshness

    def clock(self) -> datetime:
        return NOW + timedelta(minutes=self.tick)

    def advance(self, minutes: float = 1) -> datetime:
        self.tick += minutes
        return self.clock()

    def set_time(self, time: datetime) -> None:
        self.tick = (time - NOW).total_seconds() / 60

    def add_unit(self, text: str = "Test knowledge.") -> UUID:
        """Manually add a published unit to the source head."""
        with UnitOfWork(self.session_factory) as uow:
            doc_id = uuid4()
            uow.documents.add(
                Document(
                    id=doc_id,
                    source_id=self.source_id,
                    locator="test.txt",
                    content_type=DocumentContentType.TEXT,
                    checksum=doc_id.hex * 2,
                    ingested_at=self.clock(),
                )
            )
            unit_id = uuid4()
            unit = KnowledgeUnit.model_validate(
                {
                    "id": unit_id,
                    "document_id": doc_id,
                    "lineage_id": unit_id,
                    "ordinal": 0,
                    "text": text,
                    "start_char": 0,
                    "end_char": len(text),
                    "status": KnowledgeUnitStatus.ACTIVE,
                }
            )
            uow.knowledge_units.add(unit)
            # Add publication revision
            uow.revisions.add(
                Revision(
                    id=uuid4(),
                    entity_type=RevisionEntityType.SOURCE,
                    entity_id=self.source_id,
                    revision=uow.revisions.next_number(RevisionEntityType.SOURCE, self.source_id),
                    change_type=RevisionChangeType.CREATE,
                    created_at=self.clock(),
                    payload={"change": "published", "document_id": str(doc_id)},
                )
            )
            # Add unit addition revision
            uow.revisions.add(
                Revision(
                    id=uuid4(),
                    entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
                    entity_id=unit_id,
                    revision=1,
                    change_type=RevisionChangeType.CREATE,
                    created_at=self.clock(),
                    payload={"change": "added", "unit_id": str(unit_id)},
                )
            )
            uow.commit()
        return unit_id


# ---------------------------------------------------------------------------
# Domain Models
# ---------------------------------------------------------------------------


def test_freshness_policy_validation() -> None:
    # Valid
    p = FreshnessPolicy(ttl_seconds=3600, source_updated_at=NOW)
    assert p.ttl_seconds == 3600
    assert p.source_updated_at == NOW

    # TTL must be strictly positive int
    with pytest.raises(ValueError, match="positive integer"):
        FreshnessPolicy(ttl_seconds=0)
    with pytest.raises(ValueError, match="positive integer"):
        FreshnessPolicy(ttl_seconds=-1)
    with pytest.raises(ValueError, match="integer, not a boolean"):
        FreshnessPolicy(ttl_seconds=True)
    with pytest.raises(ValidationError, match="must be an integer"):
        FreshnessPolicy.model_validate({"ttl_seconds": 3600.5})

    # source_updated_at must be aware
    naive = datetime(2026, 11, 6, 10, 0)
    with pytest.raises(ValueError, match="timezone information"):
        FreshnessPolicy(source_updated_at=naive)

    # Both None is valid (disabled)
    disabled = FreshnessPolicy()
    assert disabled.ttl_seconds is None
    assert disabled.source_updated_at is None

    with pytest.raises(ValidationError, match="Extra inputs"):
        FreshnessPolicy.model_validate({"ttl_seconds": 60, "unknown": True})
    attribute = "ttl_seconds"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(disabled, attribute, 60)


def test_freshness_policy_rejects_ttl_beyond_datetime_range() -> None:
    with pytest.raises(ValueError, match="supported range"):
        FreshnessPolicy(ttl_seconds=315_537_897_600)


# ---------------------------------------------------------------------------
# Configuration and Persistence
# ---------------------------------------------------------------------------


def test_configure_and_query_policy(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    policy = FreshnessPolicy(ttl_seconds=86400)

    rev = corpus.service.configure(corpus.source_id, policy)
    assert rev.payload["change"] == "freshness_policy"
    assert rev.payload["policy"]["ttl_seconds"] == 86400

    # Current policy
    assert corpus.service.policy(corpus.source_id) == policy

    # Historical policy
    corpus.advance(1)
    before = corpus.clock()
    corpus.advance(10)
    new_policy = FreshnessPolicy(ttl_seconds=3600)
    corpus.service.configure(corpus.source_id, new_policy)

    assert corpus.service.policy(corpus.source_id) == new_policy
    assert corpus.service.policy(corpus.source_id, at=NOW - timedelta(microseconds=1)) is None
    assert corpus.service.policy(corpus.source_id, at=before) == policy
    assert corpus.service.policy(corpus.source_id, at=corpus.clock()) == new_policy

    # Persistence
    new_service = FreshnessService(session_factory)
    assert new_service.policy(corpus.source_id) == new_policy


def test_configure_unknown_source(session_factory: SessionFactory) -> None:
    service = FreshnessService(session_factory)
    unknown = uuid4()

    with pytest.raises(SourceNotFoundError, match=rf"source {unknown} was not found"):
        service.configure(unknown, FreshnessPolicy(ttl_seconds=60))
    with pytest.raises(SourceNotFoundError, match=rf"source {unknown} was not found"):
        service.policy(unknown)


def test_cli_freshness_show_unknown_source(tmp_path: Path) -> None:
    unknown = uuid4()
    result = CliRunner().invoke(
        app,
        [
            "freshness",
            "show",
            str(unknown),
            "-d",
            str(tmp_path / "knowledge.db"),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"Error: source {unknown} was not found\n"


def test_configure_rejects_backdated_clock(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    corpus.advance(10)
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=60))

    # Backdate the clock
    corpus.tick -= 5
    with pytest.raises(ValueError, match="clock is earlier"):
        corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=120))


# ---------------------------------------------------------------------------
# TTL Scans
# ---------------------------------------------------------------------------


def test_scan_ttl_boundaries(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    corpus.add_unit()
    ttl = 3600
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=ttl))

    # reference_at is head publication time here
    with UnitOfWork(session_factory) as uow:
        pub = uow.revisions.list_for(RevisionEntityType.SOURCE, corpus.source_id)[0]
        ref_at = pub.created_at

    # 1us before deadline
    deadline = ref_at + timedelta(seconds=ttl)
    corpus.set_time(deadline - timedelta(microseconds=1))
    scan = corpus.service.scan()
    assert len(scan.decisions) == 1
    assert scan.decisions[0].expired is False
    assert scan.decisions[0].status is KnowledgeUnitStatus.ACTIVE

    # Exactly at deadline
    corpus.set_time(deadline)
    scan = corpus.service.scan()
    assert scan.decisions[0].expired is True
    assert "ttl_expired" in scan.decisions[0].reason_codes

    # After deadline
    corpus.set_time(deadline + timedelta(seconds=1))
    scan = corpus.service.scan()
    assert scan.decisions[0].expired is True


def test_ttl_reset_by_modified_and_refreshed(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    unit_id = corpus.add_unit()
    ttl = 3600
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=ttl))

    corpus.advance(ttl + 1)
    assert corpus.service.scan().decisions[0].expired is True

    # Must apply stale mark first to allow refresh
    corpus.service.scan(dry_run=False)
    state = corpus.library.unit_state(unit_id)
    assert state is not None
    assert state.status is KnowledgeUnitStatus.STALE

    # Refresh resets it
    corpus.library.lifecycle.refresh(unit_id, reason="confirmed")
    assert corpus.service.scan().decisions[0].expired is False

    # Advance again until stale
    corpus.advance(ttl + 1)
    assert corpus.service.scan().decisions[0].expired is True

    # Mock a modification (add a 'modified' revision)
    with UnitOfWork(session_factory) as uow:
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
                entity_id=unit_id,
                revision=uow.revisions.next_number(RevisionEntityType.KNOWLEDGE_UNIT, unit_id),
                change_type=RevisionChangeType.UPDATE,
                created_at=corpus.clock(),
                payload={"change": "modified", "unit_id": str(unit_id)},
            )
        )
        uow.commit()

    assert corpus.service.scan().decisions[0].expired is False


def test_ttl_not_reset_by_moved_or_unchanged(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    unit_id = corpus.add_unit()
    ttl = 3600
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=ttl))

    # Add a 'moved' revision
    corpus.advance(ttl / 2)
    with UnitOfWork(session_factory) as uow:
        uow.revisions.add(
            Revision(
                id=uuid4(),
                entity_type=RevisionEntityType.KNOWLEDGE_UNIT,
                entity_id=unit_id,
                revision=uow.revisions.next_number(RevisionEntityType.KNOWLEDGE_UNIT, unit_id),
                change_type=RevisionChangeType.UPDATE,
                created_at=corpus.clock(),
                payload={"change": "moved", "unit_id": str(unit_id)},
            )
        )
        uow.commit()

    # Should still expire relative to original publication
    corpus.advance(ttl / 2 + 1)
    assert corpus.service.scan().decisions[0].expired is True


# ---------------------------------------------------------------------------
# Source Updated At Scans
# ---------------------------------------------------------------------------


def test_scan_source_updated_at(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    unit_id = corpus.add_unit()
    pub_at = corpus.clock()

    # Source updated after publication
    updated_at = pub_at + timedelta(minutes=5)
    corpus.service.configure(corpus.source_id, FreshnessPolicy(source_updated_at=updated_at))

    # Before evaluation time reaches updated_at, not stale
    corpus.set_time(updated_at - timedelta(seconds=1))
    assert corpus.service.scan().decisions[0].expired is False

    # At/After updated_at, it becomes stale
    corpus.set_time(updated_at)
    scan = corpus.service.scan()
    assert scan.decisions[0].expired is True
    assert "source_updated" in scan.decisions[0].reason_codes

    # Refresh makes it active again despite updated_at
    corpus.service.scan(dry_run=False)
    corpus.library.lifecycle.refresh(unit_id, reason="updated source content")
    assert corpus.service.scan().decisions[0].expired is False

    # But a later update makes it stale again
    later_update = updated_at + timedelta(hours=1)
    corpus.advance(10)
    corpus.service.configure(corpus.source_id, FreshnessPolicy(source_updated_at=later_update))
    corpus.set_time(later_update)
    assert corpus.service.scan().decisions[0].expired is True


# ---------------------------------------------------------------------------
# Dry Run and Apply
# ---------------------------------------------------------------------------


def test_scan_dry_run_vs_apply(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    unit_id = corpus.add_unit()
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=60))
    corpus.advance(120)

    # Dry run
    scan = corpus.service.scan(dry_run=True)
    assert len(scan.marked) == 0
    state = corpus.library.unit_state(unit_id)
    assert state is not None
    assert state.status is KnowledgeUnitStatus.ACTIVE

    # Apply
    scan = corpus.service.scan(dry_run=False)
    assert len(scan.marked) == 1
    assert scan.marked[0].payload["change"] == "stale"
    assert scan.marked[0].payload["freshness"]["reason_codes"] == ["ttl_expired"]

    state = corpus.library.unit_state(unit_id)
    assert state is not None
    assert state.status is KnowledgeUnitStatus.STALE
    assert state.stale_reason == "ttl_expired"

    # Idempotency: scan apply again shouldn't add another mark
    scan2 = corpus.service.scan(dry_run=False)
    assert len(scan2.marked) == 0

    # The write timestamp is the scan observation, not the earlier deadline.
    assert scan.marked[0].created_at == scan.evaluated_at
    expires_at = scan.decisions[0].expires_at
    assert expires_at is not None
    assert scan.marked[0].created_at > expires_at


def test_scan_disabled_policy_and_unknown_source(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    corpus.add_unit()

    # A source without a configured policy contributes no decisions.
    assert corpus.service.scan().decisions == ()

    # An explicitly disabled policy also contributes no decisions.
    corpus.service.configure(corpus.source_id, FreshnessPolicy())
    assert corpus.service.scan().decisions == ()

    with pytest.raises(SourceNotFoundError):
        corpus.service.scan(source_id=uuid4())


def test_scan_combines_reasons_and_rejects_naive_clock(
    session_factory: SessionFactory,
) -> None:
    corpus = FreshnessCorpus(session_factory)
    corpus.add_unit()
    corpus.service.configure(
        corpus.source_id,
        FreshnessPolicy(
            ttl_seconds=60,
            source_updated_at=NOW + timedelta(minutes=2),
        ),
    )
    corpus.advance(3)

    decision = corpus.service.scan().decisions[0]
    assert decision.reason_codes == ("ttl_expired", "source_updated")
    assert decision.reason == "ttl_expired,source_updated"
    assert decision.expired is True

    naive_service = FreshnessService(session_factory, clock=lambda: datetime(2026, 11, 6, 10, 0))
    with pytest.raises(ValueError, match="timezone"):
        naive_service.scan()


def test_scan_only_current_head_excludes_retired_units(
    session_factory: SessionFactory,
) -> None:
    corpus = FreshnessCorpus(session_factory)
    retired_id = corpus.add_unit("Retired knowledge.")
    corpus.advance(1)
    current_id = corpus.add_unit("Current knowledge.")
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=60))
    corpus.advance(2)

    scan = corpus.service.scan(dry_run=False)
    assert [decision.unit_id for decision in scan.decisions] == [current_id]
    assert [revision.payload["unit_id"] for revision in scan.marked] == [str(current_id)]
    state = corpus.library.unit_state(retired_id)
    assert state is not None
    assert state.status is KnowledgeUnitStatus.DELETED


def test_historical_scan_read_only(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    corpus.add_unit()
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=60))
    past = corpus.clock()
    corpus.advance(120)

    # Historical scan works
    scan = corpus.service.scan(at=past)
    assert scan.evaluated_at == past
    assert scan.decisions[0].expired is False

    # But apply is forbidden
    with pytest.raises(ValueError, match="read-only"):
        corpus.service.scan(at=past, dry_run=False)


# ---------------------------------------------------------------------------
# Knowledge Library Integration
# ---------------------------------------------------------------------------


def test_library_publishable_units_filtering(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    unit_id = corpus.add_unit()
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=60))
    corpus.advance(120)
    corpus.service.scan(dry_run=False)

    # Default: only active (empty list)
    assert corpus.library.publishable_units() == ()

    # With include_stale
    units = corpus.library.publishable_units(include_stale=True)
    assert len(units) == 1
    assert units[0].unit.id == unit_id
    assert units[0].status is KnowledgeUnitStatus.STALE

    # Historical query remains active before the stale mark
    assert len(corpus.library.publishable_units(NOW + timedelta(minutes=1))) == 1


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def test_cli_freshness_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    database = str(tmp_path / "knowledge.db")
    guide = tmp_path / "guide.md"
    guide.write_text("# Test\nContent.\n", encoding="utf-8")
    engine = create_sqlite_engine(f"sqlite:///{database}")
    request.addfinalizer(engine.dispose)
    create_schema(engine)
    cli_session_factory = create_session_factory(engine)
    moment = NOW

    def _open_controlled(path: Path) -> KnowledgeLibrary:
        assert path == Path(database)
        return KnowledgeLibrary(cli_session_factory, clock=lambda: moment)

    # Import, policy configuration and scans must use the same controlled clock.
    # Mixing a fixed future date with a wall-clock import makes this test expire.
    monkeypatch.setattr("konwledge.cli._open_library", _open_controlled)

    def _invoke(*args: str) -> tuple[int, str, str]:
        result = CliRunner().invoke(app, list(args))
        return result.exit_code, result.stdout, result.stderr

    code, _, err = _invoke("import", str(guide), "-d", database)
    assert code == 0, err
    with UnitOfWork(cli_session_factory) as uow:
        source_id = str(uow.sources.list()[0].id)

    code, out, _ = _invoke("freshness", "configure", source_id, "--ttl-days", "7", "-d", database)
    assert code == 0
    assert "freshness configured" in out

    code, out, _ = _invoke("freshness", "show", source_id, "-d", database)
    assert code == 0
    policy = json.loads(out)
    assert policy["ttl_seconds"] == 7 * 86400

    code, out, _ = _invoke("freshness", "scan", "--source", source_id, "-d", database)
    assert code == 0
    assert "Scan complete" in out
    assert "0 stale mark(s) detected" in out

    future = NOW + timedelta(days=10)
    code, out, _ = _invoke(
        "freshness", "scan", "--source", source_id, "--at", future.isoformat(), "-d", database
    )
    assert code == 0
    assert "stale: unit" in out
    assert "1 stale mark(s) detected (dry run)" in out

    # --apply cannot be combined with --at: advance the injected clock instead.
    moment = future
    code, out, _ = _invoke("freshness", "scan", "--source", source_id, "--apply", "-d", database)
    assert code == 0
    assert "1 stale mark(s) appended" in out

    code, out, _ = _invoke("list", "-d", database)
    assert code == 0
    assert "status=stale" in out

    code, out, _ = _invoke("publishable", "-d", database)
    assert code == 0
    assert not any(line.startswith("unit ") for line in out.splitlines())
    code, out, _ = _invoke("publishable", "--include-stale", "-d", database)
    assert code == 0
    assert any(line.startswith("unit ") for line in out.splitlines())
    assert "status=stale" in out

    code, out, _ = _invoke("freshness", "configure", source_id, "--disable", "-d", database)
    assert code == 0
    assert "freshness disabled" in out
    code, out, _ = _invoke("freshness", "show", source_id, "-d", database)
    assert code == 0
    assert json.loads(out) == {"ttl_seconds": None, "source_updated_at": None}


def test_cli_argument_errors(tmp_path: Path) -> None:
    database = str(tmp_path / "knowledge.db")
    dummy_id = str(uuid4())

    def _invoke(*args: str) -> tuple[int, str, str]:
        return CliRunner().invoke(app, list(args)).exit_code, "", ""

    # Combination of disable and options
    c, _, _ = _invoke(
        "freshness", "configure", dummy_id, "--disable", "--ttl-days", "1", "-d", database
    )
    assert c == 2

    # Missing all options
    c, _, _ = _invoke("freshness", "configure", dummy_id, "-d", database)
    assert c == 2

    # Non-positive TTL
    c, _, _ = _invoke("freshness", "configure", dummy_id, "--ttl-days", "0", "-d", database)
    assert c == 2

    # Invalid time
    c, _, _ = _invoke(
        "freshness", "configure", dummy_id, "--source-updated-at", "garbage", "-d", database
    )
    assert c == 2

    # Naive time
    c, _, _ = _invoke(
        "freshness",
        "configure",
        dummy_id,
        "--source-updated-at",
        "2026-11-06T10:00:00",
        "-d",
        database,
    )
    assert c == 2


def test_scan_apply_rolls_back_entire_batch_on_second_mark_failure(
    session_factory: SessionFactory,
) -> None:
    corpus_a = FreshnessCorpus(session_factory, uri="manual://a")
    unit_a = corpus_a.add_unit("A")
    corpus_a.service.configure(corpus_a.source_id, FreshnessPolicy(ttl_seconds=60))
    corpus_b = FreshnessCorpus(session_factory, uri="manual://b")
    unit_b = corpus_b.add_unit("B")
    corpus_b.service.configure(corpus_b.source_id, FreshnessPolicy(ttl_seconds=60))
    corpus_a.advance(120)

    with UnitOfWork(session_factory) as uow:
        revision_count_before = len(uow.revisions.list())
        cached_before = {}
        for uid in (unit_a, unit_b):
            u = uow.knowledge_units.get(uid)
            assert u is not None
            cached_before[uid] = u.status

    generated = iter((uuid4(),))

    def fail_on_second_id() -> UUID:
        try:
            return next(generated)
        except StopIteration:
            raise RuntimeError("second stale id failed") from None

    service = FreshnessService(session_factory, id_factory=fail_on_second_id, clock=corpus_a.clock)
    with pytest.raises(RuntimeError, match="second stale id failed"):
        service.scan(dry_run=False)

    with UnitOfWork(session_factory) as uow:
        assert len(uow.revisions.list()) == revision_count_before
        cached_after = {}
        for uid in (unit_a, unit_b):
            u = uow.knowledge_units.get(uid)
            assert u is not None
            cached_after[uid] = u.status
        assert cached_after == cached_before
        assert cached_before == {
            unit_a: KnowledgeUnitStatus.ACTIVE,
            unit_b: KnowledgeUnitStatus.ACTIVE,
        }


def test_identical_file_reimport_does_not_reset_ttl(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text("# Guide\nStable content.\n", encoding="utf-8")
    now = [NOW]
    library = KnowledgeLibrary(session_factory, clock=lambda: now[0])

    first = library.import_path(guide).imported[0].result
    unit_snap = library.show_unit(first.units[0].id)
    assert unit_snap is not None
    source_id = unit_snap.source.id
    library.freshness.configure(source_id, FreshnessPolicy(ttl_seconds=3600))
    original = library.freshness.scan().decisions[0]

    now[0] += timedelta(minutes=30)
    guide.touch()
    duplicate = library.import_path(guide).imported[0].result
    after = library.freshness.scan().decisions[0]

    assert duplicate.ingestion.created is False
    assert duplicate.diff is None
    assert duplicate.units_created is False
    assert original.reference_at is not None
    assert after.reference_at == original.reference_at
    assert after.expires_at == original.expires_at


def test_revert_resets_changed_lineage_ttl_but_not_unchanged_lineage(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    guide = tmp_path / "guide.md"
    v1 = "# Guide\nStable.\n\n## Detail\nOld wording.\n"
    v2 = "# Guide\nStable.\n\n## Detail\nNew wording.\n"
    now = [NOW]
    library = KnowledgeLibrary(session_factory, clock=lambda: now[0])

    guide.write_text(v1, encoding="utf-8")
    first = library.import_path(guide).imported[0].result
    source_id = library.show_unit(first.units[0].id).source.id
    library.freshness.configure(source_id, FreshnessPolicy(ttl_seconds=3600))
    first_references = {
        decision.lineage_id: decision.reference_at
        for decision in library.freshness.scan().decisions
    }

    now[0] += timedelta(minutes=20)
    guide.write_text(v2, encoding="utf-8")
    second = library.import_path(guide).imported[0].result
    assert second.diff is not None
    changed = next(unit for unit in second.units if "New wording" in unit.text)
    unchanged = next(unit for unit in second.units if "Stable" in unit.text)
    second_references = {
        decision.lineage_id: decision.reference_at
        for decision in library.freshness.scan().decisions
    }
    assert now[0] is not None
    assert second_references[changed.lineage_id] == now[0]
    assert first_references[unchanged.lineage_id] is not None
    assert second_references[unchanged.lineage_id] == first_references[unchanged.lineage_id]

    now[0] += timedelta(minutes=20)
    guide.write_text(v1, encoding="utf-8")
    reverted = library.import_path(guide).imported[0].result
    assert reverted.reverted is True
    assert reverted.ingestion.document.id == first.ingestion.document.id
    snapshot = library.snapshot()
    assert snapshot.sources[0].head == first.ingestion.document
    assert {state.status for state in snapshot.sources[0].units} == {KnowledgeUnitStatus.ACTIVE}

    reverted_references = {
        decision.lineage_id: decision.reference_at
        for decision in library.freshness.scan().decisions
    }
    assert now[0] is not None
    assert reverted_references[changed.lineage_id] == now[0]
    assert first_references[unchanged.lineage_id] is not None
    assert reverted_references[unchanged.lineage_id] == first_references[unchanged.lineage_id]


def test_source_policy_change_does_not_reset_ttl_reference_or_deadline(
    session_factory: SessionFactory,
) -> None:
    corpus = FreshnessCorpus(session_factory)
    corpus.add_unit()
    published_at = corpus.clock()
    ttl = 3600

    corpus.advance(30)
    corpus.service.configure(
        corpus.source_id,
        FreshnessPolicy(
            ttl_seconds=ttl,
            source_updated_at=published_at + timedelta(minutes=45),
        ),
    )
    corpus.set_time(published_at + timedelta(seconds=ttl))
    decision = corpus.service.scan().decisions[0]

    assert published_at is not None
    assert decision.reference_at == published_at
    assert decision.expires_at == published_at + timedelta(seconds=ttl)
    assert decision.reason_codes == ("ttl_expired", "source_updated")


def test_ttl_deadline_compares_same_instant_across_timezones(
    session_factory: SessionFactory,
) -> None:
    corpus = FreshnessCorpus(session_factory)
    corpus.add_unit()
    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=3600))
    utc_deadline = NOW + timedelta(hours=1)
    plus_eight_deadline = utc_deadline.astimezone(timezone(timedelta(hours=8)))

    corpus.set_time(plus_eight_deadline - timedelta(microseconds=1))
    before = corpus.service.scan().decisions[0]
    corpus.set_time(plus_eight_deadline)
    at_deadline = corpus.service.scan().decisions[0]

    assert before.expires_at == utc_deadline
    assert before.expired is False
    assert at_deadline.checked_at == plus_eight_deadline
    assert at_deadline.expires_at == utc_deadline
    assert at_deadline.expired is True


def test_cli_dry_run_counts_only_expired_active_units_as_pending(
    session_factory: SessionFactory,
) -> None:
    corpus_a = FreshnessCorpus(session_factory, uri="manual://a")
    corpus_a.add_unit("pending active")
    corpus_a.service.configure(corpus_a.source_id, FreshnessPolicy(ttl_seconds=60))
    corpus_b = FreshnessCorpus(session_factory, uri="manual://b")
    unit_b = corpus_b.add_unit("already stale")
    corpus_b.service.configure(corpus_b.source_id, FreshnessPolicy(ttl_seconds=60))

    evaluated_at = NOW + timedelta(minutes=2)
    corpus_b.set_time(evaluated_at)
    corpus_b.library.lifecycle.mark_stale(unit_b, reason="already stale")
    library = KnowledgeLibrary(session_factory, clock=lambda: evaluated_at)

    with patch("konwledge.cli._open_library", return_value=library):
        result = CliRunner().invoke(app, ["freshness", "scan"])

    assert result.exit_code == 0
    assert result.stdout.endswith(
        "Scan complete: 2 unit(s) evaluated at 2026-11-06T10:02:00+00:00 | "
        "2 expired unit(s) | 1 stale mark(s) detected (dry run).\n"
    )


def test_cli_large_ttl_days_returns_exit_2(tmp_path: Path) -> None:
    database = str(tmp_path / "knowledge.db")
    guide = tmp_path / "guide.md"
    guide.write_text("# Test\n", encoding="utf-8")
    runner = CliRunner()
    runner.invoke(app, ["import", str(guide), "-d", database])

    engine = create_sqlite_engine(f"sqlite:///{database}")
    session_factory = create_session_factory(engine)
    with UnitOfWork(session_factory) as uow:
        source_id = str(uow.sources.list()[0].id)

    # 4 million days > 315537897599 seconds
    result = runner.invoke(
        app, ["freshness", "configure", source_id, "--ttl-days", "4000000", "-d", database]
    )
    assert result.exit_code == 2
    assert "ttl_seconds exceeds datetime's supported range" in result.stderr


def test_expiry_reference_overflow_conversion(session_factory: SessionFactory) -> None:
    corpus = FreshnessCorpus(session_factory)
    unit_id = corpus.add_unit()
    with UnitOfWork(session_factory) as uow:
        unit = uow.knowledge_units.get(unit_id)
        assert unit is not None
        lineage_id = unit.lineage_id
        revision_count_before = len(
            uow.revisions.list_for(RevisionEntityType.KNOWLEDGE_UNIT, lineage_id)
        )

    # 315537897599 is the maximum allowed TTL.
    # NOW (2026) + maximum TTL will definitely exceed year 9999.
    policy = FreshnessPolicy(ttl_seconds=315_537_897_599)
    corpus.service.configure(corpus.source_id, policy)

    with pytest.raises(ValueError, match="TTL deadline exceeds datetime's supported range"):
        corpus.service.scan(dry_run=False)

    # Ensure no mutation (no stale marks appended).
    with UnitOfWork(session_factory) as uow:
        assert (
            len(uow.revisions.list_for(RevisionEntityType.KNOWLEDGE_UNIT, lineage_id))
            == revision_count_before
        )


def test_dst_transition_exact_elapsed_seconds(session_factory: SessionFactory) -> None:
    # Europe/Berlin 2026-03-29 02:00 -> 03:00 (spring forward)
    # Start: 2026-03-28T12:00+01:00
    # TTL: 86400 (24 hours)
    # Exact 24 hours later in wall clock is 13:00+02:00
    start = datetime(2026, 3, 28, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    corpus = FreshnessCorpus(session_factory)
    corpus.set_time(start)
    corpus.add_unit()

    corpus.service.configure(corpus.source_id, FreshnessPolicy(ttl_seconds=86400))

    # 24.5h later (wall time 13:30+02:00) should be expired
    # 23.5h later (wall time 12:30+02:00) should NOT be expired
    check_before = datetime(2026, 3, 29, 12, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    corpus.set_time(check_before)
    scan = corpus.service.scan()
    assert not scan.decisions[0].expired

    check_at = datetime(2026, 3, 29, 13, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    corpus.set_time(check_at)
    scan = corpus.service.scan()
    assert scan.decisions[0].expired


def test_fallback_scan_without_source_revision(session_factory: SessionFactory) -> None:
    source_id = uuid4()
    doc_id = uuid4()
    unit_id = uuid4()
    ingested_at = NOW - timedelta(days=1)

    with UnitOfWork(session_factory) as uow:
        uow.sources.add(
            Source(id=source_id, kind=SourceKind.MANUAL, uri="manual://fallback", created_at=NOW)
        )
        uow.documents.add(
            Document(
                id=doc_id,
                source_id=source_id,
                locator="old.txt",
                content_type=DocumentContentType.TEXT,
                checksum="a" * 64,
                ingested_at=ingested_at,
            )
        )
        uow.knowledge_units.add(
            KnowledgeUnit.model_validate(
                {
                    "id": unit_id,
                    "document_id": doc_id,
                    "lineage_id": unit_id,
                    "ordinal": 0,
                    "text": "Old",
                    "start_char": 0,
                    "end_char": 3,
                    "status": KnowledgeUnitStatus.ACTIVE,
                }
            )
        )
        uow.commit()

    # Configure freshness policy - this creates a SOURCE revision at NOW
    service = FreshnessService(session_factory, clock=lambda: NOW)
    service.configure(source_id, FreshnessPolicy(ttl_seconds=3600))

    # Scan at NOW. ingested_at was 24h ago. TTL is 1h. Should be expired.
    # reference_at should be ingested_at (fallback when no published revision).
    scan = service.scan()
    assert len(scan.decisions) == 1
    decision = scan.decisions[0]
    assert decision.reference_at == ingested_at
    assert decision.expired is True
