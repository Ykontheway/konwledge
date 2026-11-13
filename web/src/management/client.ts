import {
  ManagementError,
  type DocumentDTO,
  type DocumentDiffDTO,
  type EvidenceDTO,
  type JobDTO,
  type LineageDTO,
  type ListJobsQuery,
  type Page,
  type PaginationQuery,
  type RevisionDTO,
  type UnitDTO,
} from "./types";
import {
  MANAGEMENT_BASE_PATH,
  MAX_MANAGEMENT_RESPONSE_BYTES,
  assertUuid,
  buildListJobsSearchParams,
  buildPaginationSearchParams,
  validateManagementResponse,
} from "./validation";

export type ManagementFetcher = (input: string, init: RequestInit) => Promise<Response>;

const JSON_HEADERS = Object.freeze({ accept: "application/json" });

function defaultFetcher(input: string, init: RequestInit): Promise<Response> {
  if (typeof globalThis.fetch !== "function") {
    throw new ManagementError("unavailable");
  }
  return globalThis.fetch(input, init);
}

function isAbortLike(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function isTimeoutLike(error: unknown): boolean {
  return error instanceof Error && error.name === "TimeoutError";
}

function isRedirectLike(error: unknown): boolean {
  return error instanceof Error && /redirect/i.test(error.message);
}

function mapHttpStatus(status: number): ManagementError {
  if (status === 401 || status === 403) return new ManagementError("unauthorized", { status });
  if (status === 404) return new ManagementError("not_found", { status });
  if (status === 409) return new ManagementError("conflict", { status });
  if (status === 410) return new ManagementError("gone", { status });
  if (status === 408) return new ManagementError("timeout", { status });
  if (status === 422 || status === 400) return new ManagementError("invalid_request", { status });
  if (status === 429 || status === 500 || status === 502 || status === 503 || status === 504) {
    return new ManagementError("unavailable", { status });
  }
  if (status >= 300 && status < 400) return new ManagementError("invalid_response", { status });
  return new ManagementError("invalid_response", { status });
}

async function readResponseText(response: Response): Promise<string> {
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > MAX_MANAGEMENT_RESPONSE_BYTES) {
    throw new ManagementError("invalid_response", { status: response.status });
  }

  const reader = response.body?.getReader();
  if (!reader) {
    const buffer = new Uint8Array(await response.arrayBuffer());
    if (buffer.byteLength > MAX_MANAGEMENT_RESPONSE_BYTES) {
      throw new ManagementError("invalid_response", { status: response.status });
    }
    return new TextDecoder().decode(buffer);
  }

  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      size += value.byteLength;
      if (size > MAX_MANAGEMENT_RESPONSE_BYTES) {
        await reader.cancel().catch(() => undefined);
        throw new ManagementError("invalid_response", { status: response.status });
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const merged = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(merged);
}

function buildPath(pathname: string, params?: URLSearchParams): string {
  const search = params?.toString();
  return search ? `${pathname}?${search}` : pathname;
}

function normalizeTransportError(error: unknown): ManagementError {
  if (error instanceof ManagementError) return error;
  if (isAbortLike(error)) return new ManagementError("aborted", { cause: error });
  if (isTimeoutLike(error)) return new ManagementError("timeout", { cause: error });
  if (isRedirectLike(error)) return new ManagementError("invalid_response", { cause: error });
  return new ManagementError("network", { cause: error });
}

export class ManagementClient {
  private readonly fetcher: ManagementFetcher;

  constructor(fetcher: ManagementFetcher = defaultFetcher) {
    this.fetcher = fetcher;
  }

  async listJobs(query?: ListJobsQuery, signal?: AbortSignal): Promise<Page<JobDTO>> {
    return this.request(
      buildPath(`${MANAGEMENT_BASE_PATH}/jobs`, buildListJobsSearchParams(query)),
      "ListJobsResponse",
      signal,
    );
  }

  async getJob(id: string, signal?: AbortSignal): Promise<JobDTO> {
    return this.request(`${MANAGEMENT_BASE_PATH}/jobs/${assertUuid(id)}`, "GetJobResponse", signal);
  }

  async getRevision(id: string, signal?: AbortSignal): Promise<RevisionDTO> {
    return this.request(
      `${MANAGEMENT_BASE_PATH}/revisions/${assertUuid(id)}`,
      "GetRevisionResponse",
      signal,
    );
  }

  async getDocument(id: string, pagination?: PaginationQuery, signal?: AbortSignal): Promise<DocumentDTO> {
    return this.request(
      buildPath(
        `${MANAGEMENT_BASE_PATH}/documents/${assertUuid(id)}`,
        buildPaginationSearchParams(pagination),
      ),
      "GetDocumentResponse",
      signal,
    );
  }

  async getLineage(id: string, pagination?: PaginationQuery, signal?: AbortSignal): Promise<LineageDTO> {
    return this.request(
      buildPath(
        `${MANAGEMENT_BASE_PATH}/lineages/${assertUuid(id)}`,
        buildPaginationSearchParams(pagination),
      ),
      "GetLineageResponse",
      signal,
    );
  }

  async getDocumentDiff(
    newId: string,
    oldId: string,
    pagination?: PaginationQuery,
    signal?: AbortSignal,
  ): Promise<DocumentDiffDTO> {
    const params = buildPaginationSearchParams(pagination);
    params.set("old_document_id", assertUuid(oldId));
    return this.request(
      buildPath(`${MANAGEMENT_BASE_PATH}/documents/${assertUuid(newId)}/diff`, params),
      "GetDocumentDiffResponse",
      signal,
    );
  }

  async getUnit(id: string, signal?: AbortSignal): Promise<UnitDTO> {
    return this.request(`${MANAGEMENT_BASE_PATH}/units/${assertUuid(id)}`, "GetUnitResponse", signal);
  }

  async getEvidence(id: string, signal?: AbortSignal): Promise<EvidenceDTO> {
    return this.request(
      `${MANAGEMENT_BASE_PATH}/units/${assertUuid(id)}/evidence`,
      "GetEvidenceResponse",
      signal,
    );
  }

  private async request<T>(
    path: string,
    schemaName:
      | "ListJobsResponse"
      | "GetJobResponse"
      | "GetRevisionResponse"
      | "GetDocumentResponse"
      | "GetLineageResponse"
      | "GetDocumentDiffResponse"
      | "GetUnitResponse"
      | "GetEvidenceResponse",
    signal?: AbortSignal,
  ): Promise<T> {
    try {
      const response = await this.fetcher(path, {
        method: "GET",
        credentials: "omit",
        redirect: "error",
        headers: JSON_HEADERS,
        signal,
      });
      if (!response.ok) {
        throw mapHttpStatus(response.status);
      }
      const text = await readResponseText(response);
      if (!text.trim() || /^\s*</.test(text)) {
        throw new ManagementError("invalid_response", { status: response.status });
      }
      let payload: unknown;
      try {
        payload = JSON.parse(text) as unknown;
      } catch (error) {
        throw new ManagementError("invalid_response", { cause: error, status: response.status });
      }
      return validateManagementResponse(schemaName, payload) as T;
    } catch (error) {
      throw normalizeTransportError(error);
    }
  }
}

export const managementClient = new ManagementClient();
