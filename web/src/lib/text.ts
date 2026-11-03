/**
 * Character offsets in the snapshot are Python string indices, i.e. Unicode code
 * points. JavaScript strings index UTF-16 code units, which differ as soon as a
 * character lies outside the BMP (emoji, rare CJK). Every slice the site makes
 * goes through these helpers so highlights match `knowledge show` exactly.
 */

/** Split a string into code points (what Python calls characters). */
export function codePoints(text: string): string[] {
  return Array.from(text);
}

/** Number of code points, equal to Python's `len(text)`. */
export function codePointLength(text: string): number {
  let length = 0;
  for (const _ of text) length += 1;
  return length;
}

/** Python-style `text[start:end]` on code points. */
export function sliceCodePoints(text: string | readonly string[], start: number, end: number): string {
  const points = typeof text === "string" ? codePoints(text) : text;
  return points.slice(start, end).join("");
}
