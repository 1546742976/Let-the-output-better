"""API 形态的 harness：不经子进程，直接打 OpenAI 兼容端点取最终回答。

组合而非继承 ``OpenAICompatibleProvider``：那套 httpx 逻辑（base_url 拼接、
``response_format``、usage、错误映射）已有专门的 respx 测试覆盖，抽公共层属于纯重构，
收益低于回归风险；这里只做语义适配。
"""

from __future__ import annotations

import time

from app.harness.base import BaseHarness, HarnessBusy, HarnessError, HarnessRun, HarnessTimeout
from app.providers.base import BaseProvider, ProviderError, ProviderTimeout

DEFAULT_SYSTEM_PROMPT = "你是被测的 agent harness：请按任务要求直接给出最终回答，不要输出执行过程。"


class ApiHarness(BaseHarness):
    name = "openai-api"

    def __init__(
        self,
        provider: BaseProvider,
        *,
        max_output_bytes: int = 262144,
        max_concurrency: int = 1,
        system_prompt: str = "",
        label: str | None = None,
    ) -> None:
        self.name = label or self.name
        self.provider = provider
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self.max_concurrency = max(1, int(max_concurrency))
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._in_flight = 0

    @property
    def target(self) -> str:
        return str(getattr(self.provider, "model", "") or "")

    async def run(self, *, task: str, timeout_s: float) -> HarnessRun:
        if self._in_flight >= self.max_concurrency:
            raise HarnessBusy(f"harness 并发已达上限（{self.max_concurrency}），请稍后再试")
        self._in_flight += 1
        started = time.perf_counter()
        try:
            response = await self.provider.chat(
                system=self.system_prompt, user=task, task="harness", timeout_s=timeout_s
            )
        except ProviderTimeout as exc:
            raise HarnessTimeout(f"harness API 超时（{timeout_s:.1f}s）") from exc
        except ProviderError as exc:
            raise HarnessError(f"harness API 调用失败：{exc}") from exc
        finally:
            self._in_flight -= 1

        raw = response.text or ""
        if not raw.strip():
            raise HarnessError("harness API 返回空内容")
        encoded = raw.encode("utf-8")
        truncated = len(encoded) > self.max_output_bytes
        text = encoded[: self.max_output_bytes].decode("utf-8", errors="ignore")
        return HarnessRun(
            text=text,
            ms=int((time.perf_counter() - started) * 1000),
            # 无进程形态没有退出码，保持 None 而不是伪造成 0。
            exit_code=None,
            kind="api",
            source="chat_completions",
            truncated=truncated,
            bytes=len(encoded),
            target=self.target,
        )

    async def aclose(self) -> None:
        await self.provider.aclose()
