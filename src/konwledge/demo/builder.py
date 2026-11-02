"""Build the demo library deterministically and export its snapshot.

Determinism comes from three injected pieces: a sequential UUID factory, a
clock that only moves when the scenario says so, and fixed corpus files that
ship inside the package. Two builds in different directories therefore yield
byte-identical snapshots.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from uuid import UUID, uuid5

from konwledge.application import KnowledgeLibrary
from konwledge.demo.scenario import (
    DEMO_STEPS,
    DEMO_TITLE,
    DemoStep,
    ImportFile,
    MarkStale,
    Refresh,
)
from konwledge.domain import KnowledgeUnit
from konwledge.embedding import EmbeddingService, HashEmbeddingProvider
from konwledge.indexing import IndexingService, InMemoryVectorIndex
from konwledge.snapshot import LibrarySnapshot, StoryStep, export_snapshot
from konwledge.storage import (
    SessionFactory,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

DEMO_NAMESPACE = UUID("6f1c2a3e-8d4b-5e7f-9a0b-1c2d3e4f5a6b")
MARKER = ".konwledge-demo"
DATABASE_NAME = "knowledge.db"
FILES_DIR = "files"


class DemoWorkspaceError(RuntimeError):
    """Raised when a workspace cannot be (re)built or has not been built yet."""


class SequentialIds:
    """Deterministic UUID factory: ``uuid5(namespace, "1")``, ``"2"``, ..."""

    def __init__(self, namespace: UUID = DEMO_NAMESPACE) -> None:
        self._namespace = namespace
        self._issued = 0

    def __call__(self) -> UUID:
        self._issued += 1
        return uuid5(self._namespace, str(self._issued))


class ScriptedClock:
    """A clock that returns the time of the scenario step being played."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def set(self, moment: datetime) -> None:
        if moment < self._now:
            raise ValueError("scenario steps must not go back in time")
        self._now = moment

    def __call__(self) -> datetime:
        return self._now


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one scenario step did, including real vector index work."""

    step: DemoStep
    detail: str
    embedded: int = 0
    reused: int = 0
    deleted: int = 0


@dataclass(frozen=True, slots=True)
class DemoBuild:
    """A built demo workspace."""

    workspace: Path
    database: Path
    files: Path
    steps: tuple[StepResult, ...]
    vectors: int


def corpus_text(name: str) -> str:
    """Return a packaged corpus file."""

    return resources.files("konwledge.demo").joinpath("corpus", name).read_text(encoding="utf-8")


def build_demo(
    workspace: Path, *, force: bool = False, steps: Sequence[DemoStep] = DEMO_STEPS
) -> DemoBuild:
    """Play ``steps`` into a fresh library inside ``workspace``."""

    if not steps:
        raise ValueError("a demo scenario needs at least one step")
    root = workspace.expanduser().resolve()
    _prepare_workspace(root, force=force)
    files = root / FILES_DIR
    files.mkdir()
    database = root / DATABASE_NAME

    clock = ScriptedClock(steps[0].at)
    library = KnowledgeLibrary(_session_factory(database), id_factory=SequentialIds(), clock=clock)
    provider = HashEmbeddingProvider()
    index = InMemoryVectorIndex(dimension=provider.dimension)
    indexing = IndexingService(embedding_service=EmbeddingService(provider), vector_index=index)
    results = []
    for step in steps:
        clock.set(step.at)
        results.append(_play(step, library, indexing, files))
    (root / MARKER).write_text("Knowledge CI demo workspace\n", encoding="utf-8")
    return DemoBuild(
        workspace=root,
        database=database,
        files=files,
        steps=tuple(results),
        vectors=len(index),
    )


def export_demo(workspace: Path, *, steps: Sequence[DemoStep] = DEMO_STEPS) -> LibrarySnapshot:
    """Export the snapshot of a built demo workspace, narrated by ``steps``."""

    root = workspace.expanduser().resolve()
    database = root / DATABASE_NAME
    if not (root / MARKER).is_file() or not database.is_file():
        raise DemoWorkspaceError(f"no demo library in {root}; run `knowledge demo build` first")
    return export_snapshot(
        _session_factory(database),
        title=DEMO_TITLE,
        root=root / FILES_DIR,
        story=[
            StoryStep(at=step.at, title=step.title, description=step.description) for step in steps
        ],
    )


def _session_factory(database: Path) -> SessionFactory:
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    create_schema(engine)
    return create_session_factory(engine)


def _prepare_workspace(root: Path, *, force: bool) -> None:
    if root.exists() and not root.is_dir():
        raise DemoWorkspaceError(f"{root} exists and is not a directory")
    if root.is_dir() and any(root.iterdir()):
        if not (root / MARKER).is_file():
            raise DemoWorkspaceError(
                f"{root} is not empty and is not a demo workspace; choose another directory"
            )
        if not force:
            raise DemoWorkspaceError(f"{root} already holds a demo build; pass --force to rebuild")
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    root.mkdir(parents=True, exist_ok=True)


def _play(
    step: DemoStep, library: KnowledgeLibrary, indexing: IndexingService, files: Path
) -> StepResult:
    action = step.action
    if isinstance(action, ImportFile):
        target = files / action.target
        target.write_text(corpus_text(action.corpus), encoding="utf-8", newline="\n")
        [imported] = library.import_path(target).imported
        result = imported.result
        if result.diff is not None:
            report = indexing.apply_diff(result.diff)
            return StepResult(
                step,
                f"changes={result.diff.summary()}",
                embedded=len(report.embedded),
                reused=len(report.reused),
                deleted=report.deleted,
            )
        if result.units_created:
            indexing.index_units(result.units)
            return StepResult(step, f"units={len(result.units)}", embedded=len(result.units))
        return StepResult(step, "unchanged")

    unit = _current_unit(library, files / action.target, action.heading)
    if isinstance(action, MarkStale):
        library.lifecycle.mark_stale(unit.id, reason=action.reason)
        return StepResult(step, f"stale: {action.heading}")
    if isinstance(action, Refresh):
        library.lifecycle.refresh(unit.id, reason=action.reason)
        return StepResult(step, f"refreshed: {action.heading}")
    raise TypeError(f"unsupported demo action: {action!r}")  # pragma: no cover


def _current_unit(library: KnowledgeLibrary, target: Path, heading: str) -> KnowledgeUnit:
    uri = str(target.resolve())
    for source in library.snapshot().sources:
        if source.source.uri != uri:
            continue
        for state in source.units:
            if state.unit.heading == heading:
                return state.unit
    raise LookupError(f"no current unit titled {heading!r} in {target.name}")
