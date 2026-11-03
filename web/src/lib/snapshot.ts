/**
 * Read-only views over the exported snapshot.
 *
 * The page never re-derives lifecycle rules: statuses, diffs, and vector work all
 * come from `knowledge demo export`. This module only indexes, orders, and slices
 * that data for rendering, and is what the Vitest suite pins down.
 */
import type {
  DiffView,
  DocumentView,
  KnowledgeUnitStatus,
  LibrarySnapshot,
  LineageView,
  SourceView,
  UnitChangeKind,
  UnitChangeView,
  UnitView,
} from "../types/snapshot.gen";
import { codePointLength, codePoints, sliceCodePoints } from "./text";

export type {
  DiffView,
  DocumentView,
  KnowledgeUnitStatus,
  LibrarySnapshot,
  LineageView,
  SourceView,
  UnitChangeKind,
  UnitChangeView,
  UnitView,
};

export const SUPPORTED_SCHEMA_VERSION = 1;

export const CHANGE_KINDS: readonly UnitChangeKind[] = [
  "added",
  "modified",
  "moved",
  "unchanged",
  "removed",
];

export const STATUSES: readonly KnowledgeUnitStatus[] = ["active", "stale", "superseded", "deleted"];

/** Reject snapshots this build does not understand instead of rendering garbage. */
export function assertSnapshot(data: unknown): LibrarySnapshot {
  if (typeof data !== "object" || data === null) throw new Error("snapshot is not an object");
  const version = (data as { schema_version?: unknown }).schema_version ?? SUPPORTED_SCHEMA_VERSION;
  if (version !== SUPPORTED_SCHEMA_VERSION) {
    throw new Error(
      `snapshot schema v${String(version)} is not supported by this site (expects v${SUPPORTED_SCHEMA_VERSION})`,
    );
  }
  for (const key of ["stats", "sources", "lineages", "events", "timepoints"]) {
    if (!(key in data)) throw new Error(`snapshot is missing "${key}"`);
  }
  return data as LibrarySnapshot;
}

// ---------------------------------------------------------------------------
// Lookups
// ---------------------------------------------------------------------------

export interface UnitRef {
  unit: UnitView;
  document: DocumentView;
  source: SourceView;
}

export class SnapshotIndex {
  readonly snapshot: LibrarySnapshot;
  private readonly units = new Map<string, UnitRef>();
  private readonly documents = new Map<string, { document: DocumentView; source: SourceView }>();
  private readonly lineages = new Map<string, LineageView>();

  constructor(snapshot: LibrarySnapshot) {
    this.snapshot = snapshot;
    for (const source of snapshot.sources) {
      for (const document of source.documents) {
        this.documents.set(document.id, { document, source });
        for (const unit of document.units) this.units.set(unit.id, { unit, document, source });
      }
    }
    for (const lineage of snapshot.lineages) this.lineages.set(lineage.id, lineage);
  }

  unit(id: string | null | undefined): UnitRef | undefined {
    return id ? this.units.get(id) : undefined;
  }

  document(id: string | null | undefined): { document: DocumentView; source: SourceView } | undefined {
    return id ? this.documents.get(id) : undefined;
  }

  lineage(id: string | null | undefined): LineageView | undefined {
    return id ? this.lineages.get(id) : undefined;
  }

  source(id: string | null | undefined): SourceView | undefined {
    return this.snapshot.sources.find((source) => source.id === id);
  }
}

/** Published versions first (v1, v2, …), never-published versions last. */
export function orderedDocuments(source: SourceView): DocumentView[] {
  return [...source.documents].sort(
    (a, b) => (a.version ?? Number.MAX_SAFE_INTEGER) - (b.version ?? Number.MAX_SAFE_INTEGER),
  );
}

export function headDocument(source: SourceView): DocumentView | undefined {
  return source.documents.find((document) => document.id === source.head_document_id);
}

/** Human title: the document's top heading, else the source title, else its path. */
export function sourceTitle(source: SourceView): string {
  const document = headDocument(source) ?? orderedDocuments(source)[0];
  const top = document?.units.find((unit) => unit.section_path.length > 0)?.section_path[0];
  return top ?? source.title ?? source.uri;
}

export function versionLabel(document: DocumentView): string {
  return document.version === null ? "未发布" : `v${document.version}`;
}

export function sectionLabel(unit: UnitView): string {
  return unit.section_path.join(" / ") || "<root>";
}

// ---------------------------------------------------------------------------
// Traceability: document text cut at unit boundaries
// ---------------------------------------------------------------------------

export interface Segment {
  /** Inclusive code-point offset. */
  start: number;
  /** Exclusive code-point offset. */
  end: number;
  text: string;
  /** The unit covering this range, or null for text no unit covers. */
  unitId: string | null;
}

/**
 * Cut `content` into consecutive segments at unit boundaries.
 *
 * Segments always tile the whole text, so concatenating them reproduces the
 * document; uncovered gaps become `unitId: null` segments. Offsets are code points.
 */
