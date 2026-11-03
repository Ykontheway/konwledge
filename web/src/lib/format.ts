/**
 * Display helpers. Timestamps are shown in the offset they were recorded with
 * (the story uses +08:00), never converted to the viewer's zone, so every
 * visitor reads the same times as the CLI prints.
 */

const ISO = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$/;

export interface WallTime {
  year: number;
  month: number;
  day: number;
  hour: string;
  minute: string;
  offset: string;
}

export function wallTime(iso: string): WallTime {
  const match = ISO.exec(iso);
  if (!match) throw new Error(`not an ISO-8601 timestamp: ${iso}`);
  const [, year, month, day, hour, minute, , offset] = match;
  return {
    year: Number(year),
    month: Number(month),
    day: Number(day),
    hour: hour ?? "00",
    minute: minute ?? "00",
    offset: offset === undefined || offset === "Z" ? "UTC" : `UTC${offset}`,
  };
}

/** `10月5日 10:00` */
export function formatMoment(iso: string): string {
  const time = wallTime(iso);
  return `${time.month}月${time.day}日 ${time.hour}:${time.minute}`;
}

/** `2026-10-05` */
export function formatDate(iso: string): string {
  return iso.slice(0, 10);
}

/** `2026年10月20日 11:00 (UTC+08:00)` */
export function formatFull(iso: string): string {
  const time = wallTime(iso);
  return `${time.year}年${time.month}月${time.day}日 ${time.hour}:${time.minute}（${time.offset}）`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function percent(part: number, whole: number): number {
  return whole === 0 ? 0 : Math.round((part / whole) * 100);
}
