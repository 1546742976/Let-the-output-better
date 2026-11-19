"""步骤上下文：贯穿管线的可变状态，字段命名与 README §5 的契约一致。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.providers.base import BaseProvider
from app.settings import PipelineConfig, Settings


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)

    def as_dict(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}


@dataclass
class StepContext:
    raw_output: str
    settings: Settings
    config: PipelineConfig
    provider: BaseProvider | None = None
    schema: type[BaseModel] | None = None
    instruction: str = ""
    input_text: str = ""

    parsed: dict[str, Any] | None = None
    model: Any = None
    text: str | None = None
    errors: list[str] = field(default_factory=list)
    dimensions: dict[str, float | None] = field(default_factory=dict)
    markers: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    valid: bool = False
    score: float | None = None
    repaired: bool = False
    timeout_s: float = 8.0
    deadline: float = 0.0

    def remaining_s(self) -> float:
        if not self.deadline:
            return self.timeout_s
        return max(0.0, self.deadline - time.monotonic())

    def snapshot(self) -> dict[str, Any]:
        """供修复/重写前后比对的结果快照。"""
        return {"parsed": self.parsed, "text": self.text, "valid": self.valid, "score": self.score}

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.parsed = snapshot["parsed"]
        self.text = snapshot["text"]
        self.valid = snapshot["valid"]
        self.score = snapshot["score"]
