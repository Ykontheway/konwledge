import React from "react";
import { ArrowLeft, CheckCircle2, Clock3, GitBranch, RefreshCw, ShieldAlert } from "lucide-react";

import { formatFull } from "../../lib/format";
import type { JobDTO } from "../types";
import { HashLink } from "./HashLink";
import { ResourceState } from "./ResourceState";
import { StatusBadge, statusLabel } from "./StatusBadge";

interface JobDetailProps {
  readonly job: JobDTO;
  readonly refreshing: boolean;
  readonly onRefresh: () => void;
  readonly jobsHref: string;
  readonly buildJobHref: (id: string) => string;
  readonly buildRevisionHref: (id: string) => string;
}

const METRIC_LABELS = [
  ["case_count", "用例数"],
  ["answerable_count", "可回答用例"],
  ["no_answer_count", "No-answer 用例"],
  ["mean_recall", "平均 Recall"],
  ["mean_precision", "平均 Precision"],
  ["mean_reciprocal_rank", "MRR"],
  ["mean_ndcg", "NDCG"],
  ["hit_rate", "Hit Rate"],
  ["no_answer_accuracy", "No-answer Accuracy"],
  ["citation_coverage", "Citation Coverage"],
  ["stale_hits", "stale hits"],
] as const;

export const JobDetail: React.FC<JobDetailProps> = ({
  job,
  refreshing,
  onRefresh,
  jobsHref,
  buildJobHref,
  buildRevisionHref,
}) => {
  const headline =
    job.status === "blocked"
      ? { kind: "blocked" as const, text: "该任务当前处于 blocked 状态，页面只展示持久化结果，不补做缺失评测。" }
      : job.status === "superseded"
        ? { kind: "superseded" as const, text: "该任务已经被后续任务替代；这里保留原始结果，方便回看历史。" }
        : null;

  return (
    <section className="space-y-5">
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-3">
            <HashLink className="text-slate-500" href={jobsHref}>
              <ArrowLeft size={16} />
              <span>返回任务列表</span>
            </HashLink>
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge status={job.status} />
              <h2 className="text-2xl font-semibold text-slate-900">任务详情</h2>
            </div>
            <p className="max-w-3xl text-sm leading-6 text-slate-600">
              此页面保留任务当时的时间、数据集与知识指纹。失败任务只提供定位线索，不自动断言某次变更就是指标下降原因。
            </p>
          </div>
          <button
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-[#1f2a44] px-4 py-2 text-sm font-semibold text-[#f4efe6] transition hover:bg-[#2d3b5a] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#d39b3b]"
            onClick={onRefresh}
            type="button"
          >
            <RefreshCw className={refreshing ? "animate-spin" : ""} size={16} />
            <span>{refreshing ? "刷新中" : "刷新"}</span>
          </button>
        </div>
      </div>

      {headline ? (
        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <ResourceState description={headline.text} kind={headline.kind} title={`任务状态：${statusLabel(job.status)}`} />
        </div>
      ) : null}

      <section className="grid gap-5 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h3 className="text-lg font-semibold text-slate-900">固定时间与指纹</h3>
          <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2">
            <Field label="job id" mono value={job.id} />
            <Field label="parent job" mono value={job.parent_job_id ?? "—"} />
            <Field label="创建时间" value={formatFull(job.created_at)} />
            <Field label="评测时间" value={formatFull(job.evaluated_at)} />
            <Field label="started_at" value={job.started_at ? formatFull(job.started_at) : "—"} />
            <Field label="finished_at" value={job.finished_at ? formatFull(job.finished_at) : "—"} />
            <Field label="dataset id" mono value={job.dataset_id} />
            <Field label="dataset version" value={`v${job.dataset_version}`} />
            <Field label="config generation" value={String(job.config_generation)} />
            <Field label="attempt count" value={String(job.attempt_count)} />
            <Field label="dataset hash" mono value={job.dataset_hash} />
            <Field label="knowledge fingerprint" mono value={job.knowledge_fingerprint} />
            <Field label="config fingerprint" mono value={job.config_fingerprint} />
            <Field label="error code" value={job.error_code ?? "—"} />
          </dl>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h3 className="text-lg font-semibold text-slate-900">触发 revision</h3>
          <div className="mt-4 space-y-3">
            {job.parent_job_id ? (
              <HashLink href={buildJobHref(job.parent_job_id)}>查看父任务</HashLink>
            ) : (
              <p className="text-sm text-slate-500">无父任务。</p>
            )}
            {job.trigger_revision_ids.length > 0 ? (
              <ul className="space-y-2">
                {job.trigger_revision_ids.map((revisionId) => (
                  <li key={revisionId}>
                    <HashLink href={buildRevisionHref(revisionId)}>
                      <GitBranch size={16} />
                      <span className="break-all font-mono text-xs">{revisionId}</span>
                    </HashLink>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-slate-500">当前任务没有记录触发 revision。</p>
            )}
          </div>
        </div>
      </section>

      {job.report ? (
        <section className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="text-emerald-600" size={18} />
              <h3 className="text-lg font-semibold text-slate-900">聚合指标</h3>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {METRIC_LABELS.map(([key, label]) => (
                <MetricCard key={key} label={label} value={job.report?.[key as "citation_coverage" | "stale_hits"] ?? job.report?.metrics[key as keyof typeof job.report.metrics]} />
              ))}
            </div>
            <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600">
              <p>报告状态：{job.report.passed ? "通过" : "未通过"}</p>
              <p>检索退化：{job.report.degraded ? "是" : "否"}</p>
            </div>
          </div>

          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-2">
              <ShieldAlert className="text-[#1f2a44]" size={18} />
              <h3 className="text-lg font-semibold text-slate-900">质量检查</h3>
            </div>
            <ul className="mt-4 space-y-3">
              {job.report.checks.map((check) => (
                <li key={check.name} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <span className="text-sm font-semibold text-slate-900">{check.name}</span>
                    <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${check.passed ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}>
                      {check.passed ? "通过" : "未通过"}
                    </span>
                  </div>
                  <dl className="mt-3 grid gap-2 text-sm text-slate-600 sm:grid-cols-2">
                    <div>
                      <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">actual</dt>
                      <dd className="mt-1 font-medium text-slate-900">{formatMetricValue(check.actual)}</dd>
                    </div>
                    <div>
                      <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">threshold</dt>
                      <dd className="mt-1 font-medium text-slate-900">{check.threshold ?? "—"}</dd>
                    </div>
                  </dl>
                </li>
              ))}
            </ul>
          </div>
        </section>
      ) : (
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center gap-2 text-slate-700">
            <Clock3 size={18} />
            <h3 className="text-lg font-semibold">聚合报告尚未生成</h3>
          </div>
          <p className="mt-3 text-sm leading-6 text-slate-600">
            当前任务没有 report。页面不会把缺失值渲染成 0，也不会假装任务已经成功完成。
          </p>
        </div>
      )}
    </section>
  );
};

const Field: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono = false }) => (
  <div>
    <dt className="text-xs uppercase tracking-[0.16em] text-slate-500">{label}</dt>
    <dd className={`mt-1 ${mono ? "font-mono text-xs break-all" : "font-medium"} text-slate-900`}>{value}</dd>
  </div>
);

const MetricCard: React.FC<{ label: string; value: unknown }> = ({ label, value }) => (
  <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
    <div className="text-xs uppercase tracking-[0.16em] text-slate-500">{label}</div>
    <div className="mt-2 text-lg font-semibold text-slate-900">{formatMetricValue(value)}</div>
  </div>
);

function formatMetricValue(value: unknown): string {
  if (value === null || value === undefined) return "未评估";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}
