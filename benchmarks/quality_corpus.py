"""薄引用包装器，用于重用已安装的 30-case 验收语料库定义。

实际定义已移至 konwledge.quality.benchmark_corpus 以支持在非仓库路径下运行。
"""

from konwledge.quality.benchmark_corpus import (
    CASES,
    CREATED_AT,
    CURRENT_DOCUMENT_ID,
    HISTORICAL_DOCUMENT_ID,
    SET_ID,
    SOURCE_ID,
    build_evaluation_set,
    stable_id,
)

__all__ = [
    "CASES",
    "CREATED_AT",
    "CURRENT_DOCUMENT_ID",
    "HISTORICAL_DOCUMENT_ID",
    "SET_ID",
    "SOURCE_ID",
    "build_evaluation_set",
    "stable_id",
]
