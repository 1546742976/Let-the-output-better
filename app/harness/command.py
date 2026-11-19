"""按服务端白名单驱动一条命令作为 harness。

安全边界（本文件的存在理由）：

- 命令、参数、工作目录与环境变量**只来自配置**；调用方只提供 ``task`` 文本，
  且 ``task`` 经 stdin 传入，绝不拼进 argv，也不使用 shell。
- 可执行文件必须是绝对路径、真实存在的文件，且在 ``allowed_commands`` 白名单内。
  按 basename 放行等于允许 ``python -c``，因此不作妥协。
- 子进程环境是重建的最小集合，不继承 ``os.environ``；把变量名加进
  ``env_allow`` 意味着它可能带着凭据进入子进程，密钥类变量不要加。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import tempfile
import time
from pathlib import Path

from app.harness.base import (
    BaseHarness,
    ConcurrencyBudget,
    HarnessError,
    HarnessRun,
    HarnessTimeout,
    HarnessUnavailable,
)

OUTPUT_FILE_PLACEHOLDER = "{output_file}"
STDERR_TAIL_BYTES = 4096
READ_CHUNK = 65536
REAP_TIMEOUT_S = 5.0
# 典型 CLI 正常运行所需、且不含密钥的基础变量。
BASE_ENV_NAMES = (
    "PATH",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)


def resolve_allowed_command(command: str, allowed_commands: list[str]) -> Path:
    """校验并解析可执行文件；不在白名单或不是文件时抛 ``HarnessUnavailable``。"""
    if not command or not command.strip():
        raise HarnessUnavailable("HARNESS_COMMAND 为空，无法驱动 harness")
    raw = Path(command)
    if not raw.is_absolute():
        raise HarnessUnavailable(f"HARNESS_COMMAND 必须是绝对路径，当前为 {command!r}")
    resolved = raw.resolve()
    if not resolved.is_file():
        raise HarnessUnavailable(f"HARNESS_COMMAND 不存在或不是文件：{resolved}")
    allowed = {Path(item).resolve() for item in allowed_commands if item and item.strip()}
    if resolved not in allowed:
        raise HarnessUnavailable(f"HARNESS_COMMAND {resolved} 不在 HARNESS_ALLOWED_COMMANDS 白名单内")
    return resolved


def build_child_env(env_allow: tuple[str, ...]) -> dict[str, str]:
    """重建子进程环境：基础变量 + 显式允许项，绝不整体继承。"""
    env = {name: value for name in (*BASE_ENV_NAMES, *env_allow) if (value := os.environ.get(name)) is not None}
    # 子进程若也是 Python，避免 Windows 下按 GBK 写 stdout 造成解码噪声。
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


class CommandHarness(BaseHarness):
    name = "command"

    def __init__(
        self,
        *,
        command: str,
        name: str | None = None,
        args: list[str] | None = None,
        allowed_commands: list[str] | None = None,
        cwd: str | None = None,
        max_output_bytes: int = 262144,
        max_concurrency: int = 1,
        env_allow: tuple[str, ...] = (),
        budget: ConcurrencyBudget | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self.command = command
        self.args = list(args or [])
        self.allowed_commands = list(allowed_commands or [])
        self.cwd = cwd or None
        self.max_output_bytes = max(1024, int(max_output_bytes))
        # 不传预算时自建一份：默认行为与逐实例计数完全一致，共享预算由装配方决定。
        self.budget = budget or ConcurrencyBudget(max_concurrency)
        self.env_allow = tuple(env_allow)
        #: 最近一次子进程句柄。留着它是为了让"取消/异常之后进程到底死没死"可被断言——
        #: 这类泄漏在功能上完全静默，只能靠回收后的 returncode 观察。
        self.last_process: asyncio.subprocess.Process | None = None

    @property
    def max_concurrency(self) -> int:
        return self.budget.limit

    @property
    def _in_flight(self) -> int:
        return self.budget.in_flight

    @property
    def uses_output_file(self) -> bool:
        return any(OUTPUT_FILE_PLACEHOLDER in arg for arg in self.args)

    def _argv(self, output_file: Path) -> list[str]:
        exe = resolve_allowed_command(self.command, self.allowed_commands)
        rendered = [arg.replace(OUTPUT_FILE_PLACEHOLDER, str(output_file)) for arg in self.args]
        return [str(exe), *rendered]

    async def run(self, *, task: str, timeout_s: float) -> HarnessRun:
        # 先做白名单校验，避免被拒的调用占用并发额度。
        resolve_allowed_command(self.command, self.allowed_commands)
        self.budget.acquire()
        started = time.perf_counter()
        try:
            return await self._execute(task, timeout_s, started)
        finally:
            self.budget.release()

    async def _execute(self, task: str, timeout_s: float, started: float) -> HarnessRun:
        with tempfile.TemporaryDirectory(prefix="optimize-harness-") as workdir:
            output_file = Path(workdir) / "last_message.txt"
            argv = self._argv(output_file)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=self.cwd,
                    env=build_child_env(self.env_allow),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                raise HarnessError(f"无法启动 harness：{exc}") from exc

            self.last_process = proc
            try:
                stdout, stderr, truncated = await self._exchange(proc, task, timeout_s)
            except TimeoutError as exc:
                raise HarnessTimeout(f"harness 超时（{timeout_s:.1f}s），已终止子进程") from exc
            except HarnessError:
                raise
            except Exception as exc:
                # 管道、编码等意外失败也收成 HarnessError：/v1/run 不该把裸异常
                # 变成 500，而这个服务的既有语义就是"失败给 5xx 且带原因"。
                raise HarnessError(f"harness 执行异常：{type(exc).__name__}: {exc}") from exc
            finally:
                # 回收放在 finally：此前只有 TimeoutError/OSError 两条路会终止子进程，
                # 外层取消或任何其他异常都会留下还在跑的 agent 进程，而并发额度已归还。
                await self._terminate(proc)

            exit_code = proc.returncode if proc.returncode is not None else -1
            if exit_code != 0:
                raise HarnessError(
                    f"harness 退出码 {exit_code}：{_redact(_tail_text(stderr, STDERR_TAIL_BYTES)) or '子进程无 stderr 输出'}"
                )

            text, source, data_bytes, cut = self._final_answer(output_file, stdout, truncated)
            if not text.strip():
                raise HarnessError("harness 未产出可用文本（stdout 与输出文件均为空）")
            return HarnessRun(
                text=text,
                exit_code=exit_code,
                ms=int((time.perf_counter() - started) * 1000),
                kind="subprocess",
                source=source,
                truncated=cut,
                stderr_tail=_redact(_tail_text(stderr, STDERR_TAIL_BYTES)),
                bytes=data_bytes,
                target=argv[1] if len(argv) > 1 else argv[0],
            )

    async def _exchange(
        self, proc: asyncio.subprocess.Process, task: str, timeout_s: float
    ) -> tuple[bytes, bytes, bool]:
        """并发喂 stdin 与排空两条管道，任一超限即标记截断但继续读干，避免死锁。

        用 `gather` 而不是手工 `create_task`：超时/取消时 gather 会把取消广播给每个
        子任务，孤儿的 feed 任务从结构上不可能出现（旧写法要等进程退出才轮到它）。
        """
        async with asyncio.timeout(max(0.1, timeout_s)):
            _, (stdout, out_truncated), (stderr, _) = await asyncio.gather(
                self._feed(proc.stdin, task),
                self._drain(proc.stdout, self.max_output_bytes),
                self._drain(proc.stderr, STDERR_TAIL_BYTES),
            )
            await proc.wait()
        return stdout, stderr, out_truncated

    @staticmethod
    async def _feed(stdin: asyncio.StreamWriter | None, task: str) -> None:
        if stdin is None:
            return
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            stdin.write(task.encode("utf-8"))
            await stdin.drain()
        stdin.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await stdin.wait_closed()

    @staticmethod
    async def _drain(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
        """排空一条管道，只保留**尾部** limit 字节。

        保头是错的：被截断的通常是 harness 的最终回答，它在末尾——留头部等于拿一段
        没有结论的文本去评分，还把"已经截断"这个已知损坏信号变成一次 200 响应。
        """
        if stream is None:
            return b"", False
        buffer = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(READ_CHUNK)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > limit:
                truncated = True
                del buffer[: len(buffer) - limit]
        return bytes(buffer), truncated

    def _final_answer(self, output_file: Path, stdout: bytes, truncated: bool) -> tuple[str, str, int, bool]:
        """优先取 ``-o`` 落地的最终回答文件，其次退回 stdout。

        返回的末位是"是否截断"：输出文件走的是磁盘，stdout 的标志管不到它，
        不一起判就会把一段被腰斩的回答当成完整回答交付。
        """
        if self.uses_output_file and output_file.is_file():
            data = output_file.read_bytes()
            overflow = len(data) > self.max_output_bytes
            tail = data[-self.max_output_bytes :] if overflow else data
            return tail.decode("utf-8", errors="replace"), "output_file", len(data), truncated or overflow
        return (
            stdout.decode("utf-8", errors="replace"),
            "stdout",
            len(stdout) if not truncated else self.max_output_bytes,
            truncated,
        )

    async def _terminate(self, proc: asyncio.subprocess.Process | None) -> None:
        if proc is None or proc.returncode is not None:
            return
        # POSIX 下杀整个进程组，Windows 上没有 killpg/SIGKILL 这些符号，
        # 用 getattr 取以避开平台条件导入与 type: ignore（CI 开了 warn_unused_ignores）。
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if os.name == "posix" and killpg is not None and getpgid is not None and sigkill is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                killpg(getpgid(proc.pid), sigkill)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # kill 之后必须收割，否则留僵尸进程与未关闭的管道。
        with contextlib.suppress(TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), REAP_TIMEOUT_S)


def _tail_text(data: bytes, limit: int) -> str:
    return data[-limit:].decode("utf-8", errors="replace")


#: stderr 会原样进入 502/504 的 detail 与审计，而这个服务无鉴权、只绑本机也不代表
#: 日志里的凭据就该长期存在。只按形状抹值，不改"detail 里能看到子进程报错"这一既有语义。
_SECRET_VALUE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{4,}|ghp_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{6,})\b")
_SECRET_ASSIGN = re.compile(r"(?i)\b(api[_-]?key|token|secret|authorization|password)(\s*[:=]\s*)(\S+)")


def _redact(text: str) -> str:
    if not text:
        return text
    masked = _SECRET_VALUE.sub(lambda match: f"{match.group(1)[:4]}***", text)
    return _SECRET_ASSIGN.sub(lambda match: f"{match.group(1)}{match.group(2)}***", masked)
