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
    ms: int
    #: 子进程退出码；无进程形态（API 直连）为 None，不谎报 0。
    exit_code: int | None = None
    kind: str = "subprocess"
    source: str = "stdout"
    truncated: bool = False
    stderr_tail: str = ""
    bytes: int = 0
    #: 解析后的绝对执行目标，供审计留痕（路径来自发现结果，需可追溯）。
    target: str = ""

    def as_audit(self, name: str) -> dict[str, Any]:
        """审计摘要：不落 stderr 与正文，只记来源、身份与代价。"""
        audit: dict[str, Any] = {
            "name": name,
            "kind": self.kind,
            "exit_code": self.exit_code,
            "ms": self.ms,
            "source": self.source,
            "truncated": self.truncated,
            "bytes": self.bytes,
        }
        if self.target:
            audit["target"] = self.target
        return audit


class ConcurrencyBudget:
    """进程级并发预算。

    要保护的是"本机同时存在的 agent 调用数与账号速率"，那是进程级资源；每个成员
    各自计数时真实上限变成"成员数 × HARNESS_MAX_CONCURRENCY"，而对外承诺的
    ``429`` 说的却是"并发超限"这一件事。
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.in_flight = 0

    def acquire(self) -> None:
        if self.in_flight >= self.limit:
            raise HarnessBusy(f"harness 并发已达上限（{self.limit}），请稍后再试")
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight -= 1


class BaseHarness(ABC):
    name = "base"

    @abstractmethod
    async def run(self, *, task: str, timeout_s: float) -> HarnessRun:
        """执行一次 harness 调用并返回其最终回答，失败时抛 ``HarnessError``。"""

    async def aclose(self) -> None:
        return None
