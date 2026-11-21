/** Provider 层：离线 mock（默认，零密钥）与 OpenAI 兼容协议两种形态。 */

import { fixed } from "./fmt.ts";
import { REWRITES } from "./plain.ts";
import { extractJson } from "./textio.ts";

export class ProviderError extends Error {}
export class ProviderTimeout extends ProviderError {}

export type ChatResponse = { text: string; promptTokens: number; completionTokens: number };
export type ChatRequest = {
  system: string;
  user: string;
  task: string;
  jsonMode: boolean;
  timeoutS: number;
};
export type Provider = { name: string; chat(request: ChatRequest): Promise<ChatResponse> };

const REPAIR_MARK = "待修复输出：";
const SIMPLIFY_MARK = "待改写文本：";

function segment(prompt: string, mark: string): string {
  return prompt.includes(mark) ? prompt.split(mark).at(-1)! : prompt;
}

function jsonString(value: string): string {
  let out = '"';
  for (const char of value) {
    if (char === '"' || char === "\\") {
      out += `\\${char}`;
      continue;
    }
    const code = char.codePointAt(0) as number;
    if (char === "\n") out += "\\n";
    else if (char === "\r") out += "\\r";
    else if (char === "\t") out += "\\t";
    else if (code === 0x08) out += "\\b";
    else if (code === 0x0c) out += "\\f";
    else if (code < 0x20) out += `\\u${code.toString(16).padStart(4, "0")}`;
    else out += char;
  }
  return `${out}"`;
}

/**
 * Python `json.dumps(..., ensure_ascii=False)` 的形态：`, ` 与 `: ` 分隔符。
 * 已知残差：JSON 里写成 `74.0` 的浮点在 JS 解析后与整数不可区分，会被写成 `74`。
 */
export function pyJsonDumps(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return jsonString(value);
  if (Array.isArray(value)) return `[${value.map(pyJsonDumps).join(", ")}]`;
  const entries = Object.entries(value as Record<string, unknown>).map(
    ([key, item]) => `${jsonString(key)}: ${pyJsonDumps(item)}`,
  );
  return `{${entries.join(", ")}}`;
}

export function repairPayload(prompt: string): string {
  const data = extractJson(segment(prompt, REPAIR_MARK));
  if (data === null || data === undefined) return "{}";
  return pyJsonDumps(data);
}

export function simplifyText(prompt: string): string {
  const segmentText = segment(prompt, SIMPLIFY_MARK);
  let text = segmentText.trim();
  for (const [boilerplate, replacement] of REWRITES) text = text.replaceAll(boilerplate, replacement);
  text = text.replaceAll("，。", "。").replaceAll("。。", "。");
  while (text.includes("，，")) text = text.replaceAll("，，", "，");
  text = text.replace(/^[ ，,;；]+/u, "").replace(/[ ，,;；]+$/u, "");
  const endsTerminal = /[。！？.!?]$/u;
  if (
    segmentText.replace(/\s+$/u, "").endsWith("。") &&
    text.length > 0 &&
    !endsTerminal.test(text)
  ) {
    text += "。";
  }
  return text;
}

function mockProvider(): Provider {
  return {
    name: "mock",
    async chat(request: ChatRequest): Promise<ChatResponse> {
      let text: string;
      if (request.task === "repair") text = repairPayload(request.user);
      else if (request.task === "simplify") text = simplifyText(request.user);
      else text = request.user.trim();
      return { text, promptTokens: 0, completionTokens: 0 };
    },
  };
}

export type OpenAiConfig = { apiKey: string; model: string; baseUrl: string };

function openAiCompatible(config: OpenAiConfig): Provider {
  const baseUrl = config.baseUrl.replace(/\/+$/u, "");
  return {
    name: "openai",
    async chat(request: ChatRequest): Promise<ChatResponse> {
      const payload: Record<string, unknown> = {
        model: config.model,
        temperature: 0,
        messages: [
          { role: "system", content: request.system },
          { role: "user", content: request.user },
        ],
      };
      if (request.jsonMode) payload.response_format = { type: "json_object" };
      let response: Response;
      try {
        response = await fetch(`${baseUrl}/chat/completions`, {
          method: "POST",
          headers: { "content-type": "application/json", authorization: `Bearer ${config.apiKey}` },
          body: JSON.stringify(payload),
          signal: AbortSignal.timeout(Math.max(0.1, request.timeoutS) * 1000),
        });
      } catch (error) {
        if (error instanceof Error && error.name === "TimeoutError") {
          throw new ProviderTimeout(`模型调用超时（${fixed(Math.max(0.1, request.timeoutS), 1)}s）`);
        }
        throw new ProviderError(`模型调用失败：${error instanceof Error ? error.message : String(error)}`);
      }
      if (response.status === 429) throw new ProviderError("模型调用被限流（429）");
      if (response.status >= 400) throw new ProviderError(`模型服务返回 ${response.status}`);
      let data: Record<string, unknown>;
      try {
        data = (await response.json()) as Record<string, unknown>;
      } catch {
        throw new ProviderError("模型响应结构不符合 OpenAI 兼容协议");
      }
      const choices = Array.isArray(data.choices) ? data.choices : [];
      const first = choices[0] as { message?: { content?: unknown } } | undefined;
      const text = first?.message?.content;
      if (typeof text !== "string") throw new ProviderError("模型响应结构不符合 OpenAI 兼容协议");
      const usage = (data.usage ?? {}) as Record<string, unknown>;
      return {
        text,
        promptTokens: Number(usage.prompt_tokens ?? 0) || 0,
        completionTokens: Number(usage.completion_tokens ?? 0) || 0,
      };
    },
  };
}

export const SUPPORTED_PROVIDERS = ["mock", "openai"];

export class ProviderConfigError extends Error {}

export function buildProvider(settings: {
  provider: string;
  openaiApiKey: string;
  openaiModel: string;
  openaiBaseUrl: string;
}): Provider {
  if (!SUPPORTED_PROVIDERS.includes(settings.provider)) {
    throw new ProviderConfigError(`PROVIDER 仅支持 ${SUPPORTED_PROVIDERS.join(", ")}，当前为 ${settings.provider}`);
  }
  if (settings.provider === "openai") {
    if (!settings.openaiApiKey) throw new ProviderConfigError("PROVIDER=openai 必须设置 OPENAI_API_KEY");
    return openAiCompatible({
      apiKey: settings.openaiApiKey,
      model: settings.openaiModel,
      baseUrl: settings.openaiBaseUrl,
    });
  }
  return mockProvider();
}
