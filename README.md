# Let-the-output-better

**面向学术科研的 Post-hoc AI 输出优化器**

在任意 agent harness（Codex CLI、Claude Code、自研脚手架等）跑完之后，对其最终输出做二次优化：把过度防御的措辞（“值得注意的是”“或许在某些情况下”“仅供参考”）和晦涩表达改成简短、直白、能一遍读懂的说法，同时保证结构可入库、数值与引用可溯源。不改模型、不介入推理。

> **项目状态：v0.2 已实现；harness 驱动自 v0.6 起为任务主路径，默认开启（未配置可驱动成员时自动降级为“粘贴输出”）。** 仓库包含设计文档、可执行代码与评估方案，下文“快速开始”“使用指南”即为当前可用用法；mock 模式实测数字见 [§9 效果评估](#9-效果评估)，harness 驱动见 [§7.5](#75-驱动-harness任务主路径)。

**效果一览**

优化前（模型原始输出，无法被下游程序直接消费）：

````text
好的，以下是这篇论文的相关信息，希望对您有帮助！
```json
{"title": "基于深度学习的图像分类方法研究", "authors": "张三、李四", "year": "2023", "keywords": "深度学习;图像分类;卷积神经网络", "reference": "Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023.", "note": "如需更多论文信息请随时告诉我"}
```
````

优化后（校验通过、评分留痕，可直接入库）：

```json
{
  "title": "基于深度学习的图像分类方法研究",
  "authors": ["张三", "李四"],
  "year": 2023,
  "keywords": ["深度学习", "图像分类", "卷积神经网络"],
  "references": ["Zhang S, Li S. Research on image classification based on deep learning[C]//CVPR. 2023."]
}
```

剥离围栏与客套说明、剔除多余字段、作者与关键词转数组、年份转数值、参考文献统一 GB/T 7714 格式；任一环节异常时回退原始输出，不阻断业务。

---

## 目录

- [1. 项目简介](#1-项目简介)
- [2. 核心特性](#2-核心特性)
- [3. 设计原则](#3-设计原则)
- [4. 系统架构](#4-系统架构)
- [5. 优化管线](#5-优化管线)
- [6. 快速开始](#6-快速开始)
- [7. 使用指南](#7-使用指南)
- [8. 配置参考](#8-配置参考)
- [9. 效果评估](#9-效果评估)
- [10. 可靠性设计](#10-可靠性设计)
- [11. 适用范围与已知限制](#11-适用范围与已知限制)
- [12. 项目结构](#12-项目结构)
- [13. 开发与测试](#13-开发与测试)
- [14. 版本规划](#14-版本规划)
- [15. 贡献指南](#15-贡献指南)
- [16. 许可证](#16-许可证)

---

## 1. 项目简介

大模型辅助科研工作时，输出常面临四类问题：结构化字段缺失或类型错误，论文元数据与实验记录无法直接入库；参考文献格式混乱，甚至出现幻觉引用；实验数值偏离原文、无法溯源；以及空泛套话与模板化表述带来的“AI 味”，达不到人类研究者的写作水准。在推理阶段进行约束解码或重采样，需要深度绑定模型与供应商，接入成本高、可迁移性差。

其中第 4 类最要紧的偏差有名字：**防御性写作（Defensive Writing）**——模型在改写论文或专业文本时，会主动收窄、弱化甚至撤回作者原本的论断，并加入大量**无据限定（Ungrounded Qualifications）**，例如“可能在一定程度上……”“这并不意味着……”“需要指出的是……”“或许只能被视为探索性证据……”。本项目对它的要求是六条：删除无效免责声明与过度犹豫词、**保留真正影响结论的研究边界**、先讲主张再讲限制、去除机械化句式与过度防御、强化学术自信与结构清晰度、去除自我否定与模板化弱化句；同时保证改写后的内容相对简短、一读就懂。

本项目采用 **Post-hoc（后置优化）** 思路：harness 照常驱动模型生成，优化器只在输出完成之后介入。调用方持有原始输出，交由优化器完成校验、评分、修复、格式化与通俗化改写，最终返回可直接使用的结构化结果或一读就懂的文本。站点本身即这一思路的套壳形态：既可直接粘贴 harness 的输出，也可由服务端按白名单驱动 harness 跑一次再优化（§7.5）。

这一设计带来四个直接收益：

- **与模型解耦**：同一套优化策略适用于任意支持 OpenAI 兼容协议的服务，包括云端 API 与本地部署模型。
- **与 harness 解耦**：不绑定具体脚手架的提示词与流程，只约定“给我一段输出”；驱动层按白名单调用外部命令，换 harness 只改配置不改代码。
- **不损害交互体验**：优化发生在生成结束之后，不参与推理，不影响首 Token 延迟与流式输出本身的体验。
- **可度量、可回滚**：每次优化均留痕，能够对比优化前后效果；优化器异常时回退原始输出，业务流程不受阻断。

需要说明的是，instructor、guardrails 等库同样采用“生成后校验与重试”的事后思路，本项目的差异化不在“后置”本身，而在三点：质量判定使用**可复现的确定性规则评分**，不依赖额外模型调用；修复结果须通过“校验通过 + 得分不降低”**双闸门**才被采纳；每次优化的全过程以 JSONL 留痕，可直接用于回归分析与阈值调优。面向科研场景，评分额外强调**事实溯源**——输出中的数值与引用必须能在原文中定位。

适用场景：文献调研与综述整理、论文元数据抽取、参考文献规范化、实验结果结构化、科研写作辅助。本项目聚焦学术科研场景，内置对应 Schema 与评估集。

## 2. 核心特性

- **模型无关接入**：基于 OpenAI 兼容协议（`/chat/completions`）实现统一适配，可指向 OpenAI、DeepSeek、Qwen、Moonshot、vLLM、Ollama 等服务，切换模型仅需修改配置。
- **五步可组合管线**：Schema 校验、规则评分、修复重试、格式化、通俗化改写，各步骤独立开关，支持按任务裁剪。
- **学术场景内置 Schema**：`paper`（论文元数据）、`reference`（参考文献）、`experiment`（实验结果）开箱即用，也支持自定义 Pydantic 模型。
- **确定性规则评分**：散文按**通俗度 0.35、自信度 0.3、简洁度 0.2、与原文一致性 0.15**计分；结构化按字段完整度 0.3、格式合规 0.25、与原文一致性 0.25、通俗度 0.2 计分。全部由词表与句式规则计算，可复现、可单测，不依赖额外模型调用。
- **无据限定与研究边界分离**：逐句判定限定语性质——句内有**划定范围**的指称（领域名词、标识符，或在边界线索语境里的数字）且能在原文定位的是**研究边界**，保留；零指称、只有泛词、或只抄回了被撤回的那个数字的是**无据限定**，删除；没有原始材料时按未验证保守保留。判定看的不是有没有犹豫词、也不是有没有数字："这并不意味着 12.5% 可推广"里的 12.5 是被撤回的对象，不是范围。
- **去防御化改写**：删掉不承担信息的开场与收尾、推责免责声明、证据降级与自我否定模板，把长句拆短、主张提到限制之前；**有依据的研究边界必须原样保住范围，不得改写成无限定的强断言**。事实（数值、年份、引用锚点）必须不变，且改写不得变得更长，否则丢弃。
- **修复采纳闸门**：修复结果仅在“通过 Schema 校验且得分不低于原始输出”时被采纳，避免修复引入新错误。
- **默认 Fail-open**：任一环节异常或超时，均返回原始输出并记录降级原因，不阻断调用方业务。
- **全链路审计**：以 JSONL 记录原始输出、最终输出、各步骤得分、耗时与变更原因，可直接用于回归与数据分析。
- **离线可验证**：内置 mock provider，无网络、无密钥即可跑通完整管线、测试与评估流程。
- **harness 驱动是任务主路径**：`HARNESS_ENABLED` 默认为 `true`，`POST /v1/run` 先驱动本机已登记的 profile CLI 或经 OpenAI 兼容 API（如 DeepSeek）生成回答，再进同一条优化管线；请求体只能提供任务文本与 profile 名，命令与参数只来自 `.env`。没有可驱动成员时回 503 并指回 `POST /v1/optimize`，粘贴路径始终可用。

## 3. 设计原则

1. **只做后处理**：`optimize()` 不介入模型推理，不改变生成过程，不影响首 Token 延迟。harness 驱动位于该契约**外侧**——它是任务默认走过的调用方实现，但不参与五步管线，`optimize()` 本身仍可被单独调用而不需要任何 harness 配置。
2. **接口稳定优先**：对外契约只有 `optimize()` 与 `POST /v1/optimize` 两个入口，内部步骤可替换；另附一个本地查看页（`GET /`）与只读历史端点，属调试工具，不构成新的稳定接口。
3. **失败不放大**：任何优化步骤的异常都以“返回原始输出”收口，禁止因优化器故障导致业务失败。
4. **结论可追溯**：每个得分都必须能说明来源，每个变更都必须有对应的审计记录。
5. **默认值即最佳实践**：开箱配置即为经过评估的安全取值，调优属于可选行为。

## 4. 系统架构

```text
[默认路径]  task ──► harness 驱动（白名单子进程 / 兼容 API）──► raw_output
                                                                    │
post-hoc 契约 ──► schema ──► rules ──► repair ──► format ──► simplify ──► final_output
                    │          │         │          │           │
                    └──────────┴─────────┴──────────┴───────────┴──► 审计（JSONL）+ 采纳判定
```

优化器不主动调用生成模型：默认形态下原始输出由调用方提供。开启 harness 驱动时，由驱动层执行一次白名单命令取回其最终回答，再交给同一条 post-hoc 管线——这一步在契约外侧，且命令、参数、工作目录与环境变量只能来自服务端配置，请求体只提供任务文本。优化器内部仅在触发修复或通俗化改写时调用独立模型，因而不会与业务方的调用链产生耦合，也便于在同一请求内复用已有的模型响应。

## 5. 优化管线

| 步骤 | 职责 | 是否可关闭 | 默认 |
| ------ | ------ | ------------ | ------ |
| `schema` | 使用内置学术 Schema 或自定义 Pydantic 模型校验结构，支持剥离 Markdown 围栏后重试解析 | 是 | 开启 |
| `rules` | 散文按通俗度/自信度/简洁度/一致性 0.35/0.3/0.2/0.15 加权，结构化按完整度/格式/一致性/通俗度 0.3/0.25/0.25/0.2 加权；数值与引用须可在原文定位 | 是 | 开启 |
| `repair` | 未通过或低分时调用修复模型，最多 1 次，超时 6s | 是 | 开启 |
| `format` | 去空白、去围栏、统一日期/数值/单位与引用格式（GB/T 7714 或 APA）、剔除多余字段 | 是 | 开启 |
| `simplify` | 调用模型去防御化：删套话与无据限定、拆长句、把主张提到限制之前；事实不变、研究边界不丢、且不得变长才采纳 | 是 | 散文开启，结构化字段关闭 |

去防御化改写示例（散文默认生效，取自 `scripts/demo.py` 场景一）：

- 重写前：“值得注意的是，这或许只能被视为探索性证据；本结论仅在 3 个数据集上得到验证，Top-1 准确率提升 12.5%；需谨慎解读。”
- 重写后：“本结论仅在 3 个数据集上得到验证，Top-1 准确率提升 12.5%。”

“值得注意的是”“或许只能被视为探索性证据”“需谨慎解读”零指称，不指向任何可核对的对象，删掉；“仅在 3 个数据集上得到验证”带可在原文定位的指称（3、数据集），是真正影响结论的研究边界，原样保住。本项目要消的是防御姿态，不是诚实。采纳需同时满足三条，按此顺序检查：事实（数值、单位类别、缩写锚点）不变 → 研究边界不丢失 → 改写没有变得更长（超过原文 110% 即拒绝）。前两条的口径值得写明，因为它们决定闸门是保护事实还是制造误拒：

- **数值**按 `1,200,300` ≡ `1200300`、`12.50%` ≡ `12.5%` 归一比较（千分位是一个 token，`2019-2023` 是两个年份而不是一个负数），但 `-12.5%` 与 `12.5%` 是两个不同的事实；`88.5%` 改成 `88.5 个百分点` 在**单位类别**这一步被拒。
- **缩写锚点**认 `CVPR`、`NEURIPS` 这类全大写串，紧贴汉字也成立（旧写法 `\b[A-Z]{2,6}\b` 在"发表于CVPR"里根本不匹配，删掉专名不会被拦）；只查"缺失"不查"多余"，避免误拒。
- **研究边界**按同族写法整组判定：`数据集↔评测集↔基准`、`样本量↔被试`这类等价改写不算丢边界，整族消失才拒；判定层与闸门共用同一份族表，不存在"一处互认、另一处逐字"。

`simplify` 对散文默认开启，对结构化字段默认关闭（改写已校验过的内容风险更高）。它引入一次模型调用，开启时建议适当调大 `timeout_ms`。

各步骤统一实现如下契约：

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

`ctx` 提供 `raw_output`、`parsed`、`schema`、`instruction`、`input_text`、`errors`、`timeout_s` 等字段。新增步骤只需实现 `run()` 并在 `config/pipeline.yaml` 中登记，主流程无需改动。

## 6. 快速开始

> 以下命令均已实测。若在 Windows 上遇到 `10013 / 访问权限不允许`，通常是 8000 端口被系统保留，用 `netsh int ipv4 show excludedportrange protocol=tcp` 查询后换一个端口即可。

### 6.1 环境要求

- Python 3.11 及以上
- pip 23 及以上
- 可选：任一支持 OpenAI 兼容协议的模型服务（不配置时使用 mock provider）

### 6.2 安装

```bash
git clone https://github.com/1546742976/Let-the-output-better.git
cd Let-the-output-better
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # 默认 PROVIDER=mock；Windows CMD 下用 copy
```

依赖清单（`requirements.txt`）：

```text
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
pydantic>=2.8,<3.0
pydantic-settings>=2.4,<3.0
httpx>=0.27,<1.0
PyYAML>=6.0,<7.0
pytest>=9.0.3,<10.0
pytest-asyncio>=1.5,<2.0
pytest-cov>=6.0,<7.0
respx>=0.21,<1.0
```

### 6.3 离线验证

无需任何密钥即可完成端到端验证：

```bash
python scripts/demo.py
# 打印优化前后对照，并生成一条审计记录
```

```bash
uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/healthz
curl -s -X POST localhost:8000/v1/optimize -H "content-type: application/json" -d '{"raw_output":"{\"title\":\"基于深度学习的图像分类方法研究\",\"year\":\"2023\"}","schema":"paper","input_text":"《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。","options":{"repair":false}}'
```

mock 模式下，第二条命令应稳定返回 `valid` 与 `score` 字段，全过程不产生网络请求。

> **Windows 提示**：`curl.exe` 会把命令行参数按本地 ANSI 代码页重新编码，含中文的 `-d` 内联请求体到达服务端时已不是 UTF-8，会返回 `There was an error parsing the body`。把请求体放进 UTF-8 文件并用 `--data-binary @文件` 发送即可：
>
> ```bash
> curl -s --data-binary @data/request.example.json -H "content-type: application/json; charset=utf-8" \
>   -X POST localhost:8000/v1/optimize
> ```

起服后在浏览器打开 `http://localhost:8000/` 即可使用可视化页面，见 [§7.4](#74-可视化页面)。

### 6.4 接入真实模型

```env
PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

接入 DeepSeek、Qwen 或本地 vLLM 时，仅需调整 `OPENAI_BASE_URL` 与 `OPENAI_MODEL`，无需修改代码。

非 OpenAI 协议的原生适配器（v0.7）：

```env
PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
```

```env
PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

```env
PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=openai/gpt-4o-mini
```

三者与 `openai` 共用同一套失败语义（限流/超时/坏结构分别抛 `ProviderError`/`ProviderTimeout`），审计里的 `provider` 字段记录实际装配名，页面健康灯同源。协议差异在适配器内消化：Anthropic 的 system 是顶层字段且无 `response_format`（json_mode 折进 system 提示），Gemini 走 `systemInstruction` 与 `generationConfig.responseMimeType`、鉴权用 `x-goog-api-key` 头而非 URL query（密钥不进访问日志），OpenRouter 复用 OpenAI 兼容面但单列 provider 名以区分路由与直连的限流/账单口径。缺密钥在启动期即报错，不留到第一次调用。

## 7. 使用指南

### 7.1 Python 调用

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

print(result.final_output)   # 结构化结果
print(result.valid)          # 是否通过校验
print(result.score)          # 质量得分
print(result.repaired)       # 是否触发修复
print(result.degraded)       # 是否发生降级
```

### 7.2 HTTP 接口

`POST /v1/optimize`，`schema` 字段可选内置学术模型：

| Schema | 用途 | 关键字段 |
| ------ | ------ | ------ |
| `paper` | 论文元数据抽取 | `title`、`authors`、`year`、`venue`、`keywords`、`abstract`、`references` |
| `reference` | 单条参考文献规范化 | `authors`、`title`、`venue`、`year`、`doi`、`type` |
| `experiment` | 实验结果结构化 | `method`、`dataset`、`metric`、`value`、`unit`、`baseline` |

请求示例：

```json
{
  "raw_output": "{\"title\":\"基于深度学习的图像分类方法研究\",\"authors\":[\"张三\",\"李四\"],\"year\":\"2023\",\"reference\":\"Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023.\"}",
  "schema": "paper",
  "instruction": "抽取论文元数据，参考文献按 GB/T 7714 规范",
  "input_text": "《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。",
  "options": { "score": true, "repair": true, "format": true, "simplify": false, "timeout_ms": 8000 }
}
```

`reference_style` 可选 `"gb-t-7714"` / `"apa"`，**逐请求**覆盖 `pipeline.yaml` 里 format 步骤的引用风格；缺省跟随配置，`POST /v1/run` 同样接受。未知取值在入口 422：请求模型是 `extra="forbid"` 加 Literal，而管线侧 `model_copy(update=…)` 不校验 Literal，坏值若漏进去会静默按 GB/T 排——所以 `optimize()` 入口另行炸 `ValueError`，两层都有测试钉住。

响应示例：

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

上例中得分高于修复阈值，未触发修复调用；`format` 步骤统一年份类型与引用格式，因此 `usage` 为 0。

响应字段语义：

| 字段 | 说明 |
| ------ | ------ |
| `final_output` | 最终结果，结构化对象或原始字符串 |
| `valid` | 是否通过 Schema 校验 |
| `score` | 0 至 1 的质量得分，描述**最终交付内容**；若发生过通俗化改写会重算，因此它可能高于 `steps` 里 rules 当时的分值。未启用评分时为 `null` |
| `repaired` | 是否发生修复调用，包含被采纳闸门拒绝的情况 |
| `degraded` | 是否因异常或超时回退到原始输出 |
| `steps` | 各步骤的通过状态、得分与耗时 |
| `usage` | 模型 Token 消耗，纯规则路径下为 0 |

健康检查：`GET /healthz` 返回 `{"status":"ok","provider":"mock"}`。

### 7.3 扩展自定义步骤

```python
from app.optimizer.steps.base import BaseStep, StepResult

class CcfVenueStep(BaseStep):
    name = "ccf_venue"

    async def run(self, ctx) -> StepResult:
        venue = (ctx.parsed or {}).get("venue", "")
        if venue not in CCF_RECOMMENDED:
            return StepResult(name=self.name, passed=False, score=0.0,
                              reason="发表 venue 不在 CCF 推荐目录内")
        return StepResult(name=self.name, passed=True, score=1.0)
```

扩展分两步：类上用 `@register_step` 完成注册，且**定义它的模块必须已被 import**（内置步骤由 `app/optimizer/steps/` 的包导入链带起，自定义步骤要么放进该包，要么在启动路径上显式导入）；之后在 `config/pipeline.yaml` 的 `steps` 列表中登记名字即生效。名字未注册时管线记一条 `unknown_step(<name>)` 并跳过该步骤，不中断整条管线。若校验不通过，将按配置触发修复或直接返回。

### 7.4 可视化页面

起服后访问 `/`。页面是顶栏分区导航 + 三栏工作台：左栏历史会话，中间主列自上而下是概览 → 驱动 harness → 输入 → 结果（结果面板初始 `hidden`，跑完一次才出现），右栏是运行环境与前后对照。顶栏导航只列 4 个分区（概览 / 驱动 / 输入 / 结果）——**对照不占导航位，它常驻右栏底部**：宽屏下环境面板封顶自己滚，窄屏（≤1040px）下对照用 sticky 锚在视口底部，不须滚到整篇末尾。这三条规则由 `tests/test_web_contract.py` 做结构断言（测的是规则存在性，不是像素）。视觉为自托管字体 + 内联图标精灵，无任何外链资源。

视觉语言对齐 Google 官网：Google Sans 式几何标题（本机无该字体时回落到自托管 Poppins）、Roboto 正文、JetBrains Mono 代码字（可变字重、默认开启编程连字），蓝红黄绿四色品牌色、白色卡面与发丝描边、胶囊按钮与 Material 涟漪。顶栏固定并带分区导航；中部 hero 用一条可点击的五步流水线图表达管线，节点与下方步骤开关双向联动，统计条（记录数、平均 score、通过率、中位耗时）全部由真实审计记录算出。明暗双主题，选择写入 `localStorage`，默认跟随系统。

| 区域 | 内容 | 数据来源 |
| ------ | ------ | ------ |
| 历史会话 | schema、状态、得分、耗时、变更标记；搜索、三种排序；点击回填输入区并渲染该次对照 | `GET /v1/audits` |
| 输入区 | `schema` 下拉与必填提示、`instruction`、`input_text`、`raw_output`、五个步骤开关、`timeout_ms` 与引用风格下拉（跟随配置 / GB/T 7714 / APA，逐请求覆盖）、示例一键填入 | `GET /v1/config` |
| harness 面板 | profile 下拉、检测结果与「驱动 harness 并优化」；驱动不可用时给出原因。请求体发 `task`、profile 名与输入区参数（schema / 指令 / 上下文 / 步骤开关 / 引用风格），命令、参数、模型与 base_url 仍不可由请求指定 | `GET /v1/harnesses`、`POST /v1/run` |
| 运行进度 | 提交后状态行显示当前阶段与已耗时（`harness 运行中` / `管线 <步骤>`），流水线对应节点脉冲；结束或失败即退场 | `GET /v1/progress`（400ms 轮询） |
| 结果流 | score 环（环上刻度为采纳阈值）、结论行、步骤时间线（条长为得分，无得分时按耗时占比）、评分条（散文四维：通俗度/自信度/简洁度/一致性）、变更与标记、错误、复制输出与重跑；`simplify` 一行留痕下挂三组标签——中性=命中的套话、红=删掉的无据限定、绿=保住的研究边界，另有「主张先行」判定 | `POST /v1/optimize` 与该次的审计记录 |
| 运行环境 | provider、阈值、超时、fail_open、引用风格、审计条数、健康灯 | `GET /v1/config`、`GET /healthz` |
| 前后对照 | 行级 diff，增删分色，统一 / 并排两种视图；常驻右栏底部（宽屏封顶环境面板让位，窄屏 sticky 锚视口底部），不再占顶栏导航位 | 审计记录的 `raw_output` 与 `final_output` |

局限与注意点：

- 页面是**本地调试工具，无鉴权**，不要暴露到不可信网络。
- 历史只读 `AUDIT_PATH` 尾部（单次最多 200 条）；反向**按块读取**、只物化窗口所需行，不再把整个审计文件载入内存。跨块半行靠 `carry` 拼接，超长单行也不丢。读到文件头时 `total_exact=true`、`total_lines` 是全量；大文件在触头前就填满窗口时 `total_exact=false`、`total_lines` 只是下界，页面据此把"审计条数"显示为 `≥ N`。多进程共用同一审计文件时，列表仍会混入其它来源的记录。
- `request_id` 含进程 nonce + 进程内序号（`req_日期_nonce_序号`），多进程并行不再重号；页面仍以审计文件的**行号**（`line_no`）作为选中依据——行号定位到具体一行更稳。
- 步骤开关默认取 `pipeline.yaml` 的取值，提交时显式发送布尔值，不再沿用“未设置即跟随配置”的三态语义。
- 模型输出与审计内容一律按纯文本渲染，测试用例断言页面不存在 `innerHTML` 等注入写法。
- 字体与图标全部自托管（`app/web/fonts/` 为 latin 子集 woff2，附 OFL 许可证）。图标是自绘的内联 SVG sprite：24 网格、圆头描边线稿加一枚实心“节点”圆点，与 wordmark 的环+中心点同源，不沿用 Material 实心剪影；页面不引用任何站外资源，测试用例对此有断言。
- 快捷键：`Ctrl/Cmd+Enter` 运行优化，`/` 聚焦历史搜索，`Esc` 关闭移动端抽屉；新建会话清空输入时提供「撤销」。
- diff 在前端计算，超过 400 行自动降级为只显示优化后结果。
- 运行进度是**进程内**登记（`app/progress.py`），只解释现状、不参与优化语义：`active` 是在飞运行（阶段、已完成步骤、已耗时），`last` 是最近一次结束的运行（429/5xx 记为 `failed` 并带原因），其 `run_id` 与响应及审计同源。步骤的 start/end 由管线唯一隔离入口上报，单步异常被隔离时收尾照样成对。多 worker 部署时各进程各自登记；本地查看页绑定 loopback 单进程，不受影响。进度轮询失败不打断提交。
- 布局为"细左栏 + 居中主列"，窄于 1040px 折叠为单列；左上按钮可收起历史栏。颜色、面层、描边与三套字体族走 `:root` 令牌，另有 `--ok/--bad/--warn` 语义别名；间距、圆角、字号没有令牌刻度，直接写 px。

### 7.5 驱动 harness（任务主路径）

前四节不要求本服务具备生成能力：那种形态下 `raw_output` 由调用方给出。本节是任务默认走过的路径——服务端先驱动一个上游 harness 跑一次，取回它的最终回答，再走同一条五步管线。`HARNESS_ENABLED` 默认为 `true`；页面 composer 在检测到一个以上可驱动 profile 时也默认停在「驱动 harness」，粘贴入口保持可用。

三条来源，按可信度从高到低：

| 来源 | 说明 |
| ------ | ------ |
| 内置 profile 自动发现 | 扫描 PATH 目录、npm 全局 `node_modules` 的 `package.json` bin 与平台子包，解析出可执行文件；**argv 恒为 `app/harness/profiles.py` 里的常量**。登记表：`codex`（可驱动）、`opencode`（可发现、暂不可驱动） |
| OpenAI 兼容 API 直连 | `OPENAI_API_KEY` 非空即自动注册 `openai-api` profile，不经子进程；系统提示词也是常量 |
| 显式命令 | `HARNESS_COMMAND` + `HARNESS_ARGS`，v0.3 起的支持方式，参数由操作者写在 `.env` |

```bash
curl -s -X POST localhost:8000/v1/run -H "content-type: application/json" \
  -d '{"task":"总结这篇论文的方法与结论","schema":"paper","harness":"codex"}'
curl -s localhost:8000/v1/harnesses            # 只扫描，不执行任何程序
curl -s "localhost:8000/v1/harnesses?probe=true"  # 额外跑 --version / login status 探测
```

页面顶部的「驱动 harness」区即这两个接口：profile 下拉来自服务端实际装配结果，检测表默认只做文件系统扫描，按下「带探测重新检测」才会执行探测。

**安全边界**（这层能力会启动子进程或发出外部请求，因此约束是硬的）：

| 约束 | 具体做法 |
| ------ | ------ |
| 参数来源 | 请求体只接受 `task` 文本与 **profile 名**（等价于用 `schema` 选内置模型）；带 `command`/`args`/`cwd`/`model`/`base_url` 的请求直接 422——请求模型是 `extra="forbid"`，未知键不再被静默忽略。有测试固定这一行为 |
| 配置来源 | 命令、参数、工作目录、环境变量只来自 `.env`；`config/pipeline.yaml` 不接受它们（该文件入库，命令入库等于检出即可执行） |
| 白名单 | 显式模式下 `HARNESS_COMMAND` 必须是绝对路径、真实存在且在 `HARNESS_ALLOWED_COMMANDS` 中；按 basename 放行等于允许 `python -c`，不做妥协 |
| 发现器约束 | 只认内置 profile 声明的包名与入口；候选解析后必须仍在该包目录内（拒绝越界软链）；Windows 上必须是带 `MZ` 头的真实 PE——npm 的 `codex.cmd` 与未跑 postinstall 留下的 `opencode.exe` 脚本桩都会被拒 |
| 探测副作用 | 扫描与启动阶段**不执行任何被发现的程序**；只有 `probe=true` 才执行，参数取 profile 常量、stdin 关闭、受 `HARNESS_PROBE_TIMEOUT_MS` 约束，失败一律降级为"已检测未验证" |
| 输入传递 | `task` 经 **stdin**（或 API 的 user 消息）传入，不进 argv |
| 子进程环境 | 重建最小环境，不继承 `os.environ`；`OPENAI_API_KEY` 等密钥默认不进子进程 |
| 工作目录 | 发现式 profile 未配 `HARNESS_CWD` 时自动使用独立临时空目录，不让外部 agent 继承服务进程 cwd（本仓库） |
| 资源约束 | `HARNESS_TIMEOUT_MS` 超时即杀并收割进程；`HARNESS_MAX_OUTPUT_BYTES` 之外标记截断；`HARNESS_MAX_CONCURRENCY` 超限返回 429 |
| 默认值 | `HARNESS_ENABLED=false`；未启用时 `POST /v1/run` 恒返回 503，指引调用方使用 `/v1/optimize` |

并发预算是**进程级共享**的一份（`ConcurrencyBudget`），所有装配出来的成员用同一个额度：按成员各自计数时，真实上限会变成 `profile 数 × HARNESS_MAX_CONCURRENCY`，而 429 对外承诺的是"并发超限"这一件事。探测面同样受限——`/v1/harnesses?probe=true` 的结果带 TTL 缓存（60s）与单飞锁，重复点击不会重复起子进程，响应里的 `probe_cached` 表示当前缓存条目数。

**状态码语义**：422 未知 schema 或未知 profile 名、503 未启用或命令不可用、429 并发超限、504 超时、502 harness 非零退出或 API 调用失败。harness 失败时**不会**进入优化管线，也不会写审计记录。

**与 post-hoc 契约的关系**：驱动层等价于"调用方"，只是调用方从业务代码换成了页面按钮。`optimize()` 的输入输出契约、评分口径与审计结构均不因它改变；关闭驱动时本节省略即可，其余章节自洽。

**已实测与未实测**：发现器在本机真实环境验证过——能解析出 `node.exe + codex.js` 的组合入口（`which` 只会命中不能执行的 `codex.cmd`），能跳过 `opencode` 包声明的 479 字节脚本桩并定位到平台子包里的真实 PE，探测得到 `codex-cli 0.148.0` 与 `Logged in using ChatGPT`。**未验证**：`openai-api` 的成功返回（需要真实密钥）、`opencode` 的端到端驱动（非交互契约未确认，故登记为不可驱动）。

> 启用 codex 前请先确认已安装的 CLI 版本支持账号当前使用的模型。版本不匹配时 harness 会以非零退出，本层返回 502 并透传其 stderr，**不会**进入优化管线、也不写审计记录。

## 8. 配置参考

### 8.1 环境变量

| 变量 | 默认值 | 说明 |
| ------ | -------- | ------ |
| `PROVIDER` | `mock` | `mock`、`openai`、`anthropic`、`gemini` 或 `openrouter` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容服务地址 |
| `OPENAI_API_KEY` | 空 | 仅 `provider=openai` 时必填 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 修复与通俗化改写使用的模型 |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com/v1` | Anthropic Messages 服务地址 |
| `ANTHROPIC_API_KEY` | 空 | 仅 `provider=anthropic` 时必填 |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Anthropic 模型名 |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` | Gemini generateContent 服务地址 |
| `GEMINI_API_KEY` | 空 | 仅 `provider=gemini` 时必填 |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini 模型名 |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter 路由地址（OpenAI 兼容协议） |
| `OPENROUTER_API_KEY` | 空 | 仅 `provider=openrouter` 时必填 |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | OpenRouter 模型名（带厂商前缀） |
| `OPTIMIZE_SCORE_THRESHOLD` | `0.8` | 低于该分值触发修复 |
| `OPTIMIZE_MAX_REPAIR_RETRIES` | `1` | 修复调用上限 |
| `OPTIMIZE_TIMEOUT_MS` | `8000` | 整条管线的时间预算 |
| `OPTIMIZE_FAIL_OPEN` | `true` | 异常时是否回退原始输出 |
| `AUDIT_PATH` | `data/audit.jsonl` | 审计文件路径 |
| `HARNESS_ENABLED` | `true` | harness 驱动总开关；开启但无可驱动成员时 `/v1/run` 回 503 |
| `HARNESS_PROVIDERS` | 空 | 逗号分隔的 profile 白名单（如 `codex,openai-api`），空表示全部可用 |
| `HARNESS_PROFILE` | 空 | 默认 profile，空表示取第一个可用的 |
| `HARNESS_DETECT` | `true` | 是否扫描本机已安装的内置 profile CLI |
| `HARNESS_PROBE_TIMEOUT_MS` | `5000` | 页面点「带探测重新检测」时单次探测的超时 |
| `HARNESS_COMMAND` | 空 | 显式模式的可执行文件绝对路径，须出现在白名单中 |
| `HARNESS_ALLOWED_COMMANDS` | `[]` | 允许执行的可执行文件绝对路径，JSON 数组 |
| `HARNESS_ARGS` | `[]` | 固定参数，JSON 数组；`{output_file}` 会替换为本次临时输出文件路径 |
| `HARNESS_CWD` | 空 | 子进程工作目录，建议指向空的临时目录 |
| `HARNESS_TIMEOUT_MS` | `120000` | 单次 harness 调用超时，与 `OPTIMIZE_TIMEOUT_MS` 相互独立 |
| `HARNESS_MAX_OUTPUT_BYTES` | `262144` | 取回内容的上限，超出标记截断 |
| `HARNESS_MAX_CONCURRENCY` | `1` | 同时在跑的 harness 子进程数，超限返回 429 |
| `HARNESS_ENV_ALLOW` | 空 | 逗号分隔的额外环境变量名；加入即真的传入子进程，密钥类变量不要写 |

> 变量名即表中形式（大小写不敏感）。注意 `OPTIMIZE_*` 属于管线阈值，会被 `config/pipeline.yaml` 中的同名顶层键覆盖——例如仓库自带的 `fail_open: true` 优先于 `.env` 里的 `OPTIMIZE_FAIL_OPEN=false`，需要严格模式时请同时改 `pipeline.yaml`。`HARNESS_*` 不在此列：命令类配置只能来自环境变量，`pipeline.yaml` 不接受它们（见 §7.5）。

### 8.2 管线编排

```yaml
steps:
  - name: schema
    enabled: true
    strict: false          # false 时剔除多余字段而非直接判失败

  - name: rules
    enabled: true
    weights:                # 两组权重分别作用于散文与结构化；写在这里会覆盖 app/settings.py 的缺省
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
    enabled: true          # 通俗化改写
    prose_only: true       # 只改写散文；设为 false 才会动结构化字段
    prompt: prompts/simplify.txt

fail_open: true
audit: true
```

配置优先级固定为：**代码默认值 < `.env` < `pipeline.yaml`**。`pipeline.yaml` 仅允许覆盖步骤与阈值，密钥一律从环境变量读取。

## 9. 效果评估

评估基于人工标注的金标集 `data/golden.jsonl`，每行一条样本：

```json
{"id":"g001","instruction":"抽取论文元数据并规范参考文献","input_text":"《基于深度学习的图像分类方法研究》，张三、李四，CVPR 2023。关键词：深度学习；图像分类。","raw_output":"{...}","expect":{"valid":true,"fields":{"year":2023,"authors":["张三","李四"]}}}
```

金标集中预存 `raw_output`，因此评估过程**不调用任何模型**，结果完全可复现；追加 `--live` 参数才会实时调用模型生成。34 条样本按三类 Schema 分层，成对覆盖「应判失败」与「应修复」两侧：围栏与客套话前后缀、别名键（`paper_title`/`author`/`date`/`conference`/`citations`/`measure`）、全角标点与中文单位、空字段与多余字段、越界年份、无法定位的数值与幻觉引用、JSON 数组与纯文本引用等非常规形态。

```bash
python scripts/evaluate.py --dataset data/golden.jsonl --provider mock --out data/reports/mock.md
```

| 指标 | 计算方式 | 目标值 |
| ------ | ---------- | -------- |
| 通俗度 plainness | 改写后叙述文本的 plainness 均值（同一规则口径重测），报告同时给出较改写前的增量 | 均值 0.8 以上 |
| 误删研究边界比例 | 判定含研究边界的样本中，改写后指称消失的比例（与采纳闸门同一函数判定） | 0% |
| 无据限定清除率 | 改写后不再出现的无据限定句 / 改写前无据限定句总数 | 不低于 90% |
| Schema 合规率 | 优化后通过数 / 样本总数，与优化前对比 | 提升 20 个百分点以上 |
| 关键字段准确率 | 与 `expect.fields` 完全一致 / 含断言样本数 | 不低于 90% |
| 引用格式合规率 | 参考文献符合目标引用规范（GB/T 7714 或 APA）/ 引用总数 | 不低于 95% |
| 事实保持率 | 修复与改写后数值、引用与原文一致 / 变更总数 | 100% |
| 平均分变化 | 优化后均值减优化前均值 | 提升 0.1 以上 |
| 修复成功率 | 被采纳的修复 / 修复触发次数 | 不低于 50% |
| 额外延迟 P95 | 管线耗时分位数 | 不修复时 300ms 内，修复时 8s 内 |
| Fail-open 率 | 降级次数 / 请求总数 | 低于 1% |

报告输出为 Markdown，同时生成同名 CSV 供绘图使用。CSV 逐行含 `boundary_total / boundary_kept / ungrounded_total / ungrounded_removed` 四列，误删与清除率就是从这些列汇总的。两者含逐条耗时，每次运行字节级不同，因此作为本地产物不入库（已在 `.gitignore` 中排除），关键结果摘录如下表。

#### mock 模式实测结果

`python scripts/evaluate.py --dataset data/golden.jsonl --provider mock --out data/reports/mock.md`（39 条样本，其中 5 条散文；未调用任何模型，结果与运行环境无关）：

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

两行未达标，各有其度量上的原因，逐条写清：

- **修复成功率**：mock provider 按设计不补造缺失事实，其修复候选在仍不合规时被闸门拒绝——这正是闸门应有的行为，由 `tests/test_step_repair.py` 单独覆盖。该行只在 `--live` 接真实模型时可判定。曾考虑增设「修复触发正确率」把这一格变绿，但 `RepairStep.should_run` 的触发条件与 `rules` 步骤的判据互为逆否，新指标恒等于 100%，属于假信号，故不加。
- **无据限定清除率差一句**：未清除的那句是“受制于算力，其有效性有待进一步验证”——句中既无数字也无领域名词，按“零指称即无据”被判为 ungrounded，而 mock 的对照表也没有可删它的模板。这是指称启发式的已知局限（见 §11），不是漏删套话；要把它当边界保护，得让泛词参与匹配，代价是太极话也会一起被保护。
- 边界与无据两项指标只在散文样本上才有意义：金标集曾一度全是带 Schema 的样本，那时“通俗度提升”恒为 +0.000、这两项也无从测量。合并 main 的 34 条结构化样本后，散文仍是 5 条——**指标分母由这 5 条决定**，扩充它们比扩充结构化样本更影响这两行的可信度。

“优化前”判据为：对 `raw_output` 直接 `json.loads` 并按严格类型校验，即下游程序不经任何处理能否直接消费。「平均分变化」的优化前得分与它同判据：原始结构直接进 `rules` 计分，**不经 SchemaStep**。此前该行复用 `rescore()`，别名映射与类型归一已把优化器的收益预先算进“优化前”，同一口径下分差被压成 +0.005；口径对齐后实测 +0.289。

## 10. 可靠性设计

- **步骤级隔离**：单个步骤异常不会中断管线，仅记录原因并继续后续步骤。
- **硬超时**：整条管线由 `asyncio.wait_for` 包裹，超时按 fail-open 处理，不会挂起调用方。
- **采纳闸门**：修复结果须同时满足“通过校验”与“得分不降低”；通俗化改写按顺序须满足事实不变（数值、引用锚点）→ 研究边界不丢失 → 没有变得更长，否则丢弃并在审计中标记 `repair_rejected` 或 `simplify_rejected`。
- **跨版本可复现**：加权评分用 `math.fsum` 精确求和，而不是 `sum()`。`sum()` 自 Python 3.12 起改用补偿求和，同一组权重在 3.11 与 3.12+ 相差 1 ULP，会把得分推到 `round()` 的两可边沿——同一份输入不应因解释器版本而变 0.001。
- **启动期校验**：Provider 配置缺失或密钥为空时在启动阶段直接失败，避免运行期才暴露问题。
- **参数不静默失效**：请求模型一律 `extra="forbid"`——开关名写错（页面曾把 `score` 写成 `rules`）过去会被 `extra="ignore"` 吞掉，表现是勾选有效、请求 200、管线照旧，没有任何地方报错。长度同样在入口封顶（`raw_output` 200k、`input_text` 500k、`instruction`/`task` 8k），因为审计会把输入整段落盘；封顶放在入口而不是落盘截断，后者会制造"审计里没有但发生过"的黑洞。
- **两种模式同参数**：`/v1/optimize` 与 `/v1/run` 收同一组逐请求参数（含 `reference_style`），审计里另记 `reference_style` 为**实际生效值**而不是请求原话——不带这个键时记的是配置默认。这里曾漏过前端：后端两层都"支持"，页面在 Harness 模式却没发这个键，表现是选了 APA 仍按 GB/T 排、请求 200、日志无痕。
- **配置告警**：`/v1/config` 的 `warnings` 列出"写了 `.env` 但不生效"的组合（未知 profile 名、`HARNESS_COMMAND` 为空却设了 `HARNESS_ARGS`、`HARNESS_ENV_ALLOW` 含疑似凭据项）。做成告警而非拒绝启动：拼错一个 profile 只该关掉驱动面，不该让粘贴路径起不来。
- **历史面板独立降级**：`AUDIT_PATH` 指向目录或读不动时，`/v1/audits` 返回 200 + 空列表 + `error` 说明，不把整页打成 500；尾部读取同时受行数与字节预算约束，只要 1 条历史不会去扫 2000 行。
- **审计降级**：审计写入失败仅记录警告，不影响主流程返回结果。

## 11. 适用范围与已知限制

- 自定义 Schema 只享 Pydantic 通用面：任何 `BaseModel` 子类都能参与校验、评分与修复（必填/选填取自 `model_fields`，字段说明用 `model_json_schema()`）；但**纯文本→结构的定制解析**（`parse_text`）与提示词里的字段说明定制需要继承 `AcademicModel`，否则相应能力按"不可测"退化——不可测的维度会被剔出加权，不会被算成 0 分。传进来的根本不是 Pydantic 类会在入口抛 `TypeError`，不混进 fail-open。
- 规则评分关注结构、格式、事实溯源与**措辞层面**的防御/晦涩信号，对语义层面的错误（论断是否成立、推理是否跳步）不敏感；改写质量依赖所接模型能力，可用人工抽检评估。
- 去防御化只保证“事实不变、研究边界不丢、更短更直白”，不保证观点正确，也不保证术语准确性；结论责任仍在作者。词表是人工整理的候选集（非从真实语料统计得出），需按你的实际场景增删；`concision` 依赖拆句能力，离线 mock 只能删套话、不会拆句也不会重排句序，因此 `concision` 与“主张先行”在 mock 下的提升都有限，需接真实模型复测。
- **判定“无据限定 vs 研究边界”靠指称启发式**：句中要有能划定范围的指称——领域名词、标识符，或伴随边界线索的数字——才可能被认成边界。两处已知代价：不含这些成分的**文字性真实边界**（如“受制于算力”“该结论仅适用于横截面数据”中的前者）会被判为 ungrounded；反过来，带标识符的**撤回句**（“这并不意味着 ResNet-50 上的结果可以推广”）会被当边界保护下来——自我否定与真范围叠加时按**保守原则保留**（标注集 q21，该取舍已确认）。把泛词（场景/条件/规模）纳入指称可以救前一类，代价是太极话也会一起被保护——当前版本选择不救。评估里的“无据限定清除率”就是在暴露这个代价。
- `data/qualifications.jsonl` 是判定标准的**唯一定义处**，也是 `tests/test_qualifications.py` 的回归依据；标注集（现 23 条）由 `tests/test_readme_contract.py` 与文件逐条核对，这句话里的数字不能手抄。首轮自审改（14 条 → 21 条）查出三处系统性误判——裸数字被当成范围证据，导致撤回句、降级句与“可能在 2 个场景下有效”这类太极话被判为受保护边界，判定规则已收紧并各补标注。**第二轮改用真实模型生成的回答来审**（21 条 → 23 条），当场发现收紧过头：`这一结论仍需结合仅覆盖 240×320 分辨率的实验设置谨慎解读` 是货真价实的研究范围声明，却因“仅覆盖”不在边界线索表里、数字又无线索时不算指称而被判为可删——线索表补入覆盖类标记（仅覆盖/仅使用/仅测试/覆盖/涉及），并加一条对照标注说明**有覆盖线索但仍零指称**（“涉及若干医院”）依然算无据。自造样例查不出的错，真实输出一次就查出来了；但标注与分类器仍出自同一作者，标准本身是否正确，还得靠更大规模的真实语料复核。
- 事实溯源只**报告**、不**清除**：数值或引用在原文中定位不到时，`grounding` 维度相应降分并触发修复，但优化器不删除可疑条目——判断“哪一条是幻觉”需要外部知识库比对，本版本不做。该行为由金标集 `g039` 固化为回归断言（一条不在原文中的引用经修复采纳后仍保留在输出里）。
- 当前版本仅承诺 OpenAI 兼容协议；访问 Anthropic、Gemini 等专有协议需实现对应的 Provider 适配器。
- harness 驱动（§7.5）默认开启，但仍是**仅本机**的调用面：无鉴权、不流式、只取最终回答，不解析工具调用轨迹，因此不要把这个服务暴露到不可信网络。可驱动的 CLI 由内置 profile 表穷举——**已检测到但未登记的 CLI 只会显示"暂不支持驱动"，不会得到执行入口**；新增一个 CLI 需要改代码。Windows 下不能直接 exec npm 的 `codex.cmd`/`codex.ps1` 包装，发现器因此解析 `node.exe + codex.js`。子进程回收放在 `finally` 里，超时与外层取消都会终止并收割**直接子进程**；**其派生的孙进程不保证被清理**（Windows 上 `TerminateProcess` 只杀单层，而引入 `taskkill /F /T` 等于再加一个执行面，本版本不做）。stdout 截断保留的是**尾部**（最终回答在末尾），但被腰斩的 JSON 仍然不是合法 JSON，`truncated` 标志会带在留痕里。子进程 stderr 仍回显给调用方，只是按形状抹掉 `sk-…`/`token=…` 这类凭据值。`openai-api` 形态需要真实密钥才能端到端验证。
- 修复能力受修复模型本身能力约束，闸门仅能保证“不比原来更差”，不能保证“一定更好”。
- 依赖只声明区间、不锁版本（仓库无 lock 文件），CI 每次重新解析最新版。后果不是构建失败而是**数字不可复现**：transitive 依赖（pydantic-core、coverage、anyio 等）换个小版本，覆盖率与得分末位就会抖，跨平台的 README 比对因此只能放容差（§13 与 `.github/CI.md` 都写了这点）。彻底解决需要一份带平台 markers 的锁文件与"改了区间必须重锁"的新鲜度守卫，本版本未做。
- 性能数据会随修复模型、网络状况与输出长度变化，评估结论应结合具体环境复测。

## 12. 项目结构

```text
.
├── app/
│   ├── main.py                  # FastAPI 入口：/v1/optimize、/healthz
│   ├── settings.py              # 配置加载：.env 与 pipeline.yaml（含优先级合并）
│   ├── schemas.py               # 学术场景 Pydantic 模型：paper / reference / experiment
│   ├── textio.py                # 围栏剥离、JSON 抽取、空白归一、提示词加载
│   ├── plain.py                 # 语言规则层：词表、限定语判定、通俗度/自信度/简洁度打分
│   ├── harness/                 # 实验面：发现并驱动上游 harness
│   │   ├── base.py              # HarnessRun 与异常族
│   │   ├── profiles.py          # 内置 profile 常量表（argv 只能来自这里）
│   │   ├── detect.py            # 纯文件系统发现 + 按需探测（PE 校验、桩识别）
│   │   ├── command.py           # CommandHarness：白名单、stdin 传参、超时回收
│   │   └── api.py               # ApiHarness：组合 OpenAI 兼容 provider
│   ├── viewer.py                # 本地查看页：页面挂载、环境信息、只读审计尾部
│   ├── web/                     # 页面资源：index.html / styles.css / app.js / favicon.svg / fonts/（零构建）
│   ├── audit.py                 # JSONL 审计
│   ├── optimizer/
│   │   ├── __init__.py          # optimize() 对外入口
│   │   ├── pipeline.py          # 编排、超时、fail-open、采纳判定
│   │   ├── context.py           # 步骤上下文
│   │   ├── scoring.py           # 候选文本重评分，供采纳闸门比较
│   │   └── steps/
│   │       ├── base.py          # StepResult / BaseStep / 步骤注册表
│   │       ├── schema.py  rules.py  repair.py  format.py  simplify.py
│   │       └── references.py    # 参考文献解析与 GB/T 7714 / APA 渲染
│   └── providers/
│       ├── base.py              # Provider 统一接口
│       ├── openai_compatible.py # OpenAI 兼容适配器
│       └── mock.py              # 离线与测试用
├── config/pipeline.yaml
├── prompts/
│   ├── repair.txt
│   └── simplify.txt
├── data/
│   ├── golden.jsonl             # 金标集（39 条，含 5 条散文样本，预存 raw_output）
│   ├── qualifications.jsonl     # 限定语判定标注集（ungrounded / boundary / unverified）
│   ├── request.example.json     # HTTP 请求样例，供 curl --data-binary 使用
│   └── reports/                 # 评估报告（Markdown + 同名 CSV）
├── scripts/
│   ├── demo.py
│   └── evaluate.py
├── tests/                       # 含 golden_baseline.json 回归快照
├── .env.example
├── pytest.ini
├── requirements.txt
├── LICENSE
└── README.md
```

## 13. 开发与测试

```bash
pytest -q
pytest -q --cov=app --cov-branch --cov-fail-under=80 --cov-report=term-missing
```

本节下文引用的**用例数与综合覆盖率由 CI 硬校验**：Windows job 把这两个数字与同一次运行的 `junit.xml`/`coverage.json` 比对（见 `.github/CI.md`）。用例数按**下界**比对：加用例不必回来改这里，也不会让 CI 变红——只有实跑数掉到下界以下（说明文档在说谎）才失败，下界过时得太多只出告警。覆盖率仍按 ±0.5pp 逐值比对，因为它没有"只增不减"的性质。分数增益、修复率等研究目标不在硬校验范围内——它们取决于数据集与模型，只呈现于评估报告。

测试覆盖范围：

- 步骤单测：给定输入断言 `passed` 与 `score`，不触网。
- Provider 单测：使用 `respx` 模拟 `/chat/completions`，覆盖成功、限流与超时。
- 管线单测：异常注入验证 fail-open 与超时行为。
- 去防御化单测：以 mock 改写结果断言数值与引用锚点不变、改写不得变长；词表单测断言重叠短语只计一次、单个正当 hedge 不扣分。
- 判定标准单测：`tests/test_qualifications.py` 逐条比对 `data/qualifications.jsonl` 的 `expect`，并断言两侧不重叠——同一段文字里“无据句”自信度为 0、“边界句”为 1；另含同族名词互认（句子写“样本量”、原文写“被试”）、单位数指称（“3 个数据集”）与停用词（句首 `This`/`Results` 不算标识符）。
- 闸门单测：断言改写把判定为边界的限定语删掉时**整段拒绝**（`simplify_rejected`），无原文时未验证边界同样受保护。
- 回归测试（`tests/test_golden_regression.py`）分两条，各自测不同的东西：
  - `test_deterministic_snapshot_matches_baseline`：`provider=None`、只跑 schema+rules+format 的**确定性路径**快照，测的是我们自己的代码；`OPTIMIZE_UPDATE_GOLDEN=1 pytest` 可重写。
  - `test_mock_rewrites_obey_the_adoption_invariants`：开 simplify 跑一遍，但**不比字节**，只断言采纳不变式（事实不变、边界不丢、长度不超 `GROWTH_LIMIT`、`degraded is False`），并要求至少有一条真的被采纳——否则这套断言等于没测。

  此前只有一条"用 mock 现跑现比对快照"，那测的其实是 mock 词表的字符串替换：扩充对照表会让基线集体变红，而与优化质量无关。效果指标见 §9，回归不证明模型改写质量。
- 导入隔离测试：`tests/test_import_isolation.py` 在干净子解释器里逐个模块执行 `import X`，断言每个对外模块都能作为**第一个**被导入的模块加载。这条不变式必须单独测：跑完整套件时 `app.main` 总是先被导入，模块间的环被导入顺序掩盖，`import app.settings` 单独失败这种事永远不会暴露。
- harness 装配单测：以桩 `scan` 结果与假成员覆盖 `_detected()` 过滤、`HarnessRegistry` 选择与级联关闭、`build_harness` 多源合并，验证装配逻辑且全程不 exec。
- 引用方言单测：断言姓名判定否决、类型推断、`render_apa` 各作者数与 `parse_citation` 失败退回等确定性分支。

v0.6 实测：≥550 项用例通过（写成下界，精确值以同一次运行的 `junit.xml` 为准——绝对数会让"别人往 main 合了一个测试"的 PR 也红 windows 作业），`app/` 行与分支综合覆盖率 95.06%（win32，Python 3.11.8 / 3.12.13 / 3.13.13 / 3.14.9 四套隔离环境结果一致；CI 门槛 80%）。**同一份输入在这些版本上得分逐位一致**：加权评分走 `math.fsum` 精确求和，因为 `sum()` 自 3.12 起改用补偿求和，`0.35+0.3+0.2+0.15` 在 3.11 上少 1 ULP，恰好把 0.6125 推到 `round()` 的两可边沿——金标快照曾因此在 3.12+ 上失败、3.11 却通过。用例数不随平台与解释器版本变化，覆盖率只随平台有 0.1pp 量级的浮动（linux 略高）。本节数字由 `.github/scripts/check_readme_metrics.py` 与同一次运行的 `junit.xml`/`coverage.json` 比对，下界失守或覆盖率漂移即 CI 红；§9 指标表另由同一次 `evaluation.metrics.json` 同源比对，测试里不再各抄一份常量——README 写了的数才允许被断言，见 `tests/test_readme_contract.py`。

## 14. 版本规划

| 版本 | 范围 | 状态 |
| ------ | ------ | ------ |
| v0.2 | 核心管线（含拟人化重写）、学术场景 Schema、OpenAI 兼容 Provider、审计与评估 | 已交付 |
| v0.3 | harness 驱动（白名单子进程）、本地查看页、`/v1/audits` 只读历史 | 已交付 |
| v0.4 | 通俗化改为主线：词表与句式检测、plainness/concision 评分、散文默认开启 | 已交付 |
| v0.5 | 防御性写作成为可判定目标：无据限定/研究边界分类、自信度与主张先行、边界丢失否决采纳 | 已交付 |
| v0.6 | harness 驱动成为任务主路径：`HARNESS_ENABLED` 默认开启、页面默认入口跟随可用性、无成员时降级回粘贴 | 已交付 |
| v0.7 | 新增 Anthropic、Gemini、OpenRouter 适配器（已交付）；标注集扩充到真实语料抽样，据此校准指称启发式阈值（规划中） | 部分交付 |
| v0.8 | 可选 LLM 评分维度（含自然度）、多候选输出重排 | 规划中 |
| v0.9 | 基于审计数据的阈值调优与漂移检测 | 规划中 |
| v1.0 | 持久化存储、监控接入与可视化面板 | 规划中 |

## 15. 贡献指南

欢迎通过 Issue 与 Pull Request 参与共建。

1. Fork 本仓库
2. 创建分支：`git checkout -b feature/your-feature`
3. 提交修改：`git commit -m "feat: add your feature"`
4. 推送分支：`git push origin feature/your-feature`
5. 提交 Pull Request

提交前请确认：

- 新增 Provider 或 Step 附带对应测试
- `pytest` 全部通过
- 接口变更同步更新 README 与配置示例

## 16. 许可证

本项目基于 [MIT License](LICENSE) 开源。

- 允许商业使用、修改、分发、再许可与私人使用
- 分发时须保留原始版权声明与许可声明
- 软件按“现状”提供，不附带任何明示或暗示的担保

Copyright (c) 2026 Let-the-output-better contributors