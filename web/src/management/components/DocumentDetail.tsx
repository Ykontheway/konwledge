import React from "react";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { formatFull } from "../../lib/format";
import type { DocumentDTO } from "../types";
import { HashLink } from "./HashLink";
import { UnitList } from "./UnitList";

interface DocumentDetailProps {
  readonly document: DocumentDTO;
  readonly refreshing: boolean;
  readonly onRefresh: () => void;
  readonly jobsHref: string;
  readonly onPageChange: (offset: number) => void;
  readonly buildDocumentHref: (id: string) => string;
  readonly buildDiffHref: (newId: string, oldId: string) => string;
  readonly buildLineageHref: (id: string) => string;
  readonly buildEvidenceHref: (id: string) => string;
}

export const DocumentDetail: React.FC<DocumentDetailProps> = ({
  document,
  refreshing,
  onRefresh,
  jobsHref,
  onPageChange,
  buildDocumentHref,
  buildDiffHref,
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
          <h2 className="text-2xl font-semibold text-slate-900">文档版本</h2>
          <p className="max-w-3xl text-sm leading-6 text-slate-600">
            文档的前后关系代表导入顺序，不等于某一时刻的发布 head。这里展示的是不可变历史版本与其分页单元列表。
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
      <h3 className="text-lg font-semibold text-slate-900">版本信息</h3>
      <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2 xl:grid-cols-3">
        <Field label="document id" mono value={document.id} />
        <Field label="source id" mono value={document.source_id} />
        <Field label="content type" value={document.content_type} />
        <Field label="checksum" mono value={document.checksum} />
        <Field label="ingested at" value={formatFull(document.ingested_at)} />
        <Field label="units total" value={String(document.units.total)} />
      </dl>
      <div className="mt-4 flex flex-wrap gap-3 text-sm">
        {document.previous_document_id ? (
          <HashLink href={buildDocumentHref(document.previous_document_id)}>更早版本</HashLink>
        ) : null}
        {document.next_document_id ? <HashLink href={buildDocumentHref(document.next_document_id)}>后续版本</HashLink> : null}
        {document.previous_document_id ? (
          <HashLink href={buildDiffHref(document.id, document.previous_document_id)}>对比上一版本</HashLink>
        ) : null}
      </div>
    </section>

    <UnitList
      buildDocumentHref={buildDocumentHref}
      buildEvidenceHref={buildEvidenceHref}
      buildLineageHref={buildLineageHref}
      description="单元列表只显示元信息与安全导航链接；如需原文选段，请进入对应 evidence 页面。"
      onPageChange={onPageChange}
      page={document.units}
      title="文档单元"
    />
  </section>
);

const Field: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono = false }) => (
  <div>
    <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">{label}</dt>
    <dd className={`mt-1 ${mono ? "font-mono text-xs break-all" : "font-medium"} text-slate-900`}>{value}</dd>
  </div>
);
