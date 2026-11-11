# Architecture

## Scope

Konwledge is a local-first knowledge lifecycle, retrieval, context, and evaluation toolkit. Its v0.1.0 release candidate targets one machine, a local SQLite metadata store, deterministic offline demonstrations, and explicit composition through Python services and a CLI.

It is not a hosted knowledge service, distributed vector database, automatic answer generator, or production availability product. The alpha API grows incrementally and does not provide a complete backward-compatibility guarantee or production SLA.

## Layers and source boundaries

| Layer | Primary paths | Responsibility |
| --- | --- | --- |
| Public facade | `src/konwledge/__init__.py`, `application.py` | Lazily export `KnowledgeLibrary`; coordinate import, lifecycle, freshness, conflicts, retrieval, context, and evaluation |
| Domain | `src/konwledge/domain/` | Immutable validated models for sources, documents, units, revisions, citations, conflicts, freshness, and evaluation sets |
| Ingestion | `src/konwledge/ingestion/` | Load UTF-8 text, normalize it, split traceable units, and persist idempotent document versions |
| Lifecycle | `src/konwledge/lifecycle/` | Diff versions, maintain lineage and publication history, resolve as-of status, and apply stale/refresh transitions |
| Conflict analysis | `src/konwledge/conflicts/` | Produce conservative candidates with frozen evidence and append human review history |
| Storage | `src/konwledge/storage/` | Configure SQLite, map domain values, expose repositories and explicit Unit of Work transactions, and synchronize FTS5 |
| Embedding and indexing | `src/konwledge/embedding/`, `indexing/` | Define provider/index protocols, deterministic hash test vectors, exact in-memory search, and the optional LanceDB adapter |
| Retrieval | `src/konwledge/retrieval/` | Resolve eligible knowledge, run lexical/vector routes, fuse ranks, and return source-backed hits |
| Context | `src/konwledge/context/` | Select whole units, enforce character/unit budgets, create exact citations, and render untrusted evidence records |
| Evaluation | `src/konwledge/evaluation/` | Store immutable gold-set versions, score retrieval, validate reports, create compact baselines, compare, and gate |
| Demo and snapshot | `src/konwledge/demo/`, `snapshot/`, `demo/` | Reproduce the packaged story and maintain the versioned JSON contract consumed by the V2 static site |
| Interfaces | `src/konwledge/cli.py`, `__main__.py` | Expose CLI commands through `knowledge`, `python -m konwledge`, and the legacy `konwledge` alias |

`KnowledgeLibrary` is the intended high-level SDK boundary. `konwledge.domain` and `konwledge.evaluation` expose established typed values and evaluation helpers. Direct use of storage, ingestion, indexing, retrieval, and context modules is supported for composition, but these lower-level boundaries are more experimental during alpha development.

## Metadata, history, and transactions

SQLite stores metadata and audit history. SQLAlchemy repositories keep persistence mapping separate from Pydantic domain models. `UnitOfWork` gives service operations explicit commit and rollback boundaries; exiting an uncommitted unit of work rolls back.

The revision and publication log is authoritative for historical state. A unit's persisted current `status` is a query/index cache, updated in the same transaction as lifecycle writes, but as-of views are reconstructed from history. Evaluation-set versions and conflict evidence are immutable; status reviews and lifecycle transitions append records rather than rewriting the audit chain.

`create_schema()` creates missing tables, enables foreign keys and FTS helpers, and applies narrowly defined additive upgrades to older local databases. This is not a general destructive migration framework. New migrations must preserve existing data, audit history, and snapshot contracts.

SQLite FTS5 is required for lexical retrieval. The supplied engine registers the tokenizer helper needed by FTS triggers. Writes that bypass the supplied engine can violate that contract.

## Ingestion and traceability

Ingestion normalizes supported Markdown and text files, fingerprints normalized content, and stores immutable document versions. Heading-aware or paragraph splitting produces units with half-open character offsets. The central traceability invariant is:

`normalized_content[unit.start_char:unit.end_char] == unit.text`

Each unit version has a unique ID and a lineage ID that follows the logical unit across document versions. Diffs classify units as added, modified, moved, unchanged, or removed. Revision records and publication events make reverts and historical reads explicit rather than silently replacing old content.

Source paths are local runtime metadata. Ordinary libraries may therefore contain absolute file URIs. The demo snapshot relativizes paths before committing them, but raw databases and evaluation reports should be treated as potentially sensitive local artifacts.

## Eligibility-first retrieval

Retrieval resolves one metadata snapshot before applying route candidate limits. By default, only active units from each source's published head are eligible. Callers may explicitly include stale units or select a historical time and source. Superseded, deleted, unpublished, or otherwise ineligible units do not enter lexical or vector candidate pools.

