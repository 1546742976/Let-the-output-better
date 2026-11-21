/** 比对页面 6 个示例在 TS 移植与 Python 参考实现下的产出。 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { optimize, type OptimizeResult } from "../functions/pipeline.ts";

const here = dirname(fileURLToPath(import.meta.url));
const examples = JSON.parse(readFileSync(join(here, "examples.json"), "utf-8")) as any[];
const reference = JSON.parse(readFileSync(join(here, "examples_reference.json"), "utf-8")) as any[];

function normalize(value: any): any {
  if (Array.isArray(value)) return value.map(normalize);
  if (value && typeof value === "object") {
    const out: Record<string, any> = {};
    for (const key of Object.keys(value).sort()) {
      if (key === "ms" || key === "request_id") continue;
      out[key] = normalize(value[key]);
    }
    return out;
  }
  if (typeof value === "number" && Number.isFinite(value)) return Number(value.toFixed(9));
  return value;
}

function comparable(result: OptimizeResult) {
  return {
    final_output: result.final_output,
    valid: result.valid,
    score: result.score,
    repaired: result.repaired,
    degraded: result.degraded,
    steps: result.steps,
    usage: result.usage,
    dimensions: result.dimensions,
    markers: result.markers,
    changes: result.changes,
    errors: result.errors,
    degraded_reason: result.degraded_reason,
  };
}

function paths(prefix: string, expected: any, actual: any, out: string[]): void {
  if (JSON.stringify(expected) === JSON.stringify(actual)) return;
  const bothObjects =
    expected && actual && typeof expected === "object" && typeof actual === "object" &&
    !Array.isArray(expected) && !Array.isArray(actual);
  const bothArrays = Array.isArray(expected) && Array.isArray(actual);
  if (bothObjects) {
    for (const key of [...new Set([...Object.keys(expected), ...Object.keys(actual)])].sort()) {
      paths(`${prefix}.${key}`, expected[key], actual[key], out);
    }
    return;
  }
  if (bothArrays) {
    const length = Math.max(expected.length, actual.length);
    for (let index = 0; index < length; index += 1) {
      paths(`${prefix}[${index}]`, expected[index], actual[index], out);
    }
    return;
  }
  out.push(`${prefix}\n      py: ${JSON.stringify(expected)}\n      ts: ${JSON.stringify(actual)}`);
}

const env: (name: string) => string | undefined = () => undefined;
let failures = 0;

for (const [index, example] of examples.entries()) {
  const expected = normalize(reference[index].result);
  const result = await optimize(
    {
      rawOutput: example.raw_output,
      schemaName: example.schema || null,
      instruction: example.instruction,
      inputText: example.input_text,
      referenceStyle: example.reference_style || null,
      options: {},
    },
    env,
  );
  const actual = normalize(comparable(result));
  const same = JSON.stringify(expected) === JSON.stringify(actual);
  if (!same) failures += 1;
  console.log(`${same ? "OK  " : "DIFF"} 示例 ${index + 1}「${example.label}」 score=${actual.score ?? "-"}`);
  if (!same) {
    const found: string[] = [];
    paths("root", expected, actual, found);
    for (const item of found.slice(0, 8)) console.log(`  - ${item}`);
  }
}

process.exitCode = failures ? 1 : 0;
