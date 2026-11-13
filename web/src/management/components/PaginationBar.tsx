import type { Page } from "../types";

interface PaginationBarProps<T> {
  readonly page: Page<T>;
  readonly onPageChange: (offset: number) => void;
}

export const PaginationBar = <T,>({ page, onPageChange }: PaginationBarProps<T>) => {
  const start = page.total === 0 ? 0 : page.offset + 1;
  const end = Math.min(page.offset + page.items.length, page.total);
  const previousOffset = Math.max(page.offset - page.limit, 0);
  const nextOffset = page.offset + page.limit;
  const canGoPrevious = page.offset > 0;
  const canGoNext = nextOffset < page.total;

  return (
    <div className="flex flex-col gap-3 border-t border-slate-200 pt-4 sm:flex-row sm:items-center sm:justify-between">
      <p className="text-sm text-slate-600">
        当前显示 <span className="font-semibold text-slate-900">{start}</span>
        {" – "}
        <span className="font-semibold text-slate-900">{end}</span>
        {" / "}
        <span className="font-semibold text-slate-900">{page.total}</span>
      </p>
      <div className="flex items-center gap-2">
        <button
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 transition hover:border-[#d39b3b] hover:text-[#1f2a44] disabled:cursor-not-allowed disabled:opacity-50"
          disabled={!canGoPrevious}
          onClick={() => onPageChange(previousOffset)}
          type="button"
        >
          上一页
        </button>
        <button
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 transition hover:border-[#d39b3b] hover:text-[#1f2a44] disabled:cursor-not-allowed disabled:opacity-50"
          disabled={!canGoNext}
          onClick={() => onPageChange(nextOffset)}
          type="button"
        >
          下一页
        </button>
      </div>
    </div>
  );
};
