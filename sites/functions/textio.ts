/** `app/textio.py` 的移植件：围栏剥离、JSON 抽取与空白归一。 */

const FENCE = /^\s*```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)\n?```\s*$/;
const FENCE_ANYWHERE = /```[a-zA-Z0-9_+-]*\s*\n([\s\S]*?)```/;

export function stripMarkdownFences(text: string): string {
  const body = text.trim();
  const wrapped = FENCE.exec(body);
  if (wrapped) return wrapped[1].trim();
  const embedded = FENCE_ANYWHERE.exec(body);
  if (embedded) return embedded[1].trim();
  return body.startsWith("```") ? body.replace(/```[a-zA-Z0-9_+-]*/g, "").trim() : body;
}

function balancedSlice(text: string, openChar: string, closeChar: string): string | null {
  const start = text.indexOf(openChar);
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') inString = true;
    else if (char === openChar) depth += 1;
    else if (char === closeChar) {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }
  return null;
}

export function extractJson(text: string): unknown {
  for (const candidate of [text.trim(), stripMarkdownFences(text.trim())]) {
    if (!candidate) continue;
    try {
      return JSON.parse(candidate);
    } catch {
      /* 继续尝试截取平衡块 */
    }
    for (const [opener, closer] of [["{", "}"], ["[", "]"]] as const) {
      const block = balancedSlice(candidate, opener, closer);
      if (!block) continue;
      try {
        return JSON.parse(block);
      } catch {
        /* 换下一种括号 */
      }
    }
  }
  return null;
}

export function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Python `str.splitlines()` 的行分隔集，不只是 \n 与 \r\n。 */
const LINE_BREAKS = /\r\n|[\n\r\x0b\f\x1c\x1d\x1e\x85\u2028\u2029]/;

export function trimWhitespace(text: string): string {
  const lines = text.split(LINE_BREAKS).map((line) => line.replace(/[ \t]+/g, " ").trim());
  return lines.filter((line) => line.length > 0).join("\n").trim();
}
