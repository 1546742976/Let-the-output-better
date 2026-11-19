"""进程内运行进度登记：只供本地查看页轮询解释"现在卡在哪一段"，不参与优化语义。

单进程事件循环内的字典即够（uvicorn 单 worker、协程间无抢占式并发）；多 worker
部署时每个进程各自登记，页面轮询落到哪个进程就看到哪个——本地查看页本就绑定
loopback 单进程，不为假想的分布式部署加锁或共享存储。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunState:
    run_id: str
    kind: str
    phase: str
    started: float
    done_steps: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "phase": self.phase,
            "done_steps": list(self.done_steps),
            "detail": self.detail,
            "elapsed_ms": int((time.monotonic() - self.started) * 1000),
        }


_active: dict[str, RunState] = {}
_last: RunState | None = None


def begin(run_id: str, kind: str, phase: str) -> RunState:
    state = RunState(run_id=run_id, kind=kind, phase=phase, started=time.monotonic())
    _active[run_id] = state
    return state


def phase(run_id: str, phase: str, detail: str = "") -> None:
    state = _active.get(run_id)
    if state is not None:
        state.phase = phase
        state.detail = detail


def step_done(run_id: str, step: str) -> None:
    state = _active.get(run_id)
    if state is not None and step not in state.done_steps:
        state.done_steps.append(step)


def finish(run_id: str, *, ok: bool = True, detail: str = "") -> None:
    global _last
    state = _active.pop(run_id, None)
    if state is None:
        return
    state.phase = "done" if ok else "failed"
    state.detail = detail
    _last = state


def snapshot() -> dict[str, Any]:
    """active 按开始顺序（旧→新）；last 是最近一次结束的运行，供轮询收尾对齐。"""
    return {
        "active": [state.as_dict() for state in _active.values()],
        "last": _last.as_dict() if _last is not None else None,
    }


def reset() -> None:
    """测试隔离用：清空登记，避免用例间串味。"""
    global _last
    _active.clear()
    _last = None
