"""Harness 适配层接口：驱动上游 harness，产出一段待二次优化的原始输出。

这一层位于 post-hoc 契约的**外侧**：它等价于"调用方"，只是调用方从业务代码换成
页面按钮。``optimize()`` 自身的职责边界不变——它只处理拿到的文本，不参与生成。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class HarnessError(RuntimeError):
    """harness 驱动失败，调用方应保留错误详情且不要把失败输出送进优化管线。"""


class HarnessUnavailable(HarnessError):
    """驱动未启用、命令缺失或不在白名单内。"""


class HarnessTimeout(HarnessError):
    """harness 在时间预算内没有结束。"""


class HarnessBusy(HarnessError):
    """并发已达上限。"""


@dataclass
class HarnessRun:
    text: str
    exit_code: int
    ms: int
    source: str = "stdout"
    truncated: bool = False
    stderr_tail: str = ""
    bytes: int = 0

    def as_audit(self, name: str) -> dict[str, Any]:
        """审计摘要：不落 stderr 与正文，只记来源与代价。"""
        return {
            "name": name,
            "exit_code": self.exit_code,
            "ms": self.ms,
            "source": self.source,
            "truncated": self.truncated,
            "bytes": self.bytes,
        }


class BaseHarness(ABC):
    name = "base"

    @abstractmethod
    async def run(self, *, task: str, timeout_s: float) -> HarnessRun:
        """执行一次 harness 调用并返回其最终回答，失败时抛 ``HarnessError``。"""

    async def aclose(self) -> None:
        return None
