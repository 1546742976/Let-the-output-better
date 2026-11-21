/** 字段级归一工具（`app/schemas.py` 的无模型部分），供 references 与 schemas 共用。 */

export const MULTI_SPLIT = /[、;；，,/|]+|\s+and\s+|\s*&\s+/gu;
const YEAR_SEARCH = /(?:1[89]|20)\d{2}/u;
const REFERENCE_SPLIT = /\n+|;|；|(?=\[\d+\])/gu;
const NUMBER_SEARCH = /-?\d+(?:\.\d+)?/u;

export const REF_TYPE_BY_MARKER: Record<string, string> = {
  "[J]": "journal",
  "[C]": "conference",
  "[M]": "book",
  "[D]": "thesis",
  "[R]": "report",
};
export const REF_MARKER_BY_TYPE: Record<string, string> = {
  journal: "[J]",
  conference: "[C]",
  book: "[M]",
  thesis: "[D]",
  report: "[R]",
};

export function asString(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map(asString).join(",");
  if (value instanceof Set) return [...value].map(asString).join(",");
  if (typeof value === "number") return formatNumber(value);
  if (typeof value === "boolean") return value ? "True" : "False";
  return String(value);
}

/** Python `str(int)` 的形态；JS 不区分整数与浮点，JSON 里写 `2.0` 会被当成 `2`。 */
function formatNumber(value: number): string {
  return String(value);
}

/** 按字符集剥两端（Python 的 `str.strip(chars)`）。 */
export function stripChars(text: string, chars: string): string {
  const set = new Set(Array.from(chars));
  const list = Array.from(text);
  let start = 0;
  let end = list.length;
  while (start < end && set.has(list[start])) start += 1;
  while (end > start && set.has(list[end - 1])) end -= 1;
  return list.slice(start, end).join("");
}

function clean(part: string): string {
  return stripChars(stripChars(part.trim(), "\"'“”《》"), " .。;；、,，");
}

function cleanReference(item: string): string {
  return stripChars(stripChars(item.trim(), "\"'“”《》"), " ;；、,，");
}

export function splitMulti(value: unknown): string[] {
  if (value === null || value === undefined) return [];
  let parts: string[];
  if (Array.isArray(value)) parts = value.map(asString);
  else if (value instanceof Set) parts = [...value].map(asString);
  else parts = asString(value).split(MULTI_SPLIT);
  const result: string[] = [];
  for (const part of parts) {
    const cleaned = clean(part);
    if (cleaned && !result.includes(cleaned)) result.push(cleaned);
  }
  return result;
}

export function splitReferences(value: unknown): string[] {
  if (value === null || value === undefined) return [];
  let items: string[];
  if (Array.isArray(value)) items = value.map(asString);
  else if (value instanceof Set) items = [...value].map(asString);
  else items = asString(value).split(REFERENCE_SPLIT);
  const result: string[] = [];
  for (const item of items) {
    const cleaned = cleanReference(item);
    if (cleaned && !result.includes(cleaned)) result.push(cleaned);
  }
  return result;
}

export function coerceYear(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean") return null;
  let year: number | null = null;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    year = Math.trunc(value);
  } else {
    const match = YEAR_SEARCH.exec(asString(value));
    if (!match) return null;
    year = Number.parseInt(match[0], 10);
  }
  return year !== null && year >= 1500 && year <= 2100 ? year : null;
}

export function coerceNumber(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const match = NUMBER_SEARCH.exec(asString(value).replace(/,/g, ""));
  return match ? Number.parseFloat(match[0]) : null;
}
