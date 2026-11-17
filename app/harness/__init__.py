"""Harness 装配：显式配置 + 内置 profile 发现 + API 直连，统一收成一个可选项。

三条来源的 argv 都不是请求体给的：显式模式来自操作者写在 gitignored ``.env`` 里的
常量，发现模式的路径来自文件系统但参数来自 :mod:`app.harness.profiles` 的常量表，
API 模式只吃 ``task``。请求体能选择的只有 profile 名。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
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
from app.harness.detect import scan
from app.harness.profiles import OPENAI_API, PROFILES

if TYPE_CHECKING:
    from app.harness.api import ApiHarness
    from app.settings import Settings


def __getattr__(name: str) -> type["ApiHarness"]:
    """``ApiHarness`` 按需导入。

    它是本包里唯一需要 ``app.providers`` 的成员，而 ``app.providers`` 反指
    ``app.settings``；顶层导入等于让 ``app.settings → app.harness → app.providers →
    app.settings`` 成环，先导入 ``app.settings`` 就会失败。其余成员（装配、发现、
    子进程驱动）都不跨这条边，保持常规导入。
    """
    if name == "ApiHarness":
        from app.harness.api import ApiHarness as impl

        return impl
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ApiHarness",
    "BaseHarness",
    "CommandHarness",
    "HarnessBusy",
    "HarnessError",
    "HarnessRegistry",
    "HarnessRun",
    "HarnessTimeout",
    "HarnessUnavailable",
    "build_harness",
    "default_workdir",
    "resolve_allowed_command",
]

_WORKDIR: Path | None = None


def default_workdir() -> Path:
    """发现式 profile 的兜底工作目录：一个独立的临时空目录。

    不设这一条，子进程会继承服务进程的 cwd——也就是本仓库。即便沙箱只读，
    让外部 agent 在工作树里跑也不是想要的默认值。
    """
    global _WORKDIR
    if _WORKDIR is None:
        _WORKDIR = Path(tempfile.mkdtemp(prefix="optimize-harness-cwd-"))
    return _WORKDIR


class HarnessRegistry(BaseHarness):
    """多个可选 harness 时的选择器；对 ``app.state`` 而言仍是一个 BaseHarness。"""

    name = "registry"

    def __init__(self, members: dict[str, BaseHarness], default: str) -> None:
        if default not in members:
            raise HarnessUnavailable(f"默认 harness profile {default!r} 不可用")
        self.members = members
        self.default = default

    def profiles(self) -> dict[str, BaseHarness]:
        return dict(self.members)

    def select(self, name: str | None) -> BaseHarness:
        if not name:
            return self.members[self.default]
        if name not in self.members:
            raise HarnessUnavailable(f"未知 harness profile {name!r}，可用：{', '.join(self.members)}")
        return self.members[name]

    async def run(self, *, task: str, timeout_s: float) -> HarnessRun:
        return await self.select(None).run(task=task, timeout_s=timeout_s)

    async def aclose(self) -> None:
        for member in self.members.values():
            await member.aclose()


def _explicit(settings: Settings) -> CommandHarness | None:
    """v0.3 的显式模式：命令与参数都写在 ``.env`` 里。"""
    if not settings.harness_command:
        return None
    return CommandHarness(
        command=settings.harness_command,
        args=list(settings.harness_args),
        allowed_commands=list(settings.harness_allowed_commands),
        cwd=settings.harness_cwd or None,
        max_output_bytes=settings.harness_max_output_bytes,
        max_concurrency=settings.harness_max_concurrency,
        env_allow=tuple(settings.harness_env_list),
    )


def _detected(settings: Settings) -> dict[str, BaseHarness]:
    """内置 profile：路径来自扫描结果，argv 来自 profiles 常量表。"""
    members: dict[str, BaseHarness] = {}
    for candidate in scan():
        profile = PROFILES.get(candidate.profile)
        if profile is None or not profile.drivable or candidate.status != "可执行" or not candidate.launch:
            continue
        members[profile.name] = CommandHarness(
            command=candidate.launch[0],
            name=profile.name,
            args=[*candidate.launch[1:], *profile.argv_template],
            allowed_commands=[candidate.launch[0]],
            cwd=settings.harness_cwd or str(default_workdir()),
            max_output_bytes=settings.harness_max_output_bytes,
            max_concurrency=settings.harness_max_concurrency,
            env_allow=tuple(settings.harness_env_list),
        )
    return members


def _api(settings: Settings) -> ApiHarness | None:
    """API 形态仅在密钥存在时注册；缺密钥不得让启动失败。"""
    if not settings.openai_api_key:
        return None
    # 两个导入都留在这里：app.harness.api 经 app.providers 反指 app.settings，
    # 放到模块顶层会让 `import app.settings` 成为死路（见 __getattr__ 的说明）。
    from app.harness.api import ApiHarness
    from app.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        base_url=settings.openai_base_url,
    )
    return ApiHarness(
        provider,
        max_output_bytes=settings.harness_max_output_bytes,
        max_concurrency=settings.harness_max_concurrency,
        system_prompt=OPENAI_API.system_prompt,
    )


def build_harness(settings: Settings) -> BaseHarness | None:
    if not settings.harness_enabled:
        return None
    allowed = {name.strip() for name in settings.harness_providers.split(",") if name.strip()}

    def enabled(key: str) -> bool:
        return not allowed or key in allowed

    members: dict[str, BaseHarness] = {}
    if enabled("command"):
        explicit = _explicit(settings)
        if explicit is not None:
            members["command"] = explicit
    if settings.harness_detect:
        for key, harness in _detected(settings).items():
            if enabled(key):
                members[key] = harness
    if enabled(OPENAI_API.name):
        api = _api(settings)
        if api is not None:
            members[OPENAI_API.name] = api

    if not members:
        return None
    if len(members) == 1:
        return next(iter(members.values()))
    default = settings.harness_profile if settings.harness_profile in members else next(iter(members))
    return HarnessRegistry(members, default)
