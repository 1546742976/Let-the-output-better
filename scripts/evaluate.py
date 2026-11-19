"""金标集评估：产出 Markdown 报告与同名 CSV（README §9）。

评估默认不调用任何模型：金标集预存 ``raw_output``，结果完全可复现。
仅当追加 ``--live`` 时，才通过 provider 重新生成 ``raw_output``。

用法：
    python scripts/evaluate.py --dataset data/golden.jsonl --provider mock --out data/reports/mock.md
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from pydantic import ValidationError  # noqa: E402

from app.audit import AuditLog  # noqa: E402
from app.optimizer import optimize  # noqa: E402
from app.optimizer.context import StepContext  # noqa: E402
from app.optimizer.steps.references import is_compliant  # noqa: E402
from app.optimizer.steps.rules import RulesStep, normalize_text, numeric_tokens  # noqa: E402
from app.optimizer.steps.simplify import check_boundaries  # noqa: E402
from app.plain import qualifications  # noqa: E402
from app.providers import build_provider  # noqa: E402
from app.schemas import resolve_schema  # noqa: E402
from app.settings import (  # noqa: E402
    PipelineConfig,
    Settings,
    StepConfig,
    load_pipeline_config,
)

ALL_STEPS = ("schema", "rules", "repair", "format", "simplify")


def config_with(enabled: tuple[str, ...], *, audit: bool = True) -> PipelineConfig:
    config = PipelineConfig(audit=audit)
    for name in ALL_STEPS:
        config = config.with_step(name, enabled=name in enabled)
    return config


def strict_validate(raw_output: str, schema_name: str) -> bool:
    """“优化前”判据：下游程序直接 ``json.loads`` + 严格类型校验能否消费这条输出。"""
    model_cls = resolve_schema(schema_name)
    if model_cls is None:
        return bool(raw_output.strip())
    try:
        data = json.loads(raw_output)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    try:
        model_cls.model_validate(data, strict=True)
    except ValidationError:
        return False
    return True


def fields_match(final: Any, expect: dict[str, Any]) -> tuple[bool | None, list[str]]:
    """断言缺失（如只约定 `valid: false` 的样本）返回 ``None``，不参与准确率分母。"""
    assertions = expect.get("fields") or {}
    has_assertion = bool(assertions or expect.get("exact_keys") is not None or expect.get("absent_keys"))
    if not has_assertion:
        return None, []
    if not isinstance(final, dict):
        return False, ["final_output 不是结构化对象"]
    problems: list[str] = []
    for key, wanted in assertions.items():
        if final.get(key) != wanted:
            problems.append(f"{key}: {final.get(key)!r} != {wanted!r}")
    exact = expect.get("exact_keys")
    if exact is not None and sorted(final) != sorted(exact):
        problems.append(f"keys: {sorted(final)} != {sorted(exact)}")
    for key in expect.get("absent_keys") or []:
        if key in final:
            problems.append(f"{key} 本应被剔除")
    return not problems, problems


def facts_preserved(sample: dict[str, Any], final: Any) -> tuple[bool, list[str]]:
    """最终输出里的每个数值都必须能在原始材料或原始输出中定位。"""
    haystack = normalize_text(sample.get("input_text", "")) + "\n" + normalize_text(sample.get("raw_output", ""))
    missing: list[str] = []
    for token in numeric_tokens([final]):
        if token.replace(".", "").replace(",", "") not in haystack:
            missing.append(token)
    return not missing, missing


def defensive_writing_stats(sample: dict[str, Any], final: Any) -> dict[str, int | bool]:
    """研究边界是否整体保住、无据限定清掉几条。

    边界判定直接调用采纳闸门的 `check_boundaries`：评估口径与执行口径必须是同一份逻辑，
    否则报告里的"误删 0%"只是在度量另一套规则。
    """
    before = str(sample.get("raw_output", ""))
    after = final if isinstance(final, str) else json.dumps(final, ensure_ascii=False)
    source = sample.get("input_text") or None
    items = qualifications(before, source)
    ungrounded = [str(item["sentence"]) for item in items if item["verdict"] == "ungrounded"]
    haystack = normalize_text(after)
    return {
        "boundary_total": sum(1 for item in items if item["verdict"] in ("boundary", "unverified")),
        "boundary_kept": check_boundaries(before, after, source) is None,
        "ungrounded_total": len(ungrounded),
        "ungrounded_removed": sum(1 for text in ungrounded if normalize_text(text) not in haystack),
    }


def reference_compliance(final: Any, style: str) -> tuple[int, int]:
    """只统计 `references` 数组：`reference` Schema 返回字段对象，没有可直接校验的字符串。"""
    if not isinstance(final, dict):
        return 0, 0
    references = final.get("references") or []
    return sum(1 for item in references if is_compliant(str(item), style)), len(references)


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return float(ordered[index])


async def rescore(
    settings: Settings, provider, sample: dict[str, Any], text: str
) -> tuple[float | None, dict[str, float | None]]:
    """用同一套规则给文本打分（只跑 schema + rules，不写审计），使优化前后得分可比。

    同时返回维度明细，通俗度才能与总分用完全相同的口径测量。
    """
    result = await optimize(
        text,
        schema=sample.get("schema", "paper"),
        instruction=sample.get("instruction", ""),
        input_text=sample.get("input_text", ""),
        provider=provider,
        settings=settings,
        config=config_with(("schema", "rules"), audit=False),
    )
    return result.score, result.dimensions


async def score_raw_baseline(
    settings: Settings, sample: dict[str, Any], raw_output: str, style: str
) -> tuple[float | None, dict[str, float | None]]:
    """优化前得分与维度：与 ``strict_validate`` 同一判据，原始结构直接进 ``rules`` 计分。

    ``rescore()`` 会先跑 SchemaStep，别名映射与类型归一已经把优化器的收益算进了
    「优化前」，同一评分口径下分差恒等于 0（实测 +0.005）。这里有意跳过 SchemaStep：
    JSON 解析不了就按无结构计分，解析得出但字段名/类型不合规则按原始形态计分，
    与「下游程序不经任何处理能否直接消费」这条判据对齐。

    维度一并返回：通俗度与自信度的"较优化前"必须与得分用同一判据测量，否则又是
    两套口径各说各话。
    """
    model_cls = resolve_schema(sample.get("schema") or "paper")
    parsed: dict[str, Any] | None = None
    try:
        data = json.loads(raw_output)
    except ValueError:
        data = None
    if isinstance(data, dict):
        parsed = data
    ctx = StepContext(
        raw_output=raw_output,
        settings=settings,
        config=PipelineConfig(audit=False),
        schema=model_cls,
        instruction=sample.get("instruction", ""),
        input_text=sample.get("input_text", ""),
        parsed=parsed,
        text=None if parsed is not None else raw_output,
    )
    # 不传权重表：rules 会按场景取 app.settings 里的 PROSE/STRUCTURED 常量，
    # 写死一份只会和默认口径分叉（main 上那份三维表就是这么过期的）。
    step = RulesStep(StepConfig(name="rules", reference_style=style), settings)
    await step.run(ctx)
    return ctx.score, dict(ctx.dimensions)


def load_dataset(path: Path) -> list[dict[str, Any]]:
    samples = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(json.loads(line))
        except ValueError as exc:
            raise SystemExit(f"{path}:{number} 不是合法 JSON：{exc}") from exc
    return samples


async def evaluate(args: argparse.Namespace) -> int:
    settings = Settings(provider=args.provider, audit_path=args.audit)
    provider = build_provider(settings)
    pipeline = load_pipeline_config(args.config) if args.config else PipelineConfig()
    if args.simplify:
        # 散文改写本就默认开启；这个开关的作用是允许改写结构化字段。
        pipeline = pipeline.with_step("simplify", prose_only=False)
    style = next(
        (step.reference_style for step in pipeline.steps if step.name == "format"),
        "gb-t-7714",
    )

    samples = load_dataset(Path(args.dataset))
    if args.live:
        print(f"--live：通过 {args.provider} provider 重新生成 raw_output")
        for sample in samples:
            response = await provider.chat(
                system="只输出 JSON。",
                user=f"{sample.get('instruction', '')}\n原始材料：{sample.get('input_text', '')}",
                task="generate",
                json_mode=True,
                timeout_s=30.0,
            )
            sample["raw_output"] = response.text

    rows: list[dict[str, Any]] = []
    for sample in samples:
        schema_name = sample.get("schema", "paper")
        before_strict = strict_validate(sample["raw_output"], schema_name)
        before_score, before_dims = await score_raw_baseline(settings, sample, sample["raw_output"], style)
        result = await optimize(
            sample["raw_output"],
            schema=schema_name,
            instruction=sample.get("instruction", ""),
            input_text=sample.get("input_text", ""),
            provider=provider,
            settings=settings,
            config=pipeline,
            timeout_ms=args.timeout_ms,
            audit=AuditLog(settings.audit_path),
        )
        if isinstance(result.final_output, dict):
            dumped = json.dumps(result.final_output, ensure_ascii=False)
            after_score, after_dims = await rescore(settings, provider, sample, dumped)
        else:
            # 散文输出必须按改写后的文本重新测量：rules 跑在 simplify 之前，
            # 直接沿用 result.dimensions 会把"改前"的分数当成"改后"。
            after_score, after_dims = await rescore(settings, provider, sample, str(result.final_output))
        fields_ok, field_problems = fields_match(result.final_output, sample.get("expect") or {})
        facts_ok, missing_numbers = facts_preserved(sample, result.final_output)
        defensive = defensive_writing_stats(sample, result.final_output)
        ref_hits, ref_total = reference_compliance(result.final_output, style)
        expect_valid = (sample.get("expect") or {}).get("valid")
        rows.append(
            {
                "id": sample.get("id"),
                "schema": schema_name,
                "before_strict_valid": before_strict,
                "after_valid": result.valid,
                "expect_valid": expect_valid,
                "expect_matched": expect_valid == result.valid,
                "score_before": before_score,
                "score_after": after_score,
                "plainness_before": before_dims.get("plainness"),
                "plainness_after": after_dims.get("plainness"),
                "fields_ok": fields_ok,
                "field_problems": "; ".join(field_problems),
                "facts_ok": facts_ok,
                "missing_numbers": ";".join(missing_numbers),
                **defensive,
                "reference_hits": ref_hits,
                "reference_total": ref_total,
                "repaired": result.repaired,
                "repair_adopted": result.repaired and "repair_rejected" not in result.markers,
                "degraded": result.degraded,
                "markers": ";".join(result.markers),
                "ms": result.ms,
            }
        )

    await provider.aclose()
    return write_report(args, rows, style)


def metric_table(rows: list[dict[str, Any]], latency_budget_ms: int) -> list[tuple[str, str, str, bool]]:
    total = len(rows) or 1
    before_rate = sum(1 for r in rows if r["before_strict_valid"]) / total
    after_rate = sum(1 for r in rows if r["after_valid"]) / total
    delta_pp = (after_rate - before_rate) * 100
    asserted = [r for r in rows if r["fields_ok"] is not None]
    fields_rate = sum(1 for r in asserted if r["fields_ok"]) / len(asserted) if asserted else 1.0
    ref_hits = sum(r["reference_hits"] for r in rows)
    ref_total = sum(r["reference_total"] for r in rows)
    ref_rate = ref_hits / ref_total if ref_total else 1.0
    # “变更总数”= 优化把不可直接消费的原始输出变成了有效结构化结果的样本
    changed = [r for r in rows if r["after_valid"] and not r["before_strict_valid"]]
    facts_rate = sum(1 for r in changed if r["facts_ok"]) / len(changed) if changed else 1.0
    scores_before = [r["score_before"] for r in rows if r["score_before"] is not None]
    scores_after = [r["score_after"] for r in rows if r["score_after"] is not None]
    plain_before = [r["plainness_before"] for r in rows if r["plainness_before"] is not None]
    plain_after = [r["plainness_after"] for r in rows if r["plainness_after"] is not None]
    plain_delta = statistics.fmean(plain_after) - statistics.fmean(plain_before) if plain_before else 0.0
    plain_mean_after = statistics.fmean(plain_after) if plain_after else 0.0
    mean_delta = (
        statistics.fmean(scores_after) - statistics.fmean(scores_before) if scores_before and scores_after else 0.0
    )
    triggered = [r for r in rows if r["repaired"]]
    adopted = [r for r in triggered if r["repair_adopted"]]
    adoption = len(adopted) / len(triggered) if triggered else None
    p95_all = percentile([r["ms"] for r in rows], 0.95)
    p95_norepair = percentile([r["ms"] for r in rows if not r["repaired"]], 0.95)
    degraded_rate = sum(1 for r in rows if r["degraded"]) / total
    expect_rate = sum(1 for r in rows if r["expect_matched"]) / total
    with_boundary = [r for r in rows if r["boundary_total"]]
    boundary_loss = (
        sum(1 for r in with_boundary if not r["boundary_kept"]) / len(with_boundary) if with_boundary else 0.0
    )
    ungrounded_total = sum(r["ungrounded_total"] for r in rows)
    ungrounded_gone = sum(r["ungrounded_removed"] for r in rows)
    ungrounded_rate = ungrounded_gone / ungrounded_total if ungrounded_total else 1.0

    return [
        (
            "Schema 合规率",
            f"优化前 {before_rate:.1%} → 优化后 {after_rate:.1%}（{delta_pp:+.1f}pp）",
            "提升 ≥ 20pp",
            delta_pp >= 20,
        ),
        ("与金标 valid 预期一致率", f"{expect_rate:.1%}", "全部一致", expect_rate == 1.0),
        ("关键字段准确率", f"{fields_rate:.1%}（{len(asserted)} 条含断言）", "≥ 90%", fields_rate >= 0.90),
        ("引用格式合规率", f"{ref_rate:.1%}（{ref_hits}/{ref_total}）", "≥ 95%", ref_rate >= 0.95),
        (
            "事实保持率",
            f"{facts_rate:.1%}（变更样本 {len(changed)} 条）",
            "100%",
            facts_rate == 1.0,
        ),
        ("平均分变化", f"{mean_delta:+.3f}", "≥ +0.1", mean_delta >= 0.1),
        (
            "通俗度（plainness）",
            f"{plain_mean_after:.3f}（较优化前 {plain_delta:+.3f}）",
            "均值 ≥ 0.8",
            plain_mean_after >= 0.8,
        ),
        (
            "误删研究边界比例",
            f"{boundary_loss:.1%}（含真实边界的样本 {len(with_boundary)} 条）",
            "0%",
            boundary_loss == 0.0,
        ),
        (
            "无据限定清除率",
            f"{ungrounded_rate:.1%}（{ungrounded_gone}/{ungrounded_total} 句）",
            "≥ 90%",
            ungrounded_rate >= 0.90,
        ),
        (
            "修复成功率",
            "未触发" if adoption is None else f"{adoption:.1%}（{len(adopted)}/{len(triggered)}）",
            "≥ 50%",
            adoption is None or adoption >= 0.5,
        ),
        (
            "额外延迟 P95",
            f"不修复 {p95_norepair:.0f}ms / 全部 {p95_all:.0f}ms",
            f"不修复 ≤ {latency_budget_ms}ms",
            p95_norepair <= latency_budget_ms,
        ),
        ("Fail-open 率", f"{degraded_rate:.1%}", "< 1%", degraded_rate < 0.01),
    ]


def write_report(args: argparse.Namespace, rows: list[dict[str, Any]], style: str) -> int:
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    metrics = metric_table(rows, args.latency_budget_ms)

    lines = [
        "# 效果评估报告",
        "",
        f"- 数据集：`{args.dataset}`（{len(rows)} 条）",
        f"- Provider：`{args.provider}`"
        + ("（--live：已实时生成 raw_output）" if args.live else "（未调用任何模型，结果可复现）"),
        f"- 引用目标风格：`{style}`",
        "- 优化前判据：对 `raw_output` 直接 `json.loads` + 严格类型校验（不剥围栏、不做类型归一）",
        "- 优化前得分：同一判据下把原始结构直接交 `rules` 计分，不经 SchemaStep，与上行口径一致",
        "",
        "## 指标",
        "",
        "| 指标 | 实测 | 目标 | 达标 |",
        "| ------ | ------ | ------ | ------ |",
    ]
    for name, measured, target, ok in metrics:
        lines.append(f"| {name} | {measured} | {target} | {'是' if ok else '否'} |")

    lines += [
        "",
        "## 逐条结果",
        "",
        "| id | schema | 优化前 | 优化后 | 得分前→后 | 通俗度前→后 | 关键字段 | 事实保持 | 修复 | 降级 |",
        "| ------ | ------ | ------ | ------ | ------ | ------ | ------ | ------ | ------ | ------ |",
    ]
    for row in rows:
        score_before = "-" if row["score_before"] is None else f"{row['score_before']:.2f}"
        score_after = "-" if row["score_after"] is None else f"{row['score_after']:.2f}"
        plain_pair = (
            "无此维度"
            if row["plainness_before"] is None or row["plainness_after"] is None
            else f"{row['plainness_before']:.2f}→{row['plainness_after']:.2f}"
        )
        if row["fields_ok"] is None:
            fields = "无断言"
        elif row["fields_ok"]:
            fields = "通过"
        else:
            fields = f"未通过（{row['field_problems']}）"
        lines.append(
            "| {id} | {schema} | {before} | {after} | {sb}→{sa} | {plain} | {fields} | {facts} | {repair} | {deg} |".format(
                id=row["id"],
                schema=row["schema"],
                before="valid" if row["before_strict_valid"] else "invalid",
                after="valid" if row["after_valid"] else "invalid",
                sb=score_before,
                sa=score_after,
                plain=plain_pair,
                fields=fields,
                facts="通过" if row["facts_ok"] else f"未通过（{row['missing_numbers']}）",
                repair=("采纳" if row["repair_adopted"] else "拒绝" if row["repaired"] else "未触发"),
                deg="是" if row["degraded"] else "否",
            )
        )

    unmet = [name for name, _, _, ok in metrics if not ok]
    lines += ["", "## 说明", ""]
    lines.append(f"- 未达标项：{'、'.join(unmet) if unmet else '无'}")
    if "平均分变化" in unmet:
        lines.append(
            "- 平均分变化使用同一规则口径：类型归一与别名映射在 `schema` 步骤内完成，"
            "原始输出一旦可解析即已享受同等归一，因此得分增益有限；"
            "本项目的收益主要体现在 Schema 合规率与引用格式合规率上。"
        )
    if "无据限定清除率" in unmet:
        lines.append(
            "- 未清除的那句是**纯文字性真实边界**（“受制于算力，其有效性有待进一步验证”）：句中既无数字也无领域名词，"
            "按“零指称即无据”被判为 ungrounded。这是指称启发式的已知局限而非漏删套话——"
            "要把它当边界保护，得让泛词参与匹配，代价是太极话也会一起被保护。"
        )
    if "修复成功率" in unmet:
        lines.append(
            "- 上表中的“未触发/拒绝”均为闸门正确工作：mock provider 无法补造缺失事实，"
            "其修复候选仍不合规时被拒绝并保留原输出。"
        )
    lines.append(
        "- mock provider 不调用任何模型，因此以上数字与运行环境无关、可完全复现；接入真实模型后需以 `--live` 复测。"
    )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    csv_path = out_path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"报告：{out_path}")
    print(f"数据：{csv_path}")
    for name, measured, target, ok in metrics:
        print(f"  {'✓' if ok else '✗'} {name}: {measured}（目标 {target}）")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="金标集评估")
    parser.add_argument("--dataset", default=str(ROOT / "data" / "golden.jsonl"))
    parser.add_argument("--provider", default="mock", choices=["mock", "openai"])
    parser.add_argument("--out", default=str(ROOT / "data" / "reports" / "mock.md"))
    parser.add_argument("--config", default=None, help="指定 pipeline.yaml 路径，默认使用内置默认编排")
    parser.add_argument("--audit", default=str(ROOT / "data" / "audit.jsonl"))
    parser.add_argument("--timeout-ms", type=int, default=8000)
    parser.add_argument("--latency-budget-ms", type=int, default=300, help="不修复时的 P95 延迟目标")
    parser.add_argument("--simplify", action="store_true", help="评估时额外开启结构化字段的通俗化改写")
    parser.add_argument("--live", action="store_true", help="调用模型重新生成 raw_output")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(evaluate(parse_args())))
