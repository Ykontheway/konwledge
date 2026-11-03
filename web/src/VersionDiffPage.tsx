import React, { useState } from "react";
import { snapshot, index } from "./data";
import { 
  diffRows, 
  sourceTitle, 
  allDiffs,
  type UnitChangeKind
} from "./lib/snapshot";
import { 
  GitPullRequest, 
  ArrowRight, 
  Zap, 
  ArrowUpRight,
  MoveRight,
  Plus,
  Minus,
  RefreshCw,
  MinusCircle,
  CheckCircle2
} from "lucide-react";
import { formatMoment, shortId, percent } from "./lib/format";

export const VersionDiffPage: React.FC = () => {
  const diffs = allDiffs(snapshot);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const item = diffs[selectedIndex];

  if (!item) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400 italic">
        暂无版本变更记录。
      </div>
    );
  }

  const { diff } = item;
  const rows = diffRows(index, diff);

  return (
    <div className="h-full flex flex-col gap-6 animate-in fade-in duration-500">
      {/* Selector bar */}
      <div className="bg-white p-4 rounded-xl border shadow-sm flex items-center justify-between">
        <div className="flex items-center gap-6">
          <div className="flex flex-col">
            <label className="text-[10px] font-bold text-gray-400 uppercase tracking-tighter mb-1">选择变更事件</label>
            <select 
              className="text-sm font-medium bg-transparent border-none p-0 focus:ring-0 cursor-pointer max-w-md"
              value={selectedIndex}
              onChange={(e) => setSelectedIndex(Number(e.target.value))}
            >
              {diffs.map((d, i) => (
                <option key={i} value={i}>
                  [{sourceTitle(d.source)}] v{d.diff.from_version} → v{d.diff.to_version} ({formatMoment(d.diff.published_at)})
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex items-center gap-6">
          <div className="text-right">
             <div className="text-[10px] font-bold text-gray-400 uppercase tracking-tighter mb-1">向量重算率</div>
             <div className="flex items-center gap-2">
                <div className="text-sm font-bold text-gray-900">{percent(diff.embedding.embedded, diff.embedding.embedded + diff.embedding.reused)}%</div>
                <div className="w-20 bg-gray-100 h-1.5 rounded-full overflow-hidden">
                   <div 
                     className="bg-orange-500 h-full transition-all duration-700"
                     style={{ width: `${percent(diff.embedding.embedded, diff.embedding.embedded + diff.embedding.reused)}%` }}
                   />
                </div>
             </div>
          </div>
          <div className="flex items-center gap-2 px-3 py-1.5 bg-emerald-50 text-emerald-700 rounded-lg border border-emerald-100 text-xs font-bold">
            <Zap size={14} />
            <span>增量更新已开启</span>
          </div>
        </div>
      </div>

      <div className="flex-1 flex flex-col bg-white rounded-xl border shadow-sm overflow-hidden">
        <div className="p-4 border-b bg-gray-50 flex items-center justify-between">
          <div className="flex items-center gap-4">
             <div className="flex items-center gap-2">
               <GitPullRequest size={16} className="text-gray-400" />
               <span className="text-xs font-bold text-gray-500 uppercase tracking-widest">单元级 Diff 详情</span>
             </div>
             <div className="text-xs text-gray-400 font-medium italic">
               {diff.summary}
             </div>
          </div>
          
          <div className="flex gap-4">
            <ChangeBadge kind="added" count={diff.counts.added || 0} />
            <ChangeBadge kind="modified" count={diff.counts.modified || 0} />
            <ChangeBadge kind="moved" count={diff.counts.moved || 0} />
            <ChangeBadge kind="removed" count={diff.counts.removed || 0} />
            <ChangeBadge kind="unchanged" count={diff.counts.unchanged || 0} />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          <table className="w-full text-left border-collapse">
            <thead className="sticky top-0 bg-white shadow-sm z-10">
              <tr className="text-[10px] font-bold text-gray-400 uppercase tracking-widest border-b">
                <th className="px-6 py-3 w-48">片段 Lineage</th>
                <th className="px-6 py-3 w-24 text-center">变更类型</th>
                <th className="px-6 py-3">v{diff.from_version} (旧)</th>
                <th className="px-6 py-3 w-8"><ArrowRight size={14} /></th>
                <th className="px-6 py-3">v{diff.to_version} (新)</th>
                <th className="px-6 py-3 w-32 text-right text-gray-300">相似度 / 向量</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {rows.map((row, i) => (
                <tr key={i} className={`group hover:bg-gray-50 transition-colors ${
                  row.kind === 'unchanged' ? 'opacity-60 grayscale-[0.5]' : ''
                }`}>
                  <td className="px-6 py-4">
                    <div className="text-xs font-bold text-gray-900 truncate max-w-[180px]">{row.label}</div>
                    <div className="text-[9px] font-mono text-gray-400 mt-0.5">{shortId(row.change.lineage_id)}</div>
                  </td>
                  <td className="px-6 py-4 text-center">
                    <KindIcon kind={row.kind} />
                  </td>
                  <td className="px-6 py-4">
                    {row.oldUnit ? (
                      <div className="text-[11px] code-font text-gray-500 line-clamp-2 bg-gray-50 p-2 rounded border border-transparent group-hover:border-gray-100">
                        {row.oldUnit.text}
                      </div>
                    ) : (
                      <div className="text-[10px] text-gray-300 italic">（不存在）</div>
                    )}
                  </td>
                  <td className="px-6 py-4 text-gray-300">
                    <MoveRight size={14} />
                  </td>
                  <td className="px-6 py-4">
                    {row.newUnit ? (
                      <div className={`text-[11px] code-font line-clamp-2 p-2 rounded border transition-all ${
                        row.kind === 'added' ? 'bg-emerald-50 text-emerald-800 border-emerald-100' :
                        row.kind === 'modified' ? 'bg-amber-50 text-amber-800 border-amber-100' :
                        row.kind === 'moved' ? 'bg-blue-50 text-blue-800 border-blue-100' :
                        'bg-gray-50 text-gray-900 border-transparent group-hover:border-gray-100'
                      }`}>
                        {row.newUnit.text}
                      </div>
                    ) : (
                      <div className="text-[10px] text-gray-300 italic line-through">（已移除）</div>
                    )}
                  </td>
                  <td className="px-6 py-4 text-right">
                     <div className="text-[10px] font-mono font-bold text-gray-400 mb-1">
                       {row.change.similarity > 0 ? `${(row.change.similarity * 100).toFixed(0)}% Match` : '-'}
                     </div>
                     <div className={`text-[9px] font-bold px-1.5 py-0.5 rounded inline-block uppercase tracking-tighter ${
                       row.change.needs_embedding ? 'bg-orange-100 text-orange-700' : 'bg-emerald-100 text-emerald-700'
                     }`}>
                       {row.change.needs_embedding ? 'Re-embed' : 'Reused'}
                     </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="p-4 bg-[#1f2a44] text-[#f4efe6] flex items-center justify-between text-xs font-mono">
           <div className="flex gap-6">
              <div className="flex items-center gap-2">
                 <ArrowUpRight size={14} className="text-orange-400" />
                 <span>EMBEDDED: <span className="text-white">{diff.embedding.embedded}</span></span>
              </div>
              <div className="flex items-center gap-2">
                 <RefreshCw size={14} className="text-emerald-400" />
                 <span>REUSED: <span className="text-white">{diff.embedding.reused}</span></span>
              </div>
              <div className="flex items-center gap-2">
                 <MinusCircle size={14} className="text-rose-400" />
                 <span>DELETED: <span className="text-white">{diff.embedding.deleted}</span></span>
              </div>
           </div>
           <div className="text-[#a0aec0] opacity-50">
             Knowledge Library Diff Engine v1.0
           </div>
        </div>
      </div>
    </div>
  );
};

const ChangeBadge: React.FC<{ kind: UnitChangeKind; count: number }> = ({ kind, count }) => {
  if (count === 0) return null;
  const styles = {
    added: "bg-emerald-100 text-emerald-700 border-emerald-200",
    modified: "bg-amber-100 text-amber-700 border-amber-200",
    moved: "bg-blue-100 text-blue-700 border-blue-200",
    unchanged: "bg-gray-100 text-gray-500 border-gray-200",
    removed: "bg-rose-100 text-rose-700 border-rose-200"
  };
  return (
    <div className={`px-2 py-0.5 rounded border text-[10px] font-bold uppercase tracking-wider ${styles[kind]}`}>
      {kind}: {count}
    </div>
  );
};

const KindIcon: React.FC<{ kind: UnitChangeKind }> = ({ kind }) => {
  switch (kind) {
    case 'added': return <Plus size={16} className="text-emerald-500 mx-auto" />;
    case 'modified': return <RefreshCw size={16} className="text-amber-500 mx-auto" />;
    case 'moved': return <MoveRight size={16} className="text-blue-500 mx-auto" />;
    case 'removed': return <Minus size={16} className="text-rose-500 mx-auto" />;
    default: return <CheckCircle2 size={16} className="text-gray-300 mx-auto" />;
  }
};
