// @vitest-environment jsdom

import { act, createElement, StrictMode, useLayoutEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const clientMocks = vi.hoisted(() => ({
  managementClient: {
    listJobs: vi.fn(),
    getJob: vi.fn(),
    getRevision: vi.fn(),
    getDocument: vi.fn(),
    getLineage: vi.fn(),
    getDocumentDiff: vi.fn(),
    getUnit: vi.fn(),
    getEvidence: vi.fn(),
  },
}));

vi.mock("./client", () => ({
  managementClient: clientMocks.managementClient,
}));

import { useJobPage, useJobsPage, useManagementQuery } from "./hooks";
import type { JobDTO, JobStatus } from "./types";

const actEnvironment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
const previousActEnvironment = actEnvironment.IS_REACT_ACT_ENVIRONMENT;
const roots = new Set<Root>();
const containers = new Set<HTMLDivElement>();

beforeAll(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
});
afterAll(() => {
  if (previousActEnvironment === undefined) delete actEnvironment.IS_REACT_ACT_ENVIRONMENT;
  else actEnvironment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
});
afterEach(async () => {
  await act(async () => {
    for (const root of roots) root.unmount();
  });
  roots.clear();
  for (const container of containers) container.remove();
  containers.clear();
  vi.resetAllMocks();
});

const baseJob: JobDTO = {
  id: "11111111-1111-1111-1111-111111111111",
  parent_job_id: null,
  status: "passed",
  config_generation: 1,
  config_fingerprint: "a".repeat(64),
  dataset_id: "22222222-2222-2222-2222-222222222222",
  dataset_version: 1,
  dataset_hash: "b".repeat(64),
  knowledge_fingerprint: "c".repeat(64),
  created_at: "2026-11-13T10:00:00+00:00",
  evaluated_at: "2026-11-13T10:01:00+00:00",
  started_at: "2026-11-13T10:00:05+00:00",
  finished_at: "2026-11-13T10:01:00+00:00",
  attempt_count: 1,
  error_code: null,
  report: null,
  trigger_revision_ids: [],
  links: [],
};

function makeDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function setupDom() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  roots.add(root);
  containers.add(container);
  return { container, root };
}

function Harness(props: { id: string }) {
  const state = useJobPage(props.id);
  return createElement(
    "div",
    {},
    createElement("div", { "data-testid": "status" }, state.status),
    createElement("div", { "data-testid": "job-id" }, state.data?.id ?? "none"),
    createElement("div", { "data-testid": "job-status" }, state.data?.status ?? "none"),
    createElement("div", { "data-testid": "refreshing" }, String(state.refreshing)),
    createElement("button", { "data-testid": "reload", onClick: state.reload, type: "button" }, "reload"),
  );
}

async function flush(action: () => void = () => {}): Promise<void> {
  await act(async () => {
    action();
    await Promise.resolve();
  });
}

function text(container: HTMLElement, testId: string) {
  return container.querySelector(`[data-testid='${testId}']`)?.textContent;
}

async function reload(container: HTMLElement) {
  const button = container.querySelector<HTMLButtonElement>("[data-testid='reload']");
  expect(button).not.toBeNull();
  await flush(() => button!.click());
}

