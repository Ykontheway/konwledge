"""专用只读连接上的管理查询; 禁止调用会回填/提交/触发评测的 SDK。"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Engine, and_, create_engine, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, StaticPool

from konwledge.api.management_models import (
    CheckName,
    DiffCounts,
    DiffItem,
    DocumentDiffDTO,
    DocumentDTO,
    EvidenceDTO,
    JobDTO,
    JobErrorCode,
    JobsQuery,
    LineageDTO,
    Page,
    PageQuery,
    ResourceLink,
    RevisionDTO,
    RevisionNavigation,
    SafeCheck,
    SafeEvent,
    SafeMetrics,
    SafeQualityReport,
    UnitDTO,
)
from konwledge.application import KnowledgeLibrary
from konwledge.domain import KnowledgeUnit
from konwledge.lifecycle.diff import diff_by_lineage
from konwledge.storage.tables import (
    AutoEvaluationJobRow,
    DocumentRow,
    KnowledgeUnitRow,
    RevisionRow,
    SourceRow,
)


class ManagementReadError(Exception):
    """只有固定码进入传输层, 不保留存储异常字符串。"""

    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


def _link(
    kind: Literal["job", "revision", "document", "unit", "lineage", "evidence"], identifier: UUID
) -> ResourceLink:
    collection = {
        "job": "jobs",
        "revision": "revisions",
        "document": "documents",
        "unit": "units",
        "lineage": "lineages",
        "evidence": "units",
    }[kind]
    suffix = "/evidence" if kind == "evidence" else ""
    return ResourceLink(
        kind=kind, id=identifier, href=f"/v1/management/{collection}/{identifier}{suffix}"
    )


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, (str, UUID)):
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _hash(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _number(value: object) -> float | int | bool | None:
    if type(value) not in {int, float, bool}:
        return None
    if isinstance(value, (int, float)) and not math.isfinite(value):
        return None
    return value  # type: ignore[return-value]


def _score(value: object) -> float | None:
    number = _number(value)
    if number is None or isinstance(number, bool) or not 0 <= number <= 1:
        return None
    return float(number)


def _report(raw: object) -> SafeQualityReport | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("metrics"), dict):
        raise ManagementReadError(409, "management_record_invalid")
    metrics = raw["metrics"]
    # 只读取真实聚合字段; baseline/results/query/violations/detail 永不投影。
    counts: dict[str, int] = {}
    for name in ("case_count", "answerable_count", "no_answer_count"):
        value = metrics.get(name)
        if type(value) is not int or value < 0:
            raise ManagementReadError(409, "management_record_invalid")
        counts[name] = value
    safe_metrics = SafeMetrics(
        case_count=counts["case_count"],
        answerable_count=counts["answerable_count"],
        no_answer_count=counts["no_answer_count"],
        mean_recall=_score(metrics.get("mean_recall")),
        mean_precision=_score(metrics.get("mean_precision")),
        mean_reciprocal_rank=_score(metrics.get("mean_reciprocal_rank")),
        mean_ndcg=_score(metrics.get("mean_ndcg")),
        hit_rate=_score(metrics.get("hit_rate")),
        no_answer_accuracy=_score(metrics.get("no_answer_accuracy")),
    )
    checks: list[SafeCheck] = []
    for check in raw.get("checks", ()):
        if not isinstance(check, dict) or check.get("name") not in get_args(CheckName):
            continue
        if type(check.get("passed")) is not bool:
            continue
        data = {
            "name": check["name"],
            "passed": check["passed"],
            "actual": _number(check.get("measured")),
        }
        required = check.get("required")
        try:
            safe = SafeCheck.model_validate({**data, "threshold": required})
        except ValidationError:
            safe = SafeCheck.model_validate(data)
        checks.append(safe)
    return SafeQualityReport.model_validate(
        {
            "passed": raw.get("passed"),
            "metrics": safe_metrics,
            "citation_coverage": _score(raw.get("citation_coverage")),
            "stale_hits": raw.get("stale_hits"),
            "degraded": raw.get("degraded"),
            "checks": tuple(checks),
        }
    )


class ManagementService:
    """独立 SQLite 读取连接, 不注册原 session factory 的写入 hooks。

    文件库使用 mode=ro, 不操作库 engine 的连接标志。匿名内存库只能经 SQLite
    backup 取得独立快照, query_only 只作用于快照。两者均显式 BEGIN/rollback,
    finally 恢复并丢弃专用连接, 因而不会污染 StaticPool 或后续 SDK writes。
    """

    def __init__(self, library: KnowledgeLibrary) -> None:
        # KnowledgeLibrary 尚无公开只读 session 接口; 不改变 facade/存储契约。
        bind = library._session_factory.kw.get("bind")
        if not isinstance(bind, Engine) or bind.dialect.name != "sqlite":
            raise ValueError("management_storage_unsupported")
        self._engine = bind

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        database = self._engine.url.database
        raw: sqlite3.Connection | None = None
        reader: Engine | None = None
        original = 0
        try:
            if database is None or database in {"", ":memory:"}:
                raw = sqlite3.connect(":memory:")
                # 不能 checkout/close 原 StaticPool: reset-on-return 的 rollback
                # 会破坏另一线程正在写入的共享连接。直接只读现有连接做 backup。
                pool = self._engine.pool
                if not isinstance(pool, StaticPool):
                    raise ManagementReadError(500, "management_storage_unavailable")
                dbapi = pool.connection.dbapi_connection
                if not isinstance(dbapi, sqlite3.Connection):
                    raise ManagementReadError(500, "management_storage_unavailable")
                deadline = monotonic() + 5

                def progress(_status: int, _remaining: int, _total: int) -> None:
                    if monotonic() > deadline:
                        raise ManagementReadError(503, "management_snapshot_unavailable")

                dbapi.backup(raw, pages=256, progress=progress, sleep=0.01)
            else:
                # 不设置 immutable=1: WAL 中的已提交版本也必须可见。
                uri = Path(database).resolve().as_uri() + "?mode=ro"
                raw = sqlite3.connect(uri, uri=True)
            original = raw.execute("PRAGMA query_only").fetchone()[0]
            raw.execute("PRAGMA query_only=ON")
            raw.execute("BEGIN")
            reader = create_engine("sqlite+pysqlite://", creator=lambda: raw, poolclass=NullPool)
            with Session(reader, autoflush=False) as session:
                yield session
        finally:
            if raw is not None:
                # Session/NullPool 已关闭时, 无需恢复已丢弃的连接。
                try:
                    raw.rollback()
                    raw.execute(f"PRAGMA query_only={int(original)}")
                except sqlite3.ProgrammingError:
                    pass
                finally:
                    raw.close()
            if reader is not None:
                reader.dispose()

    def jobs(self, query: JobsQuery) -> Page[JobDTO]:
        with self.read_session() as session:
            statement = select(AutoEvaluationJobRow)
            for name in ("status", "dataset_id", "dataset_version", "parent_job_id"):
                value = getattr(query, name)
                if value is not None:
                    statement = statement.where(getattr(AutoEvaluationJobRow, name) == value)
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            rows = session.scalars(
                statement.order_by(
                    AutoEvaluationJobRow.created_at.desc(), AutoEvaluationJobRow.id.desc()
                )
                .limit(query.limit)
                .offset(query.offset)
            ).all()
            return Page(
                items=tuple(self._job(row) for row in rows),
                total=total,
                limit=query.limit,
                offset=query.offset,
            )

    def job(self, identifier: UUID) -> JobDTO:
        with self.read_session() as session:
            row = session.get(AutoEvaluationJobRow, identifier)
            if row is None:
                raise ManagementReadError(404, "job_not_found")
            return self._job(row)

    @staticmethod
    def _job(row: AutoEvaluationJobRow) -> JobDTO:
        revisions = tuple(
            dict.fromkeys(
                identifier
                for value in row.trigger_revision_ids
                if (identifier := _uuid(value)) is not None
            )
        )
        code = row.error_code
        if code is not None and code not in get_args(JobErrorCode):
            code = "UNKNOWN_ERROR"
        return JobDTO.model_validate(
            {
                "id": row.id,
                "parent_job_id": row.parent_job_id,
                "status": row.status,
                "config_generation": row.config_generation,
                "config_fingerprint": row.config_fingerprint,
                "dataset_id": row.dataset_id,
                "dataset_version": row.dataset_version,
                "dataset_hash": row.dataset_hash,
                "knowledge_fingerprint": row.knowledge_fingerprint,
                "created_at": row.created_at,
                "evaluated_at": row.evaluated_at,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "attempt_count": row.attempt_count,
                "error_code": code,
                "report": _report(row.report),
                "trigger_revision_ids": revisions,
                "links": tuple(_link("revision", identifier) for identifier in revisions),
            }
        )

    @staticmethod
    def _document(session: Session, identifier: UUID, *, historical: bool = False) -> DocumentRow:
        row = session.get(DocumentRow, identifier)
        if row is None:
            raise ManagementReadError(
                410 if historical else 404,
                "historical_document_unavailable" if historical else "document_not_found",
            )
        return row

    @staticmethod
    def _unit_row(session: Session, identifier: UUID) -> KnowledgeUnitRow:
        row = session.get(KnowledgeUnitRow, identifier)
        if row is None:
            raise ManagementReadError(404, "unit_not_found")
        return row

    @staticmethod
    def _unit(row: KnowledgeUnitRow, document: DocumentRow) -> UnitDTO:
        return UnitDTO.model_validate(
            {
                "id": row.id,
                "source_id": document.source_id,
                "document_id": document.id,
                "lineage_id": row.lineage_id or row.id,
                "ordinal": row.ordinal,
                "status": row.status,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "content_hash": hashlib.sha256(row.text.encode("utf-8")).hexdigest(),
                "links": (
                    _link("document", document.id),
                    _link("lineage", row.lineage_id or row.id),
                    _link("evidence", row.id),
                ),
            }
        )

    def unit(self, identifier: UUID) -> UnitDTO:
        with self.read_session() as session:
            row = self._unit_row(session, identifier)
            document = self._document(session, row.document_id, historical=True)
            return self._unit(row, document)

    def document(self, identifier: UUID, query: PageQuery) -> DocumentDTO:
        with self.read_session() as session:
            document = self._document(session, identifier)
            # 此处导航是不可变导入顺序, 不是当前发布 head; revert 也不替换历史 ID。
            earlier = or_(
                DocumentRow.ingested_at < document.ingested_at,
                and_(DocumentRow.ingested_at == document.ingested_at, DocumentRow.id < document.id),
            )
            later = or_(
                DocumentRow.ingested_at > document.ingested_at,
                and_(DocumentRow.ingested_at == document.ingested_at, DocumentRow.id > document.id),
            )
            same_source = select(DocumentRow.id).where(DocumentRow.source_id == document.source_id)
            previous = session.scalar(
                same_source.where(earlier)
                .order_by(DocumentRow.ingested_at.desc(), DocumentRow.id.desc())
                .limit(1)
            )
            following = session.scalar(
                same_source.where(later).order_by(DocumentRow.ingested_at, DocumentRow.id).limit(1)
            )
            statement = select(KnowledgeUnitRow).where(KnowledgeUnitRow.document_id == identifier)
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            rows = session.scalars(
                statement.order_by(KnowledgeUnitRow.ordinal).limit(query.limit).offset(query.offset)
            ).all()
            return DocumentDTO.model_validate(
                {
                    "id": document.id,
                    "source_id": document.source_id,
                    "content_type": document.content_type,
                    "checksum": document.checksum,
                    "ingested_at": document.ingested_at,
                    "previous_document_id": previous,
                    "next_document_id": following,
                    "units": Page(
                        items=tuple(self._unit(row, document) for row in rows),
                        total=total,
                        limit=query.limit,
                        offset=query.offset,
                    ),
                    "links": tuple(
                        _link("document", value)
                        for value in (previous, following)
                        if value is not None
                    ),
                }
            )

    def lineage(self, identifier: UUID, query: PageQuery) -> LineageDTO:
        with self.read_session() as session:
            statement = select(KnowledgeUnitRow).where(
                or_(
                    KnowledgeUnitRow.lineage_id == identifier,
                    and_(KnowledgeUnitRow.lineage_id.is_(None), KnowledgeUnitRow.id == identifier),
                )
            )
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            if not total:
                raise ManagementReadError(404, "lineage_not_found")
            rows = session.scalars(
                statement.join(DocumentRow)
                .order_by(
                    DocumentRow.ingested_at,
                    DocumentRow.id,
                    KnowledgeUnitRow.ordinal,
                )
                .limit(query.limit)
                .offset(query.offset)
            ).all()
            items = tuple(
                self._unit(row, self._document(session, row.document_id, historical=True))
                for row in rows
            )
            return LineageDTO(
                id=identifier,
                versions=Page(items=items, total=total, limit=query.limit, offset=query.offset),
            )

    def revision(self, identifier: UUID) -> RevisionDTO:
        with self.read_session() as session:
            row = session.get(RevisionRow, identifier)
            if row is None:
                raise ManagementReadError(404, "revision_not_found")
            raw = row.payload
            nav: dict[str, Any] = {}
            for name in ("document_id", "previous_document_id", "unit_id", "previous_unit_id"):
                nav[name] = _uuid(raw.get(name))
            for name in ("document_checksum", "content_hash", "previous_content_hash"):
                nav[name] = _hash(raw.get(name))
            event = raw.get("change")
            nav["event"] = event if event in get_args(SafeEvent) else None
            nav["reverted"] = raw.get("reverted") if type(raw.get("reverted")) is bool else None
            documents = {
                value
                for name in ("document_id", "previous_document_id")
                if (value := nav[name]) is not None
            }
            if row.entity_type == "document":
                documents.add(row.entity_id)
            units = {
                value
                for name in ("unit_id", "previous_unit_id")
                if (value := nav[name]) is not None
            }
            lineages: set[UUID] = set()
            if row.entity_type == "knowledge_unit":
                # revisions.entity_id 是 lineage, 不是该版本的 unit ID。
                lineages.add(row.entity_id)
            for unit_id in tuple(units):
                unit = session.get(KnowledgeUnitRow, unit_id)
                if unit is not None:
                    documents.add(unit.document_id)
                    lineages.add(unit.lineage_id or unit.id)
            # publication 的影响范围是所引用版本的 units, 不声称它们全部发生文本变更。
            if row.entity_type in {"source", "document"} and documents:
                for unit in session.scalars(
                    select(KnowledgeUnitRow).where(KnowledgeUnitRow.document_id.in_(documents))
                ):
                    units.add(unit.id)
                    lineages.add(unit.lineage_id or unit.id)
            links = (
                *(_link("document", value) for value in sorted(documents)),
                *(_link("unit", value) for value in sorted(units)),
                *(_link("lineage", value) for value in sorted(lineages)),
            )
            return RevisionDTO.model_validate(
                {
                    "id": row.id,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "revision": row.revision,
                    "change_type": row.change_type,
                    "created_at": row.created_at,
                    "navigation": RevisionNavigation.model_validate(nav),
                    "affected_document_ids": tuple(sorted(documents)),
                    "affected_unit_ids": tuple(sorted(units)),
                    "affected_lineage_ids": tuple(sorted(lineages)),
                    "links": links,
                }
            )

    def diff(self, new_id: UUID, old_id: UUID, query: PageQuery) -> DocumentDiffDTO:
        with self.read_session() as session:
            old = self._document(session, old_id)
            new = self._document(session, new_id)
            if old.source_id != new.source_id:
                raise ManagementReadError(409, "diff_source_mismatch")
            old_rows = session.scalars(
                select(KnowledgeUnitRow)
                .where(KnowledgeUnitRow.document_id == old.id)
                .order_by(KnowledgeUnitRow.ordinal)
            ).all()
            new_rows = session.scalars(
                select(KnowledgeUnitRow)
                .where(KnowledgeUnitRow.document_id == new.id)
                .order_by(KnowledgeUnitRow.ordinal)
            ).all()

            def domain(row: KnowledgeUnitRow) -> KnowledgeUnit:
                # section_path 只供既有 lineage diff 判断 moved; 永不返回给普通 DTO。
                return KnowledgeUnit(
                    id=row.id,
                    lineage_id=row.lineage_id or row.id,
                    document_id=row.document_id,
                    ordinal=row.ordinal,
                    text=row.text,
                    start_char=row.start_char,
                    end_char=row.end_char,
                    section_path=tuple(row.section_path),
                )

            for rows in (old_rows, new_rows):
                if len({row.lineage_id or row.id for row in rows}) != len(rows):
                    raise ManagementReadError(409, "historical_lineage_invalid")
            result = diff_by_lineage(
                [domain(row) for row in old_rows], [domain(row) for row in new_rows]
            )
            by_id = {row.id: row for row in (*old_rows, *new_rows)}
            changes = tuple(
                DiffItem(
                    kind=change.kind,
                    lineage_id=(change.new or change.old).lineage_id,  # type: ignore[union-attr]
                    old=None if change.old is None else self._unit(by_id[change.old.id], old),
                    new=None if change.new is None else self._unit(by_id[change.new.id], new),
                )
                for change in result.changes[query.offset : query.offset + query.limit]
            )
            return DocumentDiffDTO(
                source_id=new.source_id,
                old_document_id=old.id,
                new_document_id=new.id,
                counts=DiffCounts.model_validate(result.counts()),
                changes=Page(
                    items=changes, total=len(result.changes), limit=query.limit, offset=query.offset
                ),
                links=(_link("document", old.id), _link("document", new.id)),
            )

    def evidence(self, identifier: UUID) -> EvidenceDTO:
        with self.read_session() as session:
            unit = self._unit_row(session, identifier)
            document = self._document(session, unit.document_id, historical=True)
            if session.get(SourceRow, document.source_id) is None:
                raise ManagementReadError(410, "historical_source_unavailable")
            if (
                not unit.text.strip()
                or unit.start_char < 0
                or unit.end_char - unit.start_char != len(unit.text)
                or _hash(document.checksum) is None
            ):
                raise ManagementReadError(409, "historical_evidence_invalid")
            content_hash = hashlib.sha256(unit.text.encode("utf-8")).hexdigest()
            lineage_id = unit.lineage_id or unit.id
            revisions = session.scalars(
                select(RevisionRow)
                .where(
                    RevisionRow.entity_type == "knowledge_unit",
                    RevisionRow.entity_id == lineage_id,
                )
                .order_by(RevisionRow.revision)
            ).all()
            anchored: list[UUID] = []
            expected: set[str] = set()
            for revision in revisions:
                payload = revision.payload
                if not isinstance(payload, dict):
                    raise ManagementReadError(409, "historical_evidence_invalid")
                for id_key, hash_key in (
                    ("unit_id", "content_hash"),
                    ("previous_unit_id", "previous_content_hash"),
                ):
                    if _uuid(payload.get(id_key)) == unit.id:
                        value = _hash(payload.get(hash_key))
                        document_key = (
                            "document_id" if id_key == "unit_id" else "previous_document_id"
                        )
                        if value is None or _uuid(payload.get(document_key)) != document.id:
                            raise ManagementReadError(409, "historical_evidence_invalid")
                        expected.add(value)
                        anchored.append(revision.id)
                if _uuid(payload.get("document_id")) == document.id:
                    checksum = _hash(payload.get("document_checksum"))
                    if checksum is not None and checksum != document.checksum:
                        raise ManagementReadError(409, "historical_evidence_invalid")
            if not expected:
                # 未变化的新版本 unit 不生成独立 revision: 使用该版本首次 publication
                # 之前的 lineage 内容锚点, 仍读取 exact document/unit, 不查 latest 正文。
                publications = session.scalars(
                    select(RevisionRow)
                    .where(
                        RevisionRow.entity_type == "source",
                        RevisionRow.entity_id == document.source_id,
                    )
                    .order_by(RevisionRow.revision)
                ).all()
                if any(not isinstance(row.payload, dict) for row in publications):
                    raise ManagementReadError(409, "historical_evidence_invalid")
                publication = next(
                    (
                        row
                        for row in publications
                        if _uuid(row.payload.get("document_id")) == document.id
                    ),
                    None,
                )
                if publication is not None:
                    if _hash(publication.payload.get("document_checksum")) != document.checksum:
                        raise ManagementReadError(409, "historical_evidence_invalid")
                    prior = [
                        row
                        for row in revisions
                        if row.created_at <= publication.created_at
                        and _hash(row.payload.get("content_hash")) is not None
                    ]
                    if prior:
                        expected.add(prior[-1].payload["content_hash"])
                        anchored.extend((prior[-1].id, publication.id))
            if not expected:
                raise ManagementReadError(410, "historical_evidence_unavailable")
            if expected != {content_hash}:
                raise ManagementReadError(409, "historical_evidence_invalid")
            revision_ids = tuple(dict.fromkeys(anchored))
            return EvidenceDTO(
                source_id=document.source_id,
                document_id=document.id,
                unit_id=unit.id,
                lineage_id=lineage_id,
                start_char=unit.start_char,
                end_char=unit.end_char,
                text=unit.text,
                content_hash=content_hash,
                document_checksum=document.checksum,
                revision_ids=revision_ids,
                links=(
                    _link("document", document.id),
                    _link("lineage", lineage_id),
                    *(_link("revision", value) for value in revision_ids),
                ),
            )