export function segmentDocument(content: string, units: readonly UnitView[]): Segment[] {
  const points = codePoints(content);
  const total = points.length;
  const segments: Segment[] = [];
  let cursor = 0;

  const push = (start: number, end: number, unitId: string | null) => {
    if (end <= start) return;
    segments.push({ start, end, text: sliceCodePoints(points, start, end), unitId });
  };

  const ordered = [...units].sort((a, b) => a.start_char - b.start_char || a.ordinal - b.ordinal);
  for (const unit of ordered) {
    const start = Math.min(Math.max(unit.start_char, cursor), total);
    const end = Math.min(Math.max(unit.end_char, start), total);
    push(cursor, start, null);
    push(start, end, unit.id);
    cursor = Math.max(cursor, end);
  }
  push(cursor, total, null);
  return segments;
}

/** The exact text a unit's range selects, as `knowledge show` would print it. */
export function unitRangeText(content: string, unit: UnitView): string {
  return sliceCodePoints(content, unit.start_char, unit.end_char);
}

export function unitLength(unit: UnitView): number {
  return unit.end_char - unit.start_char;
}

/** Lines of `knowledge show <unit>` that the trace page mirrors. */
export function cliShowLines(ref: UnitRef): string[] {
  const { unit, document, source } = ref;
  return [
    `unit id: ${unit.id}`,
    `lineage: ${unit.lineage_id}`,
    `status: ${unit.status ?? "unpublished"}`,
    `document: ${document.id}`,
    `source: ${source.kind} ${source.uri}`,
    `section_path: ${sectionLabel(unit)}`,
    `heading: ${unit.heading ?? "<none>"}`,
    `range: [${unit.start_char}, ${unit.end_char})`,
  ];
}

export function documentLength(document: DocumentView): number | null {
  return document.content === null ? null : codePointLength(document.content);
}

// ---------------------------------------------------------------------------
// Version diff: side-by-side rows
// ---------------------------------------------------------------------------

export interface DiffRow {
  key: string;
  change: UnitChangeView;
  kind: UnitChangeKind;
  label: string;
  oldUnit?: UnitView;
  newUnit?: UnitView;
}

/**
 * Rows of a diff in reading order of the new version.
 *
 * Units present in the new version follow their new ordinal; a removed unit is
 * placed right after the row that held its predecessor in the old version, so
 * deletions show up where the reader would have met them.
 */
export function diffRows(index: SnapshotIndex, diff: DiffView): DiffRow[] {
  const rows: DiffRow[] = diff.changes.map((change) => ({
    key: `${change.lineage_id}:${change.old_unit_id ?? "-"}:${change.new_unit_id ?? "-"}`,
    change,
    kind: change.kind,
    label: index.lineage(change.lineage_id)?.label ?? "（未命名片段）",
    oldUnit: index.unit(change.old_unit_id)?.unit,
    newUnit: index.unit(change.new_unit_id)?.unit,
  }));

  const byOldOrdinal = [...rows]
    .filter((row) => row.oldUnit)
    .sort((a, b) => (a.oldUnit?.ordinal ?? 0) - (b.oldUnit?.ordinal ?? 0));
  const keys = new Map<DiffRow, number>();

  const sortKey = (row: DiffRow): number => {
    const cached = keys.get(row);
    if (cached !== undefined) return cached;
    let key: number;
    if (row.newUnit) {
      key = row.newUnit.ordinal;
    } else {
      const position = byOldOrdinal.indexOf(row);
      const predecessor = position > 0 ? byOldOrdinal[position - 1] : undefined;
      // Fractions keep removed rows after their anchor and in their old order.
      key = (predecessor ? sortKey(predecessor) : -1) + 0.5 + position / 10_000;
    }
    keys.set(row, key);
    return key;
  };

  return rows.sort((a, b) => sortKey(a) - sortKey(b));
}

export function countRows(rows: readonly DiffRow[]): Record<UnitChangeKind, number> {
  const counts = { added: 0, modified: 0, moved: 0, unchanged: 0, removed: 0 };
  for (const row of rows) counts[row.kind] += 1;
  return counts;
}

// ---------------------------------------------------------------------------
// Overview figures
// ---------------------------------------------------------------------------

export interface VectorSavings {
  embedded: number;
  reused: number;
  deleted: number;
  /** Share of vectors kept instead of recomputed, 0–100. */
  reusedPercent: number;
}

/** Vector work summed over every incremental update in the library. */
export function vectorSavings(snapshot: LibrarySnapshot): VectorSavings {
  let embedded = 0;
  let reused = 0;
  let deleted = 0;
  for (const source of snapshot.sources) {
    for (const diff of source.diffs) {
      embedded += diff.embedding.embedded;
      reused += diff.embedding.reused;
      deleted += diff.embedding.deleted;
    }
  }
  const work = embedded + reused;
  return {
    embedded,
    reused,
    deleted,
    reusedPercent: work === 0 ? 0 : Math.round((reused / work) * 100),
  };
}

export function allDiffs(snapshot: LibrarySnapshot): { source: SourceView; diff: DiffView }[] {
  return snapshot.sources.flatMap((source) => source.diffs.map((diff) => ({ source, diff })));
}
