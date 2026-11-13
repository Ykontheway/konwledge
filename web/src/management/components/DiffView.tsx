import React from "react";
import { ArrowLeft, ArrowRightLeft, RefreshCw } from "lucide-react";

import type { DocumentDiffDTO, DiffItem, UnitDTO } from "../types";
import { HashLink } from "./HashLink";
import { PaginationBar } from "./PaginationBar";
import { StatusBadge } from "./StatusBadge";

interface DiffViewProps {
  readonly diff: DocumentDiffDTO;
  readonly refreshing: boolean;
  readonly onRefresh: () => void;
  readonly jobsHref: string;
  readonly onPageChange: (offset: number) => void;
  readonly buildDocumentHref: (id: string) => string;
  readonly buildLineageHref: (id: string) => string;
  readonly buildEvidenceHref: (id: string) => string;
}

const KIND_META: Record<DiffItem["kind"], string> = {
  added: "bg-emerald-100 text-emerald-700 border-emerald-200",
  modified: "bg-amber-100 text-amber-800 border-amber-200",
  moved: "bg-blue-100 text-blue-700 border-blue-200",
  unchanged: "bg-slate-100 text-slate-700 border-slate-200",
  removed: "bg-rose-100 text-rose-700 border-rose-200",
};

export const DiffView: React.FC<DiffViewProps> = ({
  diff,
  refreshing,
  onRefresh,
  jobsHref,
  onPageChange,
  buildDocumentHref,
  buildLineageHref,
  buildEvidenceHref,
}) => (
  <section className="space-y-5">
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
        <div className="space-y-3">
          <HashLink className="text-slate-500" href={jobsHref}>
            <ArrowLeft size={16} />
            <span>返回任务时间线</span>
          </HashLink>
          <div className="flex items-center gap-2">
            <ArrowRightLeft className="text-[#1f2a44]" size={18} />
            <h2 className="text-2xl font-semibold text-slate-900">历史 Diff</h2>
          </div>
          <p className="max-w-3xl text-sm leading-6 text-slate-600">
            Diff 使用已持久化 lineage，不重新做相似度匹配。页面只展示 change kind、计数以及可回到新旧 unit 的安全入口。
          </p>
        </div>
        <button
          className="inline-flex items-center justify-center gap-2 rounded-xl bg-[#1f2a44] px-4 py-2 text-sm font-semibold text-[#f4efe6] transition hover:bg-[#2d3b5a]"
          onClick={onRefresh}
          type="button"
        >
          <RefreshCw className={refreshing ? "animate-spin" : ""} size={16} />
          <span>{refreshing ? "刷新中" : "刷新"}</span>
        </button>
      </div>
    </div>

    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap gap-3 text-sm">
        <HashLink href={buildDocumentHref(diff.old_document_id)}>旧文档版本</HashLink>
        <HashLink href={buildDocumentHref(diff.new_document_id)}>新文档版本</HashLink>
        {diff.changes.items[0] ? <HashLink href={buildLineageHref(diff.changes.items[0].lineage_id)}>打开首个 lineage</HashLink> : null}
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {Object.entries(diff.counts).map(([kind, count]) => (
          <div key={kind} className={`rounded-xl border px-4 py-3 text-sm font-semibold ${KIND_META[kind as DiffItem["kind"]]}`}>
            {kind}: {count}
          </div>
        ))}
      </div>
    </section>

    <div className="space-y-4">
      {diff.changes.items.map((change) => (
        <article key={`${change.lineage_id}-${change.kind}-${change.old?.id ?? "none"}-${change.new?.id ?? "none"}`} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-col gap-3 border-b border-slate-200 pb-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex flex-wrap items-center gap-3">
              <span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${KIND_META[change.kind]}`}>
                {change.kind}
              </span>
              <span className="break-all font-mono text-xs text-slate-700">lineage {change.lineage_id}</span>
            </div>
            <HashLink href={buildLineageHref(change.lineage_id)}>查看 lineage</HashLink>
          </div>
          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <SideCard buildDocumentHref={buildDocumentHref} buildEvidenceHref={buildEvidenceHref} title="旧版本" unit={change.old} />
            <SideCard buildDocumentHref={buildDocumentHref} buildEvidenceHref={buildEvidenceHref} title="新版本" unit={change.new} />
          </div>
        </article>
      ))}
    </div>

    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <PaginationBar page={diff.changes} onPageChange={onPageChange} />
    </div>
  </section>
);

const SideCard: React.FC<{
  title: string;
  unit: UnitDTO | null;
  buildDocumentHref: (id: string) => string;
  buildEvidenceHref: (id: string) => string;
}> = ({ title, unit, buildDocumentHref, buildEvidenceHref }) => (
  <section className="rounded-xl border border-slate-200 bg-slate-50 p-4">
    <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
    {unit ? (
      <div className="mt-3 space-y-3 text-sm text-slate-600">
        <StatusBadge compact status={unit.status} />
        <p className="break-all font-mono text-xs text-slate-800">{unit.id}</p>
        <p>字符范围 [{unit.start_char}, {unit.end_char})</p>
        <p className="break-all font-mono text-xs">hash {unit.content_hash}</p>
        <div className="flex flex-wrap gap-3">
          <HashLink href={buildEvidenceHref(unit.id)}>查看 unit 证据</HashLink>
          <HashLink href={buildDocumentHref(unit.document_id)}>对应文档</HashLink>
        </div>
      </div>
    ) : (
      <p className="mt-3 text-sm text-slate-500">该侧没有对应 unit。</p>
    )}
  </section>
);
