import React from "react";
import {
  BarChart3,
  BookOpen,
  ChevronRight,
  CodeIcon,
  GitCompare,
  History,
  RadioTower,
  Zap,
} from "lucide-react";

import { REPO_URL, snapshot } from "./data";
import { formatMoment } from "./lib/format";

export type View = "overview" | "trace" | "diff" | "lifecycle";

interface LayoutProps {
  readonly currentView: View;
  readonly currentSection: string;
  readonly currentTitle: string;
  readonly currentTone: "demo" | "live";
  readonly onViewChange: (view: View) => void;
  readonly children: React.ReactNode;
}

export const Layout: React.FC<LayoutProps> = ({
  currentView,
  currentSection,
  currentTitle,
  currentTone,
  onViewChange,
  children,
}) => (
  <div className="flex min-h-screen flex-col bg-[#f4efe6] lg:h-screen lg:flex-row lg:overflow-hidden">
    <aside className="w-full flex-shrink-0 bg-[#1f2a44] text-[#f4efe6] lg:w-72 lg:flex lg:flex-col">
      <div className="border-b border-[#2d3b5a] p-6">
        <h1 className="flex items-center gap-2 text-xl font-bold">
          <span className="text-[#d39b3b]">Knowledge</span>
          <span>CI</span>
        </h1>
        <p className="mt-1 text-xs italic text-[#a0aec0]">Traceable Knowledge Lifecycle</p>
      </div>

      <nav className="flex-1 space-y-6 overflow-y-auto p-4" aria-label="主导航">
        <div className="space-y-2">
          <SectionLabel label="静态演示" tone="demo" />
          <NavItem active={currentView === "overview"} icon={<BarChart3 size={18} />} label="总览" onClick={() => onViewChange("overview")} />
          <NavItem active={currentView === "trace"} icon={<BookOpen size={18} />} label="原文溯源" onClick={() => onViewChange("trace")} />
          <NavItem active={currentView === "diff"} icon={<GitCompare size={18} />} label="版本 Diff" onClick={() => onViewChange("diff")} />
        </div>

        <div className="space-y-2">
          <SectionLabel label="实时工作台 / 只读" tone="live" />
          <NavItem
            active={currentView === "lifecycle"}
            badge="Live"
            icon={<History size={18} />}
            label="生命周期工作台"
            onClick={() => onViewChange("lifecycle")}
          />
          <div className="rounded-xl border border-[#2d3b5a] bg-[#1a243b] p-3 text-xs leading-5 text-[#a0aec0]">
            <div className="flex items-center gap-2 font-semibold text-[#f4efe6]">
              <RadioTower size={14} className="text-[#d39b3b]" />
              <span>接入 B1 管理 API</span>
            </div>
            <p className="mt-2">
              没有管理服务时，静态演示页仍可访问；实时工作台会停在只读提示，不会伪造成功数据。
            </p>
          </div>
        </div>

        <div className="space-y-2">
          <SectionLabel label="演示故事线" tone="muted" />
          {snapshot.story?.map((step, index) => (
            <div
              key={index}
              className="group relative cursor-default border-l border-[#2d3b5a] py-2 pl-6 transition-colors hover:border-[#d39b3b]"
            >
              <div className="absolute left-[-5px] top-[14px] h-2 w-2 rounded-full bg-[#2d3b5a] group-hover:bg-[#d39b3b]" />
              <div className="text-[10px] text-[#a0aec0]">{formatMoment(step.at)}</div>
              <div className="line-clamp-1 text-xs font-medium text-[#e2e8f0]">{step.title}</div>
            </div>
          ))}
        </div>
      </nav>

      <div className="border-t border-[#2d3b5a] bg-[#1a243b] p-4">
        <a
          className="flex items-center gap-2 text-sm text-[#a0aec0] transition-colors hover:text-[#f4efe6]"
          href={REPO_URL}
          rel="noopener noreferrer"
          target="_blank"
        >
          <CodeIcon size={18} />
          <span>GitHub Repository</span>
        </a>
      </div>
    </aside>

    <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <header className="border-b bg-white px-4 py-4 shadow-sm sm:px-6">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <span>{currentSection}</span>
              <ChevronRight size={14} />
              <span className="font-medium text-slate-900">{currentTitle}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <div className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${currentTone === "live" ? "border-[#d39b3b] bg-amber-50 text-amber-800" : "border-slate-200 bg-slate-50 text-slate-700"}`}>
              <Zap fill="currentColor" size={14} />
              <span>{currentTone === "live" ? "实时工作台 / 只读" : `Snapshot v${snapshot.schema_version} / 静态演示`}</span>
            </div>
          </div>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-4 sm:p-6 lg:p-8">{children}</div>
    </main>
  </div>
);

const SectionLabel: React.FC<{ label: string; tone: "demo" | "live" | "muted" }> = ({ label, tone }) => (
  <div className={`px-2 text-xs font-semibold uppercase tracking-[0.16em] ${tone === "live" ? "text-[#d39b3b]" : tone === "demo" ? "text-[#8fb0df]" : "text-[#718096]"}`}>
    {label}
  </div>
);

const NavItem: React.FC<{
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  badge?: string;
}> = ({ active, onClick, icon, label, badge }) => (
  <button
    className={`flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-sm font-medium transition-all ${active ? "bg-[#d39b3b] text-[#1f2a44] shadow-lg" : "text-[#a0aec0] hover:bg-[#2d3b5a] hover:text-[#f4efe6]"}`}
    onClick={onClick}
    type="button"
  >
    <span className="flex items-center gap-3">
      {icon}
      <span>{label}</span>
    </span>
    {badge ? <span className="rounded bg-[#2d3b5a] px-1.5 py-0.5 text-[10px] text-[#a0aec0]">{badge}</span> : null}
  </button>
);
