import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { managementClient } from "./client";
import type {
  DocumentDTO,
  DocumentDiffDTO,
  EvidenceDTO,
  JobDTO,
  JobStatus,
  LineageDTO,
  Page,
  RevisionDTO,
} from "./types";

export const MANAGEMENT_PAGE_LIMIT = 20;

type QueryStatus = "loading" | "ready" | "empty" | "error";

export interface ManagementQueryState<T> {
  readonly data: T | null;
  readonly error: unknown;
  readonly refreshing: boolean;
  readonly status: QueryStatus;
  readonly reload: () => void;
}

interface QueryOptions<T> {
  // key 必须编码所有影响请求的参数；inline options/回调的引用变化不是刷新信号。
  readonly key: string;
  readonly load: (signal: AbortSignal) => Promise<T>;
  readonly isEmpty?: (value: T) => boolean;
}

type QuerySnapshot<T> = Omit<ManagementQueryState<T>, "reload"> & { readonly key: string };

export function useManagementQuery<T>({ key, load, isEmpty }: QueryOptions<T>): ManagementQueryState<T> {
  const [reloadToken, setReloadToken] = useState(0);
  const [state, setState] = useState<QuerySnapshot<T>>({
    key,
    data: null,
    error: null,
    refreshing: false,
    status: "loading",
  });
  const optionsRef = useRef({ key, load, isEmpty, reloadToken });
  const requestRef = useRef(0);

  // 只发布已 commit 的回调，避免并发 render 把未提交的 route/loader 泄漏给请求。
  // 同时在 passive effect cleanup 之前就使旧 route/reload 的响应失效。
  useLayoutEffect(() => {
    optionsRef.current = { key, load, isEmpty, reloadToken };
  });

  useEffect(() => {
    const controller = new AbortController();
    const requestId = ++requestRef.current;
    const options = optionsRef.current;
    const isCurrent = () =>
      !controller.signal.aborted &&
      requestRef.current === requestId &&
      optionsRef.current.key === key &&
      optionsRef.current.reloadToken === reloadToken;

    setState((current) => {
      const sameKey = current.key === key;
      return {
        key,
        data: sameKey ? current.data : null,
        error: null,
        refreshing: sameKey && current.data !== null,
        status: sameKey && current.data !== null ? current.status : "loading",
      };
    });

    void (async () => {
      try {
        const value = await options.load(controller.signal);
        if (!isCurrent()) return;
        setState({
          key,
          data: value,
          error: null,
          refreshing: false,
          status: options.isEmpty?.(value) ? "empty" : "ready",
        });
      } catch (error: unknown) {
        if (!isCurrent()) return;
        if (error instanceof Error && error.name === "AbortError") return;
        setState({
          key,
          data: null,
          error,
          refreshing: false,
          status: "error",
        });
      }
    })();

    return () => controller.abort();
  }, [key, reloadToken]);

  const reload = useCallback(() => {
    setReloadToken((value) => value + 1);
  }, []);

  // 新 route 的首个 commit 也不能短暂展示旧 route 的数据/错误。
  if (state.key !== key) {
    return { data: null, error: null, refreshing: false, status: "loading", reload };
  }
  return { data: state.data, error: state.error, refreshing: state.refreshing, status: state.status, reload };
}

export function useJobsPage(status: JobStatus | undefined, offset: number): ManagementQueryState<Page<JobDTO>> {
  const key = useMemo(() => `jobs:${status ?? "all"}:${offset}`, [offset, status]);
  const load = useCallback(
    (signal: AbortSignal) => {
      const query = {
        limit: MANAGEMENT_PAGE_LIMIT,
        offset,
        ...(status ? { status } : {}),
      };
      return managementClient.listJobs(query, signal);
    },
    [offset, status],
  );
  return useManagementQuery({ key, load, isEmpty: (page) => page.items.length === 0 });
}

export function useJobPage(id: string): ManagementQueryState<JobDTO> {
  const key = useMemo(() => `job:${id}`, [id]);
  const load = useCallback((signal: AbortSignal) => managementClient.getJob(id, signal), [id]);
  return useManagementQuery({ key, load });
}

export function useRevisionPage(id: string): ManagementQueryState<RevisionDTO> {
  const key = useMemo(() => `revision:${id}`, [id]);
  const load = useCallback((signal: AbortSignal) => managementClient.getRevision(id, signal), [id]);
  return useManagementQuery({ key, load });
}

export function useDocumentPage(id: string, offset: number): ManagementQueryState<DocumentDTO> {
  const key = useMemo(() => `document:${id}:${offset}`, [id, offset]);
  const load = useCallback(
    (signal: AbortSignal) =>
      managementClient.getDocument(id, { limit: MANAGEMENT_PAGE_LIMIT, offset }, signal),
    [id, offset],
  );
  return useManagementQuery({ key, load });
}

export function useLineagePage(id: string, offset: number): ManagementQueryState<LineageDTO> {
  const key = useMemo(() => `lineage:${id}:${offset}`, [id, offset]);
  const load = useCallback(
    (signal: AbortSignal) => managementClient.getLineage(id, { limit: MANAGEMENT_PAGE_LIMIT, offset }, signal),
    [id, offset],
  );
  return useManagementQuery({ key, load });
}

export function useDocumentDiffPage(
  newId: string,
  oldId: string,
  offset: number,
): ManagementQueryState<DocumentDiffDTO> {
  const key = useMemo(() => `diff:${newId}:${oldId}:${offset}`, [newId, oldId, offset]);
  const load = useCallback(
    (signal: AbortSignal) =>
      managementClient.getDocumentDiff(newId, oldId, { limit: MANAGEMENT_PAGE_LIMIT, offset }, signal),
    [newId, oldId, offset],
  );
  return useManagementQuery({ key, load });
}

export function useEvidencePage(id: string): ManagementQueryState<EvidenceDTO> {
  const key = useMemo(() => `evidence:${id}`, [id]);
  const load = useCallback((signal: AbortSignal) => managementClient.getEvidence(id, signal), [id]);
  return useManagementQuery({ key, load });
}
