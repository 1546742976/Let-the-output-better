/** Sites Function 入口：把页面的 `/v1/*` 请求映射到移植后的管线。 */

import {
  applyOverrides,
  defaultPipeline,
  optimize,
  settingsFromEnv,
  stepConfig,
  type OptimizeResult,
} from "./pipeline.ts";
import { ProviderConfigError, SUPPORTED_PROVIDERS } from "./provider.ts";
import { optionalFieldNames, requiredFieldNames, resolveSchema, SCHEMA_REGISTRY } from "./schemas.ts";

const MAX_OUTPUT_CHARS = 200_000;
const MAX_INPUT_CHARS = 500_000;
const MAX_INSTRUCTION_CHARS = 8_000;
const OPTION_KEYS = new Set(["score", "repair", "format", "simplify", "strict", "timeout_ms"]);
const REQUEST_KEYS = new Set([
  "raw_output",
  "schema",
  "schema_name",
  "instruction",
  "input_text",
  "reference_style",
  "options",
]);

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function fail(status: number, detail: string): Response {
  return json({ detail }, status);
}

function charLength(text: string): number {
  return Array.from(text).length;
}

/** `options` 用 `extra="forbid"`：开关名写错必须是 422，静默忽略过一次，页面勾选框就此失效过。 */
function parseOptions(value: unknown): { options: Record<string, unknown> | null; error: string | null } {
  if (value === undefined || value === null) return { options: {}, error: null };
  if (typeof value !== "object" || Array.isArray(value)) {
    return { options: null, error: "options 应为对象" };
  }
  const source = value as Record<string, unknown>;
  const unknown = Object.keys(source).filter((key) => !OPTION_KEYS.has(key));
  if (unknown.length) return { options: null, error: `options 含未知键：${unknown.sort().join(", ")}` };
  const options: Record<string, unknown> = {};
  for (const key of ["score", "repair", "format", "simplify", "strict"]) {
    const flag = source[key];
    if (flag !== undefined && typeof flag !== "boolean") {
      return { options: null, error: `options.${key} 应为布尔值` };
    }
    if (flag !== undefined) options[key] = flag;
  }
  const timeout = source.timeout_ms;
  if (timeout !== undefined) {
    if (typeof timeout !== "number" || !Number.isInteger(timeout) || timeout < 50 || timeout > 120_000) {
      return { options: null, error: "options.timeout_ms 应为 50–120000 的整数" };
    }
    options.timeout_ms = timeout;
  }
  return { options, error: null };
}

function parseOptimizeBody(raw: unknown): { payload: OptimizePayload | null; error: string | null } {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { payload: null, error: "请求体应为 JSON 对象" };
  }
  const body = raw as Record<string, unknown>;
  const unknown = Object.keys(body).filter((key) => !REQUEST_KEYS.has(key));
  if (unknown.length) return { payload: null, error: `请求体含未知键：${unknown.sort().join(", ")}` };

  const rawOutput = body.raw_output;
  if (typeof rawOutput !== "string" || rawOutput.length === 0) {
    return { payload: null, error: "raw_output 应为非空字符串" };
  }
  if (charLength(rawOutput) > MAX_OUTPUT_CHARS) {
    return { payload: null, error: `raw_output 超过 ${MAX_OUTPUT_CHARS} 字上限` };
  }
  const schemaName = body.schema ?? body.schema_name ?? null;
  if (schemaName !== null && typeof schemaName !== "string") {
    return { payload: null, error: "schema 应为字符串或 null" };
  }
  if (schemaName !== null && resolveSchema(schemaName) === null) {
    const known = Object.keys(SCHEMA_REGISTRY).join(", ");
    return { payload: null, error: `未知 schema=${schemaName}，内置可选：${known}` };
  }
  const instruction = body.instruction ?? "";
  const inputText = body.input_text ?? "";
  for (const [key, value, limit] of [
    ["instruction", instruction, MAX_INSTRUCTION_CHARS],
    ["input_text", inputText, MAX_INPUT_CHARS],
  ] as const) {
    if (typeof value !== "string") return { payload: null, error: `${key} 应为字符串` };
    if (charLength(value) > limit) return { payload: null, error: `${key} 超过 ${limit} 字上限` };
  }
  const referenceStyle = body.reference_style ?? null;
  if (referenceStyle !== null && referenceStyle !== "gb-t-7714" && referenceStyle !== "apa") {
    return { payload: null, error: "reference_style 仅支持 gb-t-7714 或 apa" };
  }
  const parsed = parseOptions(body.options);
  if (parsed.error) return { payload: null, error: parsed.error };
  if (parsed.options === null) return { payload: null, error: "options 格式错误" };

  return {
    payload: {
      rawOutput,
      schemaName: (schemaName as string | null) ?? null,
      instruction: instruction as string,
      inputText: inputText as string,
      referenceStyle: (referenceStyle as string | null) ?? null,
      options: parsed.options,
    },
    error: null,
  };
}

