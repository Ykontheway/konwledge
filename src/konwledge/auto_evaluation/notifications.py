"""Privacy-safe CLI notices for jobs observed around a knowledge mutation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from konwledge.auto_evaluation.models import AutoEvaluationJob, AutoEvaluationStatus

_TERMINAL_SUCCESS = frozenset({AutoEvaluationStatus.PASSED})


@dataclass(frozen=True, slots=True)
class AutoEvaluationNotice:
    """Compact status-only notice that never exposes evaluation inputs or evidence."""

    jobs: tuple[AutoEvaluationJob, ...]

    @property
    def passed(self) -> bool:
        """Whether every newly triggered job reached the sole successful state."""

        return bool(self.jobs) and all(job.status in _TERMINAL_SUCCESS for job in self.jobs)

    @property
    def summary(self) -> str:
        counts = {status: 0 for status in AutoEvaluationStatus}
        for job in self.jobs:
            counts[job.status] += 1
        rendered = " ".join(f"{status.value}={counts[status]}" for status in AutoEvaluationStatus)
        return f"Automatic evaluation: total={len(self.jobs)} {rendered}"

    @property
    def warning(self) -> str | None:
        if self.passed:
            return None
        unsuccessful = ",".join(
            f"{job.id}:{job.status.value}"
            + ("" if job.error_code is None else f":{job.error_code}")
            for job in self.jobs
            if job.status is not AutoEvaluationStatus.PASSED
        )
        return "Warning: knowledge committed; evaluation did not pass" + (
            "" if not unsuccessful else f" ({unsuccessful})"
        )


def notification_for(
    jobs: tuple[AutoEvaluationJob, ...],
) -> AutoEvaluationNotice | None:
    """Return no notice for disabled/no-op commands, preserving existing CLI output."""

    return None if not jobs else AutoEvaluationNotice(jobs=jobs)


def notify_cli(
    jobs: tuple[AutoEvaluationJob, ...],
    *,
    stderr: Callable[[str], None],
    json_output: bool = False,
) -> bool:
    """Emit status-only stderr notices and report whether the CLI should exit 3.

    JSON and ordinary stdout are deliberately untouched because every notice is
    written through the supplied stderr callback. A non-passing command receives
    both the summary and the explicit post-commit warning. SDK callers do not call
    this adapter and therefore continue to return normally.
    """

    notice = notification_for(jobs)
    if notice is None:
        return False
    stderr(notice.summary)
    warning = notice.warning
    if warning is not None:
        stderr(warning)
        return True
    return False
