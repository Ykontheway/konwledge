# Konwledge

Konwledge is a local-first toolkit for managing the full lifecycle of knowledge used by AI applications. The project will track where knowledge came from, how it changed, when it became stale, and whether a change improved or degraded retrieval quality.

> Status: pre-alpha. U16 adds portable retrieval baselines, deterministic comparisons, and fail-closed regression gates on top of U15's immutable evaluation reports. The demo frontend remains paused at V2; these increments change only the Python core and CLI. The deterministic V1 snapshot and its schema stay unchanged.

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
| U09 | 2026-10-29 | Unit-level document diff, stable lineages, append-only revision chains, partial index rebuilds, `knowledge history`, and U08 LanceDB hardening |
| U10 | 2026-10-30 | Publication timeline, `active`/`stale`/`superseded`/`deleted` statuses, as-of snapshots, revert handling, `knowledge asof`/`stale`/`refresh`/`reconcile` |
| V1 | 2026-11-02 | Demo corpus and story, deterministic `knowledge demo build`, `knowledge demo export` with a Pydantic snapshot model, JSON Schema, golden-file tests, and a CI drift check |
| V2 | 2026-11-03 | Static demo site in `web/` (Vite, React, TypeScript, Tailwind): overview, traceability, and version-diff pages rendered from the snapshot; types generated from the JSON Schema; Vitest; GitHub Pages workflow |
| U11 | 2026-11-06 | Source-scoped TTL and known source-update policies; append-only configuration; atomic dry-run/apply stale scans; active-only publication API and CLI; controlled-clock regression tests |
| U12 | 2026-11-09 | Deterministic service-port conflict candidates, frozen two-sided evidence, atomic persistence, human review states and audit chain, historical preview and conflict CLI |
| U13 | 2026-11-09 | Transactionally synchronized SQLite FTS5, vector retrieval, deterministic RRF fusion, active-only/source/as-of filtering, missing-vector diagnostics, and search CLI |
| U14 | 2026-11-10 | Deterministic exact citations, whole-unit context budgets, duplicate/overlap filtering, source-safe JSON records, and context SDK/CLI |
| U15 | 2026-11-10 | Immutable versioned evaluation sets, retrieval metrics, read-only reports, and evaluation SDK/CLI |
| U16 | 2026-11-10 | Portable healthy baselines, deterministic report comparison, policy-driven regression gates, and CI-ready exit codes |

Each increment builds on the preceding implementation. This branch contains U01-U16, V1, and V2. `U` slices grow the core library; `V` slices build the read-only demo site on top of it. Further frontend work is paused while the core lifecycle slices advance.

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
uv run knowledge history <unit-id-from-list> --database .konwledge/demo.db
uv run knowledge asof --all --database .konwledge/demo.db
uv run knowledge search "deployment" --database .konwledge/demo.db
```

`import` accepts one `.md`, `.markdown`, or `.txt` file, or recursively discovers those formats in
a directory. Files are processed in stable relative-path order; unrelated file formats in a
directory are skipped and summarized. Reimporting unchanged content is idempotent, while changed
content creates a new immutable document version whose units are diffed against the previous
version (see [Incremental updates](#incremental-updates)).

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
uv run knowledge demo build --force && uv run knowledge demo export --check
```

The demo site has its own CI job:

```bash
cd web
pnpm install --frozen-lockfile
pnpm verify        # typecheck → schema sync → Vitest → production build
```

Both the CI job and the Pages deployment run `pnpm verify`; publishing stops if any
check fails. Keep `strict`, `noUnusedLocals`, and `noUnusedParameters` enabled. During
page development, run `pnpm typecheck` after each component change, then `pnpm verify`
before committing. Vitest covers snapshot mapping, Unicode highlight ranges, and
server-rendering smoke tests for the three demo pages; it does not replace browser
interaction or visual testing.

The optional LanceDB adapter has its own CI job. Run it locally with:

```bash
uv sync --dev --extra lancedb
uv run pytest tests/test_indexing.py tests/test_lifecycle.py -k lance
```

## Repository layout

