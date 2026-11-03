import React from "react";
import { 
  BarChart3, 
  BookOpen, 
  GitCompare, 
  History, 
  Zap,
  CodeIcon,
  ChevronRight
  } from "lucide-react";
import { snapshot, REPO_URL } from "./data";
import { formatMoment } from "./lib/format";

export type View = "overview" | "trace" | "diff" | "lifecycle";

interface LayoutProps {
  currentView: View;
  onViewChange: (view: View) => void;
  children: React.ReactNode;
}

export const Layout: React.FC<LayoutProps> = ({ currentView, onViewChange, children }) => {
  return (
    <div className="flex h-screen overflow-hidden bg-[#f4efe6]">
      {/* Sidebar */}
      <aside className="w-64 flex-shrink-0 bg-[#1f2a44] text-[#f4efe6] flex flex-col">
        <div className="p-6 border-b border-[#2d3b5a]">
          <h1 className="text-xl font-bold flex items-center gap-2">
            <span className="text-[#d39b3b]">Knowledge</span>
            <span>CI</span>
          </h1>
          <p className="text-xs text-[#a0aec0] mt-1 italic">
            Traceable Knowledge Lifecycle
          </p>
        </div>

        <nav className="flex-1 p-4 space-y-2 overflow-y-auto">
          <NavItem 
            active={currentView === "overview"} 
            onClick={() => onViewChange("overview")}
            icon={<BarChart3 size={20} />}
            label="总览"
          />
          <NavItem 
            active={currentView === "trace"} 
            onClick={() => onViewChange("trace")}
            icon={<BookOpen size={20} />}
            label="原文溯源"
          />
          <NavItem 
            active={currentView === "diff"} 
            onClick={() => onViewChange("diff")}
            icon={<GitCompare size={20} />}
            label="版本 Diff"
          />
          <NavItem 
            active={currentView === "lifecycle"} 
            onClick={() => onViewChange("lifecycle")}
            icon={<History size={20} />}
            label="生命周期"
            disabled
            badge="V3"
          />

          <div className="pt-8 pb-2 px-2 text-xs font-semibold text-[#718096] uppercase tracking-wider">
            演示故事线
          </div>
          {snapshot.story?.map((step, i) => (
             <div 
               key={i} 
               className="group relative pl-6 py-2 border-l border-[#2d3b5a] hover:border-[#d39b3b] transition-colors cursor-default"
             >
               <div className="absolute left-[-5px] top-[14px] w-2 h-2 rounded-full bg-[#2d3b5a] group-hover:bg-[#d39b3b]" />
               <div className="text-[10px] text-[#a0aec0]">{formatMoment(step.at)}</div>
               <div className="text-xs font-medium text-[#e2e8f0] line-clamp-1">{step.title}</div>
             </div>
          ))}
        </nav>

        <div className="p-4 border-t border-[#2d3b5a] bg-[#1a243b]">
          <a 
            href={REPO_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 text-sm text-[#a0aec0] hover:text-[#f4efe6] transition-colors"
          >
            <CodeIcon size={18} />
            <span>GitHub Repository</span>
          </a>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 flex flex-col overflow-hidden">
        <header className="h-14 bg-white border-b flex items-center justify-between px-6 shadow-sm z-10">
          <div className="flex items-center gap-2 text-sm text-gray-500">
             <span>项目</span>
             <ChevronRight size={14} />
             <span className="text-gray-900 font-medium">
               {currentView === "overview" ? "总览" : 
                currentView === "trace" ? "原文溯源" : 
                currentView === "diff" ? "版本 Diff" : "生命周期"}
             </span>
          </div>

          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1.5 text-xs bg-amber-50 text-amber-700 px-2.5 py-1 rounded-full border border-amber-200">
               <Zap size={14} fill="currentColor" />
               <span>v{snapshot.schema_version} Demo</span>
            </div>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto p-8 relative">
          {children}
        </div>
      </main>
    </div>
  );
};

const NavItem: React.FC<{ 
  active: boolean; 
  onClick: () => void; 
  icon: React.ReactNode; 
  label: string;
  disabled?: boolean;
  badge?: string;
}> = ({ active, onClick, icon, label, disabled, badge }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className={`w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium transition-all ${
      active 
        ? "bg-[#d39b3b] text-[#1f2a44] shadow-lg" 
        : disabled
          ? "text-[#4a5568] opacity-60 cursor-not-allowed"
          : "text-[#a0aec0] hover:bg-[#2d3b5a] hover:text-[#f4efe6]"
    }`}
  >
    <div className="flex items-center gap-3">
      {icon}
      <span>{label}</span>
    </div>
    {badge && (
      <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#2d3b5a] text-[#a0aec0]">
        {badge}
      </span>
    )}
  </button>
);
