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
import signal
import tempfile
import time
from pathlib import Path

from app.harness.base import BaseHarness, HarnessBusy, HarnessError, HarnessRun, HarnessTimeout, HarnessUnavailable

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
    ) -> None:
        if name is not None:
            self.name = name
        self.command = command
        self.args = list(args or [])
        self.allowed_commands = list(allowed_commands or [])
        self.cwd = cwd or None
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self.max_concurrency = max(1, int(max_concurrency))
        self.env_allow = tuple(env_allow)
        self._in_flight = 0

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
        if self._in_flight >= self.max_concurrency:
            raise HarnessBusy(f"harness 并发已达上限（{self.max_concurrency}），请稍后再试")
        self._in_flight += 1
        started = time.perf_counter()
        try:
            return await self._execute(task, timeout_s, started)
        finally:
            self._in_flight -= 1

    async def _execute(self, task: str, timeout_s: float, started: float) -> HarnessRun:
        with tempfile.TemporaryDirectory(prefix="optimize-harness-") as workdir:
            output_file = Path(workdir) / "last_message.txt"
            argv = self._argv(output_file)
            proc: asyncio.subprocess.Process | None = None
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
                stdout, stderr, truncated = await asyncio.wait_for(
                    self._exchange(proc, task), timeout=max(0.1, timeout_s)
                )
            except asyncio.TimeoutError as exc:
                await self._terminate(proc)
                raise HarnessTimeout(f"harness 超时（{timeout_s:.1f}s），已终止子进程") from exc
            except OSError as exc:
                raise HarnessError(f"无法启动 harness：{exc}") from exc

            exit_code = proc.returncode if proc is not None and proc.returncode is not None else -1
            if exit_code != 0:
                raise HarnessError(
                    f"harness 退出码 {exit_code}：{_tail_text(stderr, STDERR_TAIL_BYTES) or '子进程无 stderr 输出'}"
                )

            text, source, data_bytes = self._final_answer(output_file, stdout, truncated)
            if not text.strip():
                raise HarnessError("harness 未产出可用文本（stdout 与输出文件均为空）")
            return HarnessRun(
                text=text,
                exit_code=exit_code,
                ms=int((time.perf_counter() - started) * 1000),
                kind="subprocess",
                source=source,
                truncated=truncated,
                stderr_tail=_tail_text(stderr, STDERR_TAIL_BYTES),
                bytes=data_bytes,
                target=argv[1] if len(argv) > 1 else argv[0],
            )

    async def _exchange(self, proc: asyncio.subprocess.Process, task: str) -> tuple[bytes, bytes, bool]:
        """并发喂 stdin 与排空两条管道，任一超限即标记截断但继续读干，避免死锁。"""
        feed = asyncio.create_task(self._feed(proc.stdin, task))
        (stdout, out_truncated), (stderr, _) = await asyncio.gather(
            self._drain(proc.stdout, self.max_output_bytes),
            self._drain(proc.stderr, STDERR_TAIL_BYTES),
        )
        await feed
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
        if stream is None:
            return b"", False
        chunks: list[bytes] = []
        total = 0
        truncated = False
        while True:
            chunk = await stream.read(READ_CHUNK)
            if not chunk:
                break
            keep = chunk[: max(0, limit - total)]
            if keep:
                chunks.append(keep)
                total += len(keep)
            if len(keep) < len(chunk):
                truncated = True
        return b"".join(chunks), truncated

    def _final_answer(self, output_file: Path, stdout: bytes, truncated: bool) -> tuple[str, str, int]:
        """优先取 ``-o`` 落地的最终回答文件，其次退回 stdout。"""
        if self.uses_output_file and output_file.is_file():
            data = output_file.read_bytes()
            return data.decode("utf-8", errors="replace")[: self.max_output_bytes], "output_file", len(data)
        return (
            stdout.decode("utf-8", errors="replace"),
            "stdout",
            len(stdout) if not truncated else self.max_output_bytes,
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
        with contextlib.suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), REAP_TIMEOUT_S)


def _tail_text(data: bytes, limit: int) -> str:
    return data[-limit:].decode("utf-8", errors="replace")
