import React, { useState, useMemo } from "react";
import { index } from "./data";
import { 
  orderedDocuments, 
  sourceTitle, 
  versionLabel, 
  segmentDocument,
  cliShowLines
} from "./lib/snapshot";
import { FileSearch, Terminal, Hash, MapPin } from "lucide-react";

export const TraceabilityPage: React.FC = () => {
  const sources = index.snapshot.sources;
  const [selectedSourceId, setSelectedSourceId] = useState(sources[0]?.id);
  const source = sources.find(s => s.id === selectedSourceId);
  
  const [selectedDocId, setSelectedDocId] = useState<string | undefined>(
    source ? orderedDocuments(source)[source.documents.length - 1]?.id : undefined
  );
  
  // Update doc selection when source changes
  const handleSourceChange = (id: string) => {
    setSelectedSourceId(id);
    const newSource = sources.find(s => s.id === id);
    if (newSource) {
      setSelectedDocId(orderedDocuments(newSource)[newSource.documents.length - 1]?.id);
    }
  };

  const document = source?.documents.find(d => d.id === selectedDocId);
  const [hoveredUnitId, setHoveredUnitId] = useState<string | null>(null);
  const [activeUnitId, setActiveUnitId] = useState<string | null>(null);

  const segments = useMemo(() => {
    if (!document?.content) return [];
    return segmentDocument(document.content, document.units);
  }, [document]);

  const activeUnitRef = index.unit(activeUnitId);

  return (
    <div className="h-full flex flex-col gap-6 animate-in fade-in duration-500">
      {/* Selector bar */}
      <div className="bg-white p-4 rounded-xl border shadow-sm flex items-center justify-between">
        <div className="flex items-center gap-6">
          <div className="flex flex-col">
            <label className="text-[10px] font-bold text-gray-400 uppercase tracking-tighter mb-1">数据源</label>
            <select 
              className="text-sm font-medium bg-transparent border-none p-0 focus:ring-0 cursor-pointer"
              value={selectedSourceId}
              onChange={(e) => handleSourceChange(e.target.value)}
            >
              {sources.map(s => (
                <option key={s.id} value={s.id}>{sourceTitle(s)}</option>
              ))}
            </select>
          </div>
          
          {source && (
            <div className="flex flex-col border-l pl-6">
              <label className="text-[10px] font-bold text-gray-400 uppercase tracking-tighter mb-1">版本记录</label>
              <div className="flex gap-2">
                {orderedDocuments(source).map(d => (
                  <button
                    key={d.id}
                    onClick={() => setSelectedDocId(d.id)}
                    className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                      selectedDocId === d.id 
                        ? "bg-[#1f2a44] text-[#f4efe6]" 
                        : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                    }`}
                  >
                    {versionLabel(d)}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center gap-3 text-xs text-gray-400 bg-gray-50 px-3 py-1.5 rounded-lg border border-dashed">
          <FileSearch size={14} />
          <span>点击片段查看 CLI 溯源详情</span>
        </div>
      </div>

      <div className="flex-1 flex gap-6 overflow-hidden">
        {/* Document Content */}
        <div className="flex-1 bg-white rounded-xl border shadow-sm flex flex-col overflow-hidden">
          <div className="p-4 border-b bg-gray-50 flex items-center justify-between">
             <div className="flex items-center gap-2">
               <Hash size={16} className="text-gray-400" />
               <span className="text-xs font-mono font-bold text-gray-500 uppercase tracking-widest">原文内容</span>
             </div>
             {document && (
               <div className="text-[10px] font-mono text-gray-400">
                 Ingested: {document.ingested_at.slice(0, 16).replace('T', ' ')}
               </div>
             )}
          </div>
          <div className="flex-1 overflow-y-auto p-8 code-font text-sm leading-relaxed whitespace-pre-wrap select-text">
            {segments.length > 0 ? (
              segments.map((seg, i) => {
                const isUnit = !!seg.unitId;
                const isHovered = seg.unitId === hoveredUnitId;
                const isActive = seg.unitId === activeUnitId;
                
                return (
                  <span
                    key={i}
                    onMouseEnter={() => isUnit && setHoveredUnitId(seg.unitId)}
                    onMouseLeave={() => isUnit && setHoveredUnitId(null)}
                    onClick={() => isUnit && setActiveUnitId(seg.unitId)}
                    className={`transition-all ${
                      isUnit 
                        ? "cursor-pointer rounded-sm px-0.5 -mx-0.5 " + (
                            isActive ? "bg-amber-200 ring-2 ring-amber-400 z-10" :
                            isHovered ? "bg-amber-100 ring-1 ring-amber-200" : "bg-amber-50"
                          )
                        : "text-gray-400 opacity-50 italic"
                    }`}
                  >
                    {seg.text}
                  </span>
                );
              })
            ) : (
              <div className="h-full flex items-center justify-center text-gray-400 italic">
                {document?.content === null ? "原文内容过大或非文本格式，仅显示知识片段。" : "暂无内容"}
              </div>
            )}
          </div>
        </div>

        {/* Traceability Panel */}
        <div className="w-96 flex flex-col gap-6">
          <div className="bg-white rounded-xl border shadow-sm p-5 space-y-4">
             <h3 className="text-sm font-bold flex items-center gap-2 border-b pb-3">
               <MapPin size={16} className="text-orange-500" />
               <span>溯源元数据</span>
             </h3>
             
             {activeUnitRef ? (
               <div className="space-y-5 animate-in slide-in-from-right-4 duration-300">
                  <div className="space-y-1">
                    <div className="text-[10px] text-gray-400 uppercase font-bold tracking-wider">章节路径</div>
                    <div className="text-sm font-medium text-gray-900">
                      {activeUnitRef.unit.section_path.map((p, i) => (
                        <span key={i}>
                          {i > 0 && <span className="mx-1 text-gray-300">/</span>}
                          {p}
                        </span>
                      ))}
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-1">
                      <div className="text-[10px] text-gray-400 uppercase font-bold tracking-wider">Lineage ID</div>
                      <div className="text-xs font-mono bg-gray-100 px-1.5 py-0.5 rounded truncate" title={activeUnitRef.unit.lineage_id}>
                        {activeUnitRef.unit.lineage_id.slice(0, 8)}...
                      </div>
                    </div>
                    <div className="space-y-1">
                      <div className="text-[10px] text-gray-400 uppercase font-bold tracking-wider">当前状态</div>
                      <div className={`text-xs px-2 py-0.5 rounded-full inline-block font-bold uppercase ${
                        activeUnitRef.unit.status === 'active' ? 'bg-emerald-100 text-emerald-700' :
                        activeUnitRef.unit.status === 'stale' ? 'bg-amber-100 text-amber-700' :
                        'bg-gray-100 text-gray-500'
                      }`}>
                        {activeUnitRef.unit.status || 'unpublished'}
                      </div>
                    </div>
                  </div>

                  <div className="space-y-2">
                    <div className="text-[10px] text-gray-400 uppercase font-bold tracking-wider">校验和 (SHA-256)</div>
                    <div className="text-[10px] font-mono break-all bg-gray-50 p-2 rounded border border-gray-100">
                      {activeUnitRef.unit.content_hash}
                    </div>
                  </div>
               </div>
             ) : (
               <div className="py-12 text-center text-gray-400 flex flex-col items-center gap-3">
                 <Terminal size={32} className="opacity-20" />
                 <p className="text-xs italic">在左侧点击黄色高亮的知识片段<br/>查看其端到端溯源信息</p>
               </div>
             )}
          </div>

          <div className="flex-1 bg-[#1f2a44] rounded-xl shadow-lg border border-[#2d3b5a] p-5 overflow-hidden flex flex-col">
            <h3 className="text-sm font-bold text-[#f4efe6] flex items-center gap-2 border-b border-[#2d3b5a] pb-3 mb-4">
               <Terminal size={16} className="text-emerald-400" />
               <span className="font-mono">CLI Inspector</span>
            </h3>
            
            <div className="flex-1 overflow-y-auto font-mono text-[11px] leading-relaxed text-[#a0aec0] space-y-1">
               {activeUnitRef ? (
                 <>
                   <div className="text-emerald-500">$ knowledge show {activeUnitRef.unit.id.slice(0, 8)}</div>
                   {cliShowLines(activeUnitRef).map((line, i) => (
                     <div key={i} className="whitespace-pre-wrap">{line}</div>
                   ))}
                   <div className="mt-4 pt-4 border-t border-[#2d3b5a] text-xs font-bold text-[#f4efe6] uppercase tracking-widest">
                     Text Preview
                   </div>
                   <div className="text-[#f4efe6] italic whitespace-pre-wrap opacity-80 mt-2">
                     {activeUnitRef.unit.text}
                   </div>
                 </>
               ) : (
                 <div className="text-[#4a5568] animate-pulse">Waiting for interaction...</div>
               )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
