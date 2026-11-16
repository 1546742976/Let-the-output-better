"""文本工具：Markdown 围栏剥离、JSON 抽取与空白归一，供步骤与 mock provider 共用。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL)
_FENCE_ANYWHERE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n(?P<body>.*?)```", re.DOTALL)


def strip_markdown_fences(text: str) -> str:
    """剥离包裹整段或内嵌的代码围栏，保留其中的内容。"""
    body = text.strip()
    match = _FENCE.match(body)
    if match:
        return match.group("body").strip()
    embedded = _FENCE_ANYWHERE.search(body)
    if embedded:
        return embedded.group("body").strip()
    return re.sub(r"```[a-zA-Z0-9_+-]*", "", body).strip() if body.startswith("```") else body


def _balanced_slice(text: str, open_char: str, close_char: str) -> str | None:
    start = text.find(open_char)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json(text: str) -> Any | None:
    """从模型输出中取出 JSON：直接解析 → 剥围栏 → 截取首个平衡块。"""
    for candidate in (text.strip(), strip_markdown_fences(text.strip())):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            block = _balanced_slice(candidate, opener, closer)
            if block:
                try:
                    return json.loads(block)
                except ValueError:
                    continue
    return None


def trim_whitespace(text: str) -> str:
    """压缩行内多余空白、去掉每行尾随空格与首尾空行。"""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def load_prompt(path: str | None, *, fallback: str = "") -> str:
    """读取提示词文件；缺失时回落到 ``fallback``，不阻断管线。"""
    if not path:
        return fallback
    candidate = Path(path)
    if not candidate.exists():
        return fallback
    return candidate.read_text(encoding="utf-8").strip()


def render_prompt(template: str, **values: str) -> str:
    """以 ``{name}`` 占位符填充提示词，避免 ``str.format`` 被 JSON 花括号干扰。"""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value if value is not None else "")
    return rendered