| Mode | Route | Important boundary |
| --- | --- | --- |
| Lexical | Transactionally synchronized SQLite FTS5 | Literal full-text matching; no semantic claim |
| Vector | Injected `IndexingService` | Requires an explicitly populated index and matching provider/model |
| Hybrid | Lexical and vector rankings combined with deterministic RRF | Raw route scores are diagnostics, not probabilities |

The metadata/revision log, not vector payload metadata, controls eligibility. For vector retrieval, eligible records are copied into a temporary exact in-memory index and scanned in O(N) time. Missing or content-mismatched vectors are surfaced as degraded diagnostics; provider or backend failures propagate instead of silently becoming empty results.

The bundled hash provider and `--demo-vectors` exercise vector plumbing deterministically. They do not measure semantic retrieval quality. Konwledge does not freeze or version the entire corpus, vector backend, embedding provider, or provider model.

## Citations and context

Context assembly consumes exactly one retrieval result. It preserves ranking order, removes duplicate units/lineages and overlapping spans, and accepts only whole units that fit the configured character and unit limits. It never truncates a quotation to force it into a budget.

Every accepted block carries source, document, unit, lineage, normalized character range, checksum, status, and exact quote. Citation identity is deterministic for the same evidence. Retrieval score is selection metadata, not citation confidence.

Rendered context starts with a trust-boundary notice and serializes each source block as compact JSON. Source text is untrusted evidence and may contain prompt-like content. Escaping prevents that text from creating additional records, but it does not guarantee downstream model safety. Callers remain responsible for system instructions, tool authorization, and handling of model output. Konwledge v0.1.0 does not call an LLM or generate an answer automatically.

## Gold sets, reports, baselines, and gates

| Artifact | Contents | Persistence and privacy |
| --- | --- | --- |
| Evaluation set | Versioned queries, expected lineages, optional source/time/stale scope | Immutable SQLite versions; committed demo fixture is reviewed input |
| Evaluation report | Dataset snapshot/hash, full retrieval hits, per-case scores, aggregates | Read-only output; can include source locators, quotes, IDs, and local paths |
| Evaluation baseline | Dataset snapshot, lineage rankings, metrics, configuration | Compact portable JSON; omits hit quotes, source locators, document/unit IDs, and absolute corpus paths |
| Gate policy/result | Metric bounds, time requirement, checks, violations | File-only validation and decision evidence |

Reports and baselines are not trusted merely because their JSON shape is valid. Validation recomputes dataset hashes, per-case metrics, aggregates, times, and compatibility fields. A baseline can be promoted only from a complete healthy report. Comparison requires the same gold snapshot and retrieval configuration.

The gate is fail closed. It blocks degraded candidates, configured regressions, required-time mismatches, and policies with no applicable numeric checks. It never reruns retrieval or replaces a baseline. CI reads the committed baseline and policy; human review is required to approve a replacement.

Lineage retrieval metrics answer whether expected knowledge was retrieved. They do not prove source correctness, fact quality, generated-answer faithfulness, or production embedding quality.

## Error contracts

SDK validation errors are explicit Pydantic or `ValueError` failures. Missing entities use lookup-oriented errors; invalid lifecycle transitions and missing vector configuration have specific exceptions. Storage corruption and unavailable FTS5 surface as runtime failures. Provider/index exceptions are not converted into successful empty searches.

The CLI maps command validation to actionable diagnostics. Evaluation gate commands reserve stable process outcomes:

| Code | Meaning |
| --- | --- |
| `0` | Valid comparison, or passing gate |
| `1` | File I/O or unexpected runtime failure |
| `2` | Malformed, tampered, incomplete, mismatched, or invalid input/configuration |
| `3` | Valid inputs, but policy blocks the candidate |

JSON mode keeps machine-readable output on stdout and diagnostics on stderr, including a valid `GateResult` when exit code `3` is used.

## Concurrency and operational limits

Transactions make individual metadata changes atomic, but the system is designed for local, single-machine use. SQLite locking and filesystem behavior define concurrent-writer limits; Konwledge does not provide distributed coordination, high availability, online migration orchestration, or a production support SLA.

The in-memory vector route is an exact linear scan. LanceDB is optional and caller-managed. Import does not automatically populate a production vector index, retrieval does not repair it, and evaluation does not freeze concurrent corpus/provider changes. Reproducible comparisons require callers to control corpus versions, explicit times, retrieval parameters, and provider/index identity.

## Static demo boundary

The V1 snapshot is a deterministic, versioned read model. The V2 site renders that committed data and does not reimplement lifecycle rules. Golden tests detect drift between models, JSON Schema, snapshot output, and generated frontend types.

Frontend development is paused at V2. V3/V4 views, live-page publication, and further deployment are deferred. A deployment workflow in the repository is configuration, not evidence that a public site is live.
