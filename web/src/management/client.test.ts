import { describe, expect, it } from "vitest";

import { ManagementClient } from "./client";
import { getManagementErrorMessage, type ListJobsQuery } from "./types";

const jobId = "11111111-1111-1111-1111-111111111111";
const revisionId = "22222222-2222-2222-2222-222222222222";
const documentId = "33333333-3333-3333-3333-333333333333";
const oldDocumentId = "44444444-4444-4444-4444-444444444444";
const lineageId = "55555555-5555-5555-5555-555555555555";
const unitId = "66666666-6666-6666-6666-666666666666";
const datasetId = "77777777-7777-7777-7777-777777777777";
const parentJobId = "88888888-8888-8888-8888-888888888888";
const sourceId = "99999999-9999-9999-9999-999999999999";
const previousUnitId = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const nextDocumentId = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const hashA = "a".repeat(64);
const hashB = "b".repeat(64);
const hashC = "c".repeat(64);
const hashD = "d".repeat(64);
const hashE = "e".repeat(64);
const date = "2026-01-02T03:04:05+00:00";

function link(kind: "job" | "revision" | "document" | "unit" | "lineage" | "evidence", id: string) {
  const collection =
    kind === "job"
      ? "jobs"
      : kind === "revision"
        ? "revisions"
        : kind === "document"
          ? "documents"
          : kind === "lineage"
            ? "lineages"
            : "units";
  const suffix = kind === "evidence" ? "/evidence" : "";
  return { kind, id, href: `/v1/management/${collection}/${id}${suffix}` };
}

function unit(overrides: Record<string, unknown> = {}) {
  const resolvedId = typeof overrides.id === "string" ? overrides.id : unitId;
  const resolvedDocumentId = typeof overrides.document_id === "string" ? overrides.document_id : documentId;
  const resolvedLineageId = typeof overrides.lineage_id === "string" ? overrides.lineage_id : lineageId;
  return {
    id: resolvedId,
    source_id: sourceId,
    document_id: resolvedDocumentId,
    lineage_id: resolvedLineageId,
    ordinal: 0,
    status: "active",
    start_char: 0,
    end_char: 12,
    content_hash: hashA,
    links: [
      link("document", resolvedDocumentId),
      link("lineage", resolvedLineageId),
      link("evidence", resolvedId),
    ],
    ...overrides,
  };
}

const unitPayload = unit();
const revisionPayload = {
  id: revisionId,
  entity_type: "knowledge_unit",
  entity_id: lineageId,
  revision: 2,
  change_type: "update",
  created_at: date,
  navigation: {
    event: "modified",
    document_id: documentId,
    previous_document_id: oldDocumentId,
    unit_id: unitId,
    previous_unit_id: previousUnitId,
    document_checksum: hashB,
    content_hash: hashA,
    previous_content_hash: hashC,
    reverted: false,
  },
  affected_document_ids: [oldDocumentId, documentId],
  affected_unit_ids: [previousUnitId, unitId],
  affected_lineage_ids: [lineageId],
  links: [link("document", oldDocumentId), link("document", documentId), link("unit", unitId), link("lineage", lineageId)],
};
const documentPayload = {
  id: documentId,
  source_id: sourceId,
  content_type: "markdown",
  checksum: hashB,
  ingested_at: date,
  previous_document_id: oldDocumentId,
  next_document_id: nextDocumentId,
  units: {
    items: [unitPayload],
    total: 1,
    limit: 1,
    offset: 0,
  },
  links: [link("document", oldDocumentId), link("document", nextDocumentId)],
};
const lineagePayload = {
  id: lineageId,
  versions: {
    items: [unitPayload],
    total: 1,
    limit: 1,
    offset: 0,
  },
};
const diffPayload = {
  source_id: sourceId,
  old_document_id: oldDocumentId,
  new_document_id: documentId,
  counts: { added: 0, modified: 1, moved: 0, unchanged: 0, removed: 0 },
  changes: {
    items: [
      {
        kind: "modified",
        lineage_id: lineageId,
        old: unit({ id: previousUnitId, document_id: oldDocumentId, content_hash: hashC }),
        new: unitPayload,
      },
    ],
    total: 1,
    limit: 1,
    offset: 0,
  },
  links: [link("document", oldDocumentId), link("document", documentId)],
};
const evidencePayload = {
  source_id: sourceId,
  document_id: documentId,
  unit_id: unitId,
  lineage_id: lineageId,
  start_char: 0,
  end_char: 12,
  text: "管理证据文本",
  content_hash: hashA,
  document_checksum: hashB,
  revision_ids: [revisionId],
  links: [link("document", documentId), link("lineage", lineageId), link("revision", revisionId)],
};
const jobPayload = {
  id: jobId,
  parent_job_id: parentJobId,
  status: "failed",
  config_generation: 3,
  config_fingerprint: hashC,
  dataset_id: datasetId,
  dataset_version: 2,
  dataset_hash: hashD,
  knowledge_fingerprint: hashE,
  created_at: date,
  evaluated_at: date,
  started_at: date,
  finished_at: null,
  attempt_count: 1,
  error_code: "QUALITY_GATE_FAILED",
  report: {
    passed: false,
    metrics: {
      case_count: 10,
      answerable_count: 8,
      no_answer_count: 2,
      mean_recall: 0.8,
      mean_precision: null,
      mean_reciprocal_rank: 0.7,
      mean_ndcg: null,
      hit_rate: 1,
      no_answer_accuracy: null,
    },
    citation_coverage: null,
    stale_hits: 0,
    degraded: false,
    checks: [
      { name: "recall_at_k", passed: true, actual: 0.8, threshold: ">=0.8" },
      { name: "regression_gate", passed: false, actual: null, threshold: null },
    ],
  },
  trigger_revision_ids: [revisionId],
  links: [link("revision", revisionId)],
};
const listJobsPayload = {
  items: [jobPayload],
  total: 1,
  limit: 5,
  offset: 10,
};

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });
}

