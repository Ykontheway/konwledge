# Demo snapshot

`snapshot.json` is everything the static demo site renders. It is generated, committed, and
checked: `tests/test_demo.py` and CI fail when a fresh export differs from these files.

```bash
uv run knowledge demo build --force   # replay the story into .konwledge/demo/
uv run knowledge demo export          # rewrite snapshot.json and snapshot.schema.json
```

Regenerate both files whenever the story, the corpus, or the snapshot model changes, and bump
`schema_version` (in `src/konwledge/snapshot/models.py`) for any change that is not an optional
addition.

## Layout (schema v1)

| Field | Content | Page that uses it |
| --- | --- | --- |
| `stats` | Source, version, unit, lineage, revision, and vector counts; statuses now; change kinds across all diffs | Overview |
| `sources[].documents[]` | Every document version with its rebuilt `content` and units (`start_char`/`end_char`, `section_path`, `status`) | Traceability |
| `sources[].timeline` | Head changes of the source, including reverts | Version diff, lifecycle |
| `sources[].diffs[]` | Unit-level changes between consecutive head versions, five-kind `counts`, and `embedding` work (embedded / reused / deleted vectors) | Version diff |
| `lineages[]` | One piece of knowledge across versions: unit ids and the append-only revision chain | Lifecycle |
| `events[]` | Publications, reverts, stale and refresh marks in time order | Lifecycle (as-of slider) |
| `timepoints[]` | Status of every unit version as of each event time, plus the effective unit ids | Lifecycle (as-of slider) |
| `story[]` | Narrated steps of the scripted scenario, aligned with event times | Guided tour |

Conventions:

- `version` numbers documents by first publication (1-based); a revert re-publishes an existing
  version, so a timeline can read `1, 2, 1`.
- Timestamps keep the offset they were recorded with (`+08:00` for the story).
- `content` is `null` when the stored units do not reproduce the document checksum exactly (for
  example trailing blank lines in a `.txt` file); the units remain usable.
- Status `null` means the document version was never published.
