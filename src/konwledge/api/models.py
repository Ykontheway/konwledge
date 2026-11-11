"""Strict request and response schemas for the optional HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from konwledge.context import ContextPacket
from konwledge.retrieval import RetrievalMode

MODEL_ID: Literal["knowledge-context"] = "knowledge-context"
MAX_QUERY_CHARS = 100_000


class APIModel(BaseModel):
    """Immutable API value that never accepts undeclared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ContextControls(APIModel):
    """Retrieval and assembly controls shared by both context routes.

    The core currently has a singular ``source_id`` filter, so the transport's
    forward-compatible ``source_ids`` array is explicitly limited to one item.
    """

    source_ids: tuple[UUID, ...] | None = Field(default=None, max_length=1)
    as_of: AwareDatetime | None = None
    mode: RetrievalMode = RetrievalMode.LEXICAL
    top_k: int = Field(default=5, strict=True, gt=0, le=100)
    candidate_limit: int | None = Field(default=None, strict=True, gt=0, le=1000)
    rrf_k: int = Field(default=60, strict=True, gt=0, le=100_000)
    max_chars: int = Field(default=8000, strict=True, ge=0, le=100_000)
    max_units: int = Field(default=10, strict=True, gt=0, le=100)
    include_stale: bool = Field(default=False, strict=True)

    @field_validator("as_of", mode="before")
    @classmethod
    def reject_numeric_datetime(cls, value: Any) -> Any:
        """Permit ISO strings but not Unix timestamps or booleans."""

        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("as_of must be an ISO 8601 timestamp")
        return value

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> Self:
        if self.candidate_limit is not None and self.candidate_limit < self.top_k:
            raise ValueError("candidate_limit must be greater than or equal to top_k")
        return self

    @property
    def source_id(self) -> UUID | None:
        """Translate the bounded public array to the core's singular filter."""

        if not self.source_ids:
            return None
        return self.source_ids[0]


class ContextRequest(ContextControls):
    """Native request for a read-only :class:`ContextPacket`."""

    query: str = Field(strict=True, min_length=1, max_length=MAX_QUERY_CHARS)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must contain non-whitespace text")
        return value


class ChatContext(ContextControls):
    """Optional chat retrieval controls; the user message supplies the query."""


class ChatMessage(APIModel):
    """The single supported OpenAI-compatible input message."""

    role: Literal["user"]
    content: str = Field(strict=True, min_length=1, max_length=MAX_QUERY_CHARS)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must contain non-whitespace text")
        return value


class ChatCompletionRequest(APIModel):
    """Deliberately small, non-generative subset of chat completions."""

    model: Literal["knowledge-context"]
    messages: tuple[ChatMessage, ...] = Field(min_length=1, max_length=1)
    stream: bool = Field(default=False, strict=True)
    context: ChatContext | None = None

    @field_validator("stream")
    @classmethod
    def reject_streaming(cls, value: bool) -> bool:
        if value:
            raise ValueError("streaming is not supported")
        return value


class HealthResponse(APIModel):
    status: Literal["ok"]
    version: str


class ModelInfo(APIModel):
    id: Literal["knowledge-context"]
    object: Literal["model"] = "model"
    created: int = Field(strict=True, ge=0)
    owned_by: Literal["konwledge"] = "konwledge"


class ModelListResponse(APIModel):
    object: Literal["list"] = "list"
    data: tuple[ModelInfo, ...]


class ChatResponseMessage(APIModel):
    role: Literal["assistant"] = "assistant"
    content: str


class ChatCompletionChoice(APIModel):
    index: int = Field(strict=True, ge=0)
    message: ChatResponseMessage
    finish_reason: Literal["stop"] = "stop"


class ChatCompletionResponse(APIModel):
    id: str = Field(pattern=r"^chatcmpl-[0-9a-f]{32}$")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(strict=True, ge=0)
    model: Literal["knowledge-context"] = "knowledge-context"
    choices: tuple[ChatCompletionChoice, ...]


class APIError(APIModel):
    message: str
    type: str
    param: str | None = None
    code: str | None = None


class APIErrorResponse(APIModel):
    error: APIError


__all__ = [
    "MAX_QUERY_CHARS",
    "MODEL_ID",
    "APIError",
    "APIErrorResponse",
    "ChatCompletionChoice",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatContext",
    "ChatMessage",
    "ChatResponseMessage",
    "ContextControls",
    "ContextPacket",
    "ContextRequest",
    "HealthResponse",
    "ModelInfo",
    "ModelListResponse",
]