function makeFetcher(routes: Record<string, () => Response | Promise<Response>>) {
  const calls: Array<{ input: string; init: RequestInit }> = [];
  const fetcher = async (input: string, init: RequestInit): Promise<Response> => {
    calls.push({ input, init });
    const route = routes[input];
    if (!route) {
      throw new Error(`unexpected route: ${input}`);
    }
    return route();
  };
  return { calls, fetcher };
}

function abortError(): Error {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

describe("ManagementClient", () => {
  it("covers all resources, accepts nullable metrics, and sends safe GET requests", async () => {
    const jobsQuery: ListJobsQuery = {
      limit: 5,
      offset: 10,
      status: "failed",
      dataset_id: datasetId,
      dataset_version: 2,
      parent_job_id: parentJobId,
    };
    const { calls, fetcher } = makeFetcher({
      "/v1/management/jobs?limit=5&offset=10&status=failed&dataset_id=77777777-7777-7777-7777-777777777777&dataset_version=2&parent_job_id=88888888-8888-8888-8888-888888888888": () =>
        jsonResponse(listJobsPayload),
      [`/v1/management/jobs/${jobId}`]: () => jsonResponse(jobPayload),
      [`/v1/management/revisions/${revisionId}`]: () => jsonResponse(revisionPayload),
      [`/v1/management/documents/${documentId}?limit=1&offset=0`]: () => jsonResponse(documentPayload),
      [`/v1/management/lineages/${lineageId}?limit=1&offset=0`]: () => jsonResponse(lineagePayload),
      [`/v1/management/documents/${documentId}/diff?limit=1&offset=0&old_document_id=${oldDocumentId}`]: () =>
        jsonResponse(diffPayload),
      [`/v1/management/units/${unitId}`]: () => jsonResponse(unitPayload),
      [`/v1/management/units/${unitId}/evidence`]: () => jsonResponse(evidencePayload),
    });
    const client = new ManagementClient(fetcher);

    const jobs = await client.listJobs(jobsQuery);
    const job = await client.getJob(jobId);
    const revision = await client.getRevision(revisionId);
    const document = await client.getDocument(documentId, { limit: 1, offset: 0 });
    const lineage = await client.getLineage(lineageId, { limit: 1, offset: 0 });
    const diff = await client.getDocumentDiff(documentId, oldDocumentId, { limit: 1, offset: 0 });
    const unitResult = await client.getUnit(unitId);
    const evidence = await client.getEvidence(unitId);

    expect(jobs.items[0]?.report?.metrics.mean_precision).toBeNull();
    expect(job.report?.citation_coverage).toBeNull();
    expect(revision.navigation.previous_document_id).toBe(oldDocumentId);
    expect(document.units.items[0]?.content_hash).toBe(hashA);
    expect(lineage.versions.items[0]?.lineage_id).toBe(lineageId);
    expect(diff.changes.items[0]?.new?.id).toBe(unitId);
    expect(unitResult.links[2]?.href).toBe(`/v1/management/units/${unitId}/evidence`);
    expect(evidence.revision_ids).toEqual([revisionId]);

    for (const call of calls) {
      expect(call.input.startsWith("/v1/management")).toBe(true);
      expect(call.init.method).toBe("GET");
      expect(call.init.credentials).toBe("omit");
      expect(call.init.redirect).toBe("error");
      expect((call.init.headers as Record<string, string>).accept).toBe("application/json");
      expect(call.init.body).toBeUndefined();
    }
  });

  it("maps expected HTTP error states to safe kinds", async () => {
    const cases = [
      { status: 401, kind: "unauthorized" },
      { status: 404, kind: "not_found" },
      { status: 409, kind: "conflict" },
      { status: 410, kind: "gone" },
      { status: 422, kind: "invalid_request" },
      { status: 503, kind: "unavailable" },
    ] as const;

    for (const { status, kind } of cases) {
      const client = new ManagementClient(async () => new Response("", { status }));
      await expect(client.getJob(jobId)).rejects.toMatchObject({ kind });
    }
  });

  it("rejects bad uuid and unknown query keys before issuing a request", async () => {
    let called = false;
    const client = new ManagementClient(async () => {
      called = true;
      return jsonResponse(jobPayload);
    });

    await expect(client.getJob("not-a-uuid")).rejects.toMatchObject({ kind: "invalid_request" });
    await expect(
      client.listJobs({ limit: 1, status: "nope" as never }),
    ).rejects.toMatchObject({ kind: "invalid_request" });
    await expect(
      client.listJobs({ limit: 1, foo: "bar" } as unknown as ListJobsQuery),
    ).rejects.toMatchObject({ kind: "invalid_request" });
    expect(called).toBe(false);
  });

  it("treats html and non-json success payloads as invalid responses", async () => {
    const htmlClient = new ManagementClient(
      async () =>
        new Response("<!doctype html><html><body>fallback</body></html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
    );
    await expect(htmlClient.getJob(jobId)).rejects.toMatchObject({ kind: "invalid_response" });

    const textClient = new ManagementClient(
      async () => new Response("not-json", { status: 200, headers: { "content-type": "text/plain" } }),
    );
    await expect(textClient.getJob(jobId)).rejects.toMatchObject({ kind: "invalid_response" });
  });

  it("rejects unknown response fields and unsafe href values", async () => {
    const extraFieldClient = new ManagementClient(async () => jsonResponse({ ...jobPayload, extra: true }));
    await expect(extraFieldClient.getJob(jobId)).rejects.toMatchObject({ kind: "invalid_response" });

    const unsafeHrefClient = new ManagementClient(
      async () => jsonResponse({ ...jobPayload, links: [{ ...link("revision", revisionId), href: "https://evil.invalid/x" }] }),
    );
    await expect(unsafeHrefClient.getJob(jobId)).rejects.toMatchObject({ kind: "invalid_response" });
  });

  it("handles aborts without misreporting them as network errors", async () => {
    const controller = new AbortController();
    const client = new ManagementClient(async (_input, init) => {
      if (init.signal?.aborted) throw abortError();
      throw abortError();
    });
    controller.abort();

    const error = await client.getJob(jobId, controller.signal).catch((value: unknown) => value);
    expect(error).toMatchObject({ kind: "aborted" });
    expect(getManagementErrorMessage(error)).toBe("请求已取消。");
  });

  it("uses redirect:error and blocks redirect-like transport failures safely", async () => {
    let redirectMode: RequestRedirect | undefined;
    const client = new ManagementClient(async (_input, init) => {
      redirectMode = init.redirect;
      throw new TypeError("redirect mode is set to error: redirect disallowed");
    });

    await expect(client.getJob(jobId)).rejects.toMatchObject({ kind: "invalid_response" });
    expect(redirectMode).toBe("error");
  });
});
