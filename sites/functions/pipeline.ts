/** `app/optimizer` 的移植件：schema → rules → repair → format → simplify 五步管线。 */

import { fixed, fsum, percent, roundTo } from "./fmt.ts";
import {
  GROUP_WEIGHTS,
  acronymAnchors,
  canonicalNumbers,
  clip,
  normalizeText,
  qualifications,
  referents,
  report as plainReport,
  scopeGroup,
  unitClasses,
} from "./plain.ts";
import { buildProvider, ProviderError, type ChatResponse, type Provider } from "./provider.ts";
import { isCompliant, normalizeReference } from "./references.ts";
import {
  allowedKeys,
  describeModel,
  isFilled,
  optionalFieldNames,
  parseModelText,
  requiredFieldNames,
  resolveSchema,
  validateSchema,
  valueFor,
  type SchemaSpec,
} from "./schemas.ts";
import { extractJson, isJsonObject, stripMarkdownFences, trimWhitespace } from "./textio.ts";

export const PROSE_WEIGHTS: Record<string, number> = {
  plainness: 0.35,
  confidence: 0.3,
  concision: 0.2,
  preservation: 0.15,
};
export const STRUCTURED_WEIGHTS: Record<string, number> = {
  completeness: 0.3,
  format: 0.25,
  grounding: 0.25,
  plainness: 0.2,
};

const NUMBER = /\d+(?:[.,]\d+)?/gu;
const WORD = /[A-Za-z]{3,}|[一-鿿]{2,}/gu;
const LEFTOVER_SPLIT = /[、;；]/u;
const GREETING = /^(好的[，,！!]?)|(希望对您有帮助)|(请随时告诉我)|(如有需要)/u;
const DOI_HEAD = /^10\.\d{4,9}\/\S+/u;
const DATE = /((?:1[89]|20)\d{2})[年/.](\d{1,2})[月/.](\d{1,2})日?/gu;
const LIST_STRIP = " .;；、,，";

const MAX_TITLE = 300;
const MAX_LIST_ITEM = 200;
const MAX_CALL_S = 6.0;
const MIN_LENGTH = 12;
const GROWTH_LIMIT = 1.1;
const MAX_FIELDS = 3;

const SIMPLIFY_SKIP_FIELDS = new Set(["doi", "type", "year", "value", "baseline", "unit"]);
const NON_NARRATIVE = new Set(["doi", "type", "year", "value", "baseline", "unit"]);

export const SIMPLIFY_SYSTEM =
  "你是去防御化改写器：像作者本人陈述结论那样重写，只输出改写后的文本，不解释、不加围栏。";
export const REPAIR_SYSTEM = "你只输出一个 JSON 对象，不要输出解释、客套话或代码围栏。";

export const SIMPLIFY_PROMPT = `请把下面这段学术文本改写为**去防御化**的版本：像原作者本人陈述自己的结论那样写，而不是替结论预先免责。句子要短、结构要清楚，但语域保持学术。

要求：
1. **先讲主张，再讲限制**。段落第一句必须是结论本身，不要以"值得注意的是""或许只能被视为"开头。
2. 删除不承担信息的开场与收尾："值得注意的是""需要指出的是""综上所述""众所周知""在一定程度上"。
3. 删除推责与免责声明："仅供参考""不构成建议""如需更多帮助请随时告诉我""无法完全保证""建议进一步核实"。
4. 删除证据降级与自我否定模板："这或许只能被视为探索性证据""这并不意味着……""不能据此说明……""需谨慎解读""不应过度解读"。
5. 删除叠加的含糊限定（"可能在某种意义上或许"）；单个"可能"若表达真实不确定性，保留。
6. **保留真正影响结论的研究边界**，并且不得改变其范围：如"仅在 3 个数据集上得到验证""受限于 2023 年的算力条件""该结论仅适用于横截面数据"。这些是作者的责任声明，不是废话。
7. 不得反向造假：不要为了显得自信，把有边界的结论改写成无条件断言。
8. 不用行话套壳："进行了相应的优化""通过……的方式来实现""在……的基础上"直接说做了什么。长句拆短，一句只说一件事，尽量控制在 25 字以内。
9. 保留全部事实：数值、百分比、年份、引用标记、方法名与数据集名一律不变，也不得增删。
10. 只输出改写后的文本本身，不要解释，不要加引号或代码围栏。

待改写文本：
{text}`;

export const REPAIR_PROMPT = `你是学术科研输出的修复器。下面是一段未通过校验的大模型输出，请只做结构修复，不得新增事实。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要任何解释性文字。
2. 字段名与类型严格遵循下方 Schema。
3. 多值字段使用数组；年份使用整数。
4. 缺失且无法从原始材料推出的字段直接省略，不要编造。
5. 数值与引用必须能在原始材料中定位，不得改写其取值。

任务说明：
{instruction}

Schema：
{schema_json}

校验错误：
{errors}

原始材料：
{input_text}

待修复输出：
{raw_output}`;

export type StepConfig = {
  name: string;
  enabled: boolean;
  strict: boolean;
  prompt: string | null;
  weights: Record<string, Record<string, number>> | null;
  strip_markdown_fences: boolean;
  trim_whitespace: boolean;
  reference_style: string;
  prose_only: boolean;
};

export type PipelineConfig = {
  steps: StepConfig[];
  fail_open: boolean | null;
  audit: boolean;
  score_threshold: number | null;
  max_repair_retries: number | null;
  timeout_ms: number | null;
};

export type Settings = {
  provider: string;
  openaiApiKey: string;
  openaiModel: string;
  openaiBaseUrl: string;
  score_threshold: number;
  max_repair_retries: number;
  timeout_ms: number;
  fail_open: boolean;
};

