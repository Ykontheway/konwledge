"""薄引用包装器，用于重用已安装的验收运行逻辑。

实际逻辑已移至 konwledge.quality.acceptance 以支持在非仓库路径下运行。
"""

from konwledge.quality.acceptance import (
    PerformanceResult,
    populate_corpus,
    run_performance,
    run_quality,
)

__all__ = [
    "PerformanceResult",
    "populate_corpus",
    "run_performance",
    "run_quality",
]
