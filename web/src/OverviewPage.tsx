import React from "react";
import { snapshot } from "./data";
import { CHANGE_KINDS, vectorSavings } from "./lib/snapshot";
import { 
  Database, 
  FileText, 
  Layers, 
  GitCommit, 
  Activity,
  CheckCircle2,
  AlertCircle
} from "lucide-react";

export const OverviewPage: React.FC = () => {
  const savings = vectorSavings(snapshot);
  const coveredKinds = CHANGE_KINDS.filter((kind) => (snapshot.stats.diff_kinds[kind] ?? 0) > 0).length;

  return (
    <div className="max-w-6xl mx-auto space-y-8 animate-in fade-in duration-500">
      <section className="bg-white rounded-xl shadow-sm border p-8">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-3xl font-bold text-gray-900">{snapshot.title}</h2>
            <p className="text-gray-500 mt-2 max-w-2xl text-lg">
              自动化的知识生命周期系统。此演示页基于预置的示例语料，展示系统在版本演进、切片追踪和状态维护方面的核心能力。
            </p>
          </div>
          <div className="text-right">
            <div className="text-sm text-gray-400">最新状态采样</div>
            <div className="text-sm font-mono text-gray-600">{snapshot.as_of}</div>
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-6 mt-10">
          <StatCard icon={<FileText className="text-blue-500" />} label="文档版本" value={snapshot.stats.documents} />
          <StatCard icon={<Layers className="text-emerald-500" />} label="知识片段" value={snapshot.stats.lineages} />
          <StatCard icon={<Database className="text-amber-500" />} label="存储记录" value={snapshot.stats.units} />
          <StatCard icon={<GitCommit className="text-indigo-500" />} label="变更历史" value={snapshot.stats.revisions} />
        </div>
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* Vector Savings */}
        <div className="lg:col-span-2 bg-white rounded-xl shadow-sm border p-6">
          <div className="flex items-center gap-2 mb-6">
             <Activity className="text-orange-500" size={20} />
             <h3 className="text-lg font-bold">工程效率：增量索引优化</h3>
          </div>
          
          <div className="flex items-end gap-12">
            <div className="flex-1 space-y-4">
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">复用向量（无需重算）</span>
                <span className="font-bold">{savings.reused}</span>
              </div>
              <div className="w-full bg-gray-100 h-3 rounded-full overflow-hidden">
                <div 
                  className="bg-emerald-500 h-full rounded-full transition-all duration-1000" 
                  style={{ width: `${savings.reusedPercent}%` }}
                />
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">重新嵌入（新/改动）</span>
                <span className="font-bold text-orange-600">{savings.embedded}</span>
              </div>
            </div>

            <div className="flex flex-col items-center">
               <div className="text-5xl font-bold text-emerald-600">{savings.reusedPercent}%</div>
               <div className="text-xs text-gray-400 mt-1 uppercase tracking-wider">复用率</div>
            </div>
          </div>

          <div className="mt-8 grid grid-cols-3 gap-4 border-t pt-6">
            <div className="text-center">
              <div className="text-sm text-gray-400 mb-1">当前存量向量</div>
              <div className="text-xl font-bold font-mono">{snapshot.stats.vectors}</div>
            </div>
            <div className="text-center border-x">
              <div className="text-sm text-gray-400 mb-1">历史清理数量</div>
              <div className="text-xl font-bold font-mono text-gray-500">{savings.deleted}</div>
            </div>
            <div className="text-center">
              <div className="text-sm text-gray-400 mb-1">Diff 覆盖场景</div>
              <div className="text-xl font-bold font-mono">{coveredKinds}/{CHANGE_KINDS.length}</div>
            </div>
          </div>
        </div>

        {/* Quality Badges */}
        <div className="bg-white rounded-xl shadow-sm border p-6">
          <div className="flex items-center gap-2 mb-6">
             <CheckCircle2 className="text-emerald-500" size={20} />
             <h3 className="text-lg font-bold">质量门禁</h3>
          </div>
          
          <div className="space-y-4">
            <BadgeItem label="快照确定性" status="pass" info="100% Deterministic" />
            <BadgeItem label="溯源一致性" status="pass" info="Checksum Verified" />
            <BadgeItem label="类型安全" status="pass" info="Schema Sync" />
            <BadgeItem label="前端数据映射" status="pass" info="Vitest" />
            <BadgeItem label="时效检测" status="pending" info="U11 Pending" />
          </div>

          <div className="mt-8 p-4 bg-gray-50 rounded-lg border border-dashed flex items-start gap-3">
            <AlertCircle className="text-gray-400 mt-0.5" size={16} />
            <p className="text-xs text-gray-500 leading-relaxed">
              演示快照通过 <code className="bg-gray-200 px-1 rounded">knowledge demo export --check</code> 在 CI 中强制校验，确保演示效果与核心库逻辑完全同步。
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};

const StatCard: React.FC<{ icon: React.ReactNode; label: string; value: number }> = ({ icon, label, value }) => (
  <div className="flex items-center gap-4 p-4 rounded-xl hover:bg-gray-50 transition-colors border border-transparent hover:border-gray-200">
    <div className="p-3 bg-gray-100 rounded-lg">{icon}</div>
    <div>
      <div className="text-2xl font-bold text-gray-900 font-mono">{value}</div>
      <div className="text-xs text-gray-500 font-medium uppercase tracking-wider">{label}</div>
    </div>
  </div>
);

const BadgeItem: React.FC<{ label: string; status: "pass" | "fail" | "pending"; info: string }> = ({ label, status, info }) => (
  <div className="flex items-center justify-between group">
    <div className="text-sm text-gray-600">{label}</div>
    <div className="flex items-center gap-2">
      <span className="text-[10px] text-gray-400 font-mono opacity-0 group-hover:opacity-100 transition-opacity">
        {info}
      </span>
      <div className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
        status === "pass" ? "bg-emerald-100 text-emerald-700" :
        status === "fail" ? "bg-rose-100 text-rose-700" : "bg-gray-100 text-gray-500"
      }`}>
        {status === "pass" ? "Pass" : status === "fail" ? "Fail" : "Wait"}
      </div>
    </div>
  </div>
);
