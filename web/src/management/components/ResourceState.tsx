import React from "react";
import { AlertTriangle, Ban, Clock3, Lock, RefreshCw, SearchX, ShieldAlert, WifiOff } from "lucide-react";

export type ResourceStateKind =
  | "loading"
  | "empty"
  | "unauthorized"
  | "service-unavailable"
  | "not-found"
  | "gone"
  | "blocked"
  | "superseded"
  | "error"
  | "invalid-route";

interface ResourceStateProps {
  readonly kind: ResourceStateKind;
  readonly title?: string;
  readonly description?: string;
  readonly onRetry?: () => void;
}

const META: Record<ResourceStateKind, { icon: React.ReactNode; title: string; tone: string }> = {
  loading: {
    icon: <RefreshCw className="animate-spin text-[#1f2a44]" size={28} />,
    title: "正在读取实时工作台",
    tone: "border-[#d7dfef] bg-white text-[#1f2a44]",
  },
  empty: {
    icon: <SearchX className="text-slate-500" size={28} />,
    title: "当前没有可展示的数据",
    tone: "border-slate-200 bg-white text-slate-700",
  },
  unauthorized: {
    icon: <ShieldAlert className="text-amber-600" size={28} />,
    title: "当前无权访问",
    tone: "border-amber-200 bg-amber-50 text-amber-900",
  },
  "service-unavailable": {
    icon: <WifiOff className="text-[#1f2a44]" size={28} />,
    title: "未检测到管理服务",
    tone: "border-[#d7dfef] bg-[#f8f4ec] text-[#1f2a44]",
  },
  "not-found": {
    icon: <SearchX className="text-slate-500" size={28} />,
    title: "未找到对应资源",
    tone: "border-slate-200 bg-white text-slate-700",
  },
  gone: {
    icon: <Clock3 className="text-amber-700" size={28} />,
    title: "历史资源已不可用",
    tone: "border-amber-200 bg-amber-50 text-amber-900",
  },
  blocked: {
    icon: <Ban className="text-amber-700" size={28} />,
    title: "当前读取被阻止",
    tone: "border-amber-200 bg-amber-50 text-amber-900",
  },
  superseded: {
    icon: <RefreshCw className="text-violet-700" size={28} />,
    title: "该结果已被更新任务替代",
    tone: "border-violet-200 bg-violet-50 text-violet-900",
  },
  error: {
    icon: <AlertTriangle className="text-rose-700" size={28} />,
    title: "读取失败",
    tone: "border-rose-200 bg-rose-50 text-rose-900",
  },
  "invalid-route": {
    icon: <Lock className="text-slate-600" size={28} />,
    title: "链接无效，已停在安全页",
    tone: "border-slate-200 bg-white text-slate-800",
  },
};

export const ResourceState: React.FC<ResourceStateProps> = ({ kind, title, description, onRetry }) => {
  const meta = META[kind];
  return (
    <section className={`mx-auto flex max-w-3xl flex-col items-center gap-4 rounded-2xl border px-6 py-10 text-center shadow-sm ${meta.tone}`}>
      <div className="rounded-full bg-white/70 p-4 shadow-sm">{meta.icon}</div>
      <div className="space-y-2">
        <h2 className="text-xl font-semibold">{title ?? meta.title}</h2>
        {description ? <p className="max-w-2xl text-sm leading-6 opacity-85">{description}</p> : null}
      </div>
      {onRetry ? (
        <button
          className="inline-flex items-center gap-2 rounded-lg bg-[#1f2a44] px-4 py-2 text-sm font-semibold text-[#f4efe6] transition hover:bg-[#2d3b5a] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#d39b3b]"
          onClick={onRetry}
          type="button"
        >
          <RefreshCw size={16} />
          <span>重试</span>
        </button>
      ) : null}
    </section>
  );
};
