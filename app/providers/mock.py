"""离线 mock provider：无网络、无密钥即可跑通管线、测试与评估。

它不是“假装成功”，而是用确定性规则扮演一个模型：修复=只回 JSON，
拟人化=按套话对照表压缩表述。因此金标评估结果可复现。
"""

from __future__ import annotations

import asyncio
import json

from app.providers.base import BaseProvider, ChatResponse
from app.textio import extract_json

REPAIR_MARK = "待修复输出："
HUMANIZE_MARK = "待改写文本："

# 空泛套话 → 简洁表述；只动措辞，不触碰数值与引用。
BOILERPLATE = (
    ("值得注意的是，", ""),
    ("值得注意，", ""),
    ("需要指出的是，", ""),
    ("不难发现，", ""),
    ("众所周知，", ""),
    ("综上所述，", ""),
    ("总而言之，", ""),
    ("实验结果充分表明", "实验表明"),
    ("结果充分表明", "结果表明"),
    ("实验充分证明", "实验表明"),
    ("充分表明", "表明"),
    ("具有显著的性能优势和广阔的应用前景", "性能优于对比方法"),
    ("具有显著的性能优势", "性能优于对比方法"),
    ("广阔的应用前景", "适用范围较广"),
    ("具有重要的理论意义和应用价值", ""),
    ("具有重要意义", ""),
    ("在一定程度上", ""),
    ("本文所提出的方法", "本文方法"),
    ("我们创新性地提出", "我们提出"),
)


def repair_payload(prompt: str) -> str:
    """扮演修复模型：只输出 JSON 对象本身，不带围栏与客套说明。"""
    segment = prompt.rsplit(REPAIR_MARK, 1)[-1] if REPAIR_MARK in prompt else prompt
    data = extract_json(segment)
    if data is None:
        return "{}"
    return json.dumps(data, ensure_ascii=False)


def condense_text(prompt: str) -> str:
    """扮演重写模型：剔除空泛套话，保留全部事实。"""
    segment = prompt.rsplit(HUMANIZE_MARK, 1)[-1] if HUMANIZE_MARK in prompt else prompt
    text = segment.strip()
    for boilerplate, replacement in BOILERPLATE:
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
        elif task == "humanize":
            text = condense_text(user)
        else:
            text = user.strip()
        return ChatResponse(text=text, prompt_tokens=0, completion_tokens=0)
