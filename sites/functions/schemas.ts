/** `app/schemas.py` 的移植件：内置 Schema 的别名解析、类型归一与校验。 */

import { coerceNumber, coerceYear, REF_TYPE_BY_MARKER, splitMulti, splitReferences } from "./fields.ts";
import { parseCitation } from "./references.ts";

export type FieldKind = "str" | "int" | "float" | "strlist" | "reflist" | "reftype";

export type FieldSpec = {
  name: string;
  aliases: string[];
  kind: FieldKind;
  required: boolean;
  default: unknown;
  /** 对应 Pydantic 的 `field_validator(mode="before")` 里按字段定制的那一支。 */
  transform?: (value: unknown) => unknown;
};

export type SchemaSpec = {
  name: string;
  fields: FieldSpec[];
  /** 对应 Pydantic 的 `model_validator(mode="before")`。 */
  before?: (data: Record<string, unknown>) => Record<string, unknown>;
  /** 对应 `parse_text`：非 JSON 输出的确定性解析。 */
  textParser?: (text: string) => Record<string, unknown> | null;
};

const REF_TYPES = ["journal", "conference", "book", "thesis", "report", "unknown"];

function field(
  name: string,
  kind: FieldKind,
  aliases: string[],
  required: boolean,
  defaultValue: unknown,
  transform?: (value: unknown) => unknown,
): FieldSpec {
  return {
    name,
    kind,
    aliases: [name, ...aliases.filter((alias) => alias !== name)],
    required,
    default: defaultValue,
    transform,
  };
}

const PAPER: SchemaSpec = {
  name: "paper",
  fields: [
    field("title", "str", ["paper_title", "name"], true, null),
    field("authors", "strlist", ["author"], false, []),
    field("year", "int", ["pub_year", "publication_year", "date"], false, null),
    field("venue", "str", ["journal", "conference", "venue_name"], false, null),
    field("keywords", "strlist", ["keyword"], false, []),
    field("abstract", "str", ["summary"], false, null),
    field("references", "reflist", ["reference", "bibliography", "citations"], false, []),
  ],
};

const REFERENCE: SchemaSpec = {
  name: "reference",
  fields: [
    field("title", "str", ["paper_title"], true, null),
    field("authors", "strlist", ["author"], false, []),
    field("venue", "str", ["journal", "conference", "book_title", "container"], false, null),
    field("year", "int", ["pub_year", "date"], false, null),
    field("doi", "str", ["DOI", "identifier"], false, null),
    field("type", "reftype", [], false, "unknown"),
  ],
  textParser: (text) => {
    const fields = parseCitation(text);
    if (fields === null) return null;
    return { ...fields };
  },
};

const EXPERIMENT: SchemaSpec = {
  name: "experiment",
  fields: [
    field("method", "str", ["model", "approach"], true, null),
    field("metric", "str", ["measure"], true, null),
    field("value", "float", ["result", "score"], true, null),
    field("dataset", "str", ["data", "corpus"], false, null),
    field("unit", "str", ["units"], false, null, normalizeUnit),
    field("baseline", "float", ["base"], false, null),
  ],
  before: (data) => {
    if (data.unit === undefined && data.units === undefined) {
      const value = data.value;
      if (typeof value === "string" && /[%％]$/.test(value.trim())) return { ...data, unit: "%" };
    }
    return data;
  },
};

export const SCHEMA_REGISTRY: Record<string, SchemaSpec> = {
  paper: PAPER,
  reference: REFERENCE,
  experiment: EXPERIMENT,
};

const UNIT_ALIASES: Record<string, string> = { 百分比: "%", 百分之: "%", 毫秒: "ms", 秒: "s" };

function normalizeUnit(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  const text = String(value).trim();
  return UNIT_ALIASES[text] ?? text;
}

function normalizeRefType(value: unknown): string {
  if (value === null || value === undefined) return "unknown";
  const text = String(value).trim();
  const marker = text.slice(0, 3).toUpperCase();
  if (REF_TYPE_BY_MARKER[marker]) return REF_TYPE_BY_MARKER[marker];
  const lowered = text.toLowerCase();
  for (const name of REF_TYPES.slice(0, 5)) if (lowered.includes(name)) return name;
  if (text.includes("会议") || text.includes("论文集")) return "conference";
  if (text.includes("期刊") || text.includes("杂志")) return "journal";
  if (text.includes("学位") || text.includes("论文")) return "thesis";
  return "unknown";
}

/** 字段级 `mode="before"` 归一：对应各模型的 field_validator。 */
function coerceForKind(kind: FieldKind, value: unknown): unknown {
  switch (kind) {
    case "strlist":
      return splitMulti(value);
    case "reflist":
      return splitReferences(value);
    case "int":
      return coerceYear(value);
    case "float":
      return coerceNumber(value);
    case "reftype":
      return normalizeRefType(value);
    case "str":
      return typeof value === "string" ? value.trim() : value;
    default:
      return value;
  }
}

function typeError(kind: FieldKind): string {
  switch (kind) {
    case "str":
      return "Input should be a valid string";
    case "int":
      return "Input should be a valid integer";
    case "float":
      return "Input should be a valid number";
    case "strlist":
    case "reflist":
      return "Input should be a valid list";
    default:
      return `Input should be ${REF_TYPES.map((name) => `'${name}'`).join(', ')}`;
  }
}

