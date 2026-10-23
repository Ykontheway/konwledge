# Konwledge

Konwledge is a local-first toolkit for managing the full lifecycle of knowledge used by AI applications. The project will track where knowledge came from, how it changed, when it became stale, and whether a change improved or degraded retrieval quality.

> Status: pre-alpha. U08 adds a vector index layer on top of the U07 embedding service: a backend-agnostic `VectorIndex` protocol, a pure-Python in-memory implementation with top-K cosine search and metadata filters, an optional LanceDB adapter, and an `IndexingService` that turns `KnowledgeUnit` objects into persisted vectors. Repeated runs produce the same ranking without any network access.

## Implemented increments

| Increment | Completed | Outcome |
| --- | --- | --- |
| U01 | 2026-10-12 | Installable package, CLI smoke test, linting, typing, tests, and CI |
| U02 | 2026-10-13 | Frozen domain models with validation and JSON roundtrips |
| U03 | 2026-10-15 | SQLite schema, typed repositories, explicit transactions, and persistence tests |
| U04 | 2026-10-16 | UTF-8 Markdown/TXT loading, normalization, SHA-256 fingerprints, and idempotent ingestion |
| U05 | 2026-10-19 | Heading-aware splitting, exact character ranges, section paths, and persisted traceability |
| U06 | 2026-10-20 | Typer import/list/show CLI, recursive corpus discovery, examples, and automatic local database setup |
| U07 | 2026-10-21 | Embedding provider protocol, deterministic hash test provider, batching service, and provider/model cache keys |
| U08 | 2026-10-23 | `VectorIndex` protocol, in-memory top-K search with metadata filters, optional LanceDB adapter, and `IndexingService` for `KnowledgeUnit` vectors |

Each increment builds on the previous branch, so the U08 branch contains the complete U01-U08 implementation.

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
uv run knowledge --version
```

Expected output:

```text
knowledge 0.0.0
```

## Import CLI

The `knowledge` entry point creates the SQLite schema and parent directory automatically. Its
default database is `.konwledge/knowledge.db`; pass `--database` (or `-d`) to every command when
you want another explicit location. From an empty checkout, the shortest end-to-end path is:

```bash
uv run knowledge import examples --database .konwledge/demo.db
uv run knowledge list --database .konwledge/demo.db
uv run knowledge show <unit-id-from-list> --database .konwledge/demo.db
```

`import` accepts one `.md`, `.markdown`, or `.txt` file, or recursively discovers those formats in
a directory. Files are processed in stable relative-path order; unrelated file formats in a
directory are skipped and summarized. Reimporting unchanged content is idempotent, while changed
content creates a new immutable document version and new traceable units.

The legacy `konwledge` executable remains an alias for compatibility. Local SQLite databases are
runtime data: keep them under `.konwledge/` or use a `*.db`/`*.sqlite` name, and do not commit them.
These paths are covered by `.gitignore`.

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
│   ├── embedding/
│   │   ├── __init__.py
│   │   ├── providers.py
│   │   └── service.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── loaders.py
│   │   ├── normalization.py
│   │   ├── pipeline.py
│   │   ├── service.py
│   │   └── splitting.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   ├── repository.py
│   │   ├── tables.py
│   │   └── unit_of_work.py
│   ├── __init__.py
│   ├── __main__.py
│   ├── application.py
│   └── cli.py
├── examples/
│   ├── getting-started.md
│   └── lifecycle.txt
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

The models reject unknown fields, require timezone-aware timestamps, and support JSON roundtrips through `model_dump_json()` and `model_validate_json()`. Document checksums use lowercase SHA-256 hex strings. Knowledge units preserve exact text whitespace, use half-open character ranges, and carry their current heading and full section path.

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

## Traceable splitting

U05 turns normalized document content into persisted `KnowledgeUnit` records. Markdown headings define section boundaries and hierarchical paths; headings inside fenced code blocks are ignored. Plain text and heading-free Markdown fall back to paragraph boundaries.

Every unit stores an exact half-open range `[start_char, end_char)`. The invariant `content[start_char:end_char] == unit.text` makes the original location directly recoverable. `TraceableIngestionService` connects U04 ingestion with idempotent unit creation, while an existing document version reuses its stored units.

```python
from konwledge.ingestion import (
    FileIngestionService,
    KnowledgeUnitService,
    TraceableIngestionService,
)

service = TraceableIngestionService(
    FileIngestionService(session_factory),
    KnowledgeUnitService(session_factory),
)
result = service.ingest(source_id)
for unit in result.units:
    assert result.ingestion.content[unit.start_char : unit.end_char] == unit.text
```

## Embedding provider

U07 introduces an offline-friendly embedding layer. The `EmbeddingProvider` protocol defines the minimal surface a backend must expose (`name`, `model`, `dimension`, and `embed`). `HashEmbeddingProvider` ships as a deterministic SHA-256 provider so tests never require network access or API keys.

`EmbeddingService` adds batching, dimension checks, and a provider/model-aware cache key. The default `InMemoryEmbeddingCache` keeps repeated calls cheap and idempotent; cache keys compose `(provider_name, model, text)` and remain stable across runs.

```python
from konwledge.embedding import (
    EmbeddingService,
    HashEmbeddingProvider,
    InMemoryEmbeddingCache,
    embedding_cache_key,
)

service = EmbeddingService(
    HashEmbeddingProvider(dimension=32),
    cache=InMemoryEmbeddingCache(),
    batch_size=8,
)
vectors = service.embed(["alpha", "beta", "alpha"])
assert vectors[0] == vectors[2]
assert embedding_cache_key("hash", "sha256-v1", "alpha") == service.cache_key("alpha")
```

## Vector index

U08 turns embeddings into a searchable store. The `VectorIndex` protocol captures `upsert`, `delete`, `search`, and `rebuild`; the default `InMemoryVectorIndex` ships a dependency-free cosine search with stable id-tiebreak ranking and metadata filters (`{"status": "active"}`, `{"tags": {"in": [...]}}`, `{"status": {"not": [...]}}`). An optional `LanceVectorIndex` adapter targets LanceDB when `pip install lancedb pyarrow` is available; the module imports cleanly without the dependency and raises a clear install hint only when the adapter is instantiated.

`IndexingService` wires the embedding service and the vector index together so you can embed and persist `KnowledgeUnit` objects in one call, run top-K searches by raw text, and rebuild the entire index deterministically:

```python
from konwledge.embedding import EmbeddingService, HashEmbeddingProvider, InMemoryEmbeddingCache
from konwledge.indexing import IndexingService, InMemoryVectorIndex

embeddings = EmbeddingService(HashEmbeddingProvider(dimension=32), cache=InMemoryEmbeddingCache())
indexing = IndexingService(
    embedding_service=embeddings,
    vector_index=InMemoryVectorIndex(dimension=32),
)
indexing.index_units(units)                       # upsert vectors + traceable payload
hits = indexing.search("python testing patterns", top_k=5, filters={"status": "active"})
indexing.remove_units([units[0].id])              # delete a single unit by id
indexing.rebuild(units)                           # atomically replace the whole index
```

Repeating `search` with identical input always returns the same ranking: `InMemoryVectorIndex`
breaks ties on `id` after sorting by descending cosine score, and `IndexingService` reuses the
embedding cache so vectors never silently drift between runs.

## License

Licensed under the [Apache License 2.0](LICENSE).