describe("management hooks", () => {
  it("ignores stale responses after route changes and aborts the previous request", async () => {
    const newId = "33333333-3333-3333-3333-333333333333";
    const oldRequest = makeDeferred<JobDTO>();
    const newRequest = makeDeferred<JobDTO>();
    clientMocks.managementClient.getJob.mockImplementation((id: string) =>
      id === baseJob.id ? oldRequest.promise : newRequest.promise,
    );
    const { container, root } = setupDom();
    await flush(() => root.render(createElement(Harness, { id: baseJob.id })));
    await flush(() => root.render(createElement(Harness, { id: newId })));
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(2);
    expect(clientMocks.managementClient.getJob.mock.calls[0]![1].aborted).toBe(true);
    await flush(() => oldRequest.resolve({ ...baseJob, status: "failed" }));
    expect(text(container, "job-id")).toBe("none");
    expect(text(container, "status")).toBe("loading");
    await flush(() => newRequest.resolve({ ...baseJob, id: newId }));
    expect(text(container, "status")).toBe("ready");
    expect(text(container, "job-id")).toBe(newId);
  });

  it("ignores an old route rejection after the new route is ready", async () => {
    const oldRequest = makeDeferred<JobDTO>();
    const newId = "33333333-3333-3333-3333-333333333333";
    clientMocks.managementClient.getJob.mockReturnValueOnce(oldRequest.promise).mockResolvedValueOnce({ ...baseJob, id: newId });
    const { container, root } = setupDom();
    await flush(() => root.render(createElement(Harness, { id: baseJob.id })));
    await flush(() => root.render(createElement(Harness, { id: newId })));
    await flush(() => oldRequest.reject(new Error("stale route failure")));
    expect(text(container, "job-id")).toBe(newId);
    expect(text(container, "status")).toBe("ready");
  });

  it.each(["older-first", "newer-first"])(
    "keeps only the latest reload result when refreshes race (%s)",
    async (order) => {
      const second = makeDeferred<JobDTO>();
      const third = makeDeferred<JobDTO>();
      const latestId = "44444444-4444-4444-4444-444444444444";
      clientMocks.managementClient.getJob
        .mockResolvedValueOnce({ ...baseJob, status: "queued" })
        .mockReturnValueOnce(second.promise)
        .mockReturnValueOnce(third.promise);
      const { container, root } = setupDom();
      await flush(() => root.render(createElement(Harness, { id: baseJob.id })));
      // 两次真实用户 click 对应两个 commit，不能放在同一个 act 中被批处理。
      await reload(container);
      expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(2);
      await reload(container);
      expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(3);
      expect(clientMocks.managementClient.getJob.mock.calls[1]![1].aborted).toBe(true);
      expect(text(container, "refreshing")).toBe("true");
      expect(text(container, "job-status")).toBe("queued");
      if (order === "older-first") {
        await flush(() => second.resolve({ ...baseJob, status: "failed" }));
        expect(text(container, "job-status")).toBe("queued");
        expect(text(container, "refreshing")).toBe("true");
      }
      await flush(() => third.resolve({ ...baseJob, id: latestId }));
      if (order === "newer-first") {
        await flush(() => second.resolve({ ...baseJob, status: "failed" }));
      }
      expect(text(container, "job-id")).toBe(latestId);
      expect(text(container, "job-status")).toBe("passed");
      expect(text(container, "status")).toBe("ready");
      expect(text(container, "refreshing")).toBe("false");
      expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(3);
    },
  );

  it("coalesces reloads within one commit instead of inventing an extra request", async () => {
    clientMocks.managementClient.getJob.mockResolvedValue(baseJob);
    const { container, root } = setupDom();
    await flush(() => root.render(createElement(Harness, { id: baseJob.id })));
    await flush(() => {
      const button = container.querySelector<HTMLButtonElement>("button")!;
      button.click();
      button.click();
    });
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(2);
    expect(text(container, "status")).toBe("ready");
  });

  it.each(["loading", "ready", "empty", "error"] as const)(
    "does not loop with inline options, loader, params and isEmpty in %s state",
    async (outcome) => {
      const request = makeDeferred<number[]>();
      const loader = vi.fn((_params: { offset: number }, _signal: AbortSignal) => request.promise);
      let renders = 0;
      function InlineHarness() {
        // 回归时快速失败而非让 worker 继续循环直至 OOM；不限制生产 hook。
        if (++renders > 20) throw new Error("inline query render loop");
        const params = { offset: 0 };
        const state = useManagementQuery({
          key: `inline:${params.offset}`,
          load: (signal) => loader(params, signal),
          isEmpty: (items) => items.length === 0,
        });
        return createElement("div", {}, state.status);
      }
      const { container, root } = setupDom();
      await flush(() => root.render(createElement(InlineHarness)));
      if (outcome === "ready") await flush(() => request.resolve([1]));
      if (outcome === "empty") await flush(() => request.resolve([]));
      if (outcome === "error") await flush(() => request.reject(new Error("unavailable")));
      await flush(() => root.render(createElement(InlineHarness)));
      expect(container.textContent).toBe(outcome);
      expect(loader).toHaveBeenCalledTimes(1);
      expect(loader.mock.calls[0]![1].aborted).toBe(false);
    },
  );

  it("uses the latest committed inline loader and classifier on reload without refetching on reference changes", async () => {
    const loader = vi.fn((value: number) => Promise.resolve(value));
    function InlineHarness({ value }: { value: number }) {
      const state = useManagementQuery({ key: "same", load: () => loader(value), isEmpty: () => value === 2 });
      return createElement("button", { onClick: state.reload }, `${state.status}:${state.data}`);
    }
    const { container, root } = setupDom();
    await flush(() => root.render(createElement(InlineHarness, { value: 1 })));
    await flush(() => root.render(createElement(InlineHarness, { value: 2 })));
    expect(loader).toHaveBeenCalledTimes(1);
    expect(container.textContent).toBe("ready:1");
    await flush(() => container.querySelector<HTMLButtonElement>("button")!.click());
    expect(loader).toHaveBeenCalledTimes(2);
    expect(loader).toHaveBeenLastCalledWith(2);
    expect(container.textContent).toBe("empty:2");
  });

  it.each(["data", "error"] as const)("hides old route %s in the new route's first commit", async (outcome) => {
    const pending = makeDeferred<number>();
    const loader = vi.fn((key: string) => {
      if (key === "new") return pending.promise;
      return outcome === "data" ? Promise.resolve(1) : Promise.reject(new Error("old error"));
    });
    const commits: Array<{ key: string; status: string; data: number | null; error: unknown }> = [];
    function InlineHarness({ queryKey }: { queryKey: string }) {
      const state = useManagementQuery({ key: queryKey, load: () => loader(queryKey) });
      useLayoutEffect(() => {
        commits.push({ key: queryKey, status: state.status, data: state.data, error: state.error });
      });
      return null;
    }
    const { root } = setupDom();
    await flush(() => root.render(createElement(InlineHarness, { queryKey: "old" })));
    expect(commits.at(-1)?.status).toBe(outcome === "data" ? "ready" : "error");
    await flush(() => root.render(createElement(InlineHarness, { queryKey: "new" })));
    const firstNewCommit = commits.find((commit) => commit.key === "new");
    expect(firstNewCommit).toEqual({ key: "new", status: "loading", data: null, error: null });
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("aborts on unmount and ignores a response from a loader that does not honor abort", async () => {
    const pending = makeDeferred<JobDTO>();
    clientMocks.managementClient.getJob.mockReturnValue(pending.promise);
    const { container, root } = setupDom();
    await flush(() => root.render(createElement(Harness, { id: baseJob.id })));
    await flush(() => root.unmount());
    roots.delete(root);
    expect(clientMocks.managementClient.getJob.mock.calls[0]![1].aborted).toBe(true);
    await flush(() => pending.resolve(baseJob));
    expect(container.textContent).toBe("");
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(1);
  });

  it("survives StrictMode effect replay without allowing the replayed request to win", async () => {
    const oldRequest = makeDeferred<JobDTO>();
    clientMocks.managementClient.getJob.mockReturnValueOnce(oldRequest.promise).mockResolvedValueOnce(baseJob);
    const { container, root } = setupDom();
    await flush(() => root.render(createElement(StrictMode, {}, createElement(Harness, { id: baseJob.id }))));
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(2);
    expect(clientMocks.managementClient.getJob.mock.calls[0]![1].aborted).toBe(true);
    await flush(() => oldRequest.resolve({ ...baseJob, status: "failed" }));
    expect(text(container, "job-status")).toBe("passed");
    expect(text(container, "refreshing")).toBe("false");
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(2);
  });

  it("fetches jobs once per semantic filter/page key despite new params and classifier objects", async () => {
    clientMocks.managementClient.listJobs.mockImplementation((params: { offset: number }) =>
      Promise.resolve({ items: [], total: 0, limit: 20, offset: params.offset }),
    );
    let renders = 0;
    function JobsHarness({ status, offset }: { status?: JobStatus; offset: number }) {
      if (++renders > 20) throw new Error("jobs query render loop");
      const state = useJobsPage(status, offset);
      return createElement("div", {}, `${state.status}:${state.data?.offset}`);
    }
    const { container, root } = setupDom();
    await flush(() => root.render(createElement(JobsHarness, { offset: 0 })));
    await flush(() => root.render(createElement(JobsHarness, { offset: 0 })));
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(1);
    await flush(() => root.render(createElement(JobsHarness, { status: "failed", offset: 20 })));
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(2);
    expect(clientMocks.managementClient.listJobs.mock.calls[1]![0]).toEqual({ limit: 20, offset: 20, status: "failed" });
    expect(clientMocks.managementClient.listJobs.mock.calls[0]![1].aborted).toBe(true);
    expect(container.textContent).toBe("empty:20");
  });
});
