# Let-the-output-better

**面向学术科研的 Post-hoc AI 输出优化器**

在任意大模型生成完成之后，对输出执行校验、评分、修复、格式化与拟人化重写，在不修改模型、不介入推理过程的前提下，持续提升科研场景输出的结构合规性、内容准确性、事实可溯源性与表达自然度。

> **项目状态：v0.2 已实现。** 仓库包含设计文档、可执行代码与评估方案，下文“快速开始”“使用指南”即为当前可用用法；mock 模式实测数字见 [§9 效果评估](#9-效果评估)。

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

本项目采用 **Post-hoc（后置优化）** 思路：模型照常生成，优化器只在输出完成之后介入。调用方持有原始输出，交由优化器完成校验、评分、修复、格式化与拟人化重写，最终返回可直接使用的结构化结果或自然的学术文本。

这一设计带来三个直接收益：

- **与模型解耦**：同一套优化策略适用于任意支持 OpenAI 兼容协议的服务，包括云端 API 与本地部署模型。
- **不损害交互体验**：优化发生在生成结束之后，不参与推理，不影响首 Token 延迟与流式输出本身的体验。
- **可度量、可回滚**：每次优化均留痕，能够对比优化前后效果；优化器异常时回退原始输出，业务流程不受阻断。

需要说明的是，instructor、guardrails 等库同样采用“生成后校验与重试”的事后思路，本项目的差异化不在“后置”本身，而在三点：质量判定使用**可复现的确定性规则评分**，不依赖额外模型调用；修复结果须通过“校验通过 + 得分不降低”**双闸门**才被采纳；每次优化的全过程以 JSONL 留痕，可直接用于回归分析与阈值调优。面向科研场景，评分额外强调**事实溯源**——输出中的数值与引用必须能在原文中定位。

适用场景：文献调研与综述整理、论文元数据抽取、参考文献规范化、实验结果结构化、科研写作辅助。本项目聚焦学术科研场景，内置对应 Schema 与评估集。

## 2. 核心特性

- **模型无关接入**：基于 OpenAI 兼容协议（`/chat/completions`）实现统一适配，可指向 OpenAI、DeepSeek、Qwen、Moonshot、vLLM、Ollama 等服务，切换模型仅需修改配置。
- **五步可组合管线**：Schema 校验、规则评分、修复重试、格式化、拟人化重写，各步骤独立开关，支持按科研任务裁剪。
- **学术场景内置 Schema**：`paper`（论文元数据）、`reference`（参考文献）、`experiment`（实验结果）开箱即用，也支持自定义 Pydantic 模型。
- **确定性规则评分**：从字段完整度、格式合规、与输入一致性三个维度加权计分，数值与引用须可在原文定位；结果可复现、可单测，不依赖额外模型调用。
- **拟人化重写**：去除空泛套话与模板化表述，将“AI 味”文本重写为接近人类研究者的自然学术语体；事实与数值保持不变，通过事实一致性校验后才采纳。
- **修复采纳闸门**：修复结果仅在“通过 Schema 校验且得分不低于原始输出”时被采纳，避免修复引入新错误。
- **默认 Fail-open**：任一环节异常或超时，均返回原始输出并记录降级原因，不阻断调用方业务。
- **全链路审计**：以 JSONL 记录原始输出、最终输出、各步骤得分、耗时与变更原因，可直接用于回归与数据分析。
- **离线可验证**：内置 mock provider，无网络、无密钥即可跑通完整管线、测试与评估流程。

## 3. 设计原则

1. **只做后处理**：不介入模型推理，不改变生成过程，不影响首 Token 延迟。
2. **接口稳定优先**：对外契约只有 `optimize()` 与 `POST /v1/optimize` 两个入口，内部步骤可替换；另附一个本地查看页（`GET /`）与只读历史端点，属调试工具，不构成新的稳定接口。
3. **失败不放大**：任何优化步骤的异常都以“返回原始输出”收口，禁止因优化器故障导致业务失败。
4. **结论可追溯**：每个得分都必须能说明来源，每个变更都必须有对应的审计记录。
5. **默认值即最佳实践**：开箱配置即为经过评估的安全取值，调优属于可选行为。

## 4. 系统架构

```text
raw_output ──► schema ──► rules ──► repair ──► format ──► humanize ──► final_output
                 │          │         │          │           │
                 └──────────┴─────────┴──────────┴───────────┴──► 审计（JSONL）+ 采纳判定
```

优化器不主动调用主模型：原始输出由调用方提供。优化器内部仅在触发修复或开启拟人化重写时调用独立模型，因而不会与业务方的调用链产生耦合，也便于在同一请求内复用已有的模型响应。

## 5. 优化管线

| 步骤 | 职责 | 是否可关闭 | 默认 |
| ------ | ------ | ------------ | ------ |
| `schema` | 使用内置学术 Schema 或自定义 Pydantic 模型校验结构，支持剥离 Markdown 围栏后重试解析 | 是 | 开启 |
| `rules` | 按字段完整度 0.4、格式合规 0.3、与输入一致性 0.3 加权计分；数值与引用须可在原文定位 | 是 | 开启 |
| `repair` | 未通过或低分时调用修复模型，最多 1 次，超时 6s | 是 | 开启 |
| `format` | 去空白、去围栏、统一日期/数值/单位与引用格式（GB/T 7714 或 APA）、剔除多余字段 | 是 | 开启 |
| `humanize` | 调用模型去除空泛套话、重写为自然学术语体；事实与数值不变，通过事实一致性校验后才采纳 | 是 | 关闭 |

拟人化重写示例（开启 `humanize` 时）：

- 重写前：“值得注意的是，实验结果充分表明，本文方法具有显著的性能优势和广阔的应用前景。”
- 重写后：“实验表明，本文方法性能优于对比方法。”

空泛套话被剔除，事实性结论保留；数值、引用与关键字段在重写前后必须一致，否则丢弃重写结果。`humanize` 默认关闭，因为它引入一次模型调用；开启时建议适当调大 `timeout_ms`。

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

- Python 3.10 及以上
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
  "options": { "score": true, "repair": true, "format": true, "humanize": false, "timeout_ms": 8000 }
}
```

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
| `score` | 0 至 1 的质量得分，未启用评分时为 `null` |
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

在 `config/pipeline.yaml` 的 `steps` 列表中登记后立即生效；若校验不通过，将按配置触发修复或直接返回。

### 7.4 可视化页面

起服后访问 `/`，三栏布局：左为历史会话，中为输入与结果流，右为运行环境与优化前后对照。

| 区域 | 内容 | 数据来源 |
| ------ | ------ | ------ |
| 历史会话 | schema、状态、得分、耗时、变更标记；点击回填输入区并渲染该次对照 | `GET /v1/audits` |
| 输入区 | `schema` 下拉与必填提示、`instruction`、`input_text`、`raw_output`、五个步骤开关与 `timeout_ms` | `GET /v1/config` |
| 结果流 | 结论行、步骤时间线（逐步耗时与原因）、三维评分条、变更与标记、错误 | `POST /v1/optimize` 与该次的审计记录 |
| 运行环境 | provider、阈值、超时、fail_open、引用风格、审计条数、健康灯 | `GET /v1/config`、`GET /healthz` |
| 前后对照 | 行级 diff，增删分色 | 审计记录的 `raw_output` 与 `final_output` |

局限与注意点：

- 页面是**本地调试工具，无鉴权**，不要暴露到不可信网络。
- 历史只读 `AUDIT_PATH` 尾部（单次最多 200 条）；多进程共用同一审计文件时，列表会混入其它来源的记录。
- `request_id` 由进程内计数器生成，多进程会产生重号，因此页面以审计文件的**行号**（`line_no`）作为选中依据。
- 步骤开关默认取 `pipeline.yaml` 的取值，提交时显式发送布尔值，不再沿用“未设置即跟随配置”的三态语义。
- 模型输出与审计内容一律按纯文本渲染，测试用例断言页面不存在 `innerHTML` 等注入写法。
- diff 在前端计算，超过 400 行自动降级为只显示优化后结果。
- 布局在桌面宽度下为三栏，窄于 1040px 自动折叠为单列；左上按钮可收起历史栏。

## 8. 配置参考

### 8.1 环境变量

| 变量 | 默认值 | 说明 |
| ------ | -------- | ------ |
| `PROVIDER` | `mock` | `mock` 或 `openai` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容服务地址 |
| `OPENAI_API_KEY` | 空 | 仅 `provider=openai` 时必填 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 修复与拟人化重写使用的模型 |
| `OPTIMIZE_SCORE_THRESHOLD` | `0.8` | 低于该分值触发修复 |
| `OPTIMIZE_MAX_REPAIR_RETRIES` | `1` | 修复调用上限 |
| `OPTIMIZE_TIMEOUT_MS` | `8000` | 整条管线的时间预算 |
| `OPTIMIZE_FAIL_OPEN` | `true` | 异常时是否回退原始输出 |
| `AUDIT_PATH` | `data/audit.jsonl` | 审计文件路径 |

> 变量名即表中形式（大小写不敏感）。注意 `OPTIMIZE_*` 属于管线阈值，会被 `config/pipeline.yaml` 中的同名顶层键覆盖——例如仓库自带的 `fail_open: true` 优先于 `.env` 里的 `OPTIMIZE_FAIL_OPEN=false`，需要严格模式时请同时改 `pipeline.yaml`。

### 8.2 管线编排

```yaml
steps:
  - name: schema
    enabled: true
    strict: false          # false 时剔除多余字段而非直接判失败

  - name: rules
    enabled: true
    weights:
      completeness: 0.4
      format: 0.3
      grounding: 0.3

  - name: repair
    enabled: true
    prompt: prompts/repair.txt

  - name: format
    enabled: true
    strip_markdown_fences: true
    trim_whitespace: true
    reference_style: gb-t-7714   # 可选 apa

  - name: humanize
    enabled: false         # 开启后调用模型进行学术语体重写
    prompt: prompts/humanize.txt

