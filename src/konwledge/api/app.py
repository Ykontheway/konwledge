"""FastAPI application factory for the read-only context transport."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from collections.abc import Mapping
from threading import RLock
from typing import TYPE_CHECKING, Any, NoReturn

from konwledge import __version__
from konwledge.api.models import (
    MODEL_ID,
    APIErrorResponse,
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatResponseMessage,
    ContextControls,
    ContextPacket,
    ContextRequest,
    HealthResponse,
    ModelInfo,
    ModelListResponse,
)
from konwledge.retrieval import RetrievalConfigurationError, RetrievalMode

if TYPE_CHECKING:
    from fastapi import FastAPI

    from konwledge.application import KnowledgeLibrary

_MODEL_CREATED = 0


def create_app(library: KnowledgeLibrary, *, api_key: str | None = None) -> FastAPI:
    """Create an API over an existing library whose lifecycle remains caller-owned.

    FastAPI is imported only when this optional transport is instantiated. The
    factory neither creates storage nor closes the injected library's engine.
    """

    try:
        from fastapi import Depends, FastAPI, HTTPException
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from fastapi.security import HTTPBearer
        from starlette.exceptions import HTTPException as StarletteHTTPException
    except ImportError:
        raise ImportError(
            "FastAPI is required for the API transport; install the 'api' extra"
        ) from None

    if api_key is not None and not isinstance(api_key, str):
        raise TypeError("api_key must be a string or None")
    if api_key == "":
        raise ValueError("api_key must not be empty")

    # When authentication is enabled, do not accidentally publish interactive
    # documentation that cannot itself enforce the bearer-key requirement.
    protected_docs = api_key is not None
    app = FastAPI(
        title="Konwledge API",
        version=__version__,
        docs_url=None if protected_docs else "/docs",
        redoc_url=None if protected_docs else "/redoc",
        openapi_url=None if protected_docs else "/openapi.json",
    )
    library_lock = RLock()
    bearer = HTTPBearer(auto_error=False)

    def api_error(
        status_code: int,
        *,
        message: str,
        error_type: str = "invalid_request_error",
        param: str | None = None,
        code: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> NoReturn:
        raise HTTPException(
            status_code=status_code,
            detail={
                "error": {
                    "message": message,
                    "type": error_type,
                    "param": param,
                    "code": code,
                }
            },
            headers=headers,
        )

    async def require_api_key(
        credentials: Any = Depends(bearer),  # noqa: B008
    ) -> None:
        if api_key is None:
            return
        supplied = "" if credentials is None else credentials.credentials
        if not secrets.compare_digest(supplied, api_key):
            api_error(
                401,
                message="Invalid or missing API key.",
                code="invalid_api_key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def error_response(
        status_code: int,
        *,
        message: str,
        error_type: str,
        param: str | None,
        code: str | None,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        body = APIErrorResponse.model_validate(
            {
                "error": {
                    "message": message,
                    "type": error_type,
                    "param": param,
                    "code": code,
                }
            }
        )
        return JSONResponse(
            status_code=status_code,
            content=body.model_dump(mode="json"),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Any, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = first.get("loc", ())
        fields = [str(part) for part in location if part not in {"body", "query", "path"}]
        param = ".".join(fields) or None
        unsupported = first.get("type") == "extra_forbidden"
        return error_response(
            400,
            message="Unsupported option." if unsupported else "Invalid request.",
            error_type="invalid_request_error",
            param=param,
            code="unsupported_option" if unsupported else "validation_error",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Any, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
            error = detail["error"]
            return error_response(
                exc.status_code,
                message=str(error.get("message", "Request failed.")),
                error_type=str(error.get("type", "invalid_request_error")),
                param=error.get("param") if isinstance(error.get("param"), str) else None,
                code=error.get("code") if isinstance(error.get("code"), str) else None,
                headers=exc.headers,
            )
        return error_response(
            exc.status_code,
            message="Request failed.",
            error_type="invalid_request_error",
            param=None,
            code="http_error",
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Any, exc: Exception) -> JSONResponse:
        # Never serialize exception text: provider, SQL, filesystem, and prompt
        # details can all occur in lower-layer exception messages.
        return error_response(
            500,
            message="An internal error occurred.",
            error_type="internal_error",
            param=None,
            code="internal_error",
        )

    error_docs: dict[int | str, dict[str, Any]] = {
        400: {"model": APIErrorResponse},
        401: {"model": APIErrorResponse},
        503: {"model": APIErrorResponse},
    }
    auth_dependencies = [Depends(require_api_key)] if api_key is not None else []

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    @app.get(
        "/v1/models",
        response_model=ModelListResponse,
        responses=error_docs,
        dependencies=auth_dependencies,
    )
    def list_models() -> ModelListResponse:
        return ModelListResponse(data=(ModelInfo(id=MODEL_ID, created=_MODEL_CREATED),))

    def build_packet(query: str, controls: ContextControls) -> ContextPacket:
        try:
            with library_lock:
                return library.build_context(
                    query,
                    mode=controls.mode,
                    top_k=controls.top_k,
                    candidate_limit=controls.candidate_limit,
                    rrf_k=controls.rrf_k,
                    at=controls.as_of,
                    include_stale=controls.include_stale,
                    source_id=controls.source_id,
                    max_chars=controls.max_chars,
                    max_units=controls.max_units,
                )
        except RetrievalConfigurationError:
            api_error(
                400,
                message="The requested retrieval mode is not configured.",
                param="mode",
                code="retrieval_mode_unavailable",
            )
        except ValueError:
            api_error(
                400,
                message="Invalid retrieval request.",
                code="validation_error",
            )
        except Exception:
            if controls.mode is not RetrievalMode.LEXICAL:
                api_error(
                    503,
                    message="Vector retrieval is temporarily unavailable.",
                    param="mode",
                    code="vector_retrieval_unavailable",
                )
            raise

    @app.post(
        "/v1/context",
        response_model=ContextPacket,
        responses=error_docs,
        dependencies=auth_dependencies,
    )
    def context(request: ContextRequest) -> ContextPacket:
        return build_packet(request.query, request)

    @app.post(
        "/v1/chat/completions",
        response_model=ChatCompletionResponse,
        responses=error_docs,
        dependencies=auth_dependencies,
    )
    def chat_completions(request: ChatCompletionRequest) -> ChatCompletionResponse:
        controls = request.context
        if controls is None:
            # Validate defaults from the same schema used for explicit controls.
            from konwledge.api.models import ChatContext

            controls = ChatContext()
        packet = build_packet(request.messages[0].content, controls)
        content = json.dumps(
            packet.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            created=int(time.time()),
            choices=(
                ChatCompletionChoice(
                    index=0,
                    message=ChatResponseMessage(content=content),
                ),
            ),
        )

    return app


__all__ = ["create_app"]
