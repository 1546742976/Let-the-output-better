# Let-the-output-better

**Post-hoc AI 输出优化器**

在任意大模型生成完成之后，对输出执行校验、评分、修复与格式化，在不修改模型、不介入推理过程的前提下，持续提升输出的结构合规性、内容准确性与可直接使用性。

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

大模型输出在工程落地时常面临三类问题：结构化字段缺失或类型错误、内容偏离指令或上下文、以及夹带多余说明导致无法被下游程序直接消费。在推理阶段进行约束解码或重采样，需要深度绑定模型与供应商，接入成本高、可迁移性差。

本项目采用 **Post-hoc（后置优化）** 思路：模型照常生成，优化器只在输出完成之后介入。调用方持有原始输出，交由优化器完成校验、评分、修复与格式化，最终返回可直接使用的结构化结果。

这一设计带来三个直接收益：

- **与模型解耦**：同一套优化策略适用于任意支持 OpenAI 兼容协议的服务，包括云端 API 与本地部署模型。
- **不损害交互体验**：优化发生在生成结束之后，不参与推理，不影响首 Token 延迟与流式输出本身的体验。
- **可度量、可回滚**：每次优化均留痕，能够对比优化前后效果；优化器异常时回退原始输出，业务流程不受阻断。

适用场景包括信息抽取、结构化问答、Agent 工具参数生成、业务系统对接等对输出格式与字段准确性有明确要求的环节。

## 2. 核心特性

- **模型无关接入**：基于 OpenAI 兼容协议（`/chat/completions`）实现统一适配，可指向 OpenAI、DeepSeek、Qwen、Moonshot、vLLM、Ollama 等服务，切换模型仅需修改配置。
- **四步可组合管线**：Schema 校验、规则评分、修复重试、格式化，各步骤独立开关，支持按业务场景裁剪。
- **确定性规则评分**：从字段完整度、格式合规、与输入一致性三个维度加权计分，结果可复现、可单测，不依赖额外模型调用。
- **修复采纳闸门**：修复结果仅在"通过 Schema 校验且得分不低于原始输出"时被采纳，避免修复引入新错误。
- **默认 Fail-open**：任一环节异常或超时，均返回原始输出并记录降级原因，不阻断调用方业务。
- **全链路审计**：以 JSONL 记录原始输出、最终输出、各步骤得分、耗时与变更原因，可直接用于回归与数据分析。
- **离线可验证**：内置 mock provider，无网络、无密钥即可跑通完整管线、测试与评估流程。

## 3. 设计原则

1. **只做后处理**：不介入模型推理，不改变生成过程，不影响首 Token 延迟。
2. **接口稳定优先**：对外只暴露 `optimize()` 与 `POST /v1/optimize` 两个入口，内部步骤可替换。
3. **失败不放大**：任何优化步骤的异常都以"返回原始输出"收口，禁止因优化器故障导致业务失败。
4. **结论可追溯**：每个得分都必须能说明来源，每个变更都必须有对应的审计记录。
5. **默认值即最佳实践**：开箱配置即为经过评估的安全取值，调优属于可选行为。

## 4. 系统架构

```text
raw_output ──► schema ──► rules ──► repair ──► format ──► final_output
                 │          │         │          │
                 └──────────┴─────────┴──────────┴──► 审计（JSONL）+ 采纳判定
```

优化器不主动调用主模型：原始输出由调用方提供。优化器内部仅在需要修复时调用一次独立的修复模型，因而不会与业务方的调用链产生耦合，也便于在同一请求内复用已有的模型响应。

## 5. 优化管线

