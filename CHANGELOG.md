# Changelog

All notable changes to Konwledge are documented in this file.

The project is in alpha. Minor releases may refine public and lower-level APIs as capabilities are added; v0.1.0 does not provide a complete backward-compatibility guarantee.

## 0.1.0 - 2026-11-11 (prepared, not published)

This release candidate closes local readiness for the first Knowledge CI workflow. It has not been merged to `main`, tagged, published as a GitHub Release, uploaded to PyPI, or used to verify a public demo deployment.

### Core capabilities delivered through U01-U16

- Local UTF-8 Markdown and text ingestion with normalization, SHA-256 identity, heading-aware splitting, exact character ranges, stable source/document/unit traceability, and idempotent imports.
- SQLite metadata persistence with explicit transactions, foreign keys, additive local upgrades, synchronized FTS5, immutable document versions, append-only revisions, lineage-aware diffs, reverts, and as-of lifecycle state.
- Freshness policies for source-scoped TTL and known source-update times, active-only publication defaults, and atomic dry-run/apply scans.
- Conservative conflict candidates with frozen two-sided evidence, deduplication, human review states, and an append-only audit chain.
- Lexical, vector, and hybrid retrieval with eligibility-first lifecycle filtering, deterministic reciprocal-rank fusion, historical/source scopes, and explicit missing-vector degradation.
- Whole-unit context assembly with exact citations, deterministic citation identity, duplicate/overlap controls, bounded character/unit budgets, and source text rendered as untrusted evidence.
- Immutable, versioned evaluation sets; deterministic retrieval metrics and read-only reports; compact healthy baselines; validated comparisons; and policy-driven fail-closed gates with stable process exit codes.
- Deterministic hash embeddings for offline plumbing tests, an in-memory exact vector index, and an optional LanceDB adapter.
- Reproducible packaged demo data, a versioned JSON snapshot/schema contract, golden drift tests, a V2 read-only static site, and CI for Python, the site build, the optional adapter, demo drift, and the committed regression gate.

### U17 release-readiness additions

- Synchronized v0.1.0 identity across `pyproject.toml`, `konwledge.__version__`, and `uv.lock`, with the package classified as alpha and Python 3.11+ retained.
- Added a lazy package-root `KnowledgeLibrary` export and the packaged PEP 561 `py.typed` marker while retaining the established domain and evaluation import surfaces.
- Added wheel and source-distribution inspection plus isolated installed-package verification for metadata, entry points, packaged resources, CLI aliases, module execution, retrieval/context, evaluation, and gate behavior.
- Added architecture, contribution, release, installation, end-to-end demo, API-boundary, and release-status documentation.
- Documented that CI runs for pull requests and pushes to `main`, not an ordinary push only to a feature branch, and that CI artifacts are not published packages.

### Known limitations

- `HashEmbeddingProvider` and `--demo-vectors` validate deterministic pipeline wiring; they do not provide semantic embeddings or production-quality retrieval scores.
- Evaluation matches expected lineages. It does not assess whether source facts are correct, whether retrieved text fully answers a question, or whether a generated answer is faithful. Konwledge does not automatically call an LLM or generate answers.
- Vector retrieval for a small local library copies eligible vectors into an exact in-memory index and scans them in O(N) time. This is a single-machine design, not a distributed or large-scale ANN service.
- The metadata store uses local SQLite with additive upgrades. It does not provide destructive migration orchestration, high availability, multi-tenant isolation, or a production support SLA.
- Reproducible reports require callers to control corpus versions, evaluation times, retrieval settings, and provider/index identity. Evaluation does not freeze the entire corpus, vector backend, or embedding provider/model.
- Regression gates cannot be bypassed by an average improvement: the committed strict policy checks aggregate and per-case metrics, blocks degraded candidates, and fails closed on invalid or uncovered evidence.
- Raw evaluation reports may include source text, locators, IDs, and local paths. Compact baselines omit hit quotes, source locators, document/unit IDs, and absolute corpus paths, but still contain dataset-authored queries and identifiers.
- Frontend development has been paused at V2 since the 2026-11-06 scope decision. V3/V4, live-page publication, and further deployment are deferred; workflow configuration is not evidence of a live site.
- No v0.1.0 tag, GitHub Release, PyPI package publication, or public-site deployment is part of this prepared candidate.
