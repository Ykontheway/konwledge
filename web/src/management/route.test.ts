import { describe, expect, it } from "vitest";

import {
  getBreadcrumbMeta,
  lifecycleDiffHash,
  lifecycleDocumentHash,
  lifecycleEntryHash,
  lifecycleEvidenceHash,
  lifecycleJobHash,
  lifecycleJobsHash,
  lifecycleLineageHash,
  lifecycleRevisionHash,
  parseHashRoute,
} from "./route";

const jobId = "11111111-1111-1111-1111-111111111111";
const revisionId = "22222222-2222-2222-2222-222222222222";
const documentId = "33333333-3333-3333-3333-333333333333";
const oldDocumentId = "44444444-4444-4444-4444-444444444444";
const lineageId = "55555555-5555-5555-5555-555555555555";
const unitId = "66666666-6666-6666-6666-666666666666";

describe("management hash routes", () => {
  it("parses supported lifecycle deep links", () => {
    expect(parseHashRoute(lifecycleEntryHash())).toEqual({ view: "lifecycle", kind: "jobs", status: undefined, offset: 0 });
    expect(parseHashRoute(lifecycleJobsHash({ status: "failed", offset: 20 }))).toEqual({
      view: "lifecycle",
      kind: "jobs",
      status: "failed",
      offset: 20,
    });
    expect(parseHashRoute(lifecycleJobHash(jobId))).toEqual({ view: "lifecycle", kind: "job", id: jobId });
    expect(parseHashRoute(lifecycleRevisionHash(revisionId))).toEqual({ view: "lifecycle", kind: "revision", id: revisionId });
    expect(parseHashRoute(lifecycleDocumentHash(documentId, 40))).toEqual({
      view: "lifecycle",
      kind: "document",
      id: documentId,
      offset: 40,
    });
    expect(parseHashRoute(lifecycleDiffHash(documentId, oldDocumentId, 20))).toEqual({
      view: "lifecycle",
      kind: "diff",
      newId: documentId,
      oldId: oldDocumentId,
      offset: 20,
    });
    expect(parseHashRoute(lifecycleLineageHash(lineageId, 60))).toEqual({
      view: "lifecycle",
      kind: "lineage",
      id: lineageId,
      offset: 60,
    });
    expect(parseHashRoute(lifecycleEvidenceHash(unitId))).toEqual({ view: "lifecycle", kind: "evidence", id: unitId });
  });

  it("rejects invalid ids and unsupported query keys without falling through to fetch", () => {
    expect(parseHashRoute("#lifecycle/jobs/not-a-uuid")).toMatchObject({ view: "lifecycle", kind: "invalid", reason: "invalid_id" });
    expect(parseHashRoute(`#lifecycle/documents/${documentId}/diff?old=not-a-uuid`)).toMatchObject({
      view: "lifecycle",
      kind: "invalid",
      reason: "invalid_query",
    });
    expect(parseHashRoute(`#lifecycle/lineages/${lineageId}?token=secret`)).toMatchObject({
      view: "lifecycle",
      kind: "invalid",
      reason: "invalid_query",
    });
    expect(parseHashRoute("#lifecycle/https://evil.invalid/")).toMatchObject({
      view: "lifecycle",
      kind: "invalid",
    });
  });

  it("keeps demo routes stable and exposes breadcrumb labels", () => {
    expect(parseHashRoute("")).toEqual({ view: "overview" });
    expect(parseHashRoute("#trace")).toEqual({ view: "trace" });
    expect(parseHashRoute("#unknown-view")).toEqual({ view: "overview" });
    expect(getBreadcrumbMeta(parseHashRoute("#diff"))).toEqual({
      section: "静态演示",
      title: "版本 Diff",
      tone: "demo",
    });
    expect(getBreadcrumbMeta(parseHashRoute(lifecycleJobHash(jobId)))).toEqual({
      section: "实时工作台 / 只读",
      title: "任务详情",
      tone: "live",
    });
  });
});
