"""步骤实现与步骤注册表：``name`` 与 ``config/pipeline.yaml`` 中的登记项对应。"""

from __future__ import annotations

from app.optimizer.steps.base import BaseStep

REGISTRY: dict[str, type[BaseStep]] = {}


def register_step(cls: type[BaseStep]) -> type[BaseStep]:
    """登记步骤类，使自定义步骤可在 ``pipeline.yaml`` 中按 ``name`` 引用。"""
    REGISTRY[cls.name] = cls
    return cls


# 导入即注册；顺序无关，仅需保证包初始化后内置步骤全部可用。
from app.optimizer.steps import (  # noqa: E402,F401
    format,
    humanize,
    repair,
    rules,
    schema,
)
