import React from "react";
import { ArrowLeft, RefreshCw, TextQuote } from "lucide-react";

import { codePointLength } from "../../lib/text";
import type { EvidenceDTO } from "../types";
import { HashLink } from "./HashLink";

interface EvidenceViewProps {
  readonly evidence: EvidenceDTO;
  readonly refreshing: boolean;
  readonly onRefresh: () => void;
  readonly jobsHref: string;
  readonly buildDocumentHref: (id: string) => string;
  readonly buildLineageHref: (id: string) => string;
  readonly buildRevisionHref: (id: string) => string;
}

export const EvidenceView: React.FC<EvidenceViewProps> = ({
  evidence,
  refreshing,
  onRefresh,
  jobsHref,
  buildDocumentHref,
  buildLineageHref,
  buildRevisionHref,
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
            <TextQuote className="text-[#1f2a44]" size={18} />
            <h2 className="text-2xl font-semibold text-slate-900">历史证据</h2>
          </div>
          <p className="max-w-3xl text-sm leading-6 text-slate-600">
            这是 B1 唯一明确返回正文的管理响应。文本按 exact unit 选段返回，不提供整篇文档定位，也不会把未受信文本当 HTML 执行。
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

    <div className="grid gap-5 xl:grid-cols-[0.95fr_1.05fr]">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h3 className="text-lg font-semibold text-slate-900">元信息</h3>
        <dl className="mt-4 grid gap-4 text-sm">
          <Field label="source id" mono value={evidence.source_id} />
          <Field label="document id" mono value={evidence.document_id} />
          <Field label="unit id" mono value={evidence.unit_id} />
          <Field label="lineage id" mono value={evidence.lineage_id} />
          <Field label="字符范围" value={`[${evidence.start_char}, ${evidence.end_char})`} />
          <Field label="选段 code points" value={String(codePointLength(evidence.text))} />
          <Field label="content hash" mono value={evidence.content_hash} />
          <Field label="document checksum" mono value={evidence.document_checksum} />
        </dl>
        <div className="mt-4 flex flex-wrap gap-3 text-sm">
          <HashLink href={buildDocumentHref(evidence.document_id)}>打开文档版本</HashLink>
          <HashLink href={buildLineageHref(evidence.lineage_id)}>打开 lineage</HashLink>
        </div>
        <div className="mt-5 space-y-2 text-sm">
          <h4 className="font-semibold text-slate-900">关联 revision</h4>
          {evidence.revision_ids.length > 0 ? (
            evidence.revision_ids.map((revisionId) => (
              <div key={revisionId}>
                <HashLink href={buildRevisionHref(revisionId)}>
                  <span className="break-all font-mono text-xs">{revisionId}</span>
                </HashLink>
              </div>
            ))
          ) : (
            <p className="text-slate-500">没有记录 revision 锚点。</p>
          )}
        </div>
      </section>

      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h3 className="text-lg font-semibold text-slate-900">未受信文本选段</h3>
        <p className="mt-2 text-sm leading-6 text-slate-600">
          这里直接展示服务返回的 exact unit 文本。Python 偏移按 Unicode code point 计算，页面不会把它当作 JavaScript UTF-16 下标来重切整篇正文。
        </p>
        <pre className="mt-4 overflow-x-auto rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm leading-7 whitespace-pre-wrap break-words text-slate-900">
          {evidence.text}
        </pre>
      </section>
    </div>
  </section>
);

const Field: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono = false }) => (
  <div>
    <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">{label}</dt>
    <dd className={`mt-1 ${mono ? "font-mono text-xs break-all" : "font-medium"} text-slate-900`}>{value}</dd>
  </div>
);
