# Konwledge

Konwledge is a local-first toolkit for managing the full lifecycle of knowledge used by AI applications. The project will track where knowledge came from, how it changed, when it became stale, and whether a change improved or degraded retrieval quality.

> Status: pre-alpha. U04 adds deterministic Markdown and TXT loading with idempotent metadata ingestion. The implementation remains intentionally small while the knowledge lifecycle is built incrementally.

## Implemented increments

| Increment | Completed | Outcome |
| --- | --- | --- |
| U01 | 2026-10-12 | Installable package, CLI smoke test, linting, typing, tests, and CI |
| U02 | 2026-10-13 | Frozen domain models with validation and JSON roundtrips |
| U03 | 2026-10-15 | SQLite schema, typed repositories, explicit transactions, and persistence tests |
| U04 | 2026-10-16 | UTF-8 Markdown/TXT loading, normalization, SHA-256 fingerprints, and idempotent ingestion |

Each increment builds on the previous branch, so the U04 branch contains the complete U01-U04 implementation.

## Why this project

Many RAG demos stop after documents can be chunked, embedded, and searched. Konwledge focuses on the work that follows:

- trace every knowledge unit back to its source;
- preserve revisions instead of silently overwriting content;
- identify stale, superseded, deleted, and conflicting knowledge;
- evaluate retrieval after every knowledge or indexing change;
- provide model-ready context with explicit citations.

## Planned delivery

The initial roadmap is split into small, testable uploads:

| Milestone | Target | Outcome |
| --- | --- | --- |
| Project foundation | Days 1–5 | Installable package, tests, linting, typing, and CI |
| Knowledge ingestion | Days 8–17 | Traceable Markdown and text ingestion |
| Knowledge lifecycle | Days 20–32 | Revisions, freshness, status, and conflict candidates |
| Retrieval | Days 35–38 | Hybrid retrieval and citation-aware context assembly |
| Knowledge CI | Days 41–45 | Versioned evaluation set and regression gates |
| Optional integrations | Days 46–60 | Model API, conversations, database schemas, UI, and MCP |

The planned public checkpoints are `v0.0.1-mvp`, `v0.1.0`, and `v0.2.0`.

## Quick start

Konwledge requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --dev
uv run konwledge --version
```

Expected output:

```text
konwledge 0.0.0
```

## Development checks

Run the same checks used by GitHub Actions:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=konwledge --cov-report=term-missing
```

## Repository layout

```text
.
├── .github/workflows/ci.yml
├── src/konwledge/
│   ├── domain/
│   │   ├── __init__.py
│   │   └── models.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── loaders.py
│   │   ├── normalization.py
│   │   └── service.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   ├── repository.py
│   │   ├── tables.py
│   │   └── unit_of_work.py
│   ├── __init__.py
│   ├── __main__.py
│   └── cli.py
├── tests/
├── LICENSE
├── README.md
└── pyproject.toml
```

The remaining modules will be added only when they gain executable behavior: `lifecycle`, `retrieval`, `evaluation`, `providers`, and `api`.

## Domain models

U02 introduces five immutable [Pydantic v2](https://docs.pydantic.dev/) models under `konwledge.domain`:

- `Source` records where imported knowledge originated;
- `Document` identifies one normalized source document and its checksum;
- `KnowledgeUnit` stores a traceable, independently retrievable text range;
- `Revision` records versioned changes without silently overwriting history;
- `Citation` links model-ready context back to an exact knowledge unit.

The models reject unknown fields, require timezone-aware timestamps, and support JSON roundtrips through `model_dump_json()` and `model_validate_json()`. Document checksums use lowercase SHA-256 hex strings. Character ranges use a half-open convention: `start_char` is inclusive and `end_char` is exclusive.

## SQLite metadata storage

U03 persists the domain graph with SQLAlchemy 2.x and SQLite. `Source`, `Document`, `KnowledgeUnit`, `Revision`, and `Citation` each have a typed repository, while `UnitOfWork` keeps commit and rollback boundaries explicit. Revision history is append-only; lifecycle services will create revision records alongside future domain changes.

SQLite foreign-key enforcement is enabled for every connection. Tests use an isolated in-memory database and cover roundtrips, updates, deletes, constraint failures, commits, and rollbacks. Create an application database with:

```python
from konwledge.storage import create_schema, create_sqlite_engine

engine = create_sqlite_engine("sqlite:///konwledge.db")
create_schema(engine)
```

## Text ingestion

U04 loads one UTF-8 `.md`, `.markdown`, or `.txt` file from a stored file `Source`. The loader removes an optional UTF-8 BOM, converts CRLF and CR line endings to LF, applies Unicode NFC normalization, and calculates a lowercase SHA-256 fingerprint. It preserves all other whitespace so later character offsets remain traceable to the normalized text.

`FileIngestionService` returns both the persisted `Document` metadata and the normalized content needed by the U05 splitter. Importing the same source path and normalized content again returns the existing document instead of inserting a duplicate.

```python
from konwledge.ingestion import FileIngestionService
from konwledge.storage import create_session_factory

session_factory = create_session_factory(engine)
result = FileIngestionService(session_factory).ingest(source_id)
print(result.document.checksum, result.created)
```

## License

Licensed under the [Apache License 2.0](LICENSE).
