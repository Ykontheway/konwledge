import React from "react";
import { ArrowLeft, GitBranch, RefreshCw } from "lucide-react";

import { formatFull } from "../../lib/format";
import type { RevisionDTO } from "../types";
import { HashLink } from "./HashLink";

interface RevisionDetailProps {
  readonly revision: RevisionDTO;
  readonly refreshing: boolean;
  readonly onRefresh: () => void;
  readonly jobsHref: string;
  readonly buildDocumentHref: (id: string) => string;
  readonly buildDiffHref: (newId: string, oldId: string) => string;
  readonly buildEvidenceHref: (id: string) => string;
  readonly buildLineageHref: (id: string) => string;
}

export const RevisionDetail: React.FC<RevisionDetailProps> = ({
  revision,
  refreshing,
  onRefresh,
  jobsHref,
  buildDocumentHref,
  buildDiffHref,
  buildEvidenceHref,
  buildLineageHref,
}) => {
  const navigation = revision.navigation;
  const canOpenDiff = Boolean(navigation.document_id && navigation.previous_document_id);

  return (
    <section className="space-y-5">
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-3">
            <HashLink className="text-slate-500" href={jobsHref}>
              <ArrowLeft size={16} />
              <span>返回任务时间线</span>
            </HashLink>
            <div className="flex items-center gap-2">
              <GitBranch className="text-[#1f2a44]" size={20} />
              <h2 className="text-2xl font-semibold text-slate-900">Revision 详情</h2>
            </div>
            <p className="max-w-3xl text-sm leading-6 text-slate-600">
              revision 提供的是白名单导航字段：你可以回到历史文档、unit 与 lineage，但页面不会把这些字段包装成因果结论。
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

      <section className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h3 className="text-lg font-semibold text-slate-900">基本信息</h3>
          <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2">
            <Field label="revision id" mono value={revision.id} />
            <Field label="entity id" mono value={revision.entity_id} />
            <Field label="entity type" value={revision.entity_type} />
            <Field label="change type" value={revision.change_type} />
            <Field label="revision" value={String(revision.revision)} />
            <Field label="created_at" value={formatFull(revision.created_at)} />
            <Field label="event" value={navigation.event ?? "—"} />
            <Field label="reverted" value={navigation.reverted === null || navigation.reverted === undefined ? "—" : String(navigation.reverted)} />
            <Field label="document checksum" mono value={navigation.document_checksum ?? "—"} />
            <Field label="content hash" mono value={navigation.content_hash ?? "—"} />
            <Field label="previous content hash" mono value={navigation.previous_content_hash ?? "—"} />
          </dl>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h3 className="text-lg font-semibold text-slate-900">导航入口</h3>
          <div className="mt-4 space-y-3 text-sm">
            {navigation.document_id ? <HashLink href={buildDocumentHref(navigation.document_id)}>打开当前文档版本</HashLink> : null}
            {navigation.previous_document_id ? (
              <HashLink href={buildDocumentHref(navigation.previous_document_id)}>打开前一文档版本</HashLink>
            ) : null}
            {canOpenDiff ? (
              <HashLink href={buildDiffHref(navigation.document_id!, navigation.previous_document_id!)}>查看这次文档 Diff</HashLink>
            ) : null}
            {navigation.unit_id ? <HashLink href={buildEvidenceHref(navigation.unit_id)}>查看当前 unit 证据</HashLink> : null}
            {navigation.previous_unit_id ? (
              <HashLink href={buildEvidenceHref(navigation.previous_unit_id)}>查看前一 unit 证据</HashLink>
            ) : null}
            {!navigation.document_id && !navigation.previous_document_id && !navigation.unit_id && !navigation.previous_unit_id ? (
              <p className="text-slate-500">该 revision 没有附带更细的导航 ID。</p>
            ) : null}
          </div>
        </div>
      </section>

      <section className="grid gap-5 xl:grid-cols-3">
        <LinkGroup ids={revision.affected_document_ids} label="关联文档" renderHref={buildDocumentHref} />
        <LinkGroup ids={revision.affected_unit_ids} label="关联 unit" renderHref={buildEvidenceHref} />
        <LinkGroup ids={revision.affected_lineage_ids} label="关联 lineage" renderHref={buildLineageHref} />
      </section>
    </section>
  );
};

const Field: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono = false }) => (
  <div>
    <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">{label}</dt>
    <dd className={`mt-1 ${mono ? "font-mono text-xs break-all" : "font-medium"} text-slate-900`}>{value}</dd>
  </div>
);

const LinkGroup: React.FC<{
  label: string;
  ids: readonly string[];
  renderHref: (id: string) => string;
}> = ({ label, ids, renderHref }) => (
  <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
    <h3 className="text-lg font-semibold text-slate-900">{label}</h3>
    <div className="mt-4 space-y-2 text-sm">
      {ids.length > 0 ? (
        ids.map((id) => (
          <div key={id}>
            <HashLink href={renderHref(id)}>
              <span className="break-all font-mono text-xs">{id}</span>
            </HashLink>
          </div>
        ))
      ) : (
        <p className="text-slate-500">无</p>
      )}
    </div>
  </section>
);