| 步骤 | 职责 | 是否可关闭 | 默认 |
| ------ | ------ | ------------ | ------ |
| `schema` | 使用 Pydantic 模型校验结构，支持剥离 Markdown 围栏后重试解析 | 是 | 开启 |
| `rules` | 按字段完整度 0.4、格式合规 0.3、与输入一致性 0.3 加权计分 | 是 | 开启 |
| `repair` | 未通过或低分时调用修复模型，最多 1 次，超时 6s | 是 | 开启 |
| `format` | 去空白、去围栏、统一日期与数字格式、剔除多余字段 | 是 | 开启 |

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
cp .env.example .env             # 默认 PROVIDER=mock
```

依赖清单（`requirements.txt`）：

```text
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
pydantic>=2.8,<3.0
pydantic-settings>=2.4,<3.0
httpx>=0.27,<1.0
PyYAML>=6.0,<7.0
pytest>=8.0,<9.0
pytest-asyncio>=0.23,<1.0
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
curl -s -X POST localhost:8000/v1/optimize -H "content-type: application/json" -d '{"raw_output":"{\"company\":\"示例科技\"}","schema":"extraction","input_text":"公司：示例科技。","options":{"repair":false}}'
```

mock 模式下，第二条命令应稳定返回 `valid` 与 `score` 字段，全过程不产生网络请求。

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
from app.schemas import Extraction

result = await optimize(
    raw_output='{"company":"示例科技","contact":"张三"}',
    schema=Extraction,
    instruction="从文本中提取公司、联系人和电话",
    input_text="联系人张三，电话 13800000000，公司：示例科技。",
    repair=True,
)

print(result.final_output)   # 结构化结果
print(result.valid)          # 是否通过校验
print(result.score)          # 质量得分
print(result.repaired)       # 是否触发修复
print(result.degraded)       # 是否发生降级
```

### 7.2 HTTP 接口

`POST /v1/optimize`

```json
{
  "raw_output": "{\"company\":\"示例科技\",\"contact\":\"张三\"}",
  "schema": "extraction",
  "instruction": "从文本中提取公司、联系人和电话",
  "input_text": "联系人张三，电话 13800000000，公司：示例科技。",
  "options": { "score": true, "repair": true, "format": true, "timeout_ms": 8000 }
}
```

```json
{
  "request_id": "req_20261116_0001",
  "final_output": { "company": "示例科技", "contact": "张三", "phone": "13800000000" },
  "valid": true,
  "score": 0.92,
  "repaired": false,
  "degraded": false,
  "steps": [
    { "name": "schema", "passed": true, "score": 1.0, "ms": 4 },
    { "name": "rules",  "passed": true, "score": 0.92, "ms": 1 }
  ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0 }
}
```

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

class BusinessRuleStep(BaseStep):
    name = "business_rule"

    async def run(self, ctx) -> StepResult:
        company = (ctx.parsed or {}).get("company", "")
        if company not in WHITELIST:
            return StepResult(name=self.name, passed=False, score=0.0,
                              reason="公司名称不在业务白名单内")
        return StepResult(name=self.name, passed=True, score=1.0)
```

在 `config/pipeline.yaml` 的 `steps` 列表中登记后立即生效；若校验不通过，将按配置触发修复或直接返回。

## 8. 配置参考

### 8.1 环境变量

| 变量 | 默认值 | 说明 |
| ------ | -------- | ------ |
| `PROVIDER` | `mock` | `mock` 或 `openai` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容服务地址 |
| `OPENAI_API_KEY` | 空 | 仅 `provider=openai` 时必填 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 修复时使用的模型 |
| `OPTIMIZE_SCORE_THRESHOLD` | `0.8` | 低于该分值触发修复 |
| `OPTIMIZE_MAX_REPAIR_RETRIES` | `1` | 修复调用上限 |
| `OPTIMIZE_TIMEOUT_MS` | `8000` | 整条管线的时间预算 |
| `OPTIMIZE_FAIL_OPEN` | `true` | 异常时是否回退原始输出 |
| `AUDIT_PATH` | `data/audit.jsonl` | 审计文件路径 |

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

fail_open: true
audit: true
```

配置优先级固定为：**代码默认值 < `.env` < `pipeline.yaml`**。`pipeline.yaml` 仅允许覆盖步骤与阈值，密钥一律从环境变量读取。

## 9. 效果评估

评估基于人工标注的金标集 `data/golden.jsonl`，每行一条样本：