export type ValidationError = { loc: string; msg: string };

function checkValue(kind: FieldKind, value: unknown, nullable: boolean): string | null {
  // `int | None` 这类可选字段允许 None；必填字段的 None 仍是类型错误。
  if (value === null && nullable) return null;
  switch (kind) {
    case "str":
      return typeof value === "string" ? null : typeError(kind);
    case "int":
      return typeof value === "number" && Number.isInteger(value) ? null : typeError(kind);
    case "float":
      return typeof value === "number" && Number.isFinite(value) ? null : typeError(kind);
    case "strlist":
    case "reflist":
      return Array.isArray(value) && value.every((item) => typeof item === "string") ? null : typeError(kind);
    case "reftype":
      return REF_TYPES.includes(String(value)) ? null : typeError(kind);
    default:
      return null;
  }
}

export type Validated = {
  parsed: Record<string, unknown> | null;
  errors: ValidationError[];
};

/** 对齐 `extra="ignore"` 与逐字段校验；返回的 `parsed` 等同 `model_dump()`。 */
export function validateSchema(spec: SchemaSpec, raw: Record<string, unknown>): Validated {
  const data = spec.before ? spec.before(raw) : raw;
  const parsed: Record<string, unknown> = {};
  const errors: ValidationError[] = [];
  for (const spec_field of spec.fields) {
    const present = spec_field.aliases.find((alias) => alias in data);
    if (present === undefined) {
      if (spec_field.required) errors.push({ loc: spec_field.name, msg: "Field required" });
      else parsed[spec_field.name] = cloneDefault(spec_field.default);
      continue;
    }
    const transformed = spec_field.transform ? spec_field.transform(data[present]) : data[present];
    const value = coerceForKind(spec_field.kind, transformed);
    const problem = checkValue(spec_field.kind, value, !spec_field.required);
    if (problem) {
      errors.push({ loc: spec_field.name, msg: problem });
      continue;
    }
    parsed[spec_field.name] = value;
  }
  return { parsed: errors.length === 0 ? parsed : null, errors };
}

function cloneDefault(value: unknown): unknown {
  return Array.isArray(value) ? [...value] : value;
}

export function allowedKeys(spec: SchemaSpec): Set<string> {
  const allowed = new Set<string>();
  for (const spec_field of spec.fields) for (const alias of spec_field.aliases) allowed.add(alias);
  return allowed;
}

export function valueFor(spec: SchemaSpec, data: Record<string, unknown>, name: string): unknown {
  const spec_field = spec.fields.find((item) => item.name === name);
  if (!spec_field) return null;
  for (const alias of spec_field.aliases) if (alias in data) return data[alias];
  return null;
}

export function requiredFieldNames(spec: SchemaSpec | null): string[] | null {
  if (spec === null) return null;
  return spec.fields.filter((item) => item.required).map((item) => item.name);
}

export function optionalFieldNames(spec: SchemaSpec | null): string[] | null {
  if (spec === null) return null;
  return spec.fields.filter((item) => !item.required).map((item) => item.name);
}

export function isFilled(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value as Record<string, unknown>).length > 0;
  return true;
}

const KIND_TO_JSON: Record<FieldKind, string> = {
  str: "string",
  int: "integer",
  float: "number",
  strlist: "array",
  reflist: "array",
  reftype: "string",
};

/** 给修复提示词的字段说明（对应 `describe_model`）。 */
export function describeModel(spec: SchemaSpec | null): string {
  if (spec === null) return "（未指定 Schema，输出 JSON 对象即可）";
  const properties: Record<string, unknown> = {};
  for (const spec_field of spec.fields) {
    const entry: Record<string, unknown> = { type: KIND_TO_JSON[spec_field.kind] };
    if (spec_field.kind === "strlist" || spec_field.kind === "reflist") entry.items = { type: "string" };
    if (spec_field.kind === "reftype") entry.enum = REF_TYPES;
    if (!spec_field.required && spec_field.kind === "int") entry.anyOf = [{ type: "integer" }, { type: "null" }];
    if (!spec_field.required && spec_field.kind === "float") entry.anyOf = [{ type: "number" }, { type: "null" }];
    if (!spec_field.required && spec_field.kind === "str") entry.anyOf = [{ type: "string" }, { type: "null" }];
    if (spec_field.default !== null && spec_field.default !== undefined && !Array.isArray(spec_field.default)) {
      entry.default = spec_field.default;
    }
    properties[spec_field.name] = entry;
  }
  return JSON.stringify(
    {
      type: "object",
      title: spec.name,
      properties,
      required: spec.fields.filter((item) => item.required).map((item) => item.name),
      additionalProperties: false,
    },
    null,
    2,
  );
}

export function parseModelText(spec: SchemaSpec, text: string): Record<string, unknown> | null {
  return spec.textParser ? spec.textParser(text) : null;
}

export function resolveSchema(name: string | null): SchemaSpec | null {
  if (name === null) return null;
  return SCHEMA_REGISTRY[name.toLowerCase()] ?? null;
}
