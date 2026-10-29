"""Typer command-line interface for the local knowledge library."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated
from uuid import UUID

import click
import typer

from konwledge import __version__
from konwledge.application import ImportPathError, KnowledgeLibrary, KnowledgeUnitNotFoundError
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


def _fail(message: str, *, code: int = 1) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


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
        state = "created" if item.result.ingestion.created else "unchanged"
        typer.echo(
            f"{state}: {item.path} | document={item.result.ingestion.document.id} "
            f"| units={len(item.result.units)}"
        )
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
        typer.echo(f"  source: {summary.source.kind.value} {summary.source.uri}")
        typer.echo(f"  locator: {document.locator}")
        typer.echo(f"  fingerprint: {document.checksum}")
        typer.echo(f"  units: {len(summary.units)}")
        for unit in summary.units:
            section = " / ".join(unit.section_path) or "<root>"
            typer.echo(
                f"    unit {unit.id} ordinal={unit.ordinal} section={section} "
                f"range=[{unit.start_char}, {unit.end_char})"
            )


@app.command("show")
def show_command(
    unit_id: Annotated[UUID, typer.Argument(help="Knowledge unit UUID shown by knowledge list.")],
    database: DatabaseOption = DEFAULT_DATABASE,
) -> None:
    """Show one knowledge unit and its source traceability."""

    try:
        detail = _open_library(database).show_unit(unit_id)
    except KnowledgeUnitNotFoundError as error:
        _fail(str(error))

    unit = detail.unit
    typer.echo(f"unit id: {unit.id}")
    typer.echo(f"document: {detail.document.id}")
    typer.echo(f"source: {detail.source.kind.value} {detail.source.uri}")
    typer.echo(f"locator: {detail.document.locator}")
    typer.echo(f"fingerprint: {detail.document.checksum}")
    typer.echo(f"section_path: {' / '.join(unit.section_path) or '<root>'}")
    typer.echo(f"heading: {unit.heading or '<none>'}")
    typer.echo(f"range: [{unit.start_char}, {unit.end_char})")
    typer.echo("text:")
    typer.echo(unit.text, nl=not unit.text.endswith("\n"))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning an exit code for console and direct callers."""

    try:
        app(args=None if argv is None else list(argv), prog_name="knowledge", standalone_mode=False)
    except click.exceptions.Exit as error:
        return error.exit_code
    except click.ClickException as error:
        error.show()
        return error.exit_code
    return 0
