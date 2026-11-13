import React from "react";

import { formatFull, shortId } from "../../lib/format";
import type { Page, UnitDTO } from "../types";
import { HashLink } from "./HashLink";
import { PaginationBar } from "./PaginationBar";
import { StatusBadge } from "./StatusBadge";

interface UnitListProps {
  readonly page: Page<UnitDTO>;
  readonly title: string;
  readonly description?: string;
  readonly showDocument?: boolean;
  readonly onPageChange: (offset: number) => void;
  readonly buildDocumentHref: (id: string) => string;
  readonly buildLineageHref: (id: string) => string;
  readonly buildEvidenceHref: (id: string) => string;
}

export const UnitList: React.FC<UnitListProps> = ({
  page,
  title,
  description,
  showDocument = false,
  onPageChange,
  buildDocumentHref,
  buildLineageHref,
  buildEvidenceHref,
}) => (
  <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
    <div className="space-y-1 border-b border-slate-200 pb-4">
      <h2 className="text-lg font-semibold text-slate-900">{title}</h2>
      {description ? <p className="text-sm leading-6 text-slate-600">{description}</p> : null}
    </div>

    <div className="mt-5 space-y-4">
      {page.items.map((unit) => (
        <article key={unit.id} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={unit.status} compact />
                <span className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
                  ordinal {unit.ordinal}
                </span>
              </div>
              <div className="text-sm font-semibold text-slate-900">
                Unit <span className="font-mono break-all text-xs text-slate-700">{unit.id}</span>
              </div>
              <div className="flex flex-wrap gap-3 text-sm text-slate-600">
                <span>字符范围 [{unit.start_char}, {unit.end_char})</span>
                <span>hash {shortId(unit.content_hash)}</span>
              </div>
            </div>
            <div className="flex flex-wrap gap-3 text-sm">
              <HashLink href={buildEvidenceHref(unit.id)}>查看证据</HashLink>
              <HashLink href={buildLineageHref(unit.lineage_id)}>打开 lineage</HashLink>
              {showDocument ? <HashLink href={buildDocumentHref(unit.document_id)}>文档版本</HashLink> : null}
            </div>
          </div>
          <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-3">
            <div>
              <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">document</dt>
              <dd className="mt-1 break-all font-mono text-slate-800">{unit.document_id}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">lineage</dt>
              <dd className="mt-1 break-all font-mono text-slate-800">{unit.lineage_id}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">source</dt>
              <dd className="mt-1 break-all font-mono text-slate-800">{unit.source_id}</dd>
            </div>
          </dl>
        </article>
      ))}
    </div>

    <div className="mt-5">
      <PaginationBar page={page} onPageChange={onPageChange} />
    </div>
  </section>
);

export function documentTrail(ingestedAt: string, previousId: string | null, nextId: string | null): string {
  const parts = [`导入时间 ${formatFull(ingestedAt)}`];
  if (previousId) parts.push("可回到更早版本");
  if (nextId) parts.push("也可继续查看后续导入版本");
  return parts.join("，");
}
