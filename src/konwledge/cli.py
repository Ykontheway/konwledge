"""Typer command-line interface for the local knowledge library."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import UUID

import click
import typer

from konwledge import __version__
from konwledge.application import ImportPathError, KnowledgeLibrary, KnowledgeUnitNotFoundError
from konwledge.demo import DemoWorkspaceError, build_demo, export_demo
from konwledge.domain import FreshnessPolicy, KnowledgeUnit
from konwledge.lifecycle import (
    DocumentNotFoundError,
    LifecycleTransitionError,
    SourceNotFoundError,
    UnitState,
)
from konwledge.snapshot import dump_schema, dump_snapshot
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
    reverted = sum(item.result.reverted for item in report.imported)
    typer.echo(
        f"Imported {len(report.imported)} file(s): {created_documents} document(s) and "
        f"{created_units} unit(s) created; "
        + (f"{reverted} file(s) republished an earlier version; " if reverted else "")
        + f"{len(report.skipped)} unsupported file(s) skipped."
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
