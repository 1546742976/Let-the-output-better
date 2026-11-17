"""离线演示：无需密钥即可看到优化前后对照，并留下一条审计记录（README §6.3）。

用法：``python scripts/demo.py``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.optimizer import optimize  # noqa: E402
from app.providers import build_provider  # noqa: E402
from app.schemas import Paper  # noqa: E402
from app.settings import get_pipeline_config, get_settings  # noqa: E402

STRUCTURED_BEFORE = """好的，以下是这篇论文的相关信息，希望对您有帮助！
```json
{"title": "基于深度学习的图像分类方法研究", "authors": "张三、李四", "year": "2023", "keywords": "深度学习;图像分类;卷积神经网络", "reference": "Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023.", "note": "如需更多论文信息请随时告诉我"}
```"""

STRUCTURED_INPUT = (
    "《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。关键词：深度学习；图像分类；卷积神经网络。"
)
STRUCTURED_INSTRUCTION = "抽取论文标题、作者、年份、关键词与参考文献，参考文献按 GB/T 7714 规范"

SIMPLIFY_BEFORE = (
    "值得注意的是，这或许只能被视为探索性证据；本结论仅在 3 个数据集上得到验证，Top-1 准确率提升 12.5%；需谨慎解读。"
)
SIMPLIFY_INPUT = "该方法在 3 个公开数据集上得到验证，Top-1 准确率提升 12.5%。"


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def show_json(label: str, payload: object) -> None:
    print(f"{label}:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def show_steps(result) -> None:
    for step in result.steps:
        score = "-" if step.get("score") is None else f"{step['score']:.2f}"
        flag = "通过" if step["passed"] else "未通过"
        print(f"  {step['name']:<9} {flag:<4} score={score:<5} {step['ms']:>3}ms  {step.get('reason', '')}")


async def main() -> int:
    settings = get_settings()
    provider = build_provider(settings)
    pipeline = get_pipeline_config()
    print(
        f"provider={settings.provider} | score_threshold={settings.score_threshold} | timeout_ms={settings.timeout_ms}"
    )
    if settings.provider != "mock":
        print("提示：当前不是 mock provider，下面会真实调用模型服务。")

    heading("场景一：删无据限定、留研究边界（simplify 默认开启）")
    print("改写前：")
    print(SIMPLIFY_BEFORE)
    simplified = await optimize(
        SIMPLIFY_BEFORE,
        schema=None,
        instruction="把这段实验结论改成普通人能一遍读懂的说法",
        input_text=SIMPLIFY_INPUT,
        provider=provider,
        settings=settings,
        config=pipeline,
    )
    print("改写后：")
    print(simplified.final_output)
    dims = simplified.dimensions
    print(
        f"valid={simplified.valid} score={simplified.score} "
        f"通俗度={dims.get('plainness')} 自信度={dims.get('confidence')} 简洁度={dims.get('concision')}"
    )
    show_steps(simplified)
    if simplified.markers:
        print("标记：", ", ".join(simplified.markers))

    heading("场景二：结构化输出优化（schema → rules → repair → format）")
    print("优化前：")
    print(STRUCTURED_BEFORE)
    result = await optimize(
        STRUCTURED_BEFORE,
        schema=Paper,
        instruction=STRUCTURED_INSTRUCTION,
        input_text=STRUCTURED_INPUT,
        provider=provider,
        settings=settings,
        config=pipeline,
    )
    show_json("优化后", result.final_output)
    print(f"valid={result.valid} score={result.score} repaired={result.repaired} degraded={result.degraded}")
    show_steps(result)
    print("变更：", "；".join(result.changes) or "无")

    heading("审计留痕")
    print(f"已写入 {settings.audit_path}（每请求一行 JSONL）")
    await provider.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
