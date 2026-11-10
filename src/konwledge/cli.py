"""Typer command-line interface for the local knowledge library."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID

import typer
from pydantic import ValidationError

from konwledge import __version__
from konwledge.application import ImportPathError, KnowledgeLibrary, KnowledgeUnitNotFoundError
from konwledge.context import assemble_context, validate_budget
from konwledge.demo import DemoWorkspaceError, build_demo, export_demo
from konwledge.domain import ConflictStatus, FreshnessPolicy, KnowledgeUnit
from konwledge.embedding import EmbeddingService, HashEmbeddingProvider
from konwledge.evaluation import (
    EvaluationReport,
    EvaluationService,
    EvaluationSet,
    dump_evaluation_set,
)
from konwledge.indexing import IndexingService, InMemoryVectorIndex
from konwledge.lifecycle import (
    DocumentNotFoundError,
    LifecycleTransitionError,
    SourceNotFoundError,
    UnitState,
)
from konwledge.retrieval import RetrievalMode, RetrievalResult, RetrievalService
from konwledge.snapshot import dump_schema, dump_snapshot
from konwledge.storage import (
    ImmutableEntityError,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)

DEFAULT_DATABASE = Path(".konwledge/knowledge.db")

app = typer.Typer(
    name="knowledge",
    help="Import and inspect traceable local knowledge.",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)

AtOption = Annotated[
    str | None,
    typer.Option(
        "--at",
        help="ISO 8601 time point, e.g. 2026-10-29T09:30:00+08:00 (naive = local time).",
    ),
]

DatabaseOption = Annotated[
    Path,
    typer.Option(
        "--database",
        "-d",
        help="SQLite database file (created automatically).",
        show_default=True,
        dir_okay=False,
        file_okay=True,
    ),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"knowledge {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = None,
) -> None:
    """Import and inspect traceable local knowledge."""

    if ctx.invoked_subcommand is None and not version:
        typer.echo(ctx.get_help())


def _open_library(database: Path) -> KnowledgeLibrary:
    database_path = database.expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_sqlite_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    create_schema(engine)
    return KnowledgeLibrary(create_session_factory(engine))


def _fail(message: str, *, code: int = 1) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        _fail(f"invalid --at time {value!r}; expected ISO 8601", code=2)
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _build_demo_index(library: KnowledgeLibrary) -> IndexingService:
    """Build one temporary hash index containing every stored unit version."""

    typer.echo(
        "Warning: demo hash embeddings test retrieval plumbing, not semantic quality. "
        "Vectors are temporary and are not persisted.",
        err=True,
    )
    provider = HashEmbeddingProvider()
    indexing = IndexingService(
        embedding_service=EmbeddingService(provider),
        vector_index=InMemoryVectorIndex(dimension=provider.dimension),
    )
    indexing.index_units([unit for summary in library.list_documents() for unit in summary.units])
    return indexing


def _perform_retrieval(
    query: str,
    mode: RetrievalMode,
    top_k: int,
    candidate_limit: int | None,
    rrf_k: int,
    at: str | None,
    include_stale: bool,
    source_id: UUID | None,
    demo_vectors: bool,
    database: Path,
) -> RetrievalResult:
    moment = _parse_time(at)
    if not query.strip():
        _fail("query must contain non-whitespace content", code=2)
    if candidate_limit is not None and candidate_limit < top_k:
        _fail("candidate_limit must be greater than or equal to top_k", code=2)
    if demo_vectors and mode is RetrievalMode.LEXICAL:
        _fail("--demo-vectors requires --mode vector or hybrid", code=2)

    library = _open_library(database)
    search = library.search
    if demo_vectors:
        indexing = _build_demo_index(library)
        engine = create_sqlite_engine(
            f"sqlite+pysqlite:///{database.expanduser().resolve().as_posix()}"
        )
        try:
            search = RetrievalService(
                create_session_factory(engine), indexing_service=indexing
            ).search
            result = search(
                query,
                mode=mode,
                top_k=top_k,
                candidate_limit=candidate_limit,
                rrf_k=rrf_k,
                at=moment,
                include_stale=include_stale,
                source_id=source_id,
            )
        except (ValueError, RuntimeError) as error:
            _fail(str(error), code=2)
        finally:
            engine.dispose()
    else:
        try:
            result = search(
                query,
                mode=mode,
                top_k=top_k,
                candidate_limit=candidate_limit,
                rrf_k=rrf_k,
                at=moment,
                include_stale=include_stale,
                source_id=source_id,
            )
        except ValueError as error:
            _fail(str(error), code=2)
        except RuntimeError as error:
            _fail(str(error))

    if result.degraded:
        typer.echo(
            f"Warning: {len(result.missing_vector_ids)} eligible unit(s) lack usable vectors; "
            "vector recall is incomplete. missing_vector_ids="
            + ",".join(str(unit_id) for unit_id in result.missing_vector_ids),
            err=True,
        )
    return result


def _section(unit: KnowledgeUnit) -> str:
    return " / ".join(unit.section_path) or "<root>"


def _preview(text: str, width: int = 60) -> str:
    line = " ".join(text.split())
    return line if len(line) <= width else line[: width - 1] + "…"


def _status_label(state: UnitState | None) -> str:
    if state is None:
        return "unpublished"
    if state.stale_reason:
        return f"{state.status.value} ({state.stale_reason})"
    return state.status.value


@app.command("import")
def import_command(
    path: Annotated[
        Path,
        typer.Argument(help="Supported file or directory to import recursively."),
    ],
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Import one Markdown/text file or a directory tree."""

    try:
        report = _open_library(database).import_path(path)
    except ImportPathError as error:
        _fail(str(error), code=2)
    except (OSError, UnicodeError) as error:
        _fail(f"could not read import content: {error}")

    created_documents = sum(item.result.ingestion.created for item in report.imported)
    created_units = sum(
        len(item.result.units) if item.result.units_created else 0 for item in report.imported
    )
    for item in report.imported:
        result = item.result
        if result.reverted:
            state = "reverted"
        elif result.diff is not None:
            state = "updated"
        elif result.ingestion.created:
            state = "created"
        else:
            state = "unchanged"
        line = (
            f"{state}: {item.path} | document={result.ingestion.document.id} "
            f"| units={len(result.units)}"
        )
        if result.diff is not None:
            line += f" | changes={result.diff.summary()} | re-embed={len(result.diff.affected)}"
        typer.echo(line)
    reverted = sum(item.result.reverted for item in report.imported)
    typer.echo(
        f"Imported {len(report.imported)} file(s): {created_documents} document(s) and "
        f"{created_units} unit(s) created; "
        + (f"{reverted} file(s) republished an earlier version; " if reverted else "")
        + f"{len(report.skipped)} unsupported file(s) skipped."
    )
    for skipped in report.skipped:
        typer.echo(f"skipped: {skipped}")


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="Literal search text (not raw FTS syntax).")],
    mode: Annotated[
        RetrievalMode, typer.Option("--mode", help="Retrieval route.")
    ] = RetrievalMode.LEXICAL,
    top_k: Annotated[int, typer.Option("--top-k", min=1, help="Maximum results.")] = 5,
    candidate_limit: Annotated[int | None, typer.Option("--candidate-limit", min=1)] = None,
    rrf_k: Annotated[int, typer.Option("--rrf-k", min=1)] = 60,
    at: AtOption = None,
    include_stale: Annotated[
        bool, typer.Option("--include-stale", help="Also search stale head units.")
    ] = False,
    source_id: Annotated[
        UUID | None, typer.Option("--source", help="Restrict to a source UUID.")
    ] = None,
    demo_vectors: Annotated[
        bool,
        typer.Option(
            "--demo-vectors", help="Build temporary offline hash vectors; not semantic embeddings."
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output the complete result as JSON.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Search eligible knowledge, keeping the original evidence and route ranks."""

    result = _perform_retrieval(
        query=query,
        mode=mode,
        top_k=top_k,
        candidate_limit=candidate_limit,
        rrf_k=rrf_k,
        at=at,
        include_stale=include_stale,
        source_id=source_id,
        demo_vectors=demo_vectors,
        database=database,
    )

    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    typer.echo(
        f"Search: mode={result.mode.value} hits={len(result.hits)} "
        f"at={result.evaluated_at.isoformat()} degraded={str(result.degraded).lower()}"
    )
    if not result.hits:
        typer.echo("No matching knowledge found.")
    for rank, hit in enumerate(result.hits, start=1):
        typer.echo(
            f"{rank}. unit={hit.unit.id} status={hit.status.value} score={hit.score:.8f} "
            f"lexical_rank={hit.lexical_rank} vector_rank={hit.vector_rank}"
        )
        typer.echo(
            f"   source={hit.source.uri} document={hit.document.id} lineage={hit.unit.lineage_id} "
            f"range=[{hit.unit.start_char}, {hit.unit.end_char}) section={_section(hit.unit)}"
        )
        typer.echo(hit.unit.text)


@app.command("context")
def context_command(
    query: Annotated[str, typer.Argument(help="Literal search text (not raw FTS syntax).")],
    mode: Annotated[
        RetrievalMode, typer.Option("--mode", help="Retrieval route.")
    ] = RetrievalMode.LEXICAL,
    top_k: Annotated[int, typer.Option("--top-k", min=1, help="Maximum results.")] = 5,
    candidate_limit: Annotated[int | None, typer.Option("--candidate-limit", min=1)] = None,
    rrf_k: Annotated[int, typer.Option("--rrf-k", min=1)] = 60,
    at: AtOption = None,
    include_stale: Annotated[
        bool, typer.Option("--include-stale", help="Also search stale head units.")
    ] = False,
    source_id: Annotated[
        UUID | None, typer.Option("--source", help="Restrict to a source UUID.")
    ] = None,
    demo_vectors: Annotated[
        bool,
        typer.Option(
            "--demo-vectors", help="Build temporary offline hash vectors; not semantic embeddings."
        ),
    ] = False,
    max_chars: Annotated[
        int, typer.Option("--max-chars", min=0, help="Maximum serialized context characters.")
    ] = 8000,
    max_units: Annotated[
        int, typer.Option("--max-units", min=1, help="Maximum evidence units.")
    ] = 10,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output the complete context packet as JSON.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Retrieve once and assemble bounded, source-backed context."""

    try:
        validate_budget(max_chars, max_units)
    except ValueError as error:
        _fail(str(error), code=2)
    result = _perform_retrieval(
        query=query,
        mode=mode,
        top_k=top_k,
        candidate_limit=candidate_limit,
        rrf_k=rrf_k,
        at=at,
        include_stale=include_stale,
        source_id=source_id,
        demo_vectors=demo_vectors,
        database=database,
    )
    try:
        packet = assemble_context(result, max_chars=max_chars, max_units=max_units)
    except ValueError as error:
        _fail(str(error), code=2)

    if json_output:
        typer.echo(packet.model_dump_json(indent=2))
        return

    typer.echo(
        f"Context: mode={packet.mode.value} evidence={len(packet.blocks)} "
        f"skipped={len(packet.skipped)} used_chars={packet.used_chars}/{packet.max_chars} "
        f"at={packet.evaluated_at.isoformat()} degraded={str(packet.degraded).lower()}"
    )
    if packet.context_text:
        typer.echo(packet.context_text, nl=not packet.context_text.endswith("\n"))
    elif result.hits:
        typer.echo("No evidence fits the context limits.")
    else:
        typer.echo("No matching evidence found.")
    for skipped in packet.skipped:
        typer.echo(f"skipped: unit={skipped.unit_id} reason={skipped.reason.value}")


@app.command("list")
def list_command(database: DatabaseOption = DEFAULT_DATABASE) -> None:
    """List imported document versions and their knowledge units."""

    summaries = _open_library(database).list_documents()
    if not summaries:
        typer.echo("Knowledge library is empty. Import content with: knowledge import PATH")
        return

    for summary in summaries:
        document = summary.document
        typer.echo(f"document {document.id}")
        if summary.is_head:
            version = "current"
        elif summary.statuses:
            version = "historical"
        else:
            version = "unpublished"
        typer.echo(f"  version: {version}")
        typer.echo(f"  source: {summary.source.kind.value} {summary.source.uri}")
        typer.echo(f"  source id: {summary.source.id}")
        typer.echo(f"  locator: {document.locator}")
        typer.echo(f"  fingerprint: {document.checksum}")
        typer.echo(f"  units: {len(summary.units)}")
        for unit in summary.units:
            status = summary.statuses.get(unit.id)
            typer.echo(
                f"    unit {unit.id} ordinal={unit.ordinal} section={_section(unit)} "
                f"range=[{unit.start_char}, {unit.end_char}) "
                f"status={'unpublished' if status is None else status.value}"
            )


@app.command("show")
def show_command(
    unit_id: Annotated[UUID, typer.Argument(help="Knowledge unit UUID shown by knowledge list.")],
    at: AtOption = None,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show one knowledge unit, its status, and its source traceability."""

    moment = _parse_time(at)
    library = _open_library(database)
    try:
        detail = library.show_unit(unit_id)
        state = library.unit_state(unit_id, moment)
    except KnowledgeUnitNotFoundError as error:
        _fail(str(error))

    unit = detail.unit
    typer.echo(f"unit id: {unit.id}")
    typer.echo(f"lineage: {unit.lineage_id}")
    when = "" if moment is None else f" (as of {moment.isoformat()})"
    typer.echo(f"status: {_status_label(state)}{when}")
    typer.echo(f"document: {detail.document.id}")
    typer.echo(f"source: {detail.source.kind.value} {detail.source.uri}")
    typer.echo(f"source id: {detail.source.id}")
    typer.echo(f"locator: {detail.document.locator}")
    typer.echo(f"fingerprint: {detail.document.checksum}")
    typer.echo(f"section_path: {_section(unit)}")
    typer.echo(f"heading: {unit.heading or '<none>'}")
    typer.echo(f"range: [{unit.start_char}, {unit.end_char})")
    typer.echo("text:")
    typer.echo(unit.text, nl=not unit.text.endswith("\n"))


@app.command("history")
def history_command(
    unit_id: Annotated[UUID, typer.Argument(help="Any knowledge unit UUID of the lineage.")],
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show the revision chain of a knowledge unit across document versions."""

    try:
        history = _open_library(database).unit_history(unit_id)
    except KnowledgeUnitNotFoundError as error:
        _fail(str(error))

    typer.echo(f"lineage: {history.lineage_id}")
    typer.echo(f"versions: {len(history.versions)}")
    for version in history.versions:
        marker = "*" if version.id == history.unit.id else " "
        status = history.statuses.get(version.id)
        typer.echo(
            f" {marker} unit {version.id} document={version.document_id} "
            f"section={_section(version)} hash={version.content_hash[:12]} "
            f"status={'unpublished' if status is None else status.value}"
        )
    typer.echo(f"revisions: {len(history.revisions)}")
    for revision in history.revisions:
        change = revision.payload.get("change", revision.change_type.value)
        if revision.payload.get("reverted"):
            change = f"{change}, revert"
        target = revision.payload.get("unit_id") or revision.payload.get("previous_unit_id")
        typer.echo(
            f"  rev {revision.revision} {revision.change_type.value} ({change}) "
            f"unit={target} at {revision.created_at.isoformat()}"
        )


@app.command("asof")
def asof_command(
    at: AtOption = None,
    document: Annotated[
        UUID | None,
        typer.Option(
            "--document",
            help="Use the moment this document version was published as the time point.",
        ),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Also list superseded and deleted unit versions."),
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show the knowledge that was effective at a time point (default: now)."""

    if at is not None and document is not None:
        _fail("use either --at or --document, not both", code=2)
    library = _open_library(database)
    moment = _parse_time(at)
    if document is not None:
        try:
            moment = library.lifecycle.document_time(document)
        except DocumentNotFoundError as error:
            _fail(str(error))

    snapshot = library.snapshot(moment, include_retired=show_all)
    typer.echo(f"as of: {'now' if moment is None else moment.isoformat()}")
    if not snapshot.sources:
        typer.echo("No knowledge was published at this time point.")
        return
    for source in snapshot.sources:
        typer.echo(f"source {source.source.kind.value} {source.source.uri}")
        published = "" if source.published_at is None else source.published_at.isoformat()
        head = "<missing>" if source.head is None else str(source.head.id)
        typer.echo(f"  head: document {head} published {published}")
        for state in (*source.units, *source.retired):
            unit = state.unit
            typer.echo(
                f"  {_status_label(state):<10} unit {unit.id} section={_section(unit)} "
                f"| {_preview(unit.text)}"
            )
    counts = snapshot.counts()
    detail = " ".join(f"{name}={count}" for name, count in counts.items() if count or show_all)
    typer.echo(
        f"{len(snapshot.effective_units)} effective unit(s) across {len(snapshot.sources)} "
        f"source(s){' | ' + detail if detail else ''}"
    )


@app.command("stale")
def stale_command(
    unit_id: Annotated[UUID, typer.Argument(help="Current knowledge unit UUID.")],
    reason: Annotated[str, typer.Option("--reason", "-r", help="Why the knowledge is stale.")],
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Mark the current version of a knowledge lineage as stale."""

    _mark(database, unit_id, stale=True, reason=reason)


@app.command("refresh")
def refresh_command(
    unit_id: Annotated[UUID, typer.Argument(help="Stale knowledge unit UUID.")],
    reason: Annotated[
        str | None, typer.Option("--reason", "-r", help="Optional review note.")
    ] = None,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Confirm a stale knowledge unit and make it active again."""

    _mark(database, unit_id, stale=False, reason=reason)


@app.command("reconcile")
def reconcile_command(database: DatabaseOption = DEFAULT_DATABASE) -> None:
    """Rebuild cached unit statuses from the revision log (for upgraded databases)."""

    changed = _open_library(database).lifecycle.reconcile()
    typer.echo(f"Reconciled statuses: {changed} unit(s) updated.")


def _mark(database: Path, unit_id: UUID, *, stale: bool, reason: str | None) -> None:
    lifecycle = _open_library(database).lifecycle
    try:
        if stale:
            revision = lifecycle.mark_stale(unit_id, reason=reason or "")
        else:
            revision = lifecycle.refresh(unit_id, reason=reason)
    except KnowledgeUnitNotFoundError as error:
        _fail(str(error))
    except LifecycleTransitionError as error:
        _fail(str(error), code=2)
    status = "stale" if stale else "active"
    typer.echo(
        f"{status}: unit {unit_id} | lineage={revision.entity_id} rev {revision.revision} "
        f"at {revision.created_at.isoformat()}"
    )


conflicts_app = typer.Typer(
    name="conflicts",
    help="Detect contradictory knowledge and manage human reviews.",
    no_args_is_help=True,
)
app.add_typer(conflicts_app, name="conflicts")


@conflicts_app.command("scan")
def conflicts_scan_command(
    at: AtOption = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Write detected conflicts to the database.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Evaluate source heads and list (or apply) conflict candidates."""

    library = _open_library(database)
    moment = _parse_time(at)

    try:
        scan = library.conflicts.scan(at=moment, dry_run=not apply)
    except ValueError as error:
        _fail(str(error), code=2)

    stored = {conflict.key: conflict for conflict in (*scan.existing, *scan.created)}
    existing_keys = {conflict.key for conflict in scan.existing}
    for candidate in scan.candidates:
        conflict = stored.get(candidate.key)
        prefix = (
            "existing"
            if candidate.key in existing_keys
            else ("created" if conflict is not None else "candidate")
        )
        identity = "" if conflict is None else f" | id={conflict.id} status={conflict.status.value}"
        typer.echo(
            f"{prefix}: {candidate.rule} | key={candidate.key}{identity} | "
            f"left={candidate.left.source_uri} [{candidate.left.value}] | "
            f"right={candidate.right.source_uri} [{candidate.right.value}]"
        )

    typer.echo(
        f"Scan complete: candidates={len(scan.candidates)} created={len(scan.created)} "
        f"existing={len(scan.existing)} | at={scan.evaluated_at.isoformat()} | "
        f"{'applied' if apply else 'dry run'}"
    )


@conflicts_app.command("list")
def conflicts_list_command(
    status: Annotated[
        ConflictStatus | None, typer.Option("--status", help="Filter by current status.")
    ] = None,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """List all detected conflicts."""

    conflicts = _open_library(database).conflicts.list(status=status)
    if not conflicts:
        typer.echo("No conflicts found.")
        return

    for conflict in conflicts:
        typer.echo(
            f"{conflict.id} | {conflict.status.value:<10} | {conflict.rule} | "
            f"{conflict.left.source_uri} vs {conflict.right.source_uri}"
        )


@conflicts_app.command("show")
def conflicts_show_command(
    conflict_id: Annotated[UUID, typer.Argument(help="Conflict UUID.")],
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show one conflict in JSON format."""

    try:
        conflict = _open_library(database).conflicts.get(conflict_id)
    except LookupError as error:
        _fail(str(error))

    typer.echo(conflict.model_dump_json(indent=2))


@conflicts_app.command("review")
def conflicts_review_command(
    conflict_id: Annotated[UUID, typer.Argument(help="Conflict UUID.")],
    status: Annotated[
        ConflictStatus, typer.Option("--status", help="New status for the conflict.")
    ],
    reason: Annotated[str, typer.Option("--reason", "-r", help="Why this status was chosen.")],
    reviewer: Annotated[
        str | None, typer.Option("--reviewer", help="Who performed the review.")
    ] = None,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Record a human decision on a conflict candidate."""

    try:
        conflict = _open_library(database).conflicts.review(
            conflict_id, status=status, reason=reason, reviewer=reviewer
        )
    except LookupError as error:
        _fail(str(error))
    except ValueError as error:
        _fail(str(error), code=2)

    typer.echo(f"reviewed: {conflict.id} status={conflict.status.value}")


@conflicts_app.command("history")
def conflicts_history_command(
    conflict_id: Annotated[UUID, typer.Argument(help="Conflict UUID.")],
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show the revision chain of one conflict."""

    try:
        revisions = _open_library(database).conflicts.history(conflict_id)
    except LookupError as error:
        _fail(str(error))

    typer.echo(f"conflict: {conflict_id}")
    typer.echo(f"revisions: {len(revisions)}")
    for revision in revisions:
        change = revision.payload.get("change", "unknown")
        typer.echo(
            f"  rev {revision.revision} {revision.change_type.value} ({change}) "
            f"at {revision.created_at.isoformat()}"
        )
        if change == "reviewed":
            typer.echo(
                f"    {revision.payload['from_status']} -> {revision.payload['to_status']} | "
                f"reason={revision.payload['reason']} | reviewer={revision.payload.get('reviewer')}"
            )
        elif change == "detected":
            typer.echo(f"    key={revision.payload['key']} | status={revision.payload['status']}")


@app.command("publishable")
def publishable_command(
    at: AtOption = None,
    include_stale: Annotated[
        bool, typer.Option("--include-stale", help="Include units marked as stale.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """List knowledge units that are ready for retrieval."""

    library = _open_library(database)
    moment = _parse_time(at)
    units = library.publishable_units(moment, include_stale=include_stale)
    if not units:
        if moment:
            typer.echo(f"No publishable knowledge as of {moment.isoformat()}.")
        else:
            typer.echo("Knowledge library is empty or has no publishable units.")
        return

    for state in units:
        typer.echo(f"unit {state.unit.id} status={state.status.value}")


evaluation_app = typer.Typer(
    name="evaluation",
    help="Manage evaluation datasets and run retrieval regressions.",
    no_args_is_help=True,
)
app.add_typer(evaluation_app, name="evaluation")


@evaluation_app.command("import")
def evaluation_import_command(
    path: Annotated[Path, typer.Argument(help="JSON file containing the evaluation set.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Output the imported dataset as JSON.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Validate and import one evaluation dataset version."""

    try:
        content = path.read_text(encoding="utf-8")
        dataset = EvaluationSet.model_validate_json(content)
    except (OSError, UnicodeError, ValidationError) as error:
        _fail(f"could not parse evaluation set: {error}", code=2)

    library = _open_library(database)
    try:
        result = library.evaluation.import_set(dataset)
    except (ImmutableEntityError, ValueError) as error:
        _fail(str(error))

    if json_output:
        typer.echo(dump_evaluation_set(result.dataset), nl=False)
        return

    action = "imported" if result.created else "existing"
    typer.echo(
        f"{action}: {result.dataset.name} v{result.dataset.version} ({result.dataset.id}) | "
        f"items={len(result.dataset.items)} hash={result.dataset.content_hash[:12]}"
    )


@evaluation_app.command("list")
def evaluation_list_command(
    json_output: Annotated[bool, typer.Option("--json", help="Output the list as JSON.")] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """List the latest version of each evaluation dataset."""

    library = _open_library(database)
    datasets = library.evaluation.list()

    if json_output:
        typer.echo(json.dumps([d.model_dump(mode="json") for d in datasets], indent=2))
        return

    if not datasets:
        typer.echo("No evaluation datasets found.")
        return

    for dataset in datasets:
        typer.echo(
            f"{dataset.id} | v{dataset.version:<3} | {dataset.name} | "
            f"items={len(dataset.items)} hash={dataset.content_hash[:12]}"
        )


@evaluation_app.command("show")
def evaluation_show_command(
    set_id: Annotated[UUID, typer.Argument(help="Evaluation set UUID.")],
    version: Annotated[
        int | None, typer.Option("--version", min=1, help="Explicit version.")
    ] = None,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show one evaluation dataset version in canonical JSON format."""

    library = _open_library(database)
    try:
        dataset = library.evaluation.get(set_id, version=version)
    except LookupError as error:
        _fail(str(error))

    typer.echo(dump_evaluation_set(dataset), nl=False)


@evaluation_app.command("history")
def evaluation_history_command(
    set_id: Annotated[UUID, typer.Argument(help="Evaluation set UUID.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Output the history as JSON.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show the version history of one evaluation dataset."""

    library = _open_library(database)
    try:
        history = library.evaluation.history(set_id)
    except LookupError as error:
        _fail(str(error))

    if json_output:
        typer.echo(json.dumps([d.model_dump(mode="json") for d in history], indent=2))
        return

    typer.echo(f"dataset: {set_id} ({history[0].name})")
    typer.echo(f"versions: {len(history)}")
    for dataset in history:
        typer.echo(
            f"  v{dataset.version:<3} | hash={dataset.content_hash[:12]} | "
            f"items={len(dataset.items)} at {dataset.created_at.isoformat()}"
        )


@evaluation_app.command("export")
def evaluation_export_command(
    set_id: Annotated[UUID, typer.Argument(help="Evaluation set UUID.")],
    version: Annotated[
        int | None, typer.Option("--version", min=1, help="Explicit version.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to file instead of stdout.")
    ] = None,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Export one evaluation dataset version to canonical JSON."""

    library = _open_library(database)
    try:
        dataset = library.evaluation.get(set_id, version=version)
    except LookupError as error:
        _fail(str(error))

    content = dump_evaluation_set(dataset)
    if output:
        try:
            output.write_text(content, encoding="utf-8")
            typer.echo(f"exported: {output}")
        except OSError as error:
            _fail(f"could not write output: {error}")
    else:
        typer.echo(content, nl=False)


@evaluation_app.command("run")
def evaluation_run_command(
    set_id: Annotated[UUID, typer.Argument(help="Evaluation set UUID.")],
    version: Annotated[
        int | None, typer.Option("--version", min=1, help="Explicit version.")
    ] = None,
    mode: Annotated[
        RetrievalMode, typer.Option("--mode", help="Retrieval route.")
    ] = RetrievalMode.LEXICAL,
    top_k: Annotated[int, typer.Option("--top-k", min=1, help="Maximum results.")] = 5,
    candidate_limit: Annotated[int | None, typer.Option("--candidate-limit", min=1)] = None,
    rrf_k: Annotated[int, typer.Option("--rrf-k", min=1)] = 60,
    at: AtOption = None,
    demo_vectors: Annotated[
        bool,
        typer.Option(
            "--demo-vectors", help="Build temporary offline hash vectors; not semantic embeddings."
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output the complete report as JSON.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Run retrieval for every item in a dataset and compute aggregate metrics."""

    moment = _parse_time(at)
    if candidate_limit is not None and candidate_limit < top_k:
        _fail("candidate_limit must be greater than or equal to top_k", code=2)
    if demo_vectors and mode is RetrievalMode.LEXICAL:
        _fail("--demo-vectors requires --mode vector or hybrid", code=2)

    library = _open_library(database)
    evaluation = library.evaluation

    if demo_vectors:
        indexing = _build_demo_index(library)
        engine = create_sqlite_engine(
            f"sqlite+pysqlite:///{database.expanduser().resolve().as_posix()}"
        )
        try:
            # Re-construct service with temporary indexing
            evaluation = EvaluationService(
                create_session_factory(engine),
                RetrievalService(create_session_factory(engine), indexing_service=indexing),
            )
            report = evaluation.run(
                set_id,
                version=version,
                mode=mode,
                top_k=top_k,
                candidate_limit=candidate_limit,
                rrf_k=rrf_k,
                at=moment,
            )
        except (LookupError, ValueError, RuntimeError) as error:
            _fail(str(error), code=2 if isinstance(error, ValueError) else 1)
        finally:
            engine.dispose()
    else:
        try:
            report = evaluation.run(
                set_id,
                version=version,
                mode=mode,
                top_k=top_k,
                candidate_limit=candidate_limit,
                rrf_k=rrf_k,
                at=moment,
            )
        except LookupError as error:
            _fail(str(error))
        except ValueError as error:
            _fail(str(error), code=2)
        except RuntimeError as error:
            _fail(str(error))

    _warn_evaluation_degradation(report)
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
        return

    _print_evaluation_report(report)


def _warn_evaluation_degradation(report: EvaluationReport) -> None:
    for case in report.results:
        if not case.retrieval.degraded:
            continue
        ids = ",".join(str(unit_id) for unit_id in case.retrieval.missing_vector_ids)
        typer.echo(
            f"Warning: evaluation set {report.dataset.id} item {case.item_id}: "
            f"{len(case.retrieval.missing_vector_ids)} eligible unit(s) lack usable vectors; "
            f"vector recall is incomplete. missing_vector_ids={ids}",
            err=True,
        )


def _print_evaluation_report(report: EvaluationReport) -> None:
    typer.echo(
        f"Evaluation: {report.dataset.name} v{report.dataset.version} "
        f"hash={report.dataset_hash[:12]}"
    )
    typer.echo(
        f"Settings: mode={report.mode.value} top_k={report.top_k} "
        f"candidate_limit={report.candidate_limit} rrf_k={report.rrf_k} "
        f"at={report.evaluated_at.isoformat()}"
    )

    m = report.metrics
    typer.echo(
        f"Cases: total={m.case_count} answerable={m.answerable_count} no-answer={m.no_answer_count}"
    )

    def fmt(val: float | None) -> str:
        return f"{val:.4f}" if val is not None else "n/a"

    typer.echo(
        f"Metrics: Recall={fmt(m.mean_recall)} Precision={fmt(m.mean_precision)} "
        f"MRR={fmt(m.mean_reciprocal_rank)} nDCG={fmt(m.mean_ndcg)}"
    )
    typer.echo(
        f"Summary: HitRate={fmt(m.hit_rate)} NoAnswerAcc={fmt(m.no_answer_accuracy)} "
        f"degraded={str(report.degraded).lower()}"
    )

    if report.degraded:
        typer.echo(
            f"Vector coverage: missing={len(report.missing_vector_ids)} "
            "(see per-case stderr warnings)"
        )

    item_by_id = {item.id: item for item in report.dataset.items}
    for res in report.results:
        case_status = "no-answer" if item_by_id[res.item_id].expect_no_answer else "answerable"
        no_answer = (
            "n/a"
            if res.metrics.no_answer_success is None
            else str(res.metrics.no_answer_success).lower()
        )
        line = (
            f"  {res.item_id:<20} | status={case_status} recall={fmt(res.metrics.recall)} "
            f"precision={fmt(res.metrics.precision)} mrr={fmt(res.metrics.reciprocal_rank)} "
            f"ndcg={fmt(res.metrics.ndcg)} hit={fmt(res.metrics.hit_rate)} "
            f"no_answer={no_answer} degraded={str(res.retrieval.degraded).lower()}"
        )
        typer.echo(line)


freshness_app = typer.Typer(
    name="freshness",
    help="Manage knowledge freshness policies and stale scans.",
    no_args_is_help=True,
)
app.add_typer(freshness_app, name="freshness")


@freshness_app.command("configure")
def freshness_configure_command(
    source_id: Annotated[UUID, typer.Argument(help="Source UUID.")],
    ttl_days: Annotated[
        int | None,
        typer.Option("--ttl-days", help="Days before head units become stale (strictly positive)."),
    ] = None,
    source_updated_at: Annotated[
        str | None,
        typer.Option(
            "--source-updated-at",
            help="ISO 8601 time when source content changed; marks head stale.",
        ),
    ] = None,
    disable: Annotated[
        bool, typer.Option("--disable", help="Disable scanning without clearing stale marks.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Configure a freshness policy for one source."""

    if disable and (ttl_days is not None or source_updated_at is not None):
        _fail("--disable cannot be combined with other settings", code=2)
    if not disable and ttl_days is None and source_updated_at is None:
        _fail("one of --ttl-days, --source-updated-at, or --disable is required", code=2)
    if ttl_days is not None and ttl_days <= 0:
        _fail("--ttl-days must be a positive integer", code=2)

    updated_at = None
    if source_updated_at is not None:
        try:
            updated_at = datetime.fromisoformat(source_updated_at.strip())
        except ValueError:
            _fail(f"invalid --source-updated-at time {source_updated_at!r}", code=2)
        if updated_at.tzinfo is None:
            _fail("--source-updated-at must be timezone-aware (e.g. +08:00)", code=2)

    try:
        policy = (
            FreshnessPolicy()
            if disable
            else FreshnessPolicy(
                ttl_seconds=ttl_days * 86400 if ttl_days is not None else None,
                source_updated_at=updated_at,
            )
        )
    except ValueError as error:
        _fail(str(error), code=2)

    library = _open_library(database)
    try:
        revision = library.freshness.configure(source_id, policy)
    except SourceNotFoundError as error:
        _fail(str(error))
    except ValueError as error:
        _fail(str(error), code=2)

    status = "disabled" if disable else "configured"
    typer.echo(
        f"freshness {status}: source {source_id} | rev {revision.revision} "
        f"at {revision.created_at.isoformat()}"
    )


@freshness_app.command("show")
def freshness_show_command(
    source_id: Annotated[UUID, typer.Argument(help="Source UUID.")],
    at: AtOption = None,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show the current or historical freshness policy of a source."""

    library = _open_library(database)
    moment = _parse_time(at)
    try:
        policy = library.freshness.policy(source_id, at=moment)
    except SourceNotFoundError as error:
        _fail(str(error))
    except ValueError as error:
        _fail(str(error), code=2)
    if policy is None:
        typer.echo(f"Source {source_id} has no freshness policy.")
        return

    typer.echo(json.dumps(policy.model_dump(mode="json"), indent=2))


@freshness_app.command("scan")
def freshness_scan_command(
    source_id: Annotated[
        UUID | None, typer.Option("--source", help="Restrict to one source.")
    ] = None,
    at: AtOption = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Write stale marks to the database.")
    ] = False,
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Evaluate source heads and list (or apply) stale marks."""

    library = _open_library(database)
    moment = _parse_time(at)

    try:
        scan = library.freshness.scan(at=moment, dry_run=not apply, source_id=source_id)
    except SourceNotFoundError as error:
        _fail(str(error))
    except ValueError as error:
        _fail(str(error), code=2)

    for decision in scan.decisions:
        if not decision.reason_codes:
            continue
        status = "stale"
        if scan.marked and any(m.entity_id == decision.lineage_id for m in scan.marked):
            status = "marked"
        typer.echo(
            f"{status}: unit {decision.unit_id} lineage={decision.lineage_id} "
            f"source={decision.source_id} reasons={decision.reason}"
        )

    expired = sum(decision.expired for decision in scan.decisions)
    pending = sum(
        decision.expired and decision.status.value == "active" for decision in scan.decisions
    )
    marks = len(scan.marked) if apply else pending
    typer.echo(
        f"Scan complete: {len(scan.decisions)} unit(s) evaluated at "
        f"{scan.evaluated_at.isoformat()} | {expired} expired unit(s) "
        f"| {marks} stale mark(s) {'appended' if apply else 'detected (dry run)'}."
    )


demo_app = typer.Typer(
    name="demo",
    help="Build the scripted demo library and export its snapshot for the demo site.",
    no_args_is_help=True,
)
app.add_typer(demo_app, name="demo")

DEFAULT_DEMO_WORKSPACE = Path(".konwledge/demo")
DEFAULT_SNAPSHOT_DIR = Path("demo")
SNAPSHOT_FILE = "snapshot.json"
SCHEMA_FILE = "snapshot.schema.json"

WorkspaceOption = Annotated[
    Path,
    typer.Option(
        "--workspace",
        "-w",
        help="Directory holding the demo database and working files.",
        show_default=True,
        file_okay=False,
    ),
]


@demo_app.command("build")
def demo_build_command(
    workspace: WorkspaceOption = DEFAULT_DEMO_WORKSPACE,
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild an existing demo workspace.")
    ] = False,
) -> None:
    """Play the demo story into a fresh, deterministic library."""

    try:
        build = build_demo(workspace, force=force)
    except DemoWorkspaceError as error:
        _fail(str(error), code=2)
    for result in build.steps:
        line = f"{result.step.at.isoformat()} | {result.step.title} | {result.detail}"
        if result.embedded or result.reused or result.deleted:
            line += f" | embedded={result.embedded} reused={result.reused} deleted={result.deleted}"
        typer.echo(line)
    typer.echo(
        f"Demo library built in {build.workspace} ({len(build.steps)} steps, "
        f"{build.vectors} vectors in the index)."
    )


@demo_app.command("export")
def demo_export_command(
    workspace: WorkspaceOption = DEFAULT_DEMO_WORKSPACE,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            help=f"Directory for {SNAPSHOT_FILE} and {SCHEMA_FILE}.",
            show_default=True,
            file_okay=False,
        ),
    ] = DEFAULT_SNAPSHOT_DIR,
    check: Annotated[
        bool,
        typer.Option("--check", help="Fail if the files on disk differ instead of writing."),
    ] = False,
) -> None:
    """Export the demo snapshot and its JSON Schema."""

    try:
        snapshot = export_demo(workspace)
    except DemoWorkspaceError as error:
        _fail(str(error), code=2)
    outputs = {
        output_dir / SNAPSHOT_FILE: dump_snapshot(snapshot),
        output_dir / SCHEMA_FILE: dump_schema(),
    }
    if check:
        stale = [
            path
            for path, text in outputs.items()
            if not path.is_file() or path.read_bytes() != text.encode("utf-8")
        ]
        if stale:
            names = ", ".join(str(path) for path in stale)
            _fail(
                f"demo snapshot is out of date: {names}. Regenerate with: "
                "knowledge demo build --force && knowledge demo export"
            )
        typer.echo(f"Demo snapshot is up to date ({len(outputs)} files checked).")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, text in outputs.items():
        path.write_bytes(text.encode("utf-8"))
        typer.echo(f"wrote {path}")
    stats = snapshot.stats
    typer.echo(
        f"schema v{snapshot.schema_version}: {stats.sources} source(s), "
        f"{stats.documents} version(s), {stats.units} unit(s), {stats.lineages} lineage(s), "
        f"{len(snapshot.timepoints)} time point(s)"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning an exit code for console and direct callers."""

    try:
        # Let Typer handle its own Click implementation and usage exceptions.
        # Console and direct callers receive the same formatted errors and code.
        app(
            args=None if argv is None else list(argv),
            prog_name="knowledge",
            standalone_mode=True,
        )
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    return 0