```text
.
├── .github/workflows/
│   ├── ci.yml
│   └── deploy-demo.yml
├── src/konwledge/
│   ├── domain/
│   │   ├── __init__.py
│   │   ├── evaluation.py
│   │   └── models.py
│   ├── embedding/
│   │   ├── __init__.py
│   │   ├── providers.py
│   │   └── service.py
│   ├── indexing/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── lance.py
│   │   ├── memory.py
│   │   └── service.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── loaders.py
│   │   ├── normalization.py
│   │   ├── pipeline.py
│   │   ├── service.py
│   │   └── splitting.py
│   ├── demo/
│   │   ├── corpus/          # packaged story files (guide v1-v3, on-call FAQ)
│   │   ├── __init__.py
│   │   ├── builder.py
│   │   └── scenario.py
│   ├── lifecycle/
│   │   ├── __init__.py
│   │   ├── diff.py
│   │   ├── revisions.py
│   │   ├── service.py
│   │   └── status.py
│   ├── retrieval/
│   │   ├── __init__.py
│   │   ├── fusion.py
│   │   ├── models.py
│   │   └── service.py
│   ├── context/
│   │   ├── __init__.py
│   │   ├── assembly.py
│   │   ├── citations.py
│   │   ├── models.py
│   │   └── service.py
│   ├── evaluation/
│   │   ├── __init__.py
│   │   ├── metrics.py
│   │   ├── models.py
│   │   └── service.py
│   ├── snapshot/
│   │   ├── __init__.py
│   │   ├── export.py
│   │   └── models.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   ├── evaluation.py
│   │   ├── repository.py
│   │   ├── tables.py
│   │   └── unit_of_work.py
│   ├── __init__.py
│   ├── __main__.py
│   ├── application.py
│   └── cli.py
├── demo/
│   ├── README.md
│   ├── snapshot.json        # generated, committed, checked in CI
│   └── snapshot.schema.json # generated, committed, checked in CI
├── examples/
│   ├── evaluation-demo.json # packaged-demo evaluation gold set
│   ├── getting-started.md
│   └── lifecycle.txt
├── tests/
├── web/                     # static demo site (V2)
│   ├── scripts/gen-types.mjs
│   └── src/
│       ├── lib/             # snapshot views, code-point slicing, formatting (+ Vitest)
│       ├── types/snapshot.gen.ts
│       └── *Page.tsx
├── LICENSE
├── README.md
└── pyproject.toml
```

The remaining modules will be added only when they gain executable behavior: `providers` and `api`.

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

`EmbeddingService` adds batching, dimension checks, and a provider/model-aware cache key. Caching is opt-in: pass an `InMemoryEmbeddingCache` (or any object with `get`/`set`) to keep repeated calls cheap and idempotent; cache keys compose `(provider_name, model, text)` and remain stable across runs.

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

U08 turns embeddings into a searchable store. The `VectorIndex` protocol captures `upsert`, `delete`, `get`, `search`, and `rebuild`; the default `InMemoryVectorIndex` ships a dependency-free cosine search with stable id-tiebreak ranking and metadata filters (`{"status": "active"}`, `{"tags": {"in": [...]}}`, `{"status": {"not": [...]}}`). An optional `LanceVectorIndex` adapter targets LanceDB when installed with `uv sync --extra lancedb` (or `pip install "konwledge[lancedb]"`); the module imports cleanly without the dependency and raises a clear install hint only when the adapter is instantiated. The adapter uses an explicit Arrow schema, so empty rebuilds keep the vector dimension, filtered searches scan every row instead of a fixed oversampling window, and reopening a table with another dimension fails with a clear error.

`IndexingService` wires the embedding service and the vector index together so you can embed and persist `KnowledgeUnit` objects in one call, run top-K searches by raw text, and rebuild the entire index deterministically:

```python
from konwledge.embedding import EmbeddingService, HashEmbeddingProvider, InMemoryEmbeddingCache
from konwledge.indexing import IndexingService, InMemoryVectorIndex

embeddings = EmbeddingService(HashEmbeddingProvider(dimension=32), cache=InMemoryEmbeddingCache())
indexing = IndexingService(
    embedding_service=embeddings,
    vector_index=InMemoryVectorIndex(dimension=32),
)
indexing.index_units(units)  # upsert vectors + traceable payload
hits = indexing.search("python testing patterns", top_k=5, filters={"status": "active"})
indexing.remove_units([units[0].id])  # delete a single unit by id
indexing.rebuild(units)  # atomically replace the whole index
```

Repeating `search` with identical input always returns the same ranking: `InMemoryVectorIndex`
breaks ties on `id` after sorting by descending cosine score, and the hash provider is
deterministic, so vectors never silently drift between runs.

## Incremental updates

U09 answers "what actually changed?" when a file is imported again with new content. Each new
document version is compared with the previous version of the same source at the knowledge-unit
level:

| Change | Rule | Re-embedded? |
| --- | --- | --- |
| `unchanged` | identical text in the same section (its position may shift) | no, vector reused |
| `moved` | identical text in a different section | no, vector reused |
| `modified` | similar text (≥ 0.5 ratio) or a rewrite inside the same named section | yes |
| `added` | no matching unit in the previous version | yes |
| `removed` | previous unit with no match in the new version | vector deleted |

Every unit has a `lineage_id` that stays stable across versions, so "the Usage paragraph" can be
followed through edits even though each immutable document version stores new unit ids. Lineage
changes are written as append-only `Revision` records (`create`, `update`, `delete`) in the same
transaction as the new units; unchanged units add no history. Databases created before U09 gain
the nullable `lineage_id` column automatically, and legacy rows start their own lineage.

```text
$ knowledge import guide.md
updated: guide.md | document=… | units=4 | changes=+1 ~1 >0 =2 -0 | re-embed=2

$ knowledge history <unit-id>
lineage: 9581…
versions: 2
revisions: 2
  rev 1 create (added) unit=9581… at 2026-10-29T03:25:26+00:00
  rev 2 update (modified) unit=ea17… at 2026-10-29T03:25:27+00:00
```

`IndexingService.apply_diff` turns the diff into a partial index rebuild. It embeds only `added`
and `modified` units, copies stored vectors for `unchanged` and `moved` units (falling back to
embedding if a vector is missing), refreshes their payload with the new `document_id`,
`lineage_id`, and `content_hash`, and deletes the previous version's ids:

