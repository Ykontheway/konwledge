import React from "react";
import { Filter, RefreshCw } from "lucide-react";

import { formatFull } from "../../lib/format";
import type { JobDTO, JobStatus, Page } from "../types";
import { HashLink } from "./HashLink";
import { PaginationBar } from "./PaginationBar";
import { StatusBadge } from "./StatusBadge";

const FILTER_OPTIONS: Array<{ value: JobStatus | "all"; label: string }> = [
  { value: "all", label: "全部状态" },
  { value: "queued", label: "排队中" },
  { value: "running", label: "运行中" },
  { value: "passed", label: "通过" },
  { value: "failed", label: "失败" },
  { value: "blocked", label: "阻塞" },
  { value: "superseded", label: "已替代" },
];

interface JobListProps {
  readonly page: Page<JobDTO>;
  readonly refreshing: boolean;
  readonly selectedStatus?: JobStatus;
  readonly onRefresh: () => void;
  readonly onStatusChange: (status: JobStatus | undefined) => void;
  readonly onPageChange: (offset: number) => void;
  readonly buildJobHref: (id: string) => string;
}

export const JobList: React.FC<JobListProps> = ({
  page,
  refreshing,
  selectedStatus,
  onRefresh,
  onStatusChange,
  onPageChange,
  buildJobHref,
}) => (
  <section className="space-y-5">
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-2">
          <p className="text-sm font-semibold uppercase tracking-[0.16em] text-[#d39b3b]">实时工作台 / 只读</p>
          <h2 className="text-2xl font-semibold text-slate-900">任务时间线</h2>
          <p className="max-w-3xl text-sm leading-6 text-slate-600">
            分页读取 B1 管理 API 返回的真实任务历史。列表可能随着新任务进入而变化，跨请求分页不视为冻结快照。
          </p>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <label className="flex items-center gap-2 rounded-xl border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-700">
            <Filter size={16} className="text-slate-500" />
            <span className="sr-only">按状态过滤任务</span>
            <select
              aria-label="按状态过滤任务"
              className="bg-transparent text-sm font-medium focus:outline-none"
              onChange={(event) => onStatusChange(event.target.value === "all" ? undefined : (event.target.value as JobStatus))}
              value={selectedStatus ?? "all"}
            >
              {FILTER_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
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
    </div>

    <ol className="space-y-4" aria-label="任务时间线">
      {page.items.length > 0 ? (
        page.items.map((job) => (
          <li key={job.id} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-3">
                  <StatusBadge status={job.status} />
                  <span className="text-sm font-semibold text-slate-900">job {job.id}</span>
                </div>
                <div className="grid gap-2 text-sm text-slate-600 sm:grid-cols-2 xl:grid-cols-3">
                  <span>创建：{formatFull(job.created_at)}</span>
                  <span>评测：{formatFull(job.evaluated_at)}</span>
                  <span>dataset v{job.dataset_version}</span>
                  <span>attempt {job.attempt_count}</span>
                  <span>revision 数：{job.trigger_revision_ids.length}</span>
                  <span>error：{job.error_code ?? "—"}</span>
                </div>
              </div>
              <div className="flex flex-wrap gap-3 text-sm">
                <HashLink href={buildJobHref(job.id)}>查看任务</HashLink>
                {job.parent_job_id ? <span className="break-all font-mono text-xs text-slate-500">parent {job.parent_job_id}</span> : null}
              </div>
            </div>
          </li>
        ))
      ) : (
        <li className="rounded-2xl border border-dashed border-slate-300 bg-white p-8 text-center text-sm leading-6 text-slate-600 shadow-sm">
          当前筛选下没有任务。你可以切换状态过滤或手动刷新，再看看是否出现新的实时任务。
        </li>
      )}
    </ol>

    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <PaginationBar page={page} onPageChange={onPageChange} />
    </div>
  </section>
);
