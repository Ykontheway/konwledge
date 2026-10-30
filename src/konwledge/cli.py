"""Typer command-line interface for the local knowledge library."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID

import click
import typer

from konwledge import __version__
from konwledge.application import ImportPathError, KnowledgeLibrary, KnowledgeUnitNotFoundError
from konwledge.domain import KnowledgeUnit
from konwledge.lifecycle import DocumentNotFoundError, LifecycleTransitionError, UnitState
from konwledge.storage import create_schema, create_session_factory, create_sqlite_engine

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
    typer.echo(
        f"Imported {len(report.imported)} file(s): {created_documents} document(s) and "
        f"{created_units} unit(s) created; {len(report.skipped)} unsupported file(s) skipped."
    )
    for skipped in report.skipped:
        typer.echo(f"skipped: {skipped}")


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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning an exit code for console and direct callers."""

    try:
        # With standalone_mode=False Click *returns* the code of typer.Exit
        # instead of raising it, so the return value carries failures too.
        result = app(
            args=None if argv is None else list(argv),
            prog_name="knowledge",
            standalone_mode=False,
        )
    except click.exceptions.Exit as error:  # pragma: no cover - defensive
        return error.exit_code
    except click.ClickException as error:
        error.show()
        return error.exit_code
    return result if isinstance(result, int) else 0
