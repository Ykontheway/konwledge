"""Optional FastAPI transport and its explicit public schemas."""

from __future__ import annotations

from konwledge.api.app import create_app
from konwledge.api.models import (
    MODEL_ID,
    APIError,
    APIErrorResponse,
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatContext,
    ChatMessage,
    ChatResponseMessage,
    ContextControls,
    ContextRequest,
    HealthResponse,
    ModelInfo,
    ModelListResponse,
)
from konwledge.context import ContextPacket

__all__ = [
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
    "create_app",
]
