"""内置 harness profile 常量表。

档位约定（本文件的存在理由）：**argv 只能来自这里的常量**。自动发现只负责回答
"这个可执行文件在哪"，不接受请求体或 ``config/pipeline.yaml`` 提供命令与参数——
一旦开放外部 argv，"只允许内置 profile" 就退化成了信任制。

新增一个 CLI = 加一条常量 + 加一条测试。已检测到但未登记的 CLI 只会出现在健康表里
标注"不支持驱动"，不会得到任何执行入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProfileKind = Literal["command", "api"]
EntryKind = Literal["node-js", "native"]

#: 输出文件占位符，运行期替换为本次调用的临时文件路径。
OUTPUT_FILE_PLACEHOLDER = "{output_file}"


@dataclass(frozen=True)
class HarnessProfile:
    name: str
    label: str
    kind: ProfileKind = "command"
    #: False 表示已能检测到、但非交互调用契约尚未验证，不提供执行入口。
    drivable: bool = True
    entry: EntryKind = "native"
    #: PATH 上可能出现的命令名。Windows 下命中的常是 .cmd 包装，只能当指针。
    path_names: tuple[str, ...] = ()
    #: 全局 node_modules 下的包名。
    npm_packages: tuple[str, ...] = ()
    #: 相对包根的可执行/脚本落点，按顺序取第一个可信候选。
    entrypoints: tuple[str, ...] = ()
    #: node-js 形态需要的宿主可执行名。
    host_names: tuple[str, ...] = ()
    #: 紧跟在入口之后的固定参数；``{output_file}`` 会被替换。
    argv_template: tuple[str, ...] = ()
    answer_from: Literal["output_file", "stdout"] = "stdout"
    probe_version: tuple[str, ...] = ()
    #: (探测参数, 判定已登录的输出子串)
    probe_login: tuple[tuple[str, ...], str] | None = None
    #: api 形态使用的系统提示词，同样只能是常量。
    system_prompt: str = ""
    requires_model_match: bool = False
    notes: tuple[str, ...] = ()


CODEX = HarnessProfile(
    name="codex",
    label="OpenAI Codex CLI",
    entry="node-js",
    path_names=("codex",),
    npm_packages=("@openai/codex",),
    entrypoints=("bin/codex.js",),
    host_names=("node",),
    argv_template=(
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--ephemeral",
        "-o",
        OUTPUT_FILE_PLACEHOLDER,
        "-",
    ),
    answer_from="output_file",
    probe_version=("--version",),
    probe_login=(("login", "status"), "Logged in"),
    requires_model_match=True,
    notes=(
        "沙箱固定 read-only，且工作目录由 HARNESS_CWD 指定，禁止在本仓库内驱动",
        "账号默认模型须被已安装 CLI 版本支持，否则 harness 非零退出",
    ),
)

OPENCODE = HarnessProfile(
    name="opencode",
    label="OpenCode CLI",
    drivable=False,
    entry="native",
    path_names=("opencode",),
    npm_packages=("opencode-ai",),
    entrypoints=("bin/opencode.exe", "bin/opencode"),
    probe_version=("--version",),
    notes=(
        "包声明的 bin 可能只是未跑 postinstall 留下的 sh 桩，真实可执行文件在平台子包内",
        "非交互调用方式尚未验证，因此只检测、不驱动",
    ),
)

OPENAI_API = HarnessProfile(
    name="openai-api",
    label="OpenAI 兼容 API",
    kind="api",
    system_prompt="你是被测的 agent harness：请按任务要求直接给出最终回答，不要输出执行过程。",
    notes=("复用 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL；密钥只从环境读取，不进请求体与审计",),
)

PROFILES: dict[str, HarnessProfile] = {p.name: p for p in (CODEX, OPENCODE, OPENAI_API)}


def get_profile(name: str) -> HarnessProfile | None:
    return PROFILES.get(name)


def cli_profiles() -> list[HarnessProfile]:
    return [p for p in PROFILES.values() if p.kind == "command"]


def drivable_profiles() -> list[HarnessProfile]:
    return [p for p in PROFILES.values() if p.drivable]
