// @vitest-environment jsdom

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

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

vi.mock("./management/client", () => ({
  managementClient: clientMocks.managementClient,
}));

import App from "./App";
import { ManagementError, type JobDTO } from "./management/types";

const actEnvironment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
const previousActEnvironment = actEnvironment.IS_REACT_ACT_ENVIRONMENT;
const roots = new Set<Root>();

beforeAll(() => {
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;
});
afterAll(() => {
  if (previousActEnvironment === undefined) delete actEnvironment.IS_REACT_ACT_ENVIRONMENT;
  else actEnvironment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
});
beforeEach(() => {
  window.history.replaceState(null, "", "#overview");
});
afterEach(async () => {
  // 即使断言失败，也先卸载以移除 hash listener/中止请求，再清 DOM 和 mock。
  await act(async () => {
    for (const root of roots) root.unmount();
  });
  roots.clear();
  document.body.innerHTML = "";
  window.history.replaceState(null, "", "#overview");
  vi.resetAllMocks();
});

const jobId = "11111111-1111-1111-1111-111111111111";
const revisionId = "22222222-2222-2222-2222-222222222222";
const documentId = "33333333-3333-3333-3333-333333333333";
const oldDocumentId = "44444444-4444-4444-4444-444444444444";
const lineageId = "55555555-5555-5555-5555-555555555555";
const unitId = "66666666-6666-6666-6666-666666666666";
const hashA = "a".repeat(64);
const hashB = "b".repeat(64);
const hashC = "c".repeat(64);
const date = "2026-11-13T10:00:00+00:00";

// 测试 fixtures 仅供 tests 使用，不参与运行时 fallback。
const jobListPayload = {
  items: [
    {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      parent_job_id: null,
      status: "passed",
      config_generation: 1,
      config_fingerprint: hashA,
      dataset_id: lineageId,
      dataset_version: 1,
      dataset_hash: hashB,
      knowledge_fingerprint: hashC,
      created_at: date,
      evaluated_at: date,
      started_at: date,
      finished_at: date,
      attempt_count: 1,
      error_code: null,
      report: null,
      trigger_revision_ids: [],
      links: [],
    },
    {
      id: jobId,
      parent_job_id: null,
      status: "failed",
      config_generation: 2,
      config_fingerprint: hashA,
      dataset_id: lineageId,
      dataset_version: 2,
      dataset_hash: hashB,
      knowledge_fingerprint: hashC,
      created_at: date,
      evaluated_at: date,
      started_at: date,
      finished_at: date,
      attempt_count: 1,
      error_code: "QUALITY_GATE_FAILED",
      report: null,
      trigger_revision_ids: [revisionId],
      links: [],
    },
    {
      id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      parent_job_id: null,
      status: "passed",
      config_generation: 3,
      config_fingerprint: hashA,
      dataset_id: lineageId,
      dataset_version: 3,
      dataset_hash: hashB,
      knowledge_fingerprint: hashC,
      created_at: date,
      evaluated_at: date,
      started_at: date,
      finished_at: date,
      attempt_count: 1,
      error_code: null,
      report: null,
      trigger_revision_ids: [],
      links: [],
    },
  ],
  total: 3,
  limit: 20,
  offset: 0,
} as const;

const detailedJob: JobDTO = {
  ...jobListPayload.items[1],
  report: {
    passed: false,
    metrics: {
      case_count: 4,
      answerable_count: 3,
      no_answer_count: 1,
      mean_recall: 0.666667,
      mean_precision: null,
      mean_reciprocal_rank: 0.75,
      mean_ndcg: null,
      hit_rate: 1,
      no_answer_accuracy: null,
    },
    citation_coverage: null,
    stale_hits: 0,
    degraded: true,
    checks: [
      { name: "recall_at_k", passed: false, actual: 0.666667, threshold: ">=0.8" },
      { name: "regression_gate", passed: false, actual: null, threshold: null },
    ],
  },
};

const revisionPayload = {
  id: revisionId,
  entity_type: "knowledge_unit",
  entity_id: lineageId,
  revision: 7,
  change_type: "update",
  created_at: date,
  navigation: {
    event: "modified",
    document_id: documentId,
    previous_document_id: oldDocumentId,
    unit_id: unitId,
    previous_unit_id: "77777777-7777-7777-7777-777777777777",
    document_checksum: hashB,
    content_hash: hashA,
    previous_content_hash: hashC,
    reverted: false,
  },
  affected_document_ids: [oldDocumentId, documentId],
  affected_unit_ids: [unitId],
  affected_lineage_ids: [lineageId],
  links: [],
};

