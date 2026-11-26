"""采集接入流控中间件。

公开入口：
    Engine   纯内存流控引擎
    Journal  事件日志 + 快照，支持 restart 后保持已授信用与实际接受的差异
    run_scenario / run_file / format_trace  本地演练驱动
"""

from .engine import (
    ESSENTIAL,
    NORMAL,
    CONN_OPEN,
    CONN_STOPPED,
    CONN_SWITCHING,
    FRAG_ADMITTED,
    FRAG_COMPLETED,
    FRAG_REJECTED,
    CreditAccount,
    Engine,
    FragmentRecord,
    GroupState,
    Occupancy,
    SourceState,
)
from .journal import Journal
from .runner import format_trace, run_file, run_scenario

__all__ = [
    "ESSENTIAL", "NORMAL",
    "CONN_OPEN", "CONN_STOPPED", "CONN_SWITCHING",
    "FRAG_ADMITTED", "FRAG_COMPLETED", "FRAG_REJECTED",
    "CreditAccount", "Engine", "FragmentRecord", "GroupState", "Occupancy",
    "SourceState", "Journal", "format_trace", "run_file", "run_scenario",
]
