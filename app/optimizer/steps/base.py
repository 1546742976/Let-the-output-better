"""步骤契约（README §5）：实现 ``run(ctx)`` 并在 ``config/pipeline.yaml`` 登记即可生效。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.settings import Settings, StepConfig

if TYPE_CHECKING:
    from app.optimizer.context import StepContext


@dataclass
class StepResult:
    name: str
    passed: bool
    score: float | None = None
    reason: str = ""
    ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name, "passed": self.passed, "ms": self.ms}
        if self.score is not None:
            result["score"] = self.score
        if self.reason:
            result["reason"] = self.reason
        return result


class BaseStep:
    name = "base"

    def __init__(self, config: StepConfig | None = None, settings: Settings | None = None) -> None:
        self.config = config
        self.settings = settings

    @staticmethod
    def elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def should_run(self, ctx: StepContext) -> bool:
        """未触发的步骤不记入 ``steps``，与 README §7.2 的响应形态一致。"""
        return True

    async def run(self, ctx: StepContext) -> StepResult:  # pragma: no cover - 抽象方法
        raise NotImplementedError
