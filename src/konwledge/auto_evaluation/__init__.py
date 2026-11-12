"""Automatic quality evaluation after committed knowledge revisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from konwledge.auto_evaluation.models import (
        AutoEvaluationConfig,
        AutoEvaluationJob,
        AutoEvaluationStatus,
    )
    from konwledge.auto_evaluation.service import (
        AutoEvaluationError,
        AutoEvaluationNotFoundError,
        AutoEvaluationService,
    )

__all__ = [
    "AutoEvaluationConfig",
    "AutoEvaluationError",
    "AutoEvaluationJob",
    "AutoEvaluationNotFoundError",
    "AutoEvaluationService",
    "AutoEvaluationStatus",
]


def __getattr__(name: str) -> Any:
    if name in {"AutoEvaluationConfig", "AutoEvaluationJob", "AutoEvaluationStatus"}:
        from konwledge.auto_evaluation import models

        return getattr(models, name)
    if name in {
        "AutoEvaluationError",
        "AutoEvaluationNotFoundError",
        "AutoEvaluationService",
    }:
        from konwledge.auto_evaluation import service

        return getattr(service, name)
    raise AttributeError(name)
