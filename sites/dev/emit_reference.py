"""生成移植对照的参考输出：用仓库里的 Python 实现跑同一批用例（provider=mock，不落审计）。

只作为本地校验脚手架，不参与打包：`sites/functions` 与 `sites/web` 才是上传内容。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["OPTIMIZE_ENV_FILE"] = "no-such-env-file.env"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.optimizer import optimize  # noqa: E402
from app.plain import (  # noqa: E402
    acronym_anchors,
    canonical_numbers,
    confidence,
    concision,
    findings,
    metrics,
    ordering,
    plainness,
    qualifications,
    referents,
    sentences,
)
from app.providers.mock import MockProvider  # noqa: E402
from app.settings import PipelineConfig, Settings, default_steps  # noqa: E402

PROSE_DEFENSE = (
    "值得注意的是，实验结果充分表明该方法具有显著的性能优势和广阔的应用前景。"
    "这或许只能被视为探索性证据，需谨慎解读。综上所述，可能在某种意义上，"
    "该结论仅在 3 个数据集上得到验证，受限于 2023 年的算力条件。"
    "如需更多论文信息请随时告诉我，希望对您有帮助！"
)
PROSE_CLEAN = (
    "方法在 3 个数据集上取得 88.5% 的准确率，比基线高 4.2 个百分点。"
    "该结论仅适用于横截面数据。"
)
PROSE_PERCENT_DRIFT = (
    "结果表明准确率提升 12.50%，样本量为 240 名被试。"
    "我们在 2019-2023 年间采集的数据上评测，CVPR 2023 的基线为 76.5%。"
)
PAPER_JSON = json.dumps(
    {
        "title": "基于深度学习的图像分类方法研究",
        "authors": "张三、李四",
        "year": "2023",
        "keywords": "深度学习;图像分类;卷积神经网络",
        "reference": "Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023.",
        "note": "如需更多论文信息请随时告诉我",
    },
    ensure_ascii=False,
)
PAPER_REFS = json.dumps(
    {
        "title": "Attention is all you need",
        "authors": ["Vaswani, A.", "Shazeer, N.", "Parmar, N.", "Uszkoreit, J."],
        "year": 2017,
        "venue": "NeurIPS",
        "abstract": "值得注意的是，本文充分表明自注意力机制足以替代循环结构。",
        "references": [
            "Vaswani A, Shazeer N, Parmar N, et al. Attention is all you need[C]. NeurIPS 2017: 5998-6008.",
            "Bahdanau D, Cho K, Bengio Y. Neural machine translation by jointly learning to align and translate",
            "Devlin J, Chang M W, Lee K, Toutanova K. BERT: Pre-training of deep bidirectional transformers. ACL 2019",
        ],
    },
    ensure_ascii=False,
)
EXPERIMENT_JSON = json.dumps(
    {"model": "ResNet-50", "measure": "Top-1 accuracy", "result": "76.5 %", "data": "ImageNet", "base": "74.0"},
    ensure_ascii=False,
)
REFERENCE_TEXT = "Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023, 1024-1031."
NOT_JSON = "抱歉，我无法完成该任务，建议进一步核实原始材料后再作判断。"
EN_PROSE = (
    "It is worth noting that the results may suggest an association, although this should be interpreted with caution. "
    "The method plays a crucial role in downstream tasks and offers significant advantages. "
    "This is for informational purposes only."
)
PROSE_THOUSANDS = (
    "总样本量为 1,200,300 条，区间 2019-2023，整体下降 −4.5 个百分点，占比 12.50%。"
    "值得注意的是，该结果充分表明方法有效。"
)
PROSE_BOUNDARY = (
    "或许只能被视为初步迹象。该结论仅在 24 名被试的横截面数据上得到验证，"
    "且尚未在公开评测集上复现。综上所述，方法在 A/B 两个基准上表现一致。"
)

CASES = [
    {"name": "prose-defense", "raw_output": PROSE_DEFENSE, "schema": None, "input_text": ""},
    {"name": "prose-defense-with-source", "raw_output": PROSE_DEFENSE, "schema": None,
     "input_text": "实验在 3 个数据集（A/B/C）上完成，2023 年算力受限，准确率 88.5%。"},
    {"name": "prose-clean", "raw_output": PROSE_CLEAN, "schema": None, "input_text": ""},
    {"name": "prose-percent", "raw_output": PROSE_PERCENT_DRIFT, "schema": None,
     "input_text": "准确率为 12.5 个百分点，样本量 240，时间范围 2019 至 2023，CVPR 2023 基线 88.5%。"},
    {"name": "paper-sample", "raw_output": PAPER_JSON, "schema": "paper", "input_text": ""},
    {"name": "paper-refs", "raw_output": PAPER_REFS, "schema": "paper", "input_text": ""},
    {"name": "paper-refs-apa", "raw_output": PAPER_REFS, "schema": "paper", "reference_style": "apa"},
    {"name": "experiment-percent-unit", "raw_output": EXPERIMENT_JSON, "schema": "experiment", "input_text": ""},
    {"name": "reference-text", "raw_output": REFERENCE_TEXT, "schema": "reference", "input_text": ""},
    {"name": "not-json-with-schema", "raw_output": NOT_JSON, "schema": "paper", "input_text": ""},
    {"name": "en-prose", "raw_output": EN_PROSE, "schema": None, "input_text": ""},
    {"name": "off-options", "raw_output": PROSE_DEFENSE, "schema": None,
     "options": {"score": False, "simplify": False}},
    {"name": "strict-off-format", "raw_output": PAPER_JSON, "schema": "paper",
     "options": {"strict": True}},
    {"name": "whitespace-only", "raw_output": "   \n  ", "schema": None},
    {"name": "fenced-json", "raw_output": f"```json\n{PAPER_JSON}\n```", "schema": "paper"},
    {"name": "apa-volume-issue", "raw_output": json.dumps(
        {
            "title": "Deep residual learning",
            "authors": ["He, K.", "Zhang, X."],
            "year": 2016,
            "venue": "CVPR",
            "references": ["He K, Zhang X, Ren S, Sun J. Deep residual learning 2016: 770-778"],
        },
        ensure_ascii=False,
    ), "schema": "paper", "reference_style": "apa"},
    {"name": "doi-url", "raw_output": json.dumps(
        {
            "title": "Attention is all you need",
            "year": 2017,
            "doi": "https://doi.org/10.18653/v1/N17-1",
            "references": ["Vaswani A. Attention is all you need. JMLR 2017;18(3):45-61. doi:10.18653/v1/N17-1"],
        },
        ensure_ascii=False,
    ), "schema": "paper"},
    {"name": "thousands-and-minus", "raw_output": PROSE_THOUSANDS, "schema": None,
     "input_text": "全样本科目均值 1,200,300 分，2019 至 2023 年区间 −4.5 个百分点，占比 12.5%。"},
    {"name": "boundary-keep", "raw_output": PROSE_BOUNDARY, "schema": None,
     "input_text": "本研究覆盖 24 名被试，横截面数据，评测集为 A/B 两个公开基准。"},
    {"name": "experiment-zero-value", "raw_output": json.dumps(
        {"method": "Baseline", "metric": "F1", "value": 0, "unit": "百分比"}, ensure_ascii=False),
     "schema": "experiment"},
    {"name": "reference-missing-year", "raw_output": json.dumps(
        {"title": "无年份引用", "authors": "张三", "venue": "学报"}, ensure_ascii=False), "schema": "reference"},
    {"name": "simplify-with-instruction", "raw_output": PROSE_DEFENSE, "schema": None,
     "instruction": "保留研究边界，删除免责声明"},
]


def plain_probe(text: str, source: str | None) -> dict:
    return {
        "text": text,
        "source": source,
        "findings": [[f.group, f.label, f.count] for f in findings(text)],
        "metrics": metrics(text),
        "plainness": plainness(text, source),
        "concision": concision(text),
        "confidence": confidence(text, source),
        "ordering": ordering(text),
        "qualifications": [
            {"verdict": item["verdict"], "text": item["text"], "referents": item["referents"]}
            for item in qualifications(text, source)
        ],
        "sentences": sentences(text),
        "referents_per_sentence": [referents(sentence) for sentence in sentences(text)],
        "canonical_numbers": dict(canonical_numbers(text)),
        "acronym_anchors": sorted(acronym_anchors(text)),
    }


async def main() -> None:

    settings = Settings()
    config = PipelineConfig(steps=default_steps(), audit=False)
    out: dict[str, dict] = {}
    for case in CASES:
        result = await optimize(
            case["raw_output"],
            schema=case.get("schema"),
            instruction=case.get("instruction", ""),
            input_text=case.get("input_text", ""),
            provider=MockProvider(),
            settings=settings,
            config=config,
            repair=case.get("options", {}).get("repair"),
            simplify=case.get("options", {}).get("simplify"),
            fmt=case.get("options", {}).get("format"),
            score=case.get("options", {}).get("score"),
            strict=case.get("options", {}).get("strict"),
            timeout_ms=case.get("options", {}).get("timeout_ms"),
            reference_style=case.get("reference_style"),
            request_id="req_case",
        )
        payload = result.as_response()
        payload["dimensions"] = result.dimensions
        payload["markers"] = result.markers
        payload["changes"] = result.changes
        payload["errors"] = result.errors
        payload["degraded_reason"] = result.degraded_reason
        payload["valid"] = result.valid
        out[case["name"]] = {"input": case, "result": payload}

    thousands_source = "全样本科目均值 1,200,300 分，2019 至 2023 年区间 −4.5 个百分点，占比 12.5%。"
    boundary_source = "本研究覆盖 24 名被试，横截面数据，评测集为 A/B 两个公开基准。"
    probes = {"prose_defense": [PROSE_DEFENSE, None],
              "prose_defense_sourced": [PROSE_DEFENSE, CASES[1]["input_text"]],
              "prose_clean": [PROSE_CLEAN, None],
              "prose_percent": [PROSE_PERCENT_DRIFT, CASES[3]["input_text"]],
              "prose_thousands": [PROSE_THOUSANDS, thousands_source],
              "prose_boundary": [PROSE_BOUNDARY, boundary_source],
              "en_prose": [EN_PROSE, None]}
    out["_plain_probes"] = {name: plain_probe(text, source) for name, (text, source) in probes.items()}

    Path("sites/dev/reference.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"cases={len(CASES)} probes={len(probes)} -> sites/dev/reference.json")


if __name__ == "__main__":
    asyncio.run(main())
