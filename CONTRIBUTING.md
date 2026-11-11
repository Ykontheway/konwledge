# Contributing to Konwledge

Thank you for contributing. Konwledge is an Apache-2.0, local-first Python project. The current release line is alpha: prefer small, reviewable changes with explicit tests and documentation over broad compatibility assumptions.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- SQLite with FTS5 enabled
- Git

The default test suite is offline and does not require provider credentials. LanceDB is optional.

## Set up a checkout

```bash
git clone https://github.com/Ykontheway/konwledge.git
cd konwledge
uv sync --locked --dev
uv run knowledge --version
```

Use the locked environment for reproducible development. To work on the optional LanceDB adapter:

```bash
uv sync --locked --dev --extra lancedb
```

Do not commit local databases, indexes, caches, virtual environments, credentials, or generated runtime output.

## Choose a change

Before coding, identify the smallest behavior that can be tested and documented. Good changes preserve these invariants:

- imported units retain exact normalized source ranges;
- source, document, unit, lineage, revision, and citation links remain traceable;
- revision and review history is append-only;
- lifecycle eligibility is resolved before retrieval route limits;
- evaluation reports and baselines are validated by recomputation;
- gates fail closed when evidence is invalid, degraded, mismatched, or uncovered by policy.

The root `KnowledgeLibrary` facade and exports from `konwledge.domain` and `konwledge.evaluation` are the clearest user-facing surfaces. Lower-level modules are still experimental. Because the package is alpha, compatibility is a review consideration rather than a complete backward-compatibility guarantee.

## Branches, commits, and reviews

Create a feature branch from the intended base. Keep commits focused and leave the branch runnable. A separate pull request is not required for every tiny implementation slice: closely related slices may be reviewed as one milestone batch when that makes the history clearer.

A pull request and maintainer review are required before contributor code is merged into `main`. In the pull request:

- explain the user-visible behavior and boundaries;
- identify tests added or changed;
- call out schema, snapshot, baseline, or public API effects;
- state any deferred work or known limitation.

Do not merge solely because local checks pass. Review is also responsible for API shape, migration safety, evidence integrity, and release scope.

## Run focused checks while developing

Start with the smallest relevant test file, then run lint and type checks over the full supported tree. Examples:

```bash
uv run pytest tests/test_retrieval.py
uv run pytest tests/test_context.py
uv run pytest tests/test_evaluation_gate_demo.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
```

For the optional adapter:

```bash
uv run pytest tests/test_indexing.py -k lance
```

Provider integrations must keep deterministic offline coverage. A new network provider does not make live API credentials mandatory for contributors or CI.

## Run the full pre-review suite

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=konwledge --cov-report=term-missing
uv run knowledge demo build --force
uv run knowledge demo export --check
uv run pytest tests/test_evaluation_gate_demo.py
uv run python scripts/verify_distribution.py
```

The distribution verifier is intentionally separate from ordinary tests. It builds wheel and sdist artifacts, inspects their metadata and contents, installs each into an isolated environment outside the checkout, and runs installed-package smoke checks.

GitHub Actions runs for pull requests and pushes to `main`. A feature-branch push by itself is not a CI trigger.

## Evaluation gates and golden files

The committed evaluation dataset, approved baseline, and gate policy live under `examples/`. Treat them as reviewed evidence, not disposable generated output.

- CI must never rebuild or overwrite `examples/evaluation-baseline.json`.
- Update a baseline only after intentionally reviewing a complete, healthy report and explaining why the new behavior is accepted.
- Keep fixed-time demo evaluation settings synchronized with `.github/workflows/ci.yml`.
- Preserve negative tests for all stable gate outcomes.

| Exit code | Contract |
| --- | --- |
| `0` | Comparison is valid, or the gate passes |
| `1` | File I/O or unexpected runtime failure |
| `2` | Input, configuration, or compatibility is invalid |
| `3` | Inputs are valid, but policy blocks the candidate |

A blocked gate is expected behavior in negative tests; do not weaken it into a generic success or runtime failure.

## Database and contract changes

SQLite initialization uses additive upgrades for older local databases. Schema work must:

- remain additive unless a separately reviewed migration design exists;
- preserve existing data and append-only audit history;
- execute through explicit transaction boundaries;
- include upgrade coverage for a pre-change database;
- keep FTS5 synchronization and foreign-key enforcement intact.

The committed `demo/snapshot.json` and `demo/snapshot.schema.json` are a versioned frontend contract. Update golden files only for an intentional, reviewed contract change. Breaking changes require a schema-version decision and synchronized generated TypeScript types.

Frontend development is currently paused at V2. Do not expand or deploy V3/V4 as an incidental part of a Python contribution.

## Documentation

Update README or focused docs when behavior, public commands, architecture, limitations, or release operations change. Use executable commands and distinguish:

- local source installation from an actually published package;
- a prepared artifact from a tagged or published release;
- configured deployment automation from a verified live site;
- retrieval lineage metrics from answer or factual quality.

Release operations are maintainer-controlled. See [docs/releasing.md](docs/releasing.md) for the manual approval boundaries.
