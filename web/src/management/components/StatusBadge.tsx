import React from "react";

import type { JobStatus, UnitDTO } from "../types";

type UnitStatus = UnitDTO["status"];
type StatusValue = JobStatus | UnitStatus;

const STATUS_META: Record<StatusValue, { label: string; classes: string }> = {
  queued: { label: "排队中", classes: "bg-slate-100 text-slate-700 border-slate-200" },
  running: { label: "运行中", classes: "bg-blue-100 text-blue-700 border-blue-200" },
  passed: { label: "通过", classes: "bg-emerald-100 text-emerald-700 border-emerald-200" },
  failed: { label: "失败", classes: "bg-rose-100 text-rose-700 border-rose-200" },
  blocked: { label: "阻塞", classes: "bg-amber-100 text-amber-800 border-amber-200" },
  superseded: { label: "已替代", classes: "bg-violet-100 text-violet-700 border-violet-200" },
  active: { label: "active", classes: "bg-emerald-100 text-emerald-700 border-emerald-200" },
  stale: { label: "stale", classes: "bg-amber-100 text-amber-800 border-amber-200" },
  deleted: { label: "deleted", classes: "bg-rose-100 text-rose-700 border-rose-200" },
};

interface StatusBadgeProps {
  readonly status: StatusValue;
  readonly compact?: boolean;
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({ status, compact = false }) => {
  const meta = STATUS_META[status];
  return (
    <span
      className={`${compact ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs"} inline-flex items-center rounded-full border font-semibold ${meta.classes}`}
      title={status}
    >
      {meta.label}
    </span>
  );
};

export function statusLabel(status: StatusValue): string {
  return STATUS_META[status].label;
}
