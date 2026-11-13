import Ajv2020, { type ValidateFunction } from "ajv/dist/2020";

import managementSchema from "./schema.json";
import {
  ManagementError,
  type DocumentDTO,
  type DocumentDiffDTO,
  type EvidenceDTO,
  type JobDTO,
  type JobStatus,
  type LineageDTO,
  type ListJobsQuery,
  type Page,
  type PaginationQuery,
  type ResourceLink,
  type RevisionDTO,
  type UnitDTO,
} from "./types";

export const MANAGEMENT_BASE_PATH = "/v1/management";
export const MAX_MANAGEMENT_RESPONSE_BYTES = 5 * 1024 * 1024;

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH_PATTERN = /^[0-9a-f]{64}$/;
const JOB_STATUS_VALUES: readonly JobStatus[] = [
  "queued",
  "running",
  "passed",
  "failed",
  "blocked",
  "superseded",
];
const JOB_STATUS_SET = new Set<string>(JOB_STATUS_VALUES);
const PAGE_QUERY_KEYS = new Set<string>(["limit", "offset"]);
const LIST_JOBS_QUERY_KEYS = new Set<string>([
  "limit",
  "offset",
  "status",
  "dataset_id",
  "dataset_version",
  "parent_job_id",
]);

const RESOURCE_COLLECTIONS = {
  job: "jobs",
  revision: "revisions",
  document: "documents",
  unit: "units",
  lineage: "lineages",
  evidence: "units",
} as const;

const schemaDocument = managementSchema as {
  readonly $schema?: string;
  readonly $defs: Record<string, unknown>;
};

type SchemaDefName =
  | "ListJobsResponse"
  | "GetJobResponse"
  | "GetRevisionResponse"
  | "GetDocumentResponse"
  | "GetLineageResponse"
  | "GetDocumentDiffResponse"
  | "GetUnitResponse"
  | "GetEvidenceResponse";

type SchemaTargetMap = {
  ListJobsResponse: Page<JobDTO>;
  GetJobResponse: JobDTO;
  GetRevisionResponse: RevisionDTO;
  GetDocumentResponse: DocumentDTO;
  GetLineageResponse: LineageDTO;
  GetDocumentDiffResponse: DocumentDiffDTO;
  GetUnitResponse: UnitDTO;
  GetEvidenceResponse: EvidenceDTO;
};

const ajv = new Ajv2020({
  allErrors: true,
  allowUnionTypes: true,
  strict: false,
  validateFormats: false,
});

ajv.addFormat("uuid", {
  type: "string",
  validate: (value: string) => UUID_PATTERN.test(value),
});

ajv.addFormat("date-time", {
  type: "string",
  validate: (value: string) => {
    if (!/[zZ]|[+-]\d{2}:\d{2}$/.test(value)) return false;
    return !Number.isNaN(Date.parse(value));
  },
});

const validatorCache = new Map<SchemaDefName, ValidateFunction<unknown>>();

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object") return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertSchemaFragment(name: SchemaDefName): Record<string, unknown> {
  const fragment = schemaDocument.$defs[name];
  if (!isPlainObject(fragment)) {
    throw new ManagementError("invalid_response");
  }
  return {
    $schema: schemaDocument.$schema,
    $defs: schemaDocument.$defs,
    ...fragment,
  };
}

function getValidator<Name extends SchemaDefName>(name: Name): ValidateFunction<SchemaTargetMap[Name]> {
  const cached = validatorCache.get(name);
  if (cached) return cached as ValidateFunction<SchemaTargetMap[Name]>;
  const compiled = ajv.compile<SchemaTargetMap[Name]>(assertSchemaFragment(name));
  validatorCache.set(name, compiled as ValidateFunction<unknown>);
  return compiled;
}

function walkPayload(value: unknown, visit: (current: unknown) => void): void {
  visit(value);
  if (Array.isArray(value)) {
    for (const item of value) walkPayload(item, visit);
    return;
  }
  if (!isPlainObject(value)) return;
  for (const item of Object.values(value)) walkPayload(item, visit);
}

function assertFiniteNumbers(payload: unknown): void {
  walkPayload(payload, (value) => {
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new ManagementError("invalid_response");
    }
  });
}

function buildManagementHref(kind: ResourceLink["kind"], id: string): string {
  const suffix = kind === "evidence" ? "/evidence" : "";
  return `${MANAGEMENT_BASE_PATH}/${RESOURCE_COLLECTIONS[kind]}/${id}${suffix}`;
}