export function defaultPipeline(): PipelineConfig {
  const step = (name: string, extra: Partial<StepConfig> = {}): StepConfig => ({
    name,
    enabled: true,
    strict: false,
    prompt: null,
    weights: null,
    strip_markdown_fences: true,
    trim_whitespace: true,
    reference_style: "gb-t-7714",
    prose_only: true,
    ...extra,
  });
  return {
    steps: [
      step("schema"),
      step("rules", {
        weights: {
          prose: { plainness: 0.35, confidence: 0.3, concision: 0.2, preservation: 0.15 },
          structured: { completeness: 0.3, format: 0.25, grounding: 0.25, plainness: 0.2 },
        },
      }),
      step("repair"),
      step("format", { reference_style: "gb-t-7714" }),
      step("simplify", { prose_only: true }),
    ],
    // 托管运行没有可写的审计文件，历史改由页面按会话保留（见 sites/web/app.js）。
    fail_open: true,
    audit: false,
    score_threshold: null,
    max_repair_retries: null,
    timeout_ms: null,
  };
}

function numberFromEnv(env: (name: string) => string | undefined, name: string, fallback: number): number {
  const raw = env(name);
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

export function settingsFromEnv(env: (name: string) => string | undefined): Settings {
  return {
    provider: env("PROVIDER") || "mock",
    openaiApiKey: env("OPENAI_API_KEY") || "",
    openaiModel: env("OPENAI_MODEL") || "gpt-4o-mini",
    openaiBaseUrl: env("OPENAI_BASE_URL") || "https://api.openai.com/v1",
    score_threshold: numberFromEnv(env, "OPTIMIZE_SCORE_THRESHOLD", 0.8),
    max_repair_retries: numberFromEnv(env, "OPTIMIZE_MAX_REPAIR_RETRIES", 1),
    timeout_ms: numberFromEnv(env, "OPTIMIZE_TIMEOUT_MS", 8000),
    fail_open: env("OPTIMIZE_FAIL_OPEN") !== "false",
  };
}

export function applyOverrides(settings: Settings, config: PipelineConfig): Settings {
  return {
    ...settings,
    score_threshold: config.score_threshold ?? settings.score_threshold,
    max_repair_retries: config.max_repair_retries ?? settings.max_repair_retries,
    timeout_ms: config.timeout_ms ?? settings.timeout_ms,
    fail_open: config.fail_open ?? settings.fail_open,
  };
}

export function stepConfig(config: PipelineConfig, name: string): StepConfig | null {
  return config.steps.find((step) => step.name === name) ?? null;
}

export function withStep(config: PipelineConfig, name: string, updates: Partial<StepConfig>): PipelineConfig {
  const found = config.steps.some((step) => step.name === name);
  const steps = found
    ? config.steps.map((step) => (step.name === name ? { ...step, ...updates } : step))
    : [...config.steps, { ...defaultPipeline().steps[0], name, ...updates }];
  return { ...config, steps };
}

export type Usage = { prompt_tokens: number; completion_tokens: number };

export type StepContext = {
  rawOutput: string;
  settings: Settings;
  config: PipelineConfig;
  provider: Provider | null;
  schema: SchemaSpec | null;
  instruction: string;
  inputText: string;
  parsed: Record<string, unknown> | null;
  text: string | null;
  errors: string[];
  dimensions: Record<string, number | null>;
  markers: string[];
  changes: string[];
  usage: Usage;
  valid: boolean;
  score: number | null;
  repaired: boolean;
  timeoutS: number;
  deadline: number;
  remainingS(): number;
};

type StepResult = { name: string; passed: boolean; score: number | null; reason: string; ms: number };

function record(result: StepResult): Record<string, unknown> {
  const out: Record<string, unknown> = { name: result.name, passed: result.passed, ms: result.ms };
  if (result.score !== null) out.score = result.score;
  if (result.reason) out.reason = result.reason;
  return out;
}

function pythonFloat(value: number): string {
  return Number.isInteger(value) ? `${value}.0` : String(value);
}

function pythonBool(value: boolean): string {
  return value ? "True" : "False";
}

function truthyParsed(parsed: Record<string, unknown> | null): boolean {
  return parsed !== null && Object.keys(parsed).length > 0;
}

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function renderPrompt(template: string, values: Record<string, string>): string {
  let rendered = template;
  for (const [key, value] of Object.entries(values)) rendered = rendered.replaceAll(`{${key}}`, value ?? "");
  return rendered;
}

/* ---------------- schema ---------------- */

function schemaStep(ctx: StepContext, config: StepConfig | null, startedAt: number): StepResult {
  ctx.errors = [];
  const spec = ctx.schema;
  if (spec === null) {
    const data = extractJson(ctx.rawOutput);
    if (isJsonObject(data)) {
      ctx.parsed = data;
      ctx.text = null;
    } else {
      ctx.parsed = null;
      ctx.text = stripMarkdownFences(ctx.rawOutput).trim();
    }
    ctx.valid = truthyParsed(ctx.parsed) || (ctx.text ?? "").trim().length > 0;
    return {
      name: "schema",
      passed: ctx.valid,
      score: ctx.valid ? 1 : 0,
      reason: ctx.valid ? "" : "输出为空文本",
      ms: Math.round(performance.now() - startedAt),
    };
  }

  let data = extractJson(ctx.rawOutput);
  if (!isJsonObject(data)) data = parseModelText(spec, ctx.rawOutput);
  if (!isJsonObject(data)) {
    ctx.parsed = null;
    ctx.valid = false;
    ctx.text = stripMarkdownFences(ctx.rawOutput).trim();
    ctx.errors.push("output_not_json");
    return {
      name: "schema",
      passed: false,
      score: 0,
      reason: "输出不是可解析的 JSON 对象（剥离围栏后仍未取得对象结构）",
      ms: Math.round(performance.now() - startedAt),
    };
  }

  const allowed = allowedKeys(spec);
  const extras = [...new Set(Object.keys(data))].filter((key) => !allowed.has(key)).sort();
  const strict = Boolean(config && config.strict);
  if (extras.length > 0 && strict) {
    ctx.parsed = null;
    ctx.valid = false;
    ctx.errors.push("extra_fields");
    return {
      name: "schema",
      passed: false,
      score: 0,
      reason: `strict 模式下存在 Schema 之外的字段：${extras.join("、")}`,
      ms: Math.round(performance.now() - startedAt),
    };
  }

  const validated = validateSchema(spec, data);
  if (validated.parsed === null) {
    ctx.valid = false;
    ctx.parsed = null;
    const messages: string[] = [];
    for (const error of validated.errors.slice(0, 5)) {
      messages.push(`${error.loc || "body"}: ${error.msg}`);
      ctx.errors.push(error.loc || "body");
    }
    const required = requiredFieldNames(spec) ?? [];
    const coverage = required.length === 0 ? 0 : required.filter((name) => isFilled(valueFor(spec, data, name))).length / required.length;
    return {
      name: "schema",
      passed: false,
      score: roundTo(coverage, 3),
      reason: messages.join("；"),
      ms: Math.round(performance.now() - startedAt),
    };
  }

  ctx.parsed = validated.parsed;
  ctx.text = null;
  ctx.valid = true;
  let reason = "";
  if (extras.length > 0) {
    reason = `剔除多余字段：${extras.join("、")}`;
    ctx.changes.push(`schema:drop_extra(${extras.join("、")})`);
  }
  return { name: "schema", passed: true, score: 1, reason, ms: Math.round(performance.now() - startedAt) };
}

/* ---------------- rules ---------------- */

function numericTokens(values: unknown[]): string[] {
  const tokens: string[] = [];
  for (const value of values) {
    if (value === null || typeof value === "boolean") continue;
    if (typeof value === "number") tokens.push(String(value));
    else if (typeof value === "string") tokens.push(...(value.match(NUMBER) ?? []));
    else if (Array.isArray(value)) tokens.push(...numericTokens(value));
  }
  return tokens.filter((token) => token.replace(/,/g, "").replace(/\./g, "").length >= 2);
}

function flattenValues(payload: Record<string, unknown> | null, text: string | null): unknown[] {
  if (payload !== null) return Object.values(payload);
  return [text ?? ""];
}

function citationIsGrounded(reference: string, haystack: string): boolean {
  const years = reference.match(new RegExp(String.raw`(?<![\p{L}\p{Nd}_])(?:1[89]|20)\d{2}(?![\p{L}\p{Nd}_])`, "gu")) ?? [];
  if (years.some((year) => !haystack.includes(year))) return false;
  const words = (reference.match(WORD) ?? []).map(normalizeText);
  if (words.length === 0) return true;
  return words.some((word) => haystack.includes(word));
}

function narrative(ctx: StepContext): string {
  if (ctx.parsed === null) return ctx.text ?? ctx.rawOutput;
  const parts = Object.entries(ctx.parsed)
    .filter(([key, value]) => typeof value === "string" && !NON_NARRATIVE.has(key))
    .map(([, value]) => value as string);
  return parts.join("。");
}

function completeness(ctx: StepContext): number | null {
  if (ctx.schema === null) return (ctx.text ?? "").trim() ? 1 : 0;
  if (ctx.parsed === null) return 0;
  const required = requiredFieldNames(ctx.schema);
  const optional = optionalFieldNames(ctx.schema);
  if (required === null || optional === null) return null;
  const parsed = ctx.parsed;
  const requiredRatio = required.length ? required.filter((name) => isFilled(parsed[name])).length / required.length : 1;
  const optionalRatio = optional.length ? optional.filter((name) => isFilled(parsed[name])).length / optional.length : 1;
  return 0.7 * requiredRatio + 0.3 * optionalRatio;
}

function formatCompliance(ctx: StepContext, style: string): number {
  const checks: boolean[] = [];
  if (ctx.schema === null || ctx.parsed === null) {
    const text = ctx.text ?? ctx.rawOutput;
    checks.push(!text.includes("```"));
    checks.push(!GREETING.test(text));
    return checks.filter(Boolean).length / checks.length;
  }
  const parsed = ctx.parsed;
  const year = parsed.year;
  if (year !== null && year !== undefined) {
    const currentYear = new Date().getFullYear();
    checks.push(typeof year === "number" && Number.isInteger(year) && year >= 1500 && year <= currentYear + 1);
  }
  for (const name of ["authors", "keywords"]) {
    const items = parsed[name];
    if (Array.isArray(items) && items.length > 0) {
      for (const item of items) {
        checks.push(
          typeof item === "string" && !LEFTOVER_SPLIT.test(item) && Array.from(item).length <= MAX_LIST_ITEM,
        );
      }
    }
  }
  const references = parsed.references;
  if (Array.isArray(references) && references.length > 0) {
    for (const item of references) checks.push(isCompliant(String(item), style));
  }
  const doi = parsed.doi;
  if (doi) checks.push(DOI_HEAD.test(String(doi)));
  const metricValue = parsed.value;
  if (typeof metricValue === "number") {
    checks.push(Number.isFinite(metricValue));
    if (parsed.unit === "%") checks.push(metricValue >= 0 && metricValue <= 100);
  }
  const title = parsed.title;
  if (typeof title === "string" && title) {
    checks.push(Array.from(title).length <= MAX_TITLE);
    checks.push(!title.includes("```"));
  }
  const abstract = parsed.abstract;
  if (typeof abstract === "string" && abstract) checks.push(!GREETING.test(abstract));
  return checks.length ? checks.filter(Boolean).length / checks.length : 1;
}

function grounding(ctx: StepContext): number {
  const haystack = normalizeText(ctx.inputText);
  const checks: boolean[] = numericTokens(flattenValues(ctx.parsed, ctx.text)).map((token) =>
    haystack.includes(token.replace(/,/g, "").replace(/\./g, "")),
  );
  const references = (ctx.parsed ?? {}).references;
  if (Array.isArray(references)) {
    for (const reference of references) checks.push(citationIsGrounded(String(reference), haystack));
  }
  return checks.length ? checks.filter(Boolean).length / checks.length : 1;
}

function weightsFor(ctx: StepContext, config: StepConfig | null, prose: boolean): Record<string, number> {
  const base = { ...(prose ? PROSE_WEIGHTS : STRUCTURED_WEIGHTS) };
  const override = config?.weights?.[prose ? "prose" : "structured"];
  if (override) Object.assign(base, override);
  return base;
}

function rulesStep(ctx: StepContext, config: StepConfig | null, startedAt: number): StepResult {
  const prose = ctx.parsed === null;
  const weights = weightsFor(ctx, config, prose);
  const style = config ? config.reference_style : "gb-t-7714";
  const language = plainReport(narrative(ctx), ctx.inputText || null);
  const preservation = ctx.inputText.trim() ? grounding(ctx) : null;

  const dimensions: Record<string, number | null> = prose
    ? {
        plainness: language.plainness,
        confidence: language.confidence,
        concision: language.concision,
        preservation,
      }
    : {
        completeness: completeness(ctx),
        format: formatCompliance(ctx, style),
        grounding: preservation,
        plainness: language.plainness,
      };

  ctx.dimensions = dimensions;
  const scored = Object.entries(dimensions).filter(([, value]) => value !== null) as Array<[string, number]>;
  const totalWeight = fsum(scored.map(([key]) => weights[key] ?? 0));
  const score = totalWeight ? fsum(scored.map(([key, value]) => (weights[key] ?? 0) * value)) / totalWeight : 0;
  ctx.score = roundTo(score, 3);
  const threshold = ctx.settings.score_threshold;
  const passed = ctx.valid && ctx.score >= threshold;

  const weak = scored.filter(([, value]) => value < threshold).map(([key, value]) => `${key}=${fixed(value, 2)}`);
  const summaryText = plainSummary(language);
  let reason = summaryText;
  const completenessValue = dimensions.completeness === undefined ? null : dimensions.completeness;
  if (!ctx.valid) {
    reason = "Schema 校验未通过";
  } else if (!passed) {
    reason = `得分 ${fixed(ctx.score, 2)} 低于阈值 ${fixed(threshold, 2)}（${weak.join(", ")}）｜${summaryText}`;
  } else if (preservation === null) {
    reason = `${summaryText}；未提供原始材料，一致性维度不参与计分`;
  } else if (completenessValue === null) {
    reason = `${summaryText}；字段完整度对该 Schema 不可测，不参与计分`;
  }
  return { name: "rules", passed, score: ctx.score, reason, ms: Math.round(performance.now() - startedAt) };
}

type PlainReportT = ReturnType<typeof plainReport>;

function plainSummary(language: PlainReportT): string {
  const hits = language.findings
    .filter((finding) => finding.group in GROUP_WEIGHTS)
    .reduce((sum, finding) => sum + finding.count, 0);
  const hedge = language.findings
    .filter((finding) => finding.group === "hedge")
    .reduce((sum, finding) => sum + finding.count, 0);
  const avgLen = language.metrics.avg_len ?? 0;
  const longRatio = language.metrics.long_ratio ?? 0;
  const bits = [`防御措辞 ${hits} 处`];
  if (hedge) bits.push(`弱化 ${hedge} 处`);
  bits.push(`句均 ${fixed(avgLen, 0)} 字`);
  bits.push(`>40 字句占 ${percent(longRatio, 0)}`);
  return bits.join("、");
}

/* ---------------- format ---------------- */

function normalizeDates(text: string): string {
  return text.replaceAll(DATE, (_match, year: string, month: string, day: string) => {
    const mm = Number.parseInt(month, 10).toString().padStart(2, "0");
    const dd = Number.parseInt(day, 10).toString().padStart(2, "0");
    return `${year}-${mm}-${dd}`;
  });
}

function cleanText(text: string, fences: boolean, trim: boolean): string {
  let cleaned = text;
  if (fences) cleaned = stripMarkdownFences(cleaned);
  if (trim) cleaned = trimWhitespace(cleaned);
  return normalizeDates(cleaned).trim();
}

function formatStep(ctx: StepContext, config: StepConfig | null, startedAt: number): StepResult {
  const doFences = config ? config.strip_markdown_fences : true;
  const doTrim = config ? config.trim_whitespace : true;
  const style = config ? config.reference_style : "gb-t-7714";
  const changes: string[] = [];
  const ms = () => Math.round(performance.now() - startedAt);

  if (ctx.parsed === null) {
    const text = ctx.text ?? ctx.rawOutput;
    const cleaned = cleanText(text, doFences, doTrim);
    if (cleaned !== text) changes.push("清理围栏与空白");
    ctx.text = cleaned;
    ctx.changes.push(...changes);
    return { name: "format", passed: true, score: 1, reason: changes.join("；") || "无需格式化", ms: ms() };
  }

  const data: Record<string, unknown> = { ...ctx.parsed };
  for (const [key, value] of Object.entries(ctx.parsed)) {
    if (!isFilled(value)) {
      delete data[key];
      changes.push(`剔除空字段(${key})`);
    }
  }

  for (const [key, value] of Object.entries(data)) {
    if (typeof value === "string") {
      const cleaned = cleanText(value, doFences, doTrim);
      if (cleaned !== value) {
        data[key] = cleaned;
        changes.push(`规范化文本(${key})`);
      }
    } else if (Array.isArray(value)) {
      const items: unknown[] = [];
      value.forEach((item, index) => {
        if (typeof item !== "string") {
          items.push(item);
          return;
        }
        let cleaned = cleanText(item, doFences, doTrim);
        if (key === "references") {
          const [restyled, changed] = normalizeReference(cleaned, style);
          // 消息形态与本地版逐字对齐（含那对不闭合的方括号），否则两侧的留痕文本会漂。
          if (changed) changes.push(`引用格式(${key}[${index})->${style})`);
          cleaned = restyled;
        } else if (key === "authors" || key === "keywords") {
          cleaned = stripCharsBoth(cleaned, LIST_STRIP);
        }
        items.push(cleaned);
      });
      if (!deepEqual(items, value)) data[key] = items;
    }
  }

  if (typeof data.unit === "string") {
    const unit = data.unit.trim().replaceAll("％", "%").replaceAll(" %", "%");
    if (unit !== data.unit) {
      data.unit = unit;
      changes.push("统一单位写法");
    }
  }

  if (ctx.schema !== null) {
    const validated = validateSchema(ctx.schema, data);
    if (validated.parsed === null) {
      const details = validated.errors
        .slice(0, 3)
        .map((error) => `${error.loc || "body"}: ${error.msg}`)
        .join("；");
      return {
        name: "format",
        passed: false,
        score: 0.5,
        reason: `格式化后校验未通过，保留格式化前结果（${details}）`,
        ms: ms(),
      };
    }
    const dumped = validated.parsed;
    ctx.parsed = Object.fromEntries(Object.entries(dumped).filter(([, value]) => isFilled(value)));
  } else {
    ctx.parsed = data;
  }

  ctx.changes.push(...changes);
  const unique = [...new Set(changes)];
  return { name: "format", passed: true, score: 1, reason: unique.join("；") || "无需格式化", ms: ms() };
}

function stripCharsBoth(text: string, chars: string): string {
  const set = new Set(Array.from(chars));
  const list = Array.from(text);
  let start = 0;
  let end = list.length;
  while (start < end && set.has(list[start])) start += 1;
  while (end > start && set.has(list[end - 1])) end -= 1;
  return list.slice(start, end).join("");
}

/* ---------------- simplify ---------------- */

function factsOf(text: string): [Map<string, number>, Set<string>] {
  return [canonicalNumbers(text), acronymAnchors(text)];
}

function countersEqual(a: Map<string, number>, b: Map<string, number>): boolean {
  if (a.size !== b.size) return false;
  for (const [key, value] of a) if (b.get(key) !== value) return false;
  return true;
}

export function checkFacts(before: string, after: string): string | null {
  const [numbersBefore, anchorsBefore] = factsOf(before);
  const [numbersAfter, anchorsAfter] = factsOf(after);
  if (!countersEqual(numbersBefore, numbersAfter)) {
    return `数值变化：${mapToPythonDict(numbersBefore)} → ${mapToPythonDict(numbersAfter)}`;
  }
  const unitsBefore = unitClasses(before);
  const unitsAfter = unitClasses(after);
  if (!countersEqual(unitsBefore, unitsAfter)) {
    return `单位类别变化：${mapToPythonDict(unitsBefore)} → ${mapToPythonDict(unitsAfter)}`;
  }
  const missing = [...anchorsBefore].filter((anchor) => !anchorsAfter.has(anchor)).sort();
  if (missing.length) return `关键表述丢失：${missing.join("、")}`;
  return null;
}

function mapToPythonDict(map: Map<string, number>): string {
  const body = [...map.entries()].map(([key, value]) => `'${key}': ${value}`).join(", ");
  return `{${body}}`;
}

function scopeGroups(text: string, source: string | null): Set<string>[] {
  const groups: Set<string>[] = [];
  for (const item of qualifications(text, source)) {
    if (item.verdict !== "boundary" && item.verdict !== "unverified") continue;
    for (const token of referents(item.sentence)) {
      const normalized = new Set(scopeGroup(token).map(normalizeText).filter((member) => member.length > 0));
      if (normalized.size > 0) groups.push(normalized);
    }
  }
  return groups;
}

export function checkBoundaries(before: string, after: string, source: string | null): string | null {
  const required = scopeGroups(before, source);
  if (required.length === 0) return null;
  const haystack = normalizeText(after);
  const lost = required.filter((group) => ![...group].some((key) => haystack.includes(key)));
  if (lost.length) {
    const names = lost.slice(0, 3).map((group) => [...group].sort().join("/")).join("、");
    return `丢失研究边界（${lost.length} 处范围指称整族消失：${names}）`;
  }
  return null;
}

function clipReason(text: string): string {
  return clip(text, 18).replace(/[，、；：,;: ]+$/u, "");
}

function comparison(before: string, after: string, source: string | null): string {
  const b = plainReport(before, source);
  const a = plainReport(after, source);
  const order = [...new Set(b.findings.filter((f) => f.group in GROUP_WEIGHTS).map((f) => f.label))].slice(0, 6);
  const cut = b.qualifications.filter((item) => item.verdict === "ungrounded").map((item) => clipReason(item.text));
  const keep = b.qualifications
    .filter((item) => item.verdict === "boundary" || item.verdict === "unverified")
    .map((item) => clipReason(item.text));
  const tails: string[] = [order.length ? `｜命中：${order.join("、")}` : ""];
  if (cut.length) tails.push(`｜删除无据：${cut.slice(0, 3).join("、")}`);
  if (keep.length) tails.push(`｜保留边界：${keep.slice(0, 3).join("、")}`);
  tails.push(a.ordering.claim_first ? "｜主张先行：是" : "｜主张先行：否");
  return (
    `防御措辞 ${Math.trunc(b.metrics.hard_hits)} → ${Math.trunc(a.metrics.hard_hits)} 处；` +
    `句均 ${fixed(b.metrics.avg_len, 0)} → ${fixed(a.metrics.avg_len, 0)} 字；` +
    `plainness ${pythonFloat(b.plainness)} → ${pythonFloat(a.plainness)}；` +
    `concision ${pythonFloat(b.concision)} → ${pythonFloat(a.concision)}；` +
    `confidence ${pythonFloat(b.confidence)} → ${pythonFloat(a.confidence)}` +
    tails.filter((tail) => tail.length > 0).join("")
  );
}

function simplifyTargets(ctx: StepContext): Array<[string, string]> {
  if (ctx.parsed === null) {
    const text = ctx.text ?? "";
    return text.trim().length >= MIN_LENGTH ? [["text", text]] : [];
  }
  const candidates: Array<[string, string]> = [];
  for (const [key, value] of Object.entries(ctx.parsed)) {
    if (SIMPLIFY_SKIP_FIELDS.has(key) || typeof value !== "string") continue;
    if (value.trim().length < MIN_LENGTH) continue;
    candidates.push([key, value]);
  }
  candidates.sort((a, b) => Array.from(b[1]).length - Array.from(a[1]).length);
  return candidates.slice(0, MAX_FIELDS);
}

async function simplifyStep(ctx: StepContext, config: StepConfig | null, startedAt: number): Promise<StepResult> {
  const ms = () => Math.round(performance.now() - startedAt);
  const provider = ctx.provider;
  if (provider === null) {
    return { name: "simplify", passed: true, score: 1, reason: "未配置模型 provider，跳过通俗化", ms: ms() };
  }
  const targets = simplifyTargets(ctx);
  if (targets.length === 0) {
    return { name: "simplify", passed: true, score: 1, reason: "没有值得改写的叙述文本", ms: ms() };
  }
  const template = config?.prompt || SIMPLIFY_PROMPT;
  const adopted: string[] = [];
  const rejected: string[] = [];
  const unchanged: string[] = [];
  for (const [fieldName, text] of targets) {
    const prompt = renderPrompt(template, { text, instruction: ctx.instruction || "（未提供）" });
    let response: ChatResponse;
    try {
      response = await provider.chat({
        system: SIMPLIFY_SYSTEM,
        user: prompt,
        task: "simplify",
        jsonMode: false,
        timeoutS: Math.min(MAX_CALL_S, Math.max(0.5, ctx.remainingS())),
      });
    } catch (error) {
      if (!(error instanceof ProviderError)) throw error;
      ctx.errors.push("simplify_failed");
      return {
        name: "simplify",
        passed: false,
        score: null,
        reason: `通俗化调用失败，保留原文：${error.message}`,
        ms: ms(),
      };
    }
    ctx.usage.prompt_tokens += response.promptTokens;
    ctx.usage.completion_tokens += response.completionTokens;
    const candidate = (response.text ?? "").trim();
    if (!candidate) {
      rejected.push(`${fieldName}：改写结果为空`);
      continue;
    }
    if (candidate === text.trim()) {
      unchanged.push(fieldName);
      continue;
    }
    let violation = checkFacts(text, candidate);
    if (violation === null) violation = checkBoundaries(text, candidate, ctx.inputText || null);
    if (violation) {
      rejected.push(`${fieldName}：${violation}`);
      continue;
    }
    if (Array.from(candidate).length > Array.from(text).length * GROWTH_LIMIT) {
      rejected.push(`${fieldName}：未变简洁（${Array.from(text).length} → ${Array.from(candidate).length} 字）`);
      continue;
    }
    if (fieldName === "text") {
      ctx.text = candidate;
    } else if (ctx.parsed !== null) {
      ctx.parsed[fieldName] = candidate;
      if (ctx.schema !== null) {
        const validated = validateSchema(ctx.schema, ctx.parsed);
        if (validated.parsed !== null) ctx.parsed = validated.parsed;
      }
    }
    adopted.push(comparison(text, candidate, ctx.inputText || null));
  }

  if (adopted.length) ctx.changes.push("采纳通俗化重写");
  if (rejected.length && !adopted.length) ctx.markers.push("simplify_rejected");

  let reason: string;
  if (adopted.length && rejected.length) {
    reason = `采纳 ${adopted.length} 段；拒绝 ${rejected.length} 段（${rejected[0]}）｜${adopted[0]}`;
  } else if (adopted.length) {
    reason = `已通俗化｜${adopted[0]}`;
  } else if (rejected.length) {
    reason = `全部拒绝：${rejected[0]}`;
  } else if (unchanged.length) {
    reason = `已是通俗表述，无需改写（${unchanged.join(", ")}）`;
  } else {
    reason = "没有值得改写的叙述文本";
  }
  return {
    name: "simplify",
    passed: rejected.length === 0,
    score: adopted.length || unchanged.length ? 1 : 0,
    reason,
    ms: ms(),
  };
}

/* ---------------- repair ---------------- */

function scoreText(ctx: StepContext, text: string): StepContext {
  const probe: StepContext = {
    rawOutput: text,
    settings: ctx.settings,
    config: ctx.config,
    provider: null,
    schema: ctx.schema,
    instruction: ctx.instruction,
    inputText: ctx.inputText,
    parsed: null,
    text: null,
    errors: [],
    dimensions: {},
    markers: [],
    changes: [],
    usage: { prompt_tokens: 0, completion_tokens: 0 },
    valid: false,
    score: null,
    repaired: false,
    timeoutS: ctx.timeoutS,
    deadline: performance.now() / 1000 + Math.max(0, ctx.remainingS()),
    remainingS() {
      return Math.max(0, this.deadline - performance.now() / 1000);
    },
  };
  schemaStep(probe, stepConfig(probe.config, "schema"), performance.now());
  rulesStep(probe, stepConfig(probe.config, "rules"), performance.now());
  return probe;
}

async function repairStep(ctx: StepContext, config: StepConfig | null, startedAt: number): Promise<StepResult> {
  const ms = () => Math.round(performance.now() - startedAt);
  const provider = ctx.provider;
  if (provider === null) {
    return { name: "repair", passed: false, score: null, reason: "未配置模型 provider，无法修复", ms: ms() };
  }
  const template = config?.prompt || REPAIR_PROMPT;
  const prompt = renderPrompt(template, {
    instruction: ctx.instruction || "（未提供）",
    schema_json: describeModel(ctx.schema),
    errors: ctx.errors.join("；") || "（无）",
    input_text: ctx.inputText || "（未提供）",
    raw_output: ctx.rawOutput,
  });
  const baseScore = ctx.score ?? 0;
  const attempts = Math.max(1, ctx.settings.max_repair_retries);
  let reason = "修复未触发";

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    let response: ChatResponse;
    try {
      response = await provider.chat({
        system: REPAIR_SYSTEM,
        user: prompt,
        task: "repair",
        jsonMode: true,
        timeoutS: Math.min(MAX_CALL_S, Math.max(0.5, ctx.remainingS())),
      });
    } catch (error) {
      if (!(error instanceof ProviderError)) throw error;
      ctx.repaired = true;
      ctx.errors.push("repair_failed");
      return {
        name: "repair",
        passed: false,
        score: null,
        reason: `第 ${attempt} 次修复调用失败，保留原输出：${error.message}`,
        ms: ms(),
      };
    }
    ctx.repaired = true;
    ctx.usage.prompt_tokens += response.promptTokens;
    ctx.usage.completion_tokens += response.completionTokens;
    const probe = scoreText(ctx, response.text);
    if (probe.valid && (probe.score ?? 0) >= baseScore) {
      ctx.parsed = probe.parsed;
      ctx.text = probe.text;
      ctx.valid = probe.valid;
      ctx.score = probe.score;
      ctx.dimensions = probe.dimensions;
      ctx.errors = probe.errors;
      ctx.rawOutput = response.text;
      ctx.changes.push(
        `采纳修复结果（第 ${attempt} 次，得分 ${fixed(baseScore, 2)} → ${fixed(probe.score ?? 0, 2)}）`,
      );
      return {
        name: "repair",
        passed: true,
        score: probe.score,
        reason: "修复结果通过校验且得分不低于原输出，已采纳",
        ms: ms(),
      };
    }
    reason = `修复结果未通过采纳闸门（valid=${pythonBool(probe.valid)}，score=${
      probe.score === null ? "None" : pythonFloat(probe.score)
    }，原得分 ${fixed(baseScore, 2)}），已丢弃`;
  }
  ctx.markers.push("repair_rejected");
  return { name: "repair", passed: false, score: null, reason, ms: ms() };
}

