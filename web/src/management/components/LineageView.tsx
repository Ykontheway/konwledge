import React from "react";
import { ArrowLeft, RefreshCw } from "lucide-react";

import type { LineageDTO } from "../types";
import { HashLink } from "./HashLink";
import { UnitList } from "./UnitList";

interface LineageViewProps {
  readonly lineage: LineageDTO;
  readonly refreshing: boolean;
  readonly onRefresh: () => void;
  readonly jobsHref: string;
  readonly onPageChange: (offset: number) => void;
  readonly buildDocumentHref: (id: string) => string;
  readonly buildLineageHref: (id: string) => string;
  readonly buildEvidenceHref: (id: string) => string;
}

export const LineageView: React.FC<LineageViewProps> = ({
  lineage,
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
          <h2 className="text-2xl font-semibold text-slate-900">Lineage 历史</h2>
          <p className="max-w-3xl text-sm leading-6 text-slate-600">
            status 来自数据库里已持久化的 cached status。它不是任务评测当时的 as-of 视角，页面不会替你补算状态。
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
      <h3 className="text-lg font-semibold text-slate-900">lineage id</h3>
      <p className="mt-3 break-all font-mono text-xs text-slate-700">{lineage.id}</p>
      <div className="mt-4 text-sm text-slate-600">共 {lineage.versions.total} 个版本。</div>
      <div className="mt-3 text-sm">
        <HashLink href={buildLineageHref(lineage.id)}>刷新当前 lineage 链接</HashLink>
      </div>
    </section>

    <UnitList
      buildDocumentHref={buildDocumentHref}
      buildEvidenceHref={buildEvidenceHref}
      buildLineageHref={buildLineageHref}
      description="下方按导入顺序展示同一稳定 lineage 的历史 unit 版本，并保留当时缓存下来的状态。"
      onPageChange={onPageChange}
      page={lineage.versions}
      showDocument
      title="版本列表"
    />
  </section>
);