function isResourceLink(value: unknown): value is ResourceLink {
  return (
    isPlainObject(value) &&
    typeof value.kind === "string" &&
    typeof value.id === "string" &&
    typeof value.href === "string"
  );
}

function assertSafeLinks(payload: unknown): void {
  walkPayload(payload, (value) => {
    if (!isResourceLink(value)) return;
    if (!UUID_PATTERN.test(value.id)) {
      throw new ManagementError("invalid_response");
    }
    if (value.href !== buildManagementHref(value.kind, value.id)) {
      throw new ManagementError("invalid_response");
    }
  });
}

function assertTrustedHashFields(payload: unknown): void {
  walkPayload(payload, (value) => {
    if (!isPlainObject(value)) return;
    for (const key of [
      "config_fingerprint",
      "dataset_hash",
      "knowledge_fingerprint",
      "checksum",
      "content_hash",
      "document_checksum",
    ]) {
      const candidate = value[key];
      if (candidate !== undefined && (typeof candidate !== "string" || !HASH_PATTERN.test(candidate))) {
        throw new ManagementError("invalid_response");
      }
    }
  });
}

function assertValidUnicodeText(text: string): void {
  for (let index = 0; index < text.length; index += 1) {
    const codeUnit = text.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (!Number.isInteger(next) || next < 0xdc00 || next > 0xdfff) {
        throw new ManagementError("invalid_response");
      }
      index += 1;
      continue;
    }
    if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      throw new ManagementError("invalid_response");
    }
  }
}

function assertPostValidation<Name extends SchemaDefName>(
  name: Name,
  payload: SchemaTargetMap[Name],
): SchemaTargetMap[Name] {
  assertFiniteNumbers(payload);
  assertSafeLinks(payload);
  assertTrustedHashFields(payload);
  if (name === "GetEvidenceResponse") {
    assertValidUnicodeText((payload as EvidenceDTO).text);
  }
  return payload;
}

export function validateManagementResponse<Name extends SchemaDefName>(
  name: Name,
  payload: unknown,
): SchemaTargetMap[Name] {
  const validator = getValidator(name);
  if (!validator(payload)) {
    throw new ManagementError("invalid_response");
  }
  return assertPostValidation(name, payload);
}

function assertKnownKeys(
  value: unknown,
  allowedKeys: ReadonlySet<string>,
  errorKind: ManagementError = new ManagementError("invalid_request"),
): Record<string, unknown> {
  if (value === undefined) return {};
  if (!isPlainObject(value)) throw errorKind;
  for (const key of Object.keys(value)) {
    if (!allowedKeys.has(key)) throw errorKind;
  }
  return value;
}

function appendInteger(
  params: URLSearchParams,
  name: string,
  value: unknown,
  limits: { min: number; max: number },
): void {
  if (value === undefined) return;
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new ManagementError("invalid_request");
  }
  if (value < limits.min || value > limits.max) {
    throw new ManagementError("invalid_request");
  }
  params.set(name, String(value));
}

function appendUuid(params: URLSearchParams, name: string, value: unknown): void {
  if (value === undefined) return;
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw new ManagementError("invalid_request");
  }
  params.set(name, value);
}

export function assertUuid(id: string): string {
  if (!UUID_PATTERN.test(id)) {
    throw new ManagementError("invalid_request");
  }
  return id;
}

function appendPaginationEntries(params: URLSearchParams, entries: Record<string, unknown>): void {
  appendInteger(params, "limit", entries.limit, { min: 1, max: 100 });
  appendInteger(params, "offset", entries.offset, { min: 0, max: 100_000 });
}

export function buildPaginationSearchParams(query?: PaginationQuery): URLSearchParams {
  const params = new URLSearchParams();
  appendPaginationEntries(params, assertKnownKeys(query, PAGE_QUERY_KEYS));
  return params;
}

export function buildListJobsSearchParams(query?: ListJobsQuery): URLSearchParams {
  const entries = assertKnownKeys(query, LIST_JOBS_QUERY_KEYS);
  const params = new URLSearchParams();
  appendPaginationEntries(params, entries);
  if (entries.status !== undefined) {
    if (typeof entries.status !== "string" || !JOB_STATUS_SET.has(entries.status)) {
      throw new ManagementError("invalid_request");
    }
    params.set("status", entries.status);
  }
  appendUuid(params, "dataset_id", entries.dataset_id);
  appendInteger(params, "dataset_version", entries.dataset_version, { min: 1, max: Number.MAX_SAFE_INTEGER });
  appendUuid(params, "parent_job_id", entries.parent_job_id);
  return params;
}
