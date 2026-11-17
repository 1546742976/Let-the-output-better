"""Provider 统一接口：优化器只在修复与通俗化时调用模型。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(RuntimeError):
    """模型调用失败，由管线按 fail-open 处理。"""


class ProviderTimeout(ProviderError):
    """模型调用超时。"""


@dataclass
class ChatResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class BaseProvider(ABC):
    name = "base"

    @abstractmethod
    async def chat(
        self,
        *,
        system: str,
        user: str,
        task: str = "generic",
        json_mode: bool = False,
        timeout_s: float = 6.0,
    ) -> ChatResponse:
        """执行一次补全调用，失败时抛出 ``ProviderError``。"""

    async def aclose(self) -> None:
        return None
