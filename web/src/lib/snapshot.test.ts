import { describe, it, expect } from "vitest";
import { snapshot } from "../data";
import { segmentDocument, unitRangeText, SnapshotIndex } from "./snapshot";
import { codePoints } from "./text";

describe("Snapshot Data Mapping", () => {
  it("should load the snapshot successfully", () => {
    expect(snapshot.title).toBeDefined();
    expect(snapshot.sources.length).toBeGreaterThan(0);
  });

  it("should index all units and documents", () => {
    const localIndex = new SnapshotIndex(snapshot);
    const firstSource = snapshot.sources[0]!;
    const firstDoc = firstSource.documents[0]!;
    const firstUnit = firstDoc.units[0]!;

    expect(localIndex.unit(firstUnit.id)).toBeDefined();
    expect(localIndex.document(firstDoc.id)).toBeDefined();
    expect(localIndex.source(firstSource.id)).toBeDefined();
    expect(localIndex.lineage(firstUnit.lineage_id)).toBeDefined();
  });

  it("should correctly segment a document and match unit text", () => {
    for (const source of snapshot.sources) {
      for (const document of source.documents) {
        if (document.content === null) continue;
        
        const segments = segmentDocument(document.content, document.units);
        
        // 1. Concatenation should reproduce original
        const reproduced = segments.map(s => s.text).join("");
        expect(reproduced).toBe(document.content);
        
        // 2. Each unit segment should match the unit's stored text exactly
        for (const unit of document.units) {
          const rangeText = unitRangeText(document.content, unit);
          expect(rangeText).toBe(unit.text);
          
          const segment = segments.find(s => s.unitId === unit.id);
          expect(segment).toBeDefined();
          expect(segment!.text).toBe(unit.text);
        }
      }
    }
  });

  it("should handle Unicode code points correctly (Emoji test)", () => {
    const text = "Hello 🚀 World"; // Rocket is surrogate pair in UTF-16
    const units = [
      { id: "1", start_char: 6, end_char: 7, text: "🚀", ordinal: 0, section_path: [], content_hash: "", status: "active" as const }
    ] as any;
    
    const segments = segmentDocument(text, units);
    expect(segments.length).toBe(3);
    expect(segments[0]!.text).toBe("Hello ");
    expect(segments[1]!.text).toBe("🚀");
    expect(segments[1]!.unitId).toBe("1");
    expect(segments[2]!.text).toBe(" World");
    
    expect(codePoints(text).length).toBe(13); // Python length
    expect(text.length).toBe(14); // JS length (surrogate pair)
  });
});
