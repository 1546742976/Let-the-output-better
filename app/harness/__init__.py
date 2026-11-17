"""Harness 工厂：默认关闭，未启用时返回 ``None``，页面与接口据此降级。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.harness.base import (
    BaseHarness,
    HarnessBusy,
    HarnessError,
    HarnessRun,
    HarnessTimeout,
    HarnessUnavailable,
)
from app.harness.command import CommandHarness, resolve_allowed_command

if TYPE_CHECKING:
    from app.settings import Settings

__all__ = [
    "BaseHarness",
    "CommandHarness",
    "HarnessBusy",
    "HarnessError",
    "HarnessRun",
    "HarnessTimeout",
    "HarnessUnavailable",
    "resolve_allowed_command",
    "build_harness",
]


def build_harness(settings: Settings) -> BaseHarness | None:
    if not settings.harness_enabled:
        return None
    return CommandHarness(
        command=settings.harness_command,
        args=list(settings.harness_args),
        allowed_commands=list(settings.harness_allowed_commands),
        cwd=settings.harness_cwd,
        max_output_bytes=settings.harness_max_output_bytes,
        max_concurrency=settings.harness_max_concurrency,
        env_allow=tuple(settings.harness_env_list),
    )
