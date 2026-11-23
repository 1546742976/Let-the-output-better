# Let-the-output-better

**面向学术科研的 Post-hoc AI 输出优化器**

在任意 Agent Harness（Codex CLI、Claude Code、自研脚手架等）完成生成后，对其最终输出进行后置优化：剔除客套话、过度防御措辞（如“值得注意的是”“或许在某些情况下”“仅供参考”）与晦涩表达，转化为简短、直白、可读性强的学术表述；同时确保结构化输出可入库、数值与引用可溯源。**不修改底层模型、不介入推理过程。**

> **项目状态**：核心优化管线与多模型适配已实现；自 v0.6 起默认开启 Harness 驱动作为任务主路径（未检测到可驱动成员时自动降级为“粘贴输出”模式）。

---

## 效果一览

### 1. 结构化抽取优化

**优化前**（含客套语、Markdown 围栏、字段类型不规范、多余字段）：

````text
好的，以下是这篇论文的相关信息，希望对您有帮助！
```json
{"title": "基于深度学习的图像分类方法研究", "authors": "张三、李四", "year": "2023", "keywords": "深度学习;图像分类;卷积神经网络", "reference": "Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023.", "note": "如需更多论文信息请随时告诉我"}
```
````

**优化后**（自动剥离围栏与多余字段、类型归一、参考文献转 GB/T 7714）：

```json
{
  "title": "基于深度学习的图像分类方法研究",
  "authors": ["张三", "李四"],
  "year": 2023,
  "keywords": ["深度学习", "图像分类", "卷积神经网络"],
  "references": ["Zhang S, Li S. Research on image classification based on deep learning[C]//CVPR. 2023."]
}
```

### 2. 学术散文去防御化

- **改写前**：“值得注意的是，这或许只能被视为探索性证据；本结论仅在 3 个数据集上得到验证，Top-1 准确率提升 12.5%；需谨慎解读。”
- **改写后**：“本结论仅在 3 个数据集上得到验证，Top-1 准确率提升 12.5%。”

> 删去零指称的客套与免责套话（“值得注意的是”“或许只能被视为探索性证据”“需谨慎解读”），同时完整保留有原文依据的研究边界（“仅在 3 个数据集上得到验证”）。

---

## 目录