type OptimizePayload = Parameters<typeof optimize>[0];

function logicalPath(pathname: string): string {
  const marker = "/functions/v1/app";
  const at = pathname.indexOf(marker);
  if (at >= 0) return pathname.slice(at + marker.length) || "/";
  const v1 = pathname.indexOf("/v1/");
  if (v1 >= 0) return pathname.slice(v1);
  const health = pathname.indexOf("/healthz");
  if (health >= 0) return pathname.slice(health);
  return pathname;
}

export function runtimeConfig(env: (name: string) => string | undefined): Record<string, unknown> {
  const pipeline = defaultPipeline();
  const settings = applyOverrides(settingsFromEnv(env), pipeline);
  const formatStep = stepConfig(pipeline, "format");
  const schemaStep = stepConfig(pipeline, "schema");
  const warnings: string[] = [];
  if (!SUPPORTED_PROVIDERS.includes(settings.provider)) {
    warnings.push(`PROVIDER=${settings.provider} 不在托管版支持范围内（${SUPPORTED_PROVIDERS.join("、")}），已回退 mock`);
  } else if (settings.provider === "openai" && !settings.openaiApiKey) {
    warnings.push("PROVIDER=openai 但 OPENAI_API_KEY 为空，修复与通俗化不会执行");
  }
  return {
    provider: settings.provider,
    warnings,
    score_threshold: settings.score_threshold,
    timeout_ms: settings.timeout_ms,
    max_repair_retries: settings.max_repair_retries,
    fail_open: settings.fail_open,
    reference_style: formatStep ? formatStep.reference_style : "gb-t-7714",
    schema_strict: schemaStep ? schemaStep.strict : false,
    steps: Object.fromEntries(pipeline.steps.map((step) => [step.name, { enabled: step.enabled, strict: step.strict }])),
    schemas: Object.fromEntries(
      Object.entries(SCHEMA_REGISTRY).map(([name, spec]) => [
        name,
        { required: requiredFieldNames(spec) ?? [], optional: optionalFieldNames(spec) ?? [] },
      ]),
    ),
  };
}

function optimizeResponse(result: OptimizeResult): Record<string, unknown> {
  const { request_id, final_output, valid, score, repaired, degraded, steps, usage } = result;
  return {
    request_id,
    final_output,
    valid,
    score,
    repaired,
    degraded,
    steps,
    usage,
    // 托管版没有服务端审计：把审计行同等字段直接回给页面，结果面板才有维度、留痕与耗时。
    record: { ...result },
  };
}

export function createHandler(env: (name: string) => string | undefined) {
  return async function handler(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = logicalPath(url.pathname);
    const method = request.method.toUpperCase();

    if (path === "/healthz") {
      if (method !== "GET") return fail(405, "仅支持 GET");
      const settings = settingsFromEnv(env);
      return json({ status: "ok", provider: settings.provider });
    }

    if (path === "/v1/config") {
      if (method !== "GET") return fail(405, "仅支持 GET");
      return json(runtimeConfig(env));
    }

    if (path === "/v1/optimize") {
      if (method !== "POST") return fail(405, "仅支持 POST");
      const contentType = request.headers.get("content-type") ?? "";
      if (!contentType.includes("application/json")) return fail(415, "请求体应为 application/json");
      let body: unknown;
      try {
        body = await request.json();
      } catch {
        return fail(422, "请求体不是合法 JSON");
      }
      const parsed = parseOptimizeBody(body);
      if (parsed.error || parsed.payload === null) return fail(422, parsed.error ?? "请求体校验失败");
      try {
        const result = await optimize(parsed.payload, env);
        return json(optimizeResponse(result));
      } catch (error) {
        if (error instanceof ProviderConfigError) return fail(503, error.message);
        return fail(500, error instanceof Error ? error.message : String(error));
      }
    }

    return fail(404, "Not Found");
  };
}
