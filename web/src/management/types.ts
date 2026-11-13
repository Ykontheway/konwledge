import type * as Gen from "./types.gen";

export type DeepReadonly<T> = T extends (...args: never[]) => unknown
  ? T
  : T extends readonly (infer Item)[]
    ? readonly DeepReadonly<Item>[]
    : T extends object
      ? { readonly [Key in keyof T]: DeepReadonly<T[Key]> }
      : T;

export type UUIDString = string;
export type HashString = string;

export interface Page<T> {
  readonly items: readonly T[];
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
}

export type JobStatus = DeepReadonly<Gen.AutoEvaluationStatus>;
export type ResourceLink = DeepReadonly<Gen.ResourceLink>;
export type SafeCheck = DeepReadonly<Gen.SafeCheck>;
export type SafeMetrics = DeepReadonly<Gen.SafeMetrics>;
export type SafeQualityReport = DeepReadonly<Gen.SafeQualityReport>;
export type DiffCounts = DeepReadonly<Gen.DiffCounts>;
export type RevisionNavigation = DeepReadonly<Gen.RevisionNavigation>;

export type UnitDTO = DeepReadonly<Gen.UnitDTO>;
export type DiffItem = DeepReadonly<Gen.DiffItem>;
export type DocumentDiffDTO = DeepReadonly<Gen.DocumentDiffDTO>;
export type EvidenceDTO = DeepReadonly<Gen.EvidenceDTO>;
export type JobDTO = DeepReadonly<Gen.JobDTO>;
export type LineageDTO = DeepReadonly<Gen.LineageDTO>;
export type RevisionDTO = DeepReadonly<Gen.RevisionDTO>;
export type DocumentDTO = DeepReadonly<Gen.DocumentDTO>;

export interface PaginationQuery {
  readonly limit?: number;
  readonly offset?: number;
}

export interface ListJobsQuery extends PaginationQuery {
  readonly status?: JobStatus;
  readonly dataset_id?: UUIDString;
  readonly dataset_version?: number;
  readonly parent_job_id?: UUIDString;
}

export type ManagementErrorKind =
  | "unauthorized"
  | "unavailable"
  | "not_found"
  | "invalid_request"
  | "conflict"
  | "gone"
  | "invalid_response"
  | "network"
  | "timeout"
  | "aborted";

export const MANAGEMENT_ERROR_MESSAGES: Readonly<Record<ManagementErrorKind, string>> = {
  unauthorized: "当前无权访问管理数据。",
  unavailable: "管理服务暂时不可用，请稍后重试。",
  not_found: "请求的管理资源不存在。",
  invalid_request: "请求参数无效。",
  conflict: "管理数据当前不可读取。",
  gone: "请求的历史管理资源已不可用。",
  invalid_response: "管理数据响应格式无效。",
  network: "网络异常，请检查连接后重试。",
  timeout: "管理请求超时，请稍后重试。",
  aborted: "请求已取消。",
};

export class ManagementError extends Error {
  readonly name = "ManagementError";
  readonly kind: ManagementErrorKind;
  readonly status?: number;
  declare readonly cause: unknown;

  constructor(kind: ManagementErrorKind, options: { cause?: unknown; status?: number } = {}) {
    super(MANAGEMENT_ERROR_MESSAGES[kind], { cause: options.cause });
    this.kind = kind;
    this.status = options.status;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export function isManagementError(error: unknown): error is ManagementError {
  return error instanceof ManagementError;
}

function isAbortLikeError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.name === "AbortError";
}

function isTimeoutLikeError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.name === "TimeoutError";
}

export function getManagementErrorMessage(error: unknown): string {
  if (error instanceof ManagementError) return error.message;
  if (isAbortLikeError(error)) return MANAGEMENT_ERROR_MESSAGES.aborted;
  if (isTimeoutLikeError(error)) return MANAGEMENT_ERROR_MESSAGES.timeout;
  return MANAGEMENT_ERROR_MESSAGES.unavailable;
}

type Exact<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
type Assert<T extends true> = T;
type GeneratedQuery<T> = {
  readonly [Key in keyof T]?: Exclude<DeepReadonly<T[Key]>, null | undefined>;
};

export type __ManagementSchemaDriftChecks = [
  Assert<Exact<JobStatus, DeepReadonly<Gen.AutoEvaluationStatus>>>,
  Assert<Exact<ResourceLink, DeepReadonly<Gen.ResourceLink>>>,
  Assert<Exact<SafeCheck, DeepReadonly<Gen.SafeCheck>>>,
  Assert<Exact<SafeMetrics, DeepReadonly<Gen.SafeMetrics>>>,
  Assert<Exact<SafeQualityReport, DeepReadonly<Gen.SafeQualityReport>>>,
  Assert<Exact<DiffCounts, DeepReadonly<Gen.DiffCounts>>>,
  Assert<Exact<RevisionNavigation, DeepReadonly<Gen.RevisionNavigation>>>,
  Assert<Exact<UnitDTO, DeepReadonly<Gen.UnitDTO>>>,
  Assert<Exact<DiffItem, DeepReadonly<Gen.DiffItem>>>,
  Assert<Exact<DocumentDiffDTO, DeepReadonly<Gen.DocumentDiffDTO>>>,
  Assert<Exact<EvidenceDTO, DeepReadonly<Gen.EvidenceDTO>>>,
  Assert<Exact<JobDTO, DeepReadonly<Gen.JobDTO>>>,
  Assert<Exact<LineageDTO, DeepReadonly<Gen.LineageDTO>>>,
  Assert<Exact<RevisionDTO, DeepReadonly<Gen.RevisionDTO>>>,
  Assert<Exact<DocumentDTO, DeepReadonly<Gen.DocumentDTO>>>,
  Assert<Exact<Page<JobDTO>, DeepReadonly<Gen.PageJobDTO>>>,
  Assert<Exact<Page<UnitDTO>, DeepReadonly<Gen.PageUnitDTO>>>,
  Assert<Exact<Page<DiffItem>, DeepReadonly<Gen.PageDiffItem1>>>,
  Assert<Exact<PaginationQuery, GeneratedQuery<Gen.PageQuery>>>,
  Assert<Exact<ListJobsQuery, GeneratedQuery<Gen.JobsQuery>>>,
];