const diffPayload = {
  source_id: lineageId,
  old_document_id: oldDocumentId,
  new_document_id: documentId,
  counts: { added: 0, modified: 1, moved: 0, unchanged: 4, removed: 0 },
  changes: {
    items: [
      {
        kind: "modified",
        lineage_id: lineageId,
        old: {
          id: "77777777-7777-7777-7777-777777777777",
          source_id: lineageId,
          document_id: oldDocumentId,
          lineage_id: lineageId,
          ordinal: 1,
          status: "active",
          start_char: 0,
          end_char: 12,
          content_hash: hashC,
          links: [],
        },
        new: {
          id: unitId,
          source_id: lineageId,
          document_id: documentId,
          lineage_id: lineageId,
          ordinal: 1,
          status: "active",
          start_char: 0,
          end_char: 14,
          content_hash: hashA,
          links: [],
        },
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  },
  links: [],
};

const evidencePayload = {
  source_id: lineageId,
  document_id: documentId,
  unit_id: unitId,
  lineage_id: lineageId,
  start_char: 0,
  end_char: 14,
  text: "旧值改成 20 ✅",
  content_hash: hashA,
  document_checksum: hashB,
  revision_ids: [revisionId],
  links: [],
};

function setupDom(): { container: HTMLDivElement; root: Root } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  roots.add(root);
  return { container, root };
}

async function renderApp(root: Root): Promise<void> {
  await act(async () => {
    root.render(createElement(App));
  });
}

async function tick(action: () => void = () => {}): Promise<void> {
  await act(async () => {
    action();
    await Promise.resolve();
  });
}

async function changeHash(action: () => void): Promise<void> {
  // 等实际 hashchange，而非猜测 history traversal 需要多少个 setTimeout(0)。
  await act(async () => {
    const changed = new Promise<void>((resolve) => {
      window.addEventListener("hashchange", () => resolve(), { once: true });
    });
    action();
    await changed;
  });
}

async function setHash(hash: string): Promise<void> {
  if (window.location.hash !== hash) await changeHash(() => { window.location.hash = hash; });
}

async function followLink(container: HTMLElement, href: string): Promise<void> {
  const link = Array.from(container.querySelectorAll("a")).find((item) => item.getAttribute("href") === href);
  expect(link).toBeTruthy();
  await changeHash(() => link!.click());
  expect(window.location.hash).toBe(href);
}

function button(container: HTMLElement, label: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll("button")).find((item) => item.textContent?.trim() === label);
  expect(match).toBeTruthy();
  return match!;
}

function makeDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("App lifecycle workbench", () => {

  it("supports full mock navigation across jobs, revision, diff and evidence, plus back/forward", async () => {
    clientMocks.managementClient.listJobs.mockResolvedValue(jobListPayload);
    clientMocks.managementClient.getJob.mockResolvedValue(detailedJob);
    clientMocks.managementClient.getRevision.mockResolvedValue(revisionPayload);
    clientMocks.managementClient.getDocumentDiff.mockResolvedValue(diffPayload);
    clientMocks.managementClient.getEvidence.mockResolvedValue(evidencePayload);

    const { container, root } = setupDom();
    await setHash("#lifecycle/jobs");
    await renderApp(root);

    expect(container.textContent).toContain("任务时间线");
    expect(container.textContent).toContain("通过");
    expect(container.textContent).toContain("失败");
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(1);

    await followLink(container, `#lifecycle/jobs/${jobId}`);
    expect(container.textContent).toContain("任务详情");
    expect(container.textContent).toContain("未评估");

    await followLink(container, `#lifecycle/revisions/${revisionId}`);
    expect(container.textContent).toContain("Revision 详情");
    expect(container.textContent).toContain("查看这次文档 Diff");

    await followLink(container, `#lifecycle/documents/${documentId}/diff?old=${oldDocumentId}`);
    expect(container.textContent).toContain("历史 Diff");
    expect(container.textContent).toContain("modified");

    await followLink(container, `#lifecycle/evidence/${unitId}`);
    expect(container.textContent).toContain("历史证据");
    expect(container.textContent).toContain("旧值改成 20 ✅");

    await changeHash(() => window.history.back());
    expect(window.location.hash).toContain("/diff");
    expect(container.textContent).toContain("历史 Diff");

    await changeHash(() => window.history.back());
    expect(window.location.hash).toContain("/revisions/");
    expect(container.textContent).toContain("Revision 详情");

    await changeHash(() => window.history.forward());
    expect(window.location.hash).toContain("/diff");
    expect(container.textContent).toContain("历史 Diff");

  });

  it("stays on a safe page for invalid routes and does not start management fetches", async () => {
    const { container, root } = setupDom();
    await setHash(`#lifecycle/documents/${documentId}/diff?old=not-a-uuid`);
    await renderApp(root);

    expect(container.textContent).toContain("链接无效，已停在安全页");
    expect(clientMocks.managementClient.listJobs).not.toHaveBeenCalled();
    expect(clientMocks.managementClient.getDocumentDiff).not.toHaveBeenCalled();

  });

  it("handles empty lists and retry after a temporary service failure", async () => {
    clientMocks.managementClient.listJobs
      .mockRejectedValueOnce(new ManagementError("network"))
      .mockResolvedValueOnce({ items: [], total: 0, limit: 20, offset: 0 });

    const { container, root } = setupDom();
    await setHash("#lifecycle/jobs");
    await renderApp(root);

    expect(container.textContent).toContain("管理服务暂不可用");

    const retryButton = Array.from(container.querySelectorAll("button")).find((item) => item.textContent?.includes("重试"));
    expect(retryButton).toBeTruthy();
    await act(async () => {
      retryButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await tick();

    expect(container.textContent).toContain("当前筛选下没有任务");
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(2);
  });

  it("renders unauthorized and gone states with explicit wording", async () => {
    clientMocks.managementClient.getJob.mockRejectedValueOnce(new ManagementError("unauthorized", { status: 403 }));
    clientMocks.managementClient.getEvidence.mockRejectedValueOnce(new ManagementError("gone", { status: 410 }));

    const first = setupDom();
    await setHash(`#lifecycle/jobs/${jobId}`);
    await renderApp(first.root);
    expect(first.container.textContent).toContain("管理服务需要授权");
    await tick(() => first.root.unmount());
    roots.delete(first.root);

    const second = setupDom();
    await setHash(`#lifecycle/evidence/${unitId}`);
    await renderApp(second.root);
    expect(second.container.textContent).toContain("历史资源已不可用");
  });

  it("filters, pages and refreshes jobs through real controls without refetch loops", async () => {
    clientMocks.managementClient.listJobs.mockImplementation((params: { offset: number }) =>
      Promise.resolve({ ...jobListPayload, total: 41, offset: params.offset }),
    );
    const { container, root } = setupDom();
    await setHash("#lifecycle/jobs");
    await renderApp(root);
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(1);
    await changeHash(() => button(container, "下一页").click());
    expect(window.location.hash).toBe("#lifecycle/jobs?offset=20");
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(2);
    const select = container.querySelector<HTMLSelectElement>("select[aria-label='按状态过滤任务']")!;
    await changeHash(() => {
      select.value = "failed";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(window.location.hash).toBe("#lifecycle/jobs?status=failed");
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(3);
    expect(clientMocks.managementClient.listJobs.mock.calls[2]![0]).toEqual({ limit: 20, offset: 0, status: "failed" });
    await tick(() => button(container, "刷新").click());
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(4);
    expect(clientMocks.managementClient.listJobs.mock.calls[3]![0]).toEqual({ limit: 20, offset: 0, status: "failed" });
    expect(container.textContent).toContain("任务时间线");
    await renderApp(root);
    expect(clientMocks.managementClient.listJobs).toHaveBeenCalledTimes(4);
  });

  it("keeps the newest job after two separately committed refresh clicks finish out of order", async () => {
    const older = makeDeferred<JobDTO>();
    const newer = makeDeferred<JobDTO>();
    clientMocks.managementClient.getJob
      .mockResolvedValueOnce(detailedJob)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const { container, root } = setupDom();
    await setHash(`#lifecycle/jobs/${jobId}`);
    await renderApp(root);
    await tick(() => button(container, "刷新").click());
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(2);
    await tick(() => button(container, "刷新中").click());
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(3);
    expect(clientMocks.managementClient.getJob.mock.calls[1]![1].aborted).toBe(true);
    await tick(() => newer.resolve({ ...detailedJob, error_code: "INVALID_EVALUATION" }));
    expect(container.textContent).toContain("INVALID_EVALUATION");
    await tick(() => older.resolve({ ...detailedJob, error_code: "EVALUATION_ERROR" }));
    expect(container.textContent).toContain("INVALID_EVALUATION");
    expect(container.textContent).not.toContain("EVALUATION_ERROR");
    expect(button(container, "刷新")).toBeTruthy();
  });

  it("never lets a delayed old job overwrite the new hash route", async () => {
    const oldRequest = makeDeferred<JobDTO>();
    const newId = "88888888-8888-8888-8888-888888888888";
    clientMocks.managementClient.getJob
      .mockReturnValueOnce(oldRequest.promise)
      .mockResolvedValueOnce({ ...detailedJob, id: newId, error_code: "CONFIG_MISSING" });
    const { container, root } = setupDom();
    await setHash(`#lifecycle/jobs/${jobId}`);
    await renderApp(root);
    await setHash(`#lifecycle/jobs/${newId}`);
    expect(clientMocks.managementClient.getJob).toHaveBeenCalledTimes(2);
    expect(clientMocks.managementClient.getJob.mock.calls[0]![1].aborted).toBe(true);
    expect(container.textContent).toContain(newId);
    await tick(() => oldRequest.resolve({ ...detailedJob, error_code: "LEASE_EXPIRED" }));
    expect(window.location.hash).toBe(`#lifecycle/jobs/${newId}`);
    expect(container.textContent).toContain("CONFIG_MISSING");
    expect(container.textContent).not.toContain("LEASE_EXPIRED");
    expect(container.textContent).not.toContain(jobId);
  });
});
