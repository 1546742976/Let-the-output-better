"""离线 mock provider：无网络、无密钥即可跑通管线、测试与评估。

它不是"假装成功"，而是用确定性规则扮演一个模型：修复=只回 JSON，
通俗化=按对照表压缩措辞。词表只有一份，住在零依赖的 `app/plain.py`，
因此金标评估结果可复现且与评分口径同源。
"""

from __future__ import annotations

import asyncio
import json

from app.plain import rewrite_table
from app.providers.base import BaseProvider, ChatResponse
from app.textio import extract_json

REPAIR_MARK = "待修复输出："
SIMPLIFY_MARK = "待改写文本："


def repair_payload(prompt: str) -> str:
    """扮演修复模型：只输出 JSON 对象本身，不带围栏与客套说明。"""
    segment = prompt.rsplit(REPAIR_MARK, 1)[-1] if REPAIR_MARK in prompt else prompt
    data = extract_json(segment)
    if data is None:
        return "{}"
    return json.dumps(data, ensure_ascii=False)


def simplify_text(prompt: str) -> str:
    """扮演通俗化模型：删套话、换直白说法，不动数值与引用。"""
    segment = prompt.rsplit(SIMPLIFY_MARK, 1)[-1] if SIMPLIFY_MARK in prompt else prompt
    text = segment.strip()
    for boilerplate, replacement in rewrite_table():
        text = text.replace(boilerplate, replacement)
    text = text.replace("，。", "。").replace("。。", "。")
    while "，，" in text:
        text = text.replace("，，", "，")
    text = text.strip(" ，,;；")
    if segment.rstrip().endswith("。") and text and not text.endswith(("。", "！", "？", ".", "!", "?")):
        text += "。"
    return text


class MockProvider(BaseProvider):
    name = "mock"

    def __init__(self, *, reply: str | None = None, delay_s: float = 0.0) -> None:
        self.reply = reply
        self.delay_s = delay_s
        self.calls: list[str] = []

    async def chat(
        self,
        *,
        system: str,
        user: str,
        task: str = "generic",
        json_mode: bool = False,
        timeout_s: float = 6.0,
    ) -> ChatResponse:
        self.calls.append(task)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.reply is not None:
            text = self.reply
        elif task == "repair":
            text = repair_payload(user)
        elif task == "simplify":
            text = simplify_text(user)
        else:
            text = user.strip()
        return ChatResponse(text=text, prompt_tokens=0, completion_tokens=0)