```json
{"id":"g001","instruction":"提取公司/联系人/电话","input_text":"联系人张三…公司：示例科技。","raw_output":"{...}","expect":{"valid":true,"fields":{"company":"示例科技"}}}
```

金标集中预存 `raw_output`，因此评估过程**不调用任何模型**，结果完全可复现；追加 `--live` 参数才会实时调用模型生成。

```bash
python scripts/evaluate.py --dataset data/golden.jsonl --provider mock --out data/reports/mock.md
```

| 指标 | 计算方式 | 目标值 |
| ------ | ---------- | -------- |
| Schema 合规率 | 优化后通过数 / 样本总数，与优化前对比 | 提升 20 个百分点以上 |
| 关键字段准确率 | 与 `expect.fields` 完全一致 / 样本总数 | 不低于 90% |
| 平均分变化 | 优化后均值减优化前均值 | 提升 0.1 以上 |
| 修复成功率 | 被采纳的修复 / 修复触发次数 | 不低于 50% |
| 额外延迟 P95 | 管线耗时分位数 | 不修复时 300ms 内，修复时 8s 内 |
| Fail-open 率 | 降级次数 / 请求总数 | 低于 1% |

报告输出为 Markdown，同时生成同名 CSV 供绘图使用。

## 10. 可靠性设计

- **步骤级隔离**：单个步骤异常不会中断管线，仅记录原因并继续后续步骤。
- **硬超时**：整条管线由 `asyncio.wait_for` 包裹，超时按 fail-open 处理，不会挂起调用方。
- **修复采纳闸门**：修复结果须同时满足"通过校验"与"得分不降低"两个条件，否则丢弃并在审计中标记 `repair_rejected`。
- **启动期校验**：Provider 配置缺失或密钥为空时在启动阶段直接失败，避免运行期才暴露问题。
- **审计降级**：审计写入失败仅记录警告，不影响主流程返回结果。

## 11. 适用范围与已知限制

- 规则评分关注结构与格式，对语义层面的错误不敏感；语义质量可通过可选的 LLM 评分维度补充，或结合人工抽检。
- 当前版本仅承诺 OpenAI 兼容协议；访问 Anthropic、Gemini 等专有协议需实现对应的 Provider 适配器。
- 修复能力受修复模型本身能力约束，闸门仅能保证"不比原来更差"，不能保证"一定更好"。
- 性能数据会随修复模型、网络状况与输出长度变化，评估结论应结合具体环境复测。

## 12. 项目结构

```text
.
├── app/
│   ├── main.py                  # FastAPI 入口：/v1/optimize、/healthz
│   ├── settings.py              # 基于 pydantic-settings 的配置加载
│   ├── schemas.py               # Pydantic 数据模型
│   ├── optimizer/
│   │   ├── __init__.py          # optimize() 对外入口
│   │   ├── pipeline.py          # 编排、超时、fail-open、采纳判定
│   │   ├── context.py           # 步骤上下文
│   │   └── steps/               # schema / rules / repair / format
│   ├── providers/
│   │   ├── base.py              # Provider 统一接口
│   │   ├── openai_compatible.py # OpenAI 兼容适配器
│   │   └── mock.py              # 离线与测试用
│   └── audit.py                 # JSONL 审计
├── config/pipeline.yaml
├── prompts/repair.txt
├── data/
│   ├── golden.jsonl
│   └── reports/
├── scripts/
│   ├── demo.py
│   └── evaluate.py
├── tests/
├── .env.example
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
- 回归测试：以金标集的 `final_output` 作为基线快照。

## 14. 版本规划

| 版本 | 范围 | 状态 |
| ------ | ------ | ------ |
| v0.2 | 核心管线、OpenAI 兼容 Provider、审计与评估 | 进行中 |
| v0.3 | 新增 Anthropic、Gemini、OpenRouter 适配器 | 规划中 |
| v0.4 | 可选 LLM 评分维度、多候选输出重排 | 规划中 |
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
- 软件按"现状"提供，不附带任何明示或暗示的担保

Copyright (c) 2026 Let-the-output-better contributors
