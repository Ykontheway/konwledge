"""B1 只读管理 router; 认证和 opt-in 开关由 create_app 接线负责。"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from konwledge.api.management_models import (
    DiffQuery,
    DocumentDiffDTO,
    DocumentDTO,
    EvidenceDTO,
    JobDTO,
    JobsQuery,
    LineageDTO,
    Page,
    PageQuery,
    RevisionDTO,
    UnitDTO,
)
from konwledge.api.management_service import ManagementReadError, ManagementService
from konwledge.api.models import APIErrorResponse
from konwledge.application import KnowledgeLibrary

__all__ = ["build_management_router"]


def _error(status: int, code: str) -> JSONResponse:
    # 统一已有 API error envelope, 不回显 UUID/query、ValidationError.input 或 SQL。
    body = APIErrorResponse.model_validate(
        {
            "error": {
                "message": "Management request failed.",
                "type": "internal_error" if status == 500 else "invalid_request_error",
                "param": None,
                "code": code,
            }
        }
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


class _SafeManagementRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                # router 局部策略: 普通 context API 仍保留原有 400 协议。
                return _error(422, "management_validation_error")
            except ManagementReadError as exc:
                return _error(exc.status, exc.code)
            except Exception:
                return _error(500, "management_internal_error")

        return safe_handler


def build_management_router(library: KnowledgeLibrary) -> APIRouter:
    """返回唯一接线接口, 自带 prefix; 必须在 app 中附加现有 auth dependency。"""
    service = ManagementService(library)
    router = APIRouter(
        prefix="/v1/management",
        tags=["management"],
        route_class=_SafeManagementRoute,
        responses={
            status: {"model": APIErrorResponse} for status in (404, 409, 410, 422, 500, 503)
        },
    )

    @router.get("/jobs", response_model=Page[JobDTO])
    def jobs(query: Annotated[JobsQuery, Query()]) -> Page[JobDTO]:
        return service.jobs(query)

    @router.get("/jobs/{job_id}", response_model=JobDTO)
    def job(job_id: UUID) -> JobDTO:
        return service.job(job_id)

    @router.get("/revisions/{revision_id}", response_model=RevisionDTO)
    def revision(revision_id: UUID) -> RevisionDTO:
        return service.revision(revision_id)

    @router.get("/lineages/{lineage_id}", response_model=LineageDTO)
    def lineage(lineage_id: UUID, query: Annotated[PageQuery, Query()]) -> LineageDTO:
        return service.lineage(lineage_id, query)

    @router.get("/documents/{document_id}", response_model=DocumentDTO)
    def document(document_id: UUID, query: Annotated[PageQuery, Query()]) -> DocumentDTO:
        return service.document(document_id, query)

    @router.get("/documents/{new_document_id}/diff", response_model=DocumentDiffDTO)
    def document_diff(
        new_document_id: UUID, query: Annotated[DiffQuery, Query()]
    ) -> DocumentDiffDTO:
        return service.diff(new_document_id, query.old_document_id, query)

    @router.get("/units/{unit_id}", response_model=UnitDTO)
    def unit(unit_id: UUID) -> UnitDTO:
        return service.unit(unit_id)

    @router.get("/units/{unit_id}/evidence", response_model=EvidenceDTO)
    def evidence(unit_id: UUID) -> EvidenceDTO:
        return service.evidence(unit_id)

    return router
