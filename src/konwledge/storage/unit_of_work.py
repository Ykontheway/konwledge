"""Explicit transaction boundary for metadata repositories."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session

from konwledge.storage.database import SessionFactory
from konwledge.storage.repository import (
    CitationRepository,
    DocumentRepository,
    KnowledgeUnitRepository,
    RevisionRepository,
    SourceRepository,
)


class UnitOfWork:
    """Group repository operations into one explicit database transaction."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> UnitOfWork:
        self._session = self._session_factory()
        self.sources = SourceRepository(self._session)
        self.documents = DocumentRepository(self._session)
        self.knowledge_units = KnowledgeUnitRepository(self._session)
        self.revisions = RevisionRepository(self._session)
        self.citations = CitationRepository(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            self._session.rollback()
        finally:
            self._session.close()
            self._session = None

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("UnitOfWork must be used inside a with block")
        return self._session