/* ---------------- 编排 ---------------- */

export const OPTION_TO_STEP: Record<string, string> = {
  score: "rules",
  repair: "repair",
  format: "format",
  simplify: "simplify",
  strict: "schema",
};

let requestCounter = 0;
const requestNonce = Math.random().toString(16).slice(2, 8);

export function newRequestId(): string {
  const date = new Date();
  const stamp = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, "0")}${String(date.getDate()).padStart(2, "0")}`;
  requestCounter += 1;
  return `req_${stamp}_${requestNonce}_${String(requestCounter).padStart(4, "0")}`;
}

export type OptimizeParams = {
  rawOutput: string;
  schemaName: string | null;
  instruction: string;
  inputText: string;
  referenceStyle: string | null;
  options: {
    score?: boolean;
    repair?: boolean;
    format?: boolean;
    simplify?: boolean;
    strict?: boolean;
    timeout_ms?: number;
  };
};

export type OptimizeResult = {
  request_id: string;
  final_output: unknown;
  valid: boolean;
  score: number | null;
  repaired: boolean;
  degraded: boolean;
  steps: Record<string, unknown>[];
  usage: Usage;
  markers: string[];
  changes: string[];
  errors: string[];
  dimensions: Record<string, number | null>;
  degraded_reason: string;
  ms: number;
  provider: string;
  schema: string | null;
  reference_style: string | null;
  instruction: string;
  input_text: string;
  raw_output: string;
  ts: string;
};

export async function optimize(
  params: OptimizeParams,
  env: (name: string) => string | undefined,
): Promise<OptimizeResult> {
  const baseSettings = settingsFromEnv(env);
  let pipeline = defaultPipeline();

  const flags: Record<string, boolean | undefined> = {
    score: params.options.score,
    repair: params.options.repair,
    format: params.options.format,
    simplify: params.options.simplify,
  };
  for (const [name, flag] of Object.entries(flags)) {
    if (flag !== undefined) pipeline = withStep(pipeline, OPTION_TO_STEP[name], { enabled: Boolean(flag) });
  }
  if (params.options.strict !== undefined) {
    pipeline = withStep(pipeline, OPTION_TO_STEP.strict, { strict: Boolean(params.options.strict) });
  }
  if (params.referenceStyle !== null) {
    if (!["gb-t-7714", "apa"].includes(params.referenceStyle)) {
      throw new Error(`未知 reference_style=${params.referenceStyle}`);
    }
    pipeline = withStep(pipeline, "format", { reference_style: params.referenceStyle });
  }

  const settings = applyOverrides(baseSettings, pipeline);
  if (params.options.timeout_ms !== undefined) {
    settings.timeout_ms = Math.trunc(params.options.timeout_ms);
  }

  const schemaSpec = resolveSchema(params.schemaName);
  const timeoutS = Math.max(0.05, settings.timeout_ms / 1000);
  const provider = buildProvider(settings);
  const startedClock = performance.now() / 1000;
  const ctx: StepContext = {
    rawOutput: params.rawOutput,
    settings,
    config: pipeline,
    provider,
    schema: schemaSpec,
    instruction: params.instruction,
    inputText: params.inputText,
    parsed: null,
    text: null,
    errors: [],
    dimensions: {},
    markers: [],
    changes: [],
    usage: { prompt_tokens: 0, completion_tokens: 0 },
    valid: false,
    score: null,
    repaired: false,
    timeoutS,
    deadline: startedClock + timeoutS,
    remainingS() {
      return Math.max(0, this.deadline - performance.now() / 1000);
    },
  };

  const identifier = newRequestId();
  const started = performance.now();
  let steps: Record<string, unknown>[] = [];
  let degraded = false;
  let degradedReason = "";

  try {
    steps = await runSteps(ctx);
  } catch (error) {
    if (error instanceof Error && error.name === "TimeoutError") {
      degraded = true;
      degradedReason = `管线超时（${settings.timeout_ms}ms），回退原始输出`;
      ctx.markers.push("timeout");
    } else if (settings.fail_open) {
      degraded = true;
      degradedReason = `管线异常（${error instanceof Error ? error.constructor.name : "Error"}: ${
        error instanceof Error ? error.message : String(error)
      }），回退原始输出`;
    } else {
      throw error;
    }
  }
  if (degraded && !settings.fail_open) throw new Error(degradedReason);
  if (degraded) {
    ctx.parsed = null;
    ctx.text = null;
    ctx.markers.push("degraded");
  }

  const elapsedMs = Math.round(performance.now() - started);
  const finalStep = stepConfig(pipeline, "format");
  return {
    request_id: identifier,
    final_output: finalOutput(ctx),
    valid: ctx.valid && !degraded,
    score: ctx.score,
    repaired: ctx.repaired,
    degraded,
    steps,
    usage: ctx.usage,
    markers: [...ctx.markers],
    changes: [...ctx.changes],
    errors: [...ctx.errors],
    dimensions: { ...ctx.dimensions },
    degraded_reason: degradedReason,
    ms: elapsedMs,
    provider: provider.name,
    schema: schemaSpec ? schemaSpec.name : null,
    reference_style: finalStep ? finalStep.reference_style : null,
    instruction: params.instruction,
    input_text: params.inputText,
    raw_output: params.rawOutput,
    ts: new Date().toISOString(),
  };
}

function finalOutput(ctx: StepContext): unknown {
  if (ctx.parsed !== null) return ctx.parsed;
  if (ctx.text !== null) return ctx.text;
  return ctx.rawOutput;
}

const KNOWN_STEPS = new Set(["schema", "rules", "repair", "format", "simplify"]);

async function runSteps(ctx: StepContext): Promise<Record<string, unknown>[]> {
  const results: Record<string, unknown>[] = [];
  for (const step of ctx.config.steps) {
    if (!step.enabled) continue;
    if (!KNOWN_STEPS.has(step.name)) {
      ctx.errors.push(`unknown_step(${step.name})`);
      continue;
    }
    if (!shouldRun(ctx, step.name, step)) continue;
    if (ctx.remainingS() <= 0) throw new TimeoutError();
    const dict = record(await executeStep(ctx, step, performance.now()));
    if (step.name === "simplify" && ctx.changes.includes("采纳通俗化重写")) {
      // rules 跑在 simplify 之前；改写发生后不重算，score 描述的就只是输入而非交付物。
      const status = remeasure(ctx);
      if (status === "measured") dict.score_after = ctx.score;
      else if (status === "failed") dict.score_after = null;
    }
    results.push(dict);
  }
  return results;
}

class TimeoutError extends Error {
  override name = "TimeoutError";
}

function shouldRun(ctx: StepContext, name: string, step: StepConfig): boolean {
  if (name === "repair") {
    if (ctx.schema === null) return false;
    if (ctx.provider === null || ctx.remainingS() <= 0.2) return false;
    if (!ctx.valid) return true;
    return ctx.score !== null && ctx.score < ctx.settings.score_threshold;
  }
  if (name === "simplify") {
    if (ctx.provider === null) return false;
    return !(step.prose_only && ctx.parsed !== null);
  }
  return true;
}

async function executeStep(ctx: StepContext, step: StepConfig, startedAt: number): Promise<StepResult> {
  try {
    switch (step.name) {
      case "schema":
        return schemaStep(ctx, step, startedAt);
      case "rules":
        return rulesStep(ctx, step, startedAt);
      case "format":
        return formatStep(ctx, step, startedAt);
      case "simplify":
        return await simplifyStep(ctx, step, startedAt);
      default:
        return await repairStep(ctx, step, startedAt);
    }
  } catch (error) {
    ctx.errors.push(`step_error(${step.name})`);
    return {
      name: step.name,
      passed: false,
      score: null,
      reason: `步骤异常已隔离：${error instanceof Error ? error.constructor.name : "Error"}: ${
        error instanceof Error ? error.message : String(error)
      }`,
      ms: Math.round(performance.now() - startedAt),
    };
  }
}

function remeasure(ctx: StepContext): string {
  const config = stepConfig(ctx.config, "rules");
  if (config === null || !config.enabled) return "skipped";
  const key = "remeasure_error(rules)";
  try {
    rulesStep(ctx, config, performance.now());
  } catch {
    ctx.errors.push(key);
    return "failed";
  }
  return ctx.errors.includes(key) ? "failed" : "measured";
}