- [1. 为什么选择 Post-hoc 优化](#1-为什么选择-post-hoc-优化)
- [2. 核心特性](#2-核心特性)
- [3. 系统架构与优化管线](#3-系统架构与优化管线)
- [4. 快速开始](#4-快速开始)
- [5. 使用指南](#5-使用指南)
- [6. 配置参考](#6-配置参考)
- [7. 效果评估](#7-效果评估)
- [8. 可靠性设计与已知限制](#8-可靠性设计与已知限制)
- [9. 项目结构](#9-项目结构)
- [10. 开发与测试](#10-开发与测试)
- [11. 版本规划、贡献与许可证](#11-版本规划贡献与许可证)

---

## 1. 为什么选择 Post-hoc 优化

大模型辅助科研写作与信息抽取时，原始输出常存在四类痛点：

1. **结构化不合规**：字段缺失、类型错误（如年份为字符串）、包裹多余客套语或围栏，无法直接入库。
2. **参考文献不规范**：格式混乱（未统一为 GB/T 7714 或 APA），甚至夹杂幻觉引用。
3. **事实难以溯源**：实验数值偏离原文或无法在输入材料中定位。
4. **防御性写作（Defensive Writing）**：模型常主动弱化或撤回作者原有论断，堆砌“可能在一定程度上”“这并不意味着”“需要指出的是”等**无据限定（Ungrounded Qualifications）**，导致文本冗长晦涩。

相比在推理期介入约束解码或重采样，**Post-hoc（后置优化）** 在输出完成后介入，带来四个直接优势：

- **与模型解耦**：支持 OpenAI 兼容协议（OpenAI、DeepSeek、Qwen、vLLM、Ollama 等）及 Anthropic、Gemini、OpenRouter 原生适配器。
- **与 Harness 解耦**：不绑定特定脚手架的提示词与工作流，按白名单驱动外部命令或直接接收原始文本。
- **零首 Token 延迟损耗**：不参与主推理流程，不影响流式输出体验。
- **可度量、可回退**：确定性规则评分 + 采纳双闸门 + 全链路 JSONL 审计，任一环节异常时 Fail-open 回退原始输出。

---

## 2. 核心特性

- **多模型统一接入**：内置 `mock`（离线可测）、`openai`（及兼容协议）、`anthropic`、`gemini`、`openrouter` 五种 Provider。
- **五步可组合管线**：`schema`（结构校验）→ `rules`（规则评分）→ `repair`（修复重试）→ `format`（格式统一）→ `simplify`（去防御化改写），支持按需开关。
- **内置学术 Schema**：开箱提供 `paper`（论文元数据）、`reference`（参考文献）、`experiment`（实验结果），并支持自定义 Pydantic 模型。
- **确定性规则评分**：散文按通俗度 0.35、自信度 0.3、简洁度 0.2、一致性 0.15 加权；结构化按完整度 0.3、格式合规 0.25、原文一致性 0.25、通俗度 0.2 加权。纯词表与句式规则计算，不依赖额外模型调用，结果可复现。
- **无据限定与研究边界分离**：逐句识别限定语——句内包含划定范围的指称（领域名词、标识符或边界语境数值）且可溯源至原文的，视为**研究边界**并保留；零指称或仅撤回结果的，视为**无据限定**并清除；未提供原文时保守保留。
- **去防御化改写与采纳闸门**：删去冗余开场/收尾与推责模板，拆短长句并将主张前置；改写结果须同时满足**事实不变（数值、单位类别、缩写锚点）→ 研究边界不丢失 → 长度未超过原文 110%** 才会被采纳。
- **Harness 驱动主路径**：默认开启 `HARNESS_ENABLED=true`，支持通过 `POST /v1/run` 驱动本机白名单 CLI 或 API 生成回答后直接进入优化管线。
- **默认 Fail-open 与全链路审计**：异常或超时自动回退原始输出；每次运行以 JSONL 记录输入、输出、各步骤得分、耗时与拒收原因。
- **本地可视化工作台**：内置零构建前端页面（`GET /`），提供历史回溯、流水线开关、行级 Diff 对照与实时进度展示。

---

## 3. 系统架构与优化管线

### 3.1 系统架构

```text
[任务主路径]  task ──► harness 驱动（白名单子进程 / 兼容 API）──► raw_output
                                                                        │
[Post-hoc]    raw_output ──► schema ──► rules ──► repair ──► format ──► simplify ──► final_output
                               │          │         │          │           │
                               └──────────┴─────────┴──────────┴───────────┴──► 审计（JSONL）+ 采纳判定
```

### 3.2 五步管线职责

| 步骤 | 职责 | 可关闭 | 默认状态 |
| ------ | ------ | -------- | ---------- |
| `schema` | 使用内置学术 Schema 或自定义 Pydantic 模型校验结构，支持剥离 Markdown 围栏后解析 | 是 | 开启 |
| `rules` | 按散文 / 结构化权重计算确定性得分，并校验数值与引用是否可在原文定位 | 是 | 开启 |
| `repair` | 未通过校验或得分低于阈值时调用修复模型（最多 1 次，超时 6s），通过校验且不降分才采纳 | 是 | 开启 |
| `format` | 清理空白与围栏、统一日期/数值/单位及参考文献格式（`gb-t-7714` 或 `apa`）、剔除多余字段 | 是 | 开启 |
| `simplify` | 调用模型去防御化改写：删套话与无据限定、拆长句、主张前置；过闸门校验后采纳 | 是 | 散文开启，结构化关闭 |

### 3.3 采纳闸门口径

- **数值与单位**：`1,200,300` 与 `1200300`、`12.50%` 与 `12.5%` 归一等价；正负号变化（`-12.5%` vs `12.5%`）或单位类别变更（`%` vs `个百分点`）直接拒绝。
- **缩写锚点**：识别如 `CVPR`、`NEURIPS` 等全大写专名（支持紧贴汉字），缺失即拒绝。
- **研究边界**：基于同族词表判定（如 `数据集 ↔ 评测集 ↔ 基准`、`样本量 ↔ 被试`），整族消失即拒绝（`simplify_rejected`）。
- **长度约束**：改写后不得超过原文 110%，同时受 `MAX_FIELDS=3`（单次最多改写字段数）与 `MIN_LENGTH=12`（低于该长度的短文本跳过改写）约束。

### 3.4 步骤统一契约

```python
from dataclasses import dataclass

@dataclass
class StepResult:
    name: str
    passed: bool
    score: float | None = None
    reason: str = ""
    ms: int = 0

class BaseStep:
    name: str

    async def run(self, ctx) -> StepResult:
        ...
```

---

## 4. 快速开始

### 4.1 环境要求

- Python 3.11 及以上
- pip 23 及以上
- 可选：任一受支持的模型服务（不配置密钥时默认使用 `mock` 模式，可完全离线运行）

### 4.2 安装与启动

```bash
git clone https://github.com/1546742976/Let-the-output-better.git
cd Let-the-output-better
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # 默认 PROVIDER=mock；Windows CMD 可用 copy
```

### 4.3 离线端到端验证

运行演示脚本（无需 API Key）：

```bash
python scripts/demo.py
```

启动本地服务并验证接口：

```bash
uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/healthz
curl -s -X POST localhost:8000/v1/optimize \
  -H "content-type: application/json" \
  -d '{"raw_output":"{\"title\":\"基于深度学习的图像分类方法研究\",\"year\":\"2023\"}","schema":"paper","input_text":"《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。","options":{"repair":false}}'
```

> **Windows 提示**：`curl.exe` 的 `-d` 内联参数会按系统 ANSI 代码页编码导致中文乱码。建议将请求体保存为 UTF-8 文件后使用 `--data-binary` 发送：
>
> ```bash
> curl -s --data-binary @data/request.example.json -H "content-type: application/json; charset=utf-8" -X POST localhost:8000/v1/optimize
> ```
>
> 若遇到 `10013` 端口权限错误，可用 `netsh int ipv4 show excludedportrange protocol=tcp` 检查保留端口并更换 `--port`。

启动后在浏览器访问 `http://localhost:8000/` 即可打开本地可视化工作台。

### 4.4 接入真实模型

在 `.env` 中配置对应 Provider 即可，无需改动代码：

```env
# 1. OpenAI 兼容协议（适用于 OpenAI / DeepSeek / Qwen / Moonshot / vLLM / Ollama）
PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini

# 2. Anthropic
PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5

# 3. Google Gemini
PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash

# 4. OpenRouter
PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=openai/gpt-4o-mini
```

> 缺失必填密钥将在服务启动期直接报错，不会延迟到首次请求时才暴露。

---

## 5. 使用指南

### 5.1 Python SDK 调用

```python
from app.optimizer import optimize
from app.schemas import Paper

result = await optimize(
    raw_output='{"title":"基于深度学习的图像分类方法研究","authors":"张三、李四","year":"2023"}',
    schema=Paper,
    instruction="抽取论文标题、作者、年份、关键词与参考文献，参考文献按 GB/T 7714 规范",
    input_text="《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。",
    repair=True,
)

print(result.final_output)   # 结构化结果或优化后文本
print(result.valid)          # 是否通过校验
print(result.score)          # 最终质量得分
print(result.repaired)       # 是否触发过修复
print(result.degraded)       # 是否触发 Fail-open 降级
```

### 5.2 HTTP API

主要端点：

- `POST /v1/optimize`：对已有 `raw_output` 执行优化管线。
- `POST /v1/run`：先驱动 Harness 生成输出，再进入同一条优化管线。
- `GET /v1/harnesses`：扫描本机可用 Harness Profile（加 `?probe=true` 可执行版本/登录态探测）。
- `GET /healthz`：健康检查，返回当前状态与装配的 Provider。

内置学术 Schema（`schema` 字段可选值）：

| Schema | 用途 | 关键字段 |
| ------ | ------ | ------ |
| `paper` | 论文元数据抽取 | `title`、`authors`、`year`、`venue`、`keywords`、`abstract`、`references` |
| `reference` | 单条参考文献规范化 | `authors`、`title`、`venue`、`year`、`doi`、`type` |
| `experiment` | 实验结果结构化 | `method`、`dataset`、`metric`、`value`、`unit`、`baseline` |

请求示例（`POST /v1/optimize`）：

```json
{
  "raw_output": "{\"title\":\"基于深度学习的图像分类方法研究\",\"authors\":[\"张三\",\"李四\"],\"year\":\"2023\",\"reference\":\"Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023.\"}",
  "schema": "paper",
  "instruction": "抽取论文元数据，参考文献按 GB/T 7714 规范",
  "input_text": "《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。",
  "reference_style": "gb-t-7714",
  "options": { "score": true, "repair": true, "format": true, "simplify": false, "timeout_ms": 8000 }
}
```

> `reference_style` 支持 `"gb-t-7714"` 或 `"apa"`，可**逐请求**覆盖 `pipeline.yaml` 中的配置（`POST /v1/run` 同样支持），传入未知值将在入口返回 `422`。

响应示例与字段语义：

```json
{
  "request_id": "req_20261116_0001",
  "final_output": {
    "title": "基于深度学习的图像分类方法研究",
    "authors": ["张三", "李四"],
    "year": 2023,
    "references": ["Zhang S, Li S. Research on image classification based on deep learning[C]//CVPR. 2023."]
  },
  "valid": true,
  "score": 0.92,
  "repaired": false,
  "degraded": false,
  "steps": [
    { "name": "schema", "passed": true, "score": 1.0, "ms": 4 },
    { "name": "rules",  "passed": true, "score": 0.92, "ms": 1 },
    { "name": "format", "passed": true, "score": 1.0, "ms": 2 }
  ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0 }
}
```

| 字段 | 说明 |
| ------ | ------ |
| `final_output` | 最终结果（结构化对象或优化后文本） |
| `valid` | 是否通过 Schema 校验 |
| `score` | 0–1 之间，表示**最终交付内容**的质量得分（发生改写时会重算）；未启用评分时为 `null` |
| `repaired` | 是否触发过修复调用（含被采纳闸门拒绝的情况） |
| `degraded` | 是否因异常或超时触发 Fail-open 回退 |
| `steps` | 各步骤的通过状态、得分与耗时（ms） |
| `usage` | 模型 Token 消耗（纯规则路径下为 0） |

### 5.3 驱动 Harness（任务主路径）

`HARNESS_ENABLED` 默认为 `true`。调用 `POST /v1/run` 时，服务端按以下优先级装配可驱动成员：

| 来源 | 说明 |
| ------ | ------ |
| 内置 Profile 自动发现 | 扫描 PATH 与 npm 全局目录并解析真实可执行文件；参数恒为 `app/harness/profiles.py` 中的常量（当前支持 `codex` 驱动，`opencode` 仅作发现展示） |
| OpenAI 兼容 API 直连 | 当 `OPENAI_API_KEY` 非空时自动注册 `openai-api` Profile，无需启动子进程 |
| 显式命令 | 通过 `.env` 中的 `HARNESS_COMMAND` + `HARNESS_ARGS` 指定 |

**安全边界**：

| 约束维度 | 具体实现 |
| ------ | ------ |
| 请求隔离 | 请求体仅接受 `task` 与 `harness`（Profile 名），携带 `command`/`args`/`cwd`/`model`/`base_url` 等字段直接返回 `422` |
| 配置隔离 | 命令、参数、工作目录与白名单仅可来自 `.env`，`config/pipeline.yaml` 不接受此类配置 |
| 路径校验 | 显式命令须为真实存在的绝对路径且在 `HARNESS_ALLOWED_COMMANDS` 中；发现式 Profile 在 Windows 下须为真实 PE（拒绝 `.cmd`/脚本桩） |
| 输入与环境隔离 | `task` 通过 `stdin` 传入而非命令行参数；子进程使用重建的最小环境变量，默认不继承 `OPENAI_API_KEY` 等密钥 |
| 工作目录与资源限制 | 未指定 `HARNESS_CWD` 时使用独立临时空目录；受 `HARNESS_TIMEOUT_MS`、`HARNESS_MAX_OUTPUT_BYTES` 与进程级 `HARNESS_MAX_CONCURRENCY` 约束 |
| 默认与状态码 | `HARNESS_ENABLED` 默认为 `true`（未配成员或关闭时 `POST /v1/run` 返回 `503`）；并发超限返回 `429`，超时返回 `504`，非零退出或调用失败返回 `502`（不进入优化管线且不写审计） |

### 5.4 本地可视化工作台

访问 `http://localhost:8000/` 即可使用内置零构建工作台（无外链资源，支持明暗双主题）：

- **左栏（历史会话）**：按 schema、得分、耗时筛选与检索最近审计记录（反向按块读取尾部最多 200 条），点击可一键回填输入与对照结果。
- **主列（操作区）**：支持「驱动 Harness」与「粘贴输出」两种模式，提供内置示例填入、五步开关、引用风格覆盖、实时进度脉冲与评分维度拆解（含套话/删去的无据限定/保住的研究边界标签）。
- **右栏（环境与对照）**：展示当前 Provider、阈值、健康状态，并在底部常驻行级 Diff 对照视图（超过 400 行自动降级为仅显示优化后结果）。
- **快捷键**：`Ctrl/Cmd + Enter` 运行优化，`/` 聚焦历史搜索，`Esc` 关闭移动端抽屉。

> **安全提示**：工作台为本地调试工具，**无内置鉴权**，请勿将其直接暴露至不可信网络。

### 5.5 扩展自定义步骤

1. 继承 `BaseStep` 并使用 `@register_step` 注册：

```python
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult

@register_step
class CcfVenueStep(BaseStep):
    name = "ccf_venue"

    async def run(self, ctx) -> StepResult:
        venue = (ctx.parsed or {}).get("venue", "")
        if venue not in CCF_RECOMMENDED:
            return StepResult(name=self.name, passed=False, score=0.0, reason="发表 venue 不在 CCF 推荐目录内")
        return StepResult(name=self.name, passed=True, score=1.0)
```

2. 确保定义该步骤的模块在启动路径上被导入（例如放入 `app/optimizer/steps/` 包内）。
3. 在 `config/pipeline.yaml` 的 `steps` 列表中登记 `name: ccf_venue` 即可生效。

---

## 6. 配置参考

配置优先级固定为：**代码默认值 < `.env` < `config/pipeline.yaml`**。密钥一律通过环境变量传入，`pipeline.yaml` 仅负责步骤编排与阈值覆盖。

### 6.1 环境变量

| 变量 | 默认值 | 说明 |
| ------ | -------- | ------ |
| `PROVIDER` | `mock` | `mock`、`openai`、`anthropic`、`gemini` 或 `openrouter` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容服务地址 |
| `OPENAI_API_KEY` | `` | 仅 `provider=openai` 时必填 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 修复与改写使用的模型 |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com/v1` | Anthropic Messages 服务地址 |
| `ANTHROPIC_API_KEY` | `` | 仅 `provider=anthropic` 时必填 |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Anthropic 模型名 |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` | Gemini generateContent 服务地址 |
| `GEMINI_API_KEY` | `` | 仅 `provider=gemini` 时必填 |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini 模型名 |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter 路由地址 |
| `OPENROUTER_API_KEY` | `` | 仅 `provider=openrouter` 时必填 |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | OpenRouter 模型名（带厂商前缀） |
| `OPTIMIZE_SCORE_THRESHOLD` | `0.8` | 低于该分值触发修复 |
| `OPTIMIZE_MAX_REPAIR_RETRIES` | `1` | 修复调用上限 |
| `OPTIMIZE_TIMEOUT_MS` | `8000` | 整条优化管线的时间预算（ms） |
| `OPTIMIZE_FAIL_OPEN` | `true` | 异常或超时是否回退原始输出 |
| `AUDIT_PATH` | `data/audit.jsonl` | 审计日志落盘路径 |
| `HARNESS_ENABLED` | `true` | Harness 驱动总开关；无可用成员时 `/v1/run` 返回 503 |
| `HARNESS_PROVIDERS` | `` | 逗号分隔的 Profile 白名单（如 `codex,openai-api`），空表示全部可用 |
| `HARNESS_PROFILE` | `` | 默认 Profile，空表示取第一个可用成员 |
| `HARNESS_DETECT` | `true` | 是否扫描本机已安装的内置 Profile CLI |
| `HARNESS_PROBE_TIMEOUT_MS` | `5000` | 单次探测（`probe=true`）的超时时间（ms） |
| `HARNESS_COMMAND` | `` | 显式模式的可执行文件绝对路径，须在白名单内 |
| `HARNESS_ALLOWED_COMMANDS` | `[]` | 允许执行的可执行文件绝对路径（JSON 数组） |
| `HARNESS_ARGS` | `[]` | 固定参数（JSON 数组）；`{output_file}` 会替换为临时输出文件路径 |
| `HARNESS_CWD` | `` | 子进程工作目录，留空则使用独立临时空目录 |
| `HARNESS_TIMEOUT_MS` | `120000` | 单次 Harness 调用超时（ms），与 `OPTIMIZE_TIMEOUT_MS` 独立 |
| `HARNESS_MAX_OUTPUT_BYTES` | `262144` | 捕获输出的最大字节数，超出则标记截断 |
| `HARNESS_MAX_CONCURRENCY` | `1` | 进程级共享的最大并发 Harness 子进程数，超限返回 429 |
| `HARNESS_ENV_ALLOW` | `` | 逗号分隔的额外允许透传给子进程的环境变量名（勿填密钥） |

### 6.2 管线编排（`config/pipeline.yaml`）

```yaml
steps:
  - name: schema
    enabled: true
    strict: false          # false 时剔除多余字段而非直接判失败

  - name: rules
    enabled: true
    weights:                # 两组权重分别作用于散文与结构化，会覆盖 app/settings.py 缺省值
      prose: { plainness: 0.35, confidence: 0.3, concision: 0.2, preservation: 0.15 }
      structured: { completeness: 0.3, format: 0.25, grounding: 0.25, plainness: 0.2 }

  - name: repair
    enabled: true
    prompt: prompts/repair.txt

  - name: format
    enabled: true
    strip_markdown_fences: true
    trim_whitespace: true
    reference_style: gb-t-7714   # 可选 apa

  - name: simplify
    enabled: true
    prose_only: true       # 默认仅改写散文；设为 false 才会处理结构化文本字段
    prompt: prompts/simplify.txt

fail_open: true
audit: true
```

---

## 7. 效果评估

评估基于人工标注的金标集 `data/golden.jsonl`（预存 `raw_output`，离线评估不产生网络调用，追加 `--live` 可启用真实模型实时生成与修复）：

```bash
python scripts/evaluate.py --dataset data/golden.jsonl --provider mock --out data/reports/mock.md
```

运行后将输出 Markdown 报告、同名 CSV 明细以及 `.metrics.json` 汇总文件（本地产物已加入 `.gitignore`）。

#### mock 模式实测结果

在 `data/golden.jsonl`（39 条样本，其中 5 条散文）上使用 `mock` Provider 的实测结果如下：

| 指标 | 目标 | 实测 | 达标 |
| ------ | ------ | ------ | ------ |
| Schema 合规率 | 提升 ≥ 20pp | 优化前 41.0% → 优化后 87.2%（+46.2pp） | 是 |
| 与金标 valid 预期一致率 | 全部一致 | 100.0% | 是 |
| 关键字段准确率 | ≥ 90% | 100.0%（29 条含断言） | 是 |
| 引用格式合规率 | ≥ 95% | 100.0%（8/8） | 是 |
| 事实保持率 | 100% | 100.0%（变更样本 18 条） | 是 |
| 平均分变化 | ≥ +0.1 | +0.105 | 是 |
| 通俗度（plainness） | 均值 ≥ 0.8 | 0.979（较优化前 +0.116；散文样本 5 条） | 是 |
| 误删研究边界比例 | 0% | 0.0%（含真实边界的样本 5 条） | 是 |
| 无据限定清除率 | ≥ 90% | 88.9%（8/9 句） | 否 |
| 修复成功率 | ≥ 50% | 37.5%（3/8） | 否 |
| 额外延迟 P95 | 不修复 ≤ 300ms | 不修复 1ms / 全部 1ms | 是 |
| Fail-open 率 | < 1% | 0.0% | 是 |

> **未达标项说明**：
> - **修复成功率（37.5%）**：`mock` Provider 按设计不凭空补造缺失事实，不合规的候选被采纳闸门拒绝属预期行为；真实修复能力需通过 `--live` 接真实模型评估。
> - **无据限定清除率（88.9%）**：未清除的 1 句为“受制于算力，其有效性有待进一步验证”——因不含具体数值或领域专名被指称启发式归类，且 `mock` 对照表未收录该句式。

---

## 8. 可靠性设计与已知限制

### 可靠性设计

- **步骤级隔离与硬超时**：单步异常仅记录原因并继续；整条管线由 `asyncio.wait_for` 控制超时，超时按 Fail-open 返回原始输出。
- **跨版本数值可复现**：加权评分使用 `math.fsum` 精确求和，确保 Python 3.11–3.14 各版本得分逐位一致。
- **严格入口校验与限长**：请求模型使用 `extra="forbid"` 杜绝参数拼写错误静默失效；入口统一限制长度（`raw_output` 200k、`input_text` 500k、`instruction`/`task` 8k）。
- **配置告警与独立降级**：`/v1/config` 自动报告无效配置组合告警；审计读写异常仅记录警告，不阻断主流程与历史面板响应。

### 已知限制

- **自定义 Schema 能力边界**：任意 `BaseModel` 子类均支持校验与修复，但纯文本定制解析（`parse_text`）需继承 `AcademicModel`。
- **简化步骤静默截断**：受 `MAX_FIELDS=3`（单次最多改写 3 个字段）与 `MIN_LENGTH=12`（短于 12 字符跳过改写）限制，长结构化对象不会全字段触发改写。
- **指称启发式边界**：限定语分类标准以 `data/qualifications.jsonl` 标注集（现 23 条）为唯一基准。不含领域名词或边界数值的纯文字限制（如“受制于算力”）可能被误判为无据限定，而包含专名的撤回句会保守保留。
- **事实溯源策略**：`grounding` 维度仅对无法在原文定位的数值/引用进行降分并触发修复，**不会擅自删除**可疑条目（防止误删真实补充信息）。
- **Harness 进程回收**：超时或取消会终止直接子进程，但 Windows 下不保证清理其派生的孙进程；输出截断时保留末尾内容并附带 `truncated` 标记。
- **未锁定传递依赖**：仓库未提供 lock 文件，不同平台或时间点安装的传递依赖版本可能导致覆盖率存在约 0.1–0.2pp 浮动。

---

## 9. 项目结构

```text
.
├── app/
│   ├── main.py                      # FastAPI 入口：/v1/optimize、/v1/run、/v1/harnesses、/healthz
│   ├── settings.py                  # 配置加载：.env 与 pipeline.yaml 优先级合并
│   ├── schemas.py                   # 内置学术 Schema：paper / reference / experiment
│   ├── textio.py                    # 围栏剥离、JSON 抽取、空白归一、提示词加载
│   ├── plain.py                     # 语言规则层：词表、限定语分类、散文/结构化评分
│   ├── progress.py                  # 进程内运行进度状态登记
│   ├── audit.py                     # JSONL 全链路审计落盘与尾部读取
│   ├── viewer.py                    # 本地调试工作台路由与环境信息接口
│   ├── web/                         # 零构建前端资源（HTML / CSS / JS / 自托管字体与图标）
│   ├── harness/                     # Harness 驱动层（发现、探测、白名单命令与 API 驱动）
│   ├── optimizer/                   # 核心优化管线（context / pipeline / scoring / steps）
│   └── providers/                   # 模型适配器（mock / openai_compatible / anthropic / gemini / openrouter）
├── config/pipeline.yaml             # 管线步骤编排与权重配置
├── prompts/                         # repair.txt 与 simplify.txt 提示词模板
├── data/
│   ├── golden.jsonl                 # 金标集（39 条，含 5 条散文样本，预存 raw_output）
│   ├── qualifications.jsonl         # 限定语判定标注集（ungrounded / boundary / unverified）
│   ├── request.example.json         # HTTP 请求样例
│   └── reports/                     # 本地评估报告输出目录
├── scripts/
│   ├── demo.py                      # 离线演示脚本
│   └── evaluate.py                  # 批量评估脚本（产出 Markdown / CSV / metrics.json）
├── sites/                           # 静态站点与边缘函数适配（Deno / Functions）
├── tests/                           # 单元测试、回归快照与 README 契约校验
├── .env.example
├── pytest.ini
├── requirements.txt
└── LICENSE
```

---

## 10. 开发与测试

运行全量测试与分支覆盖率检查：

```bash
pytest -q
pytest -q --cov=app --cov-branch --cov-fail-under=80 --cov-report=term-missing
```

测试套件涵盖步骤单测、Provider 模拟测试（`respx`）、Fail-open 与超时注入、去防御化与采纳闸门不变式、金标回归快照、导入隔离测试以及 README 机械契约校验。

**CI 实测基准（win32，Python 3.11–3.14）**：≥550 项用例通过，`app/` 行与分支综合覆盖率 95.06%（CI 门槛 80%）。该基准与 §7 实测表由 CI 脚本自动核对，防止文档与代码漂移。

---

## 11. 版本规划、贡献与许可证

### 版本规划

| 版本 | 核心范围 | 状态 |
| ------ | ------ | ------ |
| v0.2 | 核心优化管线、学术 Schema、OpenAI 兼容 Provider、审计与评估 | 已交付 |
| v0.3 | 白名单 Harness 驱动、本地工作台、`/v1/audits` 只读历史 | 已交付 |
| v0.4 | 通俗化主线：词表与句式检测、`plainness`/`concision` 评分 | 已交付 |
| v0.5 | 防御性写作判定：无据限定与研究边界分离、边界丢失否决闸门 | 已交付 |
| v0.6 | Harness 驱动成为默认任务主路径、不可用时自动降级 | 已交付 |
| v0.7 | 新增 Anthropic / Gemini / OpenRouter 适配器（已交付）；真实语料标注扩充（规划中） | 部分交付 |
| v0.8 | 可选 LLM 评分维度、多候选输出重排 | 规划中 |
| v0.9 | 基于审计数据的阈值调优与漂移检测 | 规划中 |
| v1.0 | 持久化存储、监控接入与可视化增强 | 规划中 |

### 贡献指南

欢迎通过 Issue 与 Pull Request 参与共建：

1. Fork 本仓库并创建分支：`git checkout -b feature/your-feature`
2. 提交修改并确保 `pytest` 全部通过（新增 Provider 或 Step 请附带对应测试）
3. 若涉及配置项、评分权重或接口变更，请同步更新 `README.md` 与 `.env.example`
4. 推送分支并提交 Pull Request

### 许可证

本项目基于 [MIT License](LICENSE) 开源。