fail_open: true
audit: true
```

配置优先级固定为：**代码默认值 < `.env` < `pipeline.yaml`**。`pipeline.yaml` 仅允许覆盖步骤与阈值，密钥一律从环境变量读取。

## 9. 效果评估

评估基于人工标注的金标集 `data/golden.jsonl`，每行一条样本：

```json
{"id":"g001","instruction":"抽取论文元数据并规范参考文献","input_text":"《基于深度学习的图像分类方法研究》，张三、李四，CVPR 2023。关键词：深度学习；图像分类。","raw_output":"{...}","expect":{"valid":true,"fields":{"year":2023,"authors":["张三","李四"]}}}
```

金标集中预存 `raw_output`，因此评估过程**不调用任何模型**，结果完全可复现；追加 `--live` 参数才会实时调用模型生成。

```bash
python scripts/evaluate.py --dataset data/golden.jsonl --provider mock --out data/reports/mock.md
```

| 指标 | 计算方式 | 目标值 |
| ------ | ---------- | -------- |
| Schema 合规率 | 优化后通过数 / 样本总数，与优化前对比 | 提升 20 个百分点以上 |
| 关键字段准确率 | 与 `expect.fields` 完全一致 / 样本总数 | 不低于 90% |
| 引用格式合规率 | 参考文献符合目标引用规范（GB/T 7714 或 APA）/ 引用总数 | 不低于 95% |
| 事实保持率 | 修复与重写后数值、引用、关键字段与原文一致 / 变更总数 | 100% |
| 平均分变化 | 优化后均值减优化前均值 | 提升 0.1 以上 |
| 修复成功率 | 被采纳的修复 / 修复触发次数 | 不低于 50% |
| 额外延迟 P95 | 管线耗时分位数 | 不修复时 300ms 内，修复时 8s 内 |
| Fail-open 率 | 降级次数 / 请求总数 | 低于 1% |

报告输出为 Markdown，同时生成同名 CSV 供绘图使用。两者含逐条耗时，每次运行字节级不同，因此作为本地产物不入库（已在 `.gitignore` 中排除），关键结果摘录如下表。

#### mock 模式实测结果

`python scripts/evaluate.py --dataset data/golden.jsonl --provider mock --out data/reports/mock.md`（12 条样本，未调用任何模型，结果与运行环境无关）：

| 指标 | 目标 | 实测 | 达标 |
| ------ | ------ | ------ | ------ |
| Schema 合规率 | 提升 20pp 以上 | 41.7% → 83.3%（+41.7pp） | 是 |
| 关键字段准确率 | 不低于 90% | 100%（10 条含断言） | 是 |
| 引用格式合规率 | 不低于 95% | 100%（3/3） | 是 |
| 事实保持率 | 100% | 100%（5 条变更样本） | 是 |
| 平均分变化 | 提升 0.1 以上 | +0.005 | 否 |
| 修复成功率 | 不低于 50% | 33.3%（1/3） | 否 |
| 额外延迟 P95 | 不修复 300ms 内 | 0ms | 是 |
| Fail-open 率 | 低于 1% | 0.0% | 是 |

两项未达标均为口径所致，已在报告中注明：

- **平均分变化**：优化前的“原始输出”一旦可解析，即已享受与优化后相同的 Schema 类型归一，因此同一评分口径下分差有限；收益体现在合规率与引用格式，而非分数本身。
- **修复成功率**：mock provider 按设计不会补造缺失事实，其修复候选在仍不合规时被闸门拒绝（这正是闸门应有的行为）。接入真实模型后需以 `--live` 复测。

“优化前”判据为：对 `raw_output` 直接 `json.loads` 并按严格类型校验，即下游程序不经任何处理能否直接消费。

## 10. 可靠性设计

- **步骤级隔离**：单个步骤异常不会中断管线，仅记录原因并继续后续步骤。
- **硬超时**：整条管线由 `asyncio.wait_for` 包裹，超时按 fail-open 处理，不会挂起调用方。
- **采纳闸门**：修复结果须同时满足“通过校验”与“得分不降低”；拟人化重写须额外通过事实一致性校验（数值、引用、关键字段不变），否则丢弃并在审计中标记 `repair_rejected` 或 `humanize_rejected`。
- **启动期校验**：Provider 配置缺失或密钥为空时在启动阶段直接失败，避免运行期才暴露问题。
- **审计降级**：审计写入失败仅记录警告，不影响主流程返回结果。

## 11. 适用范围与已知限制

- 规则评分关注结构、格式与事实溯源，对语义层面的错误不敏感；拟人化的自然度依赖重写模型能力，可通过可选的 LLM 评分维度或人工抽检评估。
- 拟人化重写仅保证“事实不变”，不保证学术观点正确；学术结论的正确性仍由作者负责。
- 当前版本仅承诺 OpenAI 兼容协议；访问 Anthropic、Gemini 等专有协议需实现对应的 Provider 适配器。
- 修复能力受修复模型本身能力约束，闸门仅能保证“不比原来更差”，不能保证“一定更好”。
- 性能数据会随修复模型、网络状况与输出长度变化，评估结论应结合具体环境复测。

## 12. 项目结构

```text
.
├── app/
│   ├── main.py                  # FastAPI 入口：/v1/optimize、/healthz
│   ├── settings.py              # 配置加载：.env 与 pipeline.yaml（含优先级合并）
│   ├── schemas.py               # 学术场景 Pydantic 模型：paper / reference / experiment
│   ├── textio.py                # 围栏剥离、JSON 抽取、空白归一、提示词加载
│   ├── viewer.py                # 本地查看页：页面挂载、环境信息、只读审计尾部
│   ├── web/                     # 页面资源：index.html / styles.css / app.js（零构建）
│   ├── audit.py                 # JSONL 审计
│   ├── optimizer/
│   │   ├── __init__.py          # optimize() 对外入口
│   │   ├── pipeline.py          # 编排、超时、fail-open、采纳判定
│   │   ├── context.py           # 步骤上下文
│   │   ├── scoring.py           # 候选文本重评分，供采纳闸门比较
│   │   └── steps/
│   │       ├── base.py          # StepResult / BaseStep / 步骤注册表
│   │       ├── schema.py  rules.py  repair.py  format.py  humanize.py
│   │       └── references.py    # 参考文献解析与 GB/T 7714 / APA 渲染
│   └── providers/
│       ├── base.py              # Provider 统一接口
│       ├── openai_compatible.py # OpenAI 兼容适配器
│       └── mock.py              # 离线与测试用
├── config/pipeline.yaml
├── prompts/
│   ├── repair.txt
│   └── humanize.txt
├── data/
│   ├── golden.jsonl             # 金标集（12 条，预存 raw_output）
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
pytest --cov=app --cov-report=term-missing
```

测试覆盖范围：

- 步骤单测：给定输入断言 `passed` 与 `score`，不触网。
- Provider 单测：使用 `respx` 模拟 `/chat/completions`，覆盖成功、限流与超时。
- 管线单测：异常注入验证 fail-open 与超时行为。
- 拟人化单测：以 mock 重写结果断言数值、引用与关键字段在重写前后不变。
- 回归测试：以金标集的 `final_output` 作为基线快照（`tests/golden_baseline.json`，`OPTIMIZE_UPDATE_GOLDEN=1 pytest` 可重写）。

v0.2 实测：178 项用例通过，`app/` 语句覆盖率 95%；除 `tests/test_providers.py` 中被 `respx` 拦截的模拟响应外，测试全程不产生真实网络请求。

## 14. 版本规划

| 版本 | 范围 | 状态 |
| ------ | ------ | ------ |
| v0.2 | 核心管线（含拟人化重写）、学术场景 Schema、OpenAI 兼容 Provider、审计与评估 | 已交付 |
| v0.3 | 新增 Anthropic、Gemini、OpenRouter 适配器 | 规划中 |
| v0.4 | 可选 LLM 评分维度（含自然度）、多候选输出重排 | 规划中 |
| v0.5 | 基于审计数据的阈值调优与漂移检测 | 规划中 |
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