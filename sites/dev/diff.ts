/**
 * 移植保真度的本地校验：同一批用例分别跑 Python 参考输出与 TS 移植，逐项比对。
 *
 * 用法：`.venv/Scripts/python.exe sites/dev/emit_reference.py && node sites/dev/diff.ts`
 * 时间字段（ms）不参与比对，其余按结构严格比较。
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { optimize } from "../functions/pipeline.ts";
import {
  acronymAnchors,
  canonicalNumbers,
  confidence,
  concision,
  findings,
  metrics,
  ordering,
  plainness,
  qualifications,
  referents,
  sentences,
} from "../functions/plain.ts";

const here = dirname(fileURLToPath(import.meta.url));
const reference = JSON.parse(readFileSync(join(here, "reference.json"), "utf-8")) as Record<string, any>;

const problems: string[] = [];

function sortKeys(value: any): any {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const out: Record<string, any> = {};
    for (const key of Object.keys(value).sort()) out[key] = sortKeys(value[key]);
    return out;
  }
  return value;
}

function normalize(value: any): any {
  if (Array.isArray(value)) return value.map(normalize);
  if (value && typeof value === "object") {
    const out: Record<string, any> = {};
    for (const [key, item] of Object.entries(value)) {
      if (key === "ms" || key === "request_id") continue;
      out[key] = normalize(item);
    }
    return sortKeys(out);
  }
  if (typeof value === "number" && Number.isFinite(value)) return Number(value.toFixed(9));
  return value;
}

function diff(prefix: string, expected: any, actual: any): void {
  const left = JSON.stringify(normalize(expected));
  const right = JSON.stringify(normalize(actual));
  if (left === right) return;
  const parsedLeft = JSON.parse(left);
  const parsedRight = JSON.parse(right);
  if (parsedLeft && parsedRight && typeof parsedLeft === "object" && typeof parsedRight === "object" && !Array.isArray(parsedLeft) && !Array.isArray(parsedRight)) {
    const keys = new Set([...Object.keys(parsedLeft), ...Object.keys(parsedRight)]);
    for (const key of [...keys].sort()) {
      if (JSON.stringify(parsedLeft[key]) !== JSON.stringify(parsedRight[key])) {
        if (parsedLeft[key] && parsedRight[key] && typeof parsedLeft[key] === "object" && typeof parsedRight[key] === "object" && !Array.isArray(parsedLeft[key]) && !Array.isArray(parsedRight[key])) {
          diff(`${prefix}.${key}`, parsedLeft[key], parsedRight[key]);
        } else {
          problems.push(`${prefix}\n    py: ${short(parsedLeft[key])}\n    ts: ${short(parsedRight[key])}`);
        }
      }
    }
    return;
  }
  if (Array.isArray(parsedLeft) && Array.isArray(parsedRight)) {
    const length = Math.max(parsedLeft.length, parsedRight.length);
    for (let index = 0; index < length; index += 1) {
      if (JSON.stringify(parsedLeft[index]) !== JSON.stringify(parsedRight[index])) {
        diff(`${prefix}[${index}]`, parsedLeft[index], parsedRight[index]);
      }
    }
    return;
  }
  problems.push(`${prefix}\n    py: ${short(parsedLeft)}\n    ts: ${short(parsedRight)}`);
}

function short(value: any): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (text === undefined) return "undefined";
  return text.length > 240 ? `${text.slice(0, 240)}…` : text;
}

const env: (name: string) => string | undefined = () => undefined;

/* ---------- 管线结果 ---------- */

for (const [name, entry] of Object.entries(reference)) {
  if (name.startsWith("_")) continue;
  const input = entry.input;
  const expected = entry.result;
  const actual = await optimize(
    {
      rawOutput: input.raw_output,
      schemaName: input.schema ?? null,
      instruction: input.instruction ?? "",
      inputText: input.input_text ?? "",
      referenceStyle: input.reference_style ?? null,
      options: input.options ?? {},
    },
    env,
  );
  const steps = (actual.steps as any[]).map((step) => ({ ...step }));
  const comparable = {
    final_output: actual.final_output,
    valid: actual.valid,
    score: actual.score,
    repaired: actual.repaired,
    degraded: actual.degraded,
    usage: actual.usage,
    dimensions: actual.dimensions,
    markers: actual.markers,
    changes: actual.changes,
    errors: actual.errors,
    degraded_reason: actual.degraded_reason,
    steps,
  };
  diff(`case:${name}`, expected, comparable);
}

/* ---------- 语言层探针 ---------- */

function plainProbe(text: string, source: string | null) {
  return {
    findings: findings(text).map((f) => [f.group, f.label, f.count]),
    metrics: metrics(text),
    plainness: plainness(text, source),
    concision: concision(text),
    confidence: confidence(text, source),
    ordering: ordering(text),
    qualifications: qualifications(text, source).map((item) => ({
      verdict: item.verdict,
      text: item.text,
      referents: item.referents,
    })),
    sentences: sentences(text),
    referents_per_sentence: sentences(text).map((sentence) => referents(sentence)),
    canonical_numbers: Object.fromEntries(canonicalNumbers(text)),
    acronym_anchors: [...acronymAnchors(text)].sort(),
  };
}

for (const [name, probe] of Object.entries<any>(reference._plain_probes)) {
  const { text, source, ...expected } = probe;
  diff(`probe:${name}`, expected, plainProbe(text, source));
}

if (problems.length === 0) {
  console.log(`OK：${Object.keys(reference).length - 1} 个管线用例 + ${Object.keys(reference._plain_probes).length} 个语言探针全部一致`);
} else {
  console.log(`${problems.length} 处差异：`);
  for (const problem of problems.slice(0, 40)) console.log(`- ${problem}`);
  process.exitCode = 1;
}