```python
service = TraceableIngestionService(ingestion, KnowledgeUnitService(session_factory))
result = service.ingest(source_id)
if result.diff is not None:
    report = indexing.apply_diff(result.diff)
    print(result.diff.summary(), report.embedded, report.reused, report.deleted)
```

Tests verify the acceptance criterion directly: with no embedding cache configured, the provider
receives exactly the texts of the changed units. Statuses, as-of queries, and reverts build on this
in U10 (see [Knowledge status and as-of queries](#knowledge-status-and-as-of-queries)).

## Knowledge status and as-of queries

U10 answers "which knowledge was valid at time T?". Each source keeps an append-only
**publication timeline**: every time its head version changes, a `published` revision is recorded
on the source. Because each document version is an immutable snapshot, the status of any unit
version at any moment follows from that timeline plus the lineage's own revision chain:

| Status | Meaning at time T |
| --- | --- |
| `active` | the unit belongs to the source's head version |
| `stale` | head version, but the newest lineage revision at or before T is an explicit stale mark |
| `superseded` | an older version whose lineage lives on in the head (edited, moved, or carried over) |
| `deleted` | an older version whose lineage no longer exists in the head |

Units of a version not yet published at T are invisible. A stale mark lasts until the next revision
of the lineage: re-importing unchanged content keeps it, while an edit, a revert, or
`knowledge refresh` clears it.

```text
$ knowledge asof --at 2026-10-30T15:11:51+08:00 --all
as of: 2026-10-30T15:11:51+08:00
source file /notes/guide.md
  head: document 7f9c… published 2026-10-30T07:11:50+00:00
  active     unit cd9f… section=Guide | # Guide Intro text.
  active     unit 8401… section=Guide / Usage | ## Usage Run the CLI with --database.
  active     unit 1868… section=Guide / FAQ | ## FAQ Ask in issues.
  superseded unit f074… section=Guide | # Guide Intro text.
  deleted    unit 8940… section=Guide / Install | ## Install Run uv sync.
  superseded unit 6343… section=Guide / Usage | ## Usage Run the CLI.
3 effective unit(s) across 1 source(s) | active=3 stale=0 superseded=2 deleted=1

$ knowledge stale <unit-id> --reason "uv flags changed"
$ knowledge show <unit-id> --at 2026-10-30T15:00:00+08:00
$ knowledge asof --document <document-id>     # the moment that version was published
$ knowledge refresh <unit-id> --reason "reviewed"
```

Other behaviour worth knowing:

- **Reverts are real versions.** Restoring a file to content that was imported before republishes
  the stored version (`reverted:` in the import output), diffs it against the head by lineage, and
  appends `reverted` revisions, so `IndexingService.apply_diff` brings the index back as well. The
  next new version is diffed against the head, not against the most recently created document.
- **The revision log is the source of truth.** `knowledge_units.status` is a cache of the current
  status, rewritten in the same transaction as every publication or mark so filters and vector
  payloads can use it. As-of answers are always derived from the log.
- **Older databases keep working.** Without publications, the timeline is inferred from ingestion
  order (U09 behaviour). The next import of a source materializes that history as `backfilled`
  publications, and `knowledge reconcile` rebuilds the cached statuses in one go.

The Python API mirrors the CLI:

```python
library = KnowledgeLibrary(session_factory)
snapshot = library.snapshot(at=datetime(2026, 10, 30, 7, 12, tzinfo=UTC), include_retired=True)
print(snapshot.counts())  # {'active': 3, 'stale': 0, 'superseded': 2, 'deleted': 1}
library.lifecycle.mark_stale(unit_id, reason="uv flags changed")
```

## Freshness policies

U11 builds on U10's explicit `stale`/`refresh` revisions. A `FreshnessPolicy` is configured
per source and stored as an append-only `freshness_policy` source revision, so configuration
survives restarts and historical scans read the policy that existed at that time. No new
SQLite columns or demo schema changes are required.

| Rule | When it flags a current head unit |
| --- | --- |
| `ttl_expired` | Scan time is at or after the last content confirmation plus `ttl_seconds` |
| `source_updated` | A supplied source-update time has arrived and is later than both head publication and the lineage's last review |

The TTL reference is the lineage's latest `added`, `modified`, or `refreshed` revision.
Moving a unit or carrying identical content into a new version does **not** renew its TTL.
A revert renews only lineages whose content actually changes. Older histories without these
revisions fall back to the head publication time. Timestamps must be timezone-aware; the
boundary is inclusive (`checked_at >= expires_at`), and a CLI day is exactly 86,400 seconds.
Deadlines are computed in UTC so daylight-saving transitions do not shorten or lengthen the
TTL. Unsupported intervals/deadlines are rejected with a validation error rather than overflowing.

```bash
uv run knowledge list                         # includes source UUIDs
uv run knowledge freshness configure <source-id> --ttl-days 30
uv run knowledge freshness show <source-id>
uv run knowledge freshness scan               # preview; does not write stale marks
uv run knowledge freshness scan --apply       # append marks atomically
uv run knowledge publishable                  # only active head units
uv run knowledge publishable --include-stale  # opt in to stale knowledge
uv run knowledge refresh <unit-id> --reason "reviewed against source"
uv run knowledge freshness configure <source-id> --disable
```

A configuration command replaces the whole policy. To keep both rules, supply both
`--ttl-days` and `--source-updated-at`, for example:

```bash
uv run knowledge freshness configure <source-id> --ttl-days 30 \
  --source-updated-at 2026-11-06T12:00:00+08:00
uv run knowledge freshness scan --source <source-id> \
  --at 2026-11-06T13:00:00+08:00                 # historical preview only
```

Source-update times are **known inputs**, not a filesystem watcher, HTTP poller, or automatic
external-source synchronization. Future update times do not flag units until that time arrives.
An empty/disabled policy skips scanning; it does not clear existing stale marks. Review those
units with `refresh`. Already-stale units are not marked twice, and retired/unpublished versions
are never scanned. Stale marks retain the rule codes, TTL reference/deadline, and applied policy
in their revision payload.

Applying a scan writes all marks and cached statuses in one transaction, at the observed scan
time rather than backdating them to the TTL deadline. This preserves earlier as-of answers.
`--at` is read-only and cannot be combined with `--apply`; a regressing clock is rejected.
No background task runs by itself: schedule the CLI externally if periodic scans are wanted.

```python
from konwledge.domain import FreshnessPolicy

library.freshness.configure(source_id, FreshnessPolicy(ttl_seconds=30 * 86400))
preview = library.freshness.scan()  # dry_run=True by default
expired = [item for item in preview.decisions if item.expired]
applied = library.freshness.scan(dry_run=False)
ready = library.publishable_units()  # active only
historical = library.publishable_units(at=aware_datetime)
```

`KnowledgeSnapshot.effective_units` still includes active **and stale** head units for U10
compatibility. `publishable_units` is the new active-only boundary for future retrieval/context
assembly. U11 does not automatically filter the lower-level `VectorIndex` or implement the U13
retrieval pipeline. The controlled-clock tests cover deadlines, review/updates/reverts, policy
history, dry-run/apply, batch rollback, CLI validation, and historical publication filtering.

## Conflict detection

U12 detects **candidates**, not which source is correct. Its first deterministic rule,
`port_mismatch`, compares different sources asserting different scalar ports for the same
named `*-service`. It scans only the published head's active and stale units; superseded,
deleted, and unpublished units do not participate. Stale evidence stays visible because an
outdated FAQ may be the contradictory source that needs review.

The service name must be unambiguous in the unit, its section path, or the document (in that
order). Recognized forms include `order-service listens on port 8080`, the Chinese listening
form used in the demo, and `PORT=8080`. Negations, examples, alternatives, ranges, missing
service names, and invalid port numbers are skipped rather than guessed. These conservative
rules can miss natural-language conflicts; U12 does not provide general semantic detection.

```bash
uv run knowledge conflicts scan               # preview; no conflict records are written
uv run knowledge conflicts scan --apply       # persist new candidates atomically
uv run knowledge conflicts list --status pending
uv run knowledge conflicts show <conflict-id>  # JSON with both exact evidence quotes
uv run knowledge conflicts review <conflict-id> --status confirmed \
  --reason "Verified that the current sources disagree" --reviewer "reviewer@example.com"
uv run knowledge conflicts history <conflict-id>
uv run knowledge conflicts scan --at 2026-10-20T12:00:00+08:00  # historical preview only
```

Each record freezes both sources, documents, units, lineages, quotes, character offsets,
section paths, and observed unit statuses. A key based on the rule, subject, predicate,
lineages, and values deduplicates repeated scans and unchanged assertions across versions.
Changing a value creates a new key. Existing records and their original evidence are never
automatically deleted, overwritten, reopened, or resolved when content changes.

| Status | Meaning |
| --- | --- |
| `pending` | Awaiting review, or explicitly reopened |
| `confirmed` | A reviewer confirmed that the sources disagree |
| `dismissed` | A reviewer rejected this candidate |
| `resolved` | A reviewer recorded that the conflict was handled |

Any explicit status change requires a non-empty reason and appends a conflict revision;
repeating the current status is rejected. Review time cannot precede detection or earlier
reviews. Review does not choose a winning source, modify knowledge-unit statuses, or filter
retrieval results. The caller is responsible for recording reviewer identity accurately.

`scan --at` evaluates both publication state and conflict-review state at that time, without
exposing future review fields. It cannot be combined with `--apply`. The scan summary exposes
stable `candidates=`, `created=`, and `existing=` counts; persisted entries also show their UUID
and status. `list`, `show`, and `history` display the current record and complete audit chain.

```python
preview = library.conflicts.scan()  # dry_run=True by default
applied = library.conflicts.scan(dry_run=False)
created = applied.created
existing = applied.existing
historical = library.conflicts.scan(at=aware_datetime)
```

The CLI's existing schema initialization adds the new `conflicts` table to older libraries;
no existing columns or demo snapshot schema change. To try the demo conflict:

```bash
uv run knowledge demo build
uv run knowledge conflicts scan -d .konwledge/demo/knowledge.db
```

This finds exactly the current guide's active `8080` assertion versus the FAQ's stale `8000`
assertion, with both original quotes and offsets. Old guide versions are not treated as
current contradictory evidence. The static frontend and demo snapshot remain unchanged.

## Hybrid retrieval

U13 adds `library.search(...)` and `library.retrieval` with three explicit modes:

| Mode | Candidate routes | Configuration |
| --- | --- | --- |
| `lexical` (default) | SQLite FTS5, ordered by BM25 | No model or vector index required |
| `vector` | Exact cosine over eligible stored vectors | An injected `IndexingService` |
| `hybrid` | Both routes, merged with reciprocal rank fusion | An injected `IndexingService` |

The default query scope is **active published head units only**. `--include-stale` opts into
stale head units, and `--source` restricts the source UUID. Eligibility is resolved from the
publication/revision log at one timezone-aware time, before either route's candidate limit.
Cached vector status or text never controls eligibility or result evidence. Superseded,
deleted, and unpublished versions are excluded; `--at` selects the head that existed then.
The hit's top-level `status` is authoritative for the requested time; its nested `unit.status`
is the persisted current cache and may differ in a historical answer.

```bash
uv run knowledge search "order-service"                    # full-text, active only
uv run knowledge search "服务端口" --include-stale
uv run knowledge search "监听" --at 2026-10-10T12:00:00+08:00
uv run knowledge search "deployment" --top-k 3 --candidate-limit 20 --json
uv run knowledge search "port" --source <source-id>
uv run knowledge search "order-service" --mode hybrid --demo-vectors
```

The standalone CLI has no production embedding provider configured. `--demo-vectors` builds
an **ephemeral hash-vector index** of stored versions, does not write it to disk, and warns on
stderr that hash embeddings test the retrieval pipeline rather than semantic quality.
Without that flag or an injected SDK index, `vector` and `hybrid` fail explicitly rather than
silently switching to full-text. Normal search is read-only: it does not embed the corpus,
write revisions, change statuses, repair vectors, or modify the original index. Opening the
CLI still performs the existing additive schema initialization.

RRF scores are `sum(1 / (rrf_k + route_rank))`, with 1-based ranks and `rrf_k=60` by default.
Each unit counts at most once per route; ties are broken by UUID. Raw BM25 and cosine scores
are diagnostic fields, not quantities added together or confidence probabilities. Single-route
search uses the same rank-derived score. `candidate_limit` defaults to `max(50, top_k)`, applies
to each route, and must be at least `top_k`.

FTS5 indexes every stored unit version. Insert/text-update/delete triggers synchronize it in
the same SQLite transaction; existing libraries are backfilled on initialization. CJK text is
split into single-character tokens, with contiguous CJK query runs compiled as phrases. Other
terms are quoted and combined with AND, so user input is literal text, not FTS operators or
SQL. This supports short Chinese substrings without an extra dependency, but does not provide
Chinese linguistic segmentation, stemming, synonym expansion, or general semantic matching.
Writes through raw SQLite connections must register `knowledge_fts_text`; use the supplied
`create_sqlite_engine` for normal writes. A SQLite build without FTS5 fails with a clear error.

For a small local library, vector retrieval fetches eligible records and performs an exact
cosine scan in a temporary in-memory index. This avoids retired/stale vectors crowding out
valid candidates, but is O(N) over eligible units, not a large-scale ANN solution. U09 still
removes old-version vectors on incremental updates. Missing vectors or conflicting stored
content hashes are exposed in `missing_vector_ids` and `result.degraded`; hybrid can still
return historical FTS evidence, while the CLI warns that vector recall is incomplete. Provider
or backend failures are not silently treated as missing coverage. Callers must use the same
embedding provider/model for stored vectors and queries.

```python
from konwledge.application import KnowledgeLibrary
from konwledge.retrieval import RetrievalMode

# `indexing` is an IndexingService using your provider and a Memory/Lance VectorIndex.
# Populate it explicitly with index_units/rebuild/apply_diff; import does not auto-index.
library = KnowledgeLibrary(session_factory, indexing_service=indexing)
result = library.search("deployment port", mode=RetrievalMode.HYBRID, top_k=5)
for hit in result.hits:
    print(hit.unit.text, hit.source.uri, hit.unit.start_char, hit.unit.end_char)
    print(hit.lexical_rank, hit.vector_rank, hit.score)
if result.degraded:
    print("Missing usable vectors:", result.missing_vector_ids)
```

Results preserve original units, source/document/lineage IDs, section paths and character
ranges, along with resolved status, route ranks and raw scores. `--json` emits the complete
result on stdout; diagnostics stay on stderr. U13 does not generate answers, create citations,
assemble model context, resolve conflicts, or apply evaluation gates; those remain later slices.
The static frontend and V1 snapshot contract are unchanged.

## Context assembly and citations

U14 turns one retrieval snapshot into a `ContextPacket`: ranked whole-unit evidence,
exact citations, explicit skip reasons, and bounded `context_text`. It does not generate
an answer or call an LLM.

```bash
uv run knowledge context "deployment" --max-chars 8000 --max-units 3
uv run knowledge context "deployment" --top-k 10 --candidate-limit 20 --json
uv run knowledge context "监听" --at 2026-10-10T12:00:00+08:00 \
  -d .konwledge/demo/knowledge.db --json
uv run knowledge context "order-service" --mode hybrid --demo-vectors --include-stale
```

The CLI accepts the same retrieval options as `search`. Vector/hybrid modes still require
an injected SDK index or the explicit offline `--demo-vectors` flag. The latter only tests
pipeline wiring, not semantic quality. Diagnostics remain on stderr; `--json` writes exactly
one full packet to stdout. Human output includes a summary, the rendered context, and skips.

Selection preserves retrieval order and applies the following rules:

- Include only `active` evidence, or `stale` when explicitly requested. Historical packets
  use the hit's status at `evaluated_at`, not today's nested unit-status cache.
- Never repeat a unit or lineage. Reject overlapping spans inside one document version;
  adjacent spans and evidence from different documents are allowed.
- Keep each accepted unit's complete original text. When a record does not fit, skip the
  whole unit and consider the next hit; do not truncate quotes or synthesize summaries.
- Assign continuous labels `[1]`, `[2]`, etc. Return skipped unit IDs with `status`,
  `duplicate`, `overlap`, `budget`, or `max_units` reasons.

`max_chars` defaults to **8,000 Python Unicode code points**, not bytes or model tokens.
It measures `len(context_text)`, including the fixed preamble, labels, metadata, JSON escapes,
and record newlines. It excludes the query, CLI summary, outer packet JSON, and any caller
system prompt. `max_chars=0` returns empty context and budget skips; `max_units` must be
positive and defaults to 10. `top_k` caps retrieved hits **before** assembly: increase it if
more alternatives should be available after budget skips. Selection is greedy, not an
optimal packing algorithm. An empty packet is a valid result, never a reason to fabricate
an answer.

Each block carries a `Citation` with source/document/unit IDs, the source locator, exact
`[start_char, end_char)` offsets, and `quote == unit.text`. Offsets refer to the **normalized
imported document version**, not raw-file byte offsets. Original whitespace and Unicode are
preserved. Citation IDs are deterministic UUID5 values derived from source, document, unit,
range, and quote hash: rerunning retrieval or changing ranks does not change the ID for the
same evidence. A new document/unit version gets its own citation. Retrieval scores are
selection diagnostics, not citation confidence; `confidence` stays `None`.

Rendered context is a fixed trust-boundary notice followed by one compact JSON record per
block. Source quotes are escaped JSON strings, so embedded newlines or fake markup do not
create additional records. This identifies source text as untrusted evidence; it is **not**
a guarantee that a downstream model will resist prompt injection. Callers still need their
own system instructions and tool authorization boundaries.

```python
from konwledge.context import assemble_context, verify_citation

packet = library.build_context("deployment", top_k=10, max_chars=8000, max_units=3)
print(packet.context_text, packet.used_chars)
for label, citation in packet.citation_map.items():
    print(label, citation.locator, citation.start_char, citation.end_char)

# Reuse an existing retrieval result without another search or clock read.
result = library.search("deployment")
packet = assemble_context(result, max_chars=8000, max_units=3)
assert packet == library.context.assemble(result, max_chars=8000, max_units=3)
for block in packet.blocks:
    hit = next(hit for hit in result.hits if hit.unit.id == block.citation.knowledge_unit_id)
    verify_citation(block.citation, hit)
```

`ContextPacket` validates continuous labels, allowed statuses, unique units/lineages,
non-overlapping spans, exact offset lengths, rendered text, and both limits on JSON roundtrip.
`packet.citations`, `packet.citation_map`, and `packet.degraded` are SDK properties, not
serialized fields; JSON contains `blocks[].citation`, `skipped`, and `missing_vector_ids`.
Missing-vector diagnostics propagate unchanged even if all evidence is skipped.

Assembly is read-only: it does not persist citations, add revisions, change statuses,
repair vectors, or read live source files. CLI opening still performs the existing additive
schema initialization, and explicit demo mode builds temporary vectors. The generic
`CitationRepository` remains available for callers who explicitly choose persistence, but
U14 never writes to it. No evaluation gates, frontend changes, or demo snapshot/schema
changes are included in this increment.

## Retrieval evaluation

U15 stores evaluation sets as immutable, contiguous versions and runs retrieval against that pinned
gold version, with a shared default evaluation time, without persisting reports. Build the packaged
demo first, then import and run its gold set:

```bash
uv run knowledge demo build
uv run knowledge evaluation import examples/evaluation-demo.json \
  -d .konwledge/demo/knowledge.db
uv run knowledge evaluation list -d .konwledge/demo/knowledge.db --json
uv run knowledge evaluation show 52522ebd-b1dc-4ace-824c-f8054c8547af --version 1 \
  -d .konwledge/demo/knowledge.db
uv run knowledge evaluation export 52522ebd-b1dc-4ace-824c-f8054c8547af --version 1 \
  --output exported-evaluation.json -d .konwledge/demo/knowledge.db
uv run knowledge evaluation run 52522ebd-b1dc-4ace-824c-f8054c8547af --version 1 \
  --top-k 5 -d .konwledge/demo/knowledge.db --json
```

The JSON file is the complete gold snapshot: its set UUID identifies a logical dataset, `version`
identifies immutable gold at that point, and stable item IDs identify cases across versions. Each
case carries its own optional source, historical `at`, and `include_stale` scope; the runner does
not replace those fields with blanket CLI filters. Imports reject unknown gold lineages/sources.
Historical and stale cases are therefore compared only within the scope encoded by the author,
which keeps lifecycle comparisons fair. Re-importing identical content is idempotent; changing an
existing version is rejected. `list` returns only latest versions, while `history`, `show`, and
`export` expose exact versions.

Reports are read-only and contain the dataset/hash, full retrieval results, per-case scores, and
macro aggregates. Recall, MRR, and nDCG use binary lineage matches; duplicate ranked slots do not
inflate credit. Precision@k always divides by `k`. Answerable aggregates exclude no-answer cases,
and no-answer accuracy excludes answerable cases, so unavailable aggregates are `null` in JSON
and `n/a` in human output. A no-answer success means retrieval returned no hits—it does not mean an
LLM generated a refusal. Lineage matching measures whether the intended knowledge was retrieved;
it does not prove that an edited fact is correct or that a generated answer is faithful. Gold labels
still need human review. Reproducible comparisons require the same dataset version, query scopes,
retrieval settings, corpus versions, and embedding provider/model/index; U15 does not freeze or
version the entire corpus or vector backend.

The CLI has no production vector provider. Vector/hybrid evaluation requires an SDK-configured
`IndexingService` or explicit `--demo-vectors`, which creates one temporary hash index over all
stored versions for the run and warns that it is not a semantic-quality measurement. Missing
vectors remain visible per case and are never repaired automatically. U15 reports are the complete,
validated candidate input consumed by U16's file-only comparison and gate commands.

```python
from konwledge.evaluation import EvaluationSet

created = library.evaluation.import_set(EvaluationSet.model_validate_json(text))
report = library.evaluation.run(created.dataset.id, version=1, top_k=5)
print(report.metrics.mean_recall, report.dataset_hash, report.degraded)
```

## Evaluation baselines and regression gates

U16 turns an approved, healthy U15 report into a compact portable baseline, compares a new complete
report with it, and optionally applies a fail-closed policy. These commands only read and write JSON
files: they do not open SQLite, initialize a provider, use the network, rerun retrieval, or modify the
source report.

```bash
# Capture a complete candidate report from the U15 runner.
uv run knowledge evaluation run <set-id> --version 1 --top-k 5 --json \
  -d .konwledge/demo/knowledge.db > candidate.json

# Promote a reviewed healthy report explicitly; existing outputs are never replaced implicitly.
uv run knowledge evaluation baseline candidate.json --output approved-baseline.json
uv run knowledge evaluation baseline candidate.json --output approved-baseline.json --overwrite

# Inspect every aggregate and per-case delta, then enforce the strict default or a policy file.
uv run knowledge evaluation compare approved-baseline.json candidate.json
uv run knowledge evaluation gate approved-baseline.json candidate.json --json
uv run knowledge evaluation gate approved-baseline.json candidate.json \
  --policy examples/evaluation-gate.json
```

`baseline` validates the complete report and recomputes every metric before creating any output
parent or file. It refuses degraded reports and existing destinations (including input/output being
the same path) unless `--overwrite` is explicit, then publishes atomically in the destination
directory. A baseline keeps dataset scopes, case ranks and scores, but omits hit quotes, source
locators, document/unit IDs, and absolute corpus paths. Dataset-authored strings such as names and
queries remain. This makes it portable, not a signed proof that retrieval really ran; gold review
and baseline replacement are human decisions and are never passed automatically.

`compare` requires exactly the same dataset ID, version, content hash, retrieval mode, `top_k`,
`candidate_limit`, and `rrf_k`, plus complete cases and recomputed scores. Normal run times may
differ so monitoring can observe corpus changes; fixed case-level `at` anchors remain fixed. Set
`require_same_time` in a policy when all effective times must match. For reproducible CI, run both
sides at a common explicit time such as `--at 2026-11-10T00:00:00Z`.

The default policy covers all six metrics—recall, precision, MRR, nDCG, hit rate, and no-answer
accuracy—at both aggregate and case scopes with zero allowed absolute drop. Thus an average gain
cannot hide one bad case. Rules may set an inclusive `min_value`, `max_abs_drop`, and/or
`max_relative_drop`; all configured numeric checks are conjunctive with an epsilon of `1e-12`.
Relative thresholds are unit fractions (`0.01` means 1%). Relative drop from a zero baseline is zero,
while an absolute floor still applies. A metric unavailable for a cohort is `null`, not zero, and its
check has `passed: null`; a policy producing no applicable numeric checks blocks rather than passing.
Missing vectors/degraded candidates, time-policy violations, and metric regressions also block.

The stable process exit codes are:

| Code | Meaning |
| --- | --- |
| `0` | Valid comparison, or a gate that passed |
| `1` | File I/O or unexpected runtime failure |
| `2` | Malformed, incomplete, tampered, mismatched, or otherwise invalid input/configuration |
| `3` | Inputs were valid, but the gate blocked the candidate |

With `--json`, stdout is exactly one parseable baseline, comparison, or `GateResult`, including on
exit 3; diagnostics and candidate missing-vector IDs stay on stderr. Human gate output labels
aggregate and `case <item-id>` checks and prints clear violations. Comparison itself always exits 0
for a valid degraded or regressed candidate because it applies no threshold.

The committed `examples/evaluation-baseline.json` and `examples/evaluation-gate.json` provide the CI
wiring. CI builds/imports the demo, runs at `2026-11-10T00:00:00Z` with `--top-k 5`, writes the full
candidate report, and gates it against those files. The normal workflow trigger remains pushes to
`main` and pull requests. The hash-vector demo tests plumbing only: neither these retrieval metrics
nor their gates assess source correctness, content quality, generation quality, answer faithfulness,
or production embedding quality.

## Demo snapshot

The demo site (V2-V4) is a static page that only renders data; every number it shows comes from a
JSON snapshot exported by the CLI. V1 provides that data pipeline.

```bash
uv run knowledge demo build            # plays the story into .konwledge/demo/
uv run knowledge demo export           # writes demo/snapshot.json and demo/snapshot.schema.json
uv run knowledge demo export --check   # exit 1 if the committed files are out of date
```

The story ("the life of one piece of knowledge") follows a fictional service deployment guide
through three versions while an on-call FAQ keeps quoting the old port:

```text
2026-10-05T10:00:00+08:00 | 导入《服务部署手册》v1 | units=8 | embedded=8 reused=0 deleted=0
2026-10-05T10:30:00+08:00 | 导入《值班 FAQ》 | units=3 | embedded=3 reused=0 deleted=0
2026-10-12T14:00:00+08:00 | 发布《服务部署手册》v2 | changes=+2 ~1 >1 =5 -1 | embedded=3 reused=6 deleted=8
2026-10-15T09:30:00+08:00 | 标记“监控告警”过期 | stale: 监控告警
2026-10-16T11:00:00+08:00 | 标记“健康检查”过期 | stale: 健康检查
2026-10-17T15:00:00+08:00 | 确认“健康检查”仍然有效 | refreshed: 健康检查
2026-10-19T16:00:00+08:00 | 发布《服务部署手册》v3 | changes=+0 ~2 >0 =7 -0 | embedded=2 reused=7 deleted=9
2026-10-20T11:00:00+08:00 | 标记 FAQ 端口答案过期 | stale: 服务端口是多少？
Demo library built in .../.konwledge/demo (8 steps, 12 vectors in the index).
```

The v1 -> v2 diff contains all five change kinds, and the final state contains all four statuses.
The embedding numbers above come from a real `IndexingService` run during the build and match the
`embedding` block the exporter computes for each diff.

- **Deterministic by construction.** The builder injects a sequential UUID factory and a scripted
  clock into `KnowledgeLibrary` (which now passes both to every layer), and the corpus ships inside
  the package. Two builds in different directories export byte-identical files.
- **Derived, not duplicated.** `export_snapshot` reads only the metadata store: statuses and the
  per-event as-of `timepoints` come from `LifecycleService`, diffs are recomputed by lineage
  between consecutive head versions, and document text is rebuilt from the units and kept only
  when it reproduces the stored checksum. File URIs are made relative so no machine paths leak.
- **A versioned contract.** `LibrarySnapshot` (Pydantic) is the single contract with the front end;
  `demo/snapshot.schema.json` is generated from it. Breaking changes bump `schema_version`.
- **Guarded against drift.** `tests/test_demo.py` compares a fresh export with the committed golden
  files, validates the snapshot against the schema, and CI repeats the check through the CLI.

See [demo/README.md](demo/README.md) for the snapshot layout.

## Demo site

`web/` is a static Vite + React + TypeScript + Tailwind site that renders `demo/snapshot.json`
(bundled at build time). It has no backend and re-implements no lifecycle rule.

| Page | Shows | Backed by |
| --- | --- | --- |
| Overview | Source, version, unit, lineage, and revision counts; vector reuse across incremental updates | `stats`, `sources[].diffs[].embedding` |
| Traceability | Document text and its units side by side; clicking a unit highlights its character range and mirrors `knowledge show` | `sources[].documents[]` |
| Version diff | Unit-level changes between consecutive versions, colored by the five kinds, with re-embedded/reused vectors | `sources[].diffs[]`, `lineages[]` |

```bash
cd web
pnpm install
pnpm dev           # http://localhost:5173
pnpm gen:types     # regenerate src/types/snapshot.gen.ts after a schema change
```

- **Typed by the schema.** `src/types/snapshot.gen.ts` is generated from `demo/snapshot.schema.json`;
  CI fails when it drifts.
- **Ranges match the CLI.** Snapshot offsets are Python code points, so the site slices by code
  point, not UTF-16 unit; Vitest checks that every unit's range reproduces its stored text and that
  the segments tile each document exactly.
- **Deployed by Actions.** `.github/workflows/deploy-demo.yml` builds `web/` and publishes it to
  GitHub Pages on every push to `main` (enable *Settings → Pages → Source: GitHub Actions* once).

## License

Licensed under the [Apache License 2.0](LICENSE).
