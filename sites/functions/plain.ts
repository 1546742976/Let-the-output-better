/** `app/plain.py` 的移植件：防御性/晦涩/冗长措辞的词表、检测与打分。 */

import { fixed, fsum, percent, roundTo } from "./fmt.ts";

export const HEDGE_PER_SENTENCE = 1 / 3;
export const AVG_LEN_TARGET = 24.0;
export const LONG_SENTENCE_CHARS = 40;
export const LONG_RATIO_TARGET = 0.2;
export const CLAIM_LAST_PENALTY = 0.15;

/**
 * Python 的 `\w` 与 `\b` 在 str 模式下把汉字、数字都算词字符，JS 只认 ASCII。
 * 这个差别会改变指称切分（"仅在 3 个数据集"里 Python 取出含汉字的 token，
 * 而它是否算"裸数字"直接决定限定语判成 boundary 还是 ungrounded），
 * 所以凡 Python 用了 `\w`/`\b` 的模式都换成显式的 Unicode 词类与环视。
 */
export const WORD_CLASS = String.raw`[\p{L}\p{Nd}_]`;
export const AT_WORD_START = String.raw`(?<![\p{L}\p{Nd}_])`;
export const AT_WORD_END = String.raw`(?![\p{L}\p{Nd}_])`;

const NOISE = /[^0-9a-z一-鿿]+/gu;
const SENT_SPLIT = /[。！？；!?;\n]+|\.(?=\s|$)/gu;

export const BOILERPLATE = [
  "值得注意的是",
  "值得指出的是",
  "需要指出的是",
  "需要说明的是",
  "不难发现",
  "不难看出",
  "众所周知",
  "值得一提的是",
  "综上所述",
  "总而言之",
  "总的来说",
  "整体来看",
  "在一定程度上",
  "在某种程度上",
];

export const PRAISE = [
  "充分表明",
  "充分证明",
  "有力证明",
  "至关重要",
  "不可或缺",
  "举足轻重",
  "具有广阔的应用前景",
  "广阔的应用前景",
  "巨大的应用潜力",
  "巨大的商业价值",
  "具有重要意义",
  "深远影响",
  "显著提升",
  "大幅提升",
  "极大提升",
  "性能优势和广阔的应用前景",
];

export const DISCLAIMER = [
  "仅供参考",
  "不构成任何专业建议",
  "不构成建议",
  "无法完全保证",
  "不保证完全准确",
  "建议进一步核实",
  "以上信息以原文为准",
  "据我们所知",
  "据作者了解",
  "如需更多帮助请随时告诉我",
  "如需更多",
  "请随时告诉我",
  "希望对您有帮助",
];

export const HEDGE = [
  "或许",
  "大概",
  "似乎",
  "看起来",
  "某种程度",
  "某种意义上",
  "视情况而定",
  "尚不明确",
  "尚需进一步",
  "有待进一步",
  "不排除",
  "原则上",
  "基本上",
  "较为",
  "相对",
  "可能",
];

export const DOWNGRADE = [
  "探索性证据",
  "初步迹象",
  "初步结果",
  "需谨慎解读",
  "需要谨慎解读",
  "不应过度解读",
  "不可过度推广",
  "仅供参考的证据",
  "有限的支持",
  "或许只能被视为",
  "充其量",
];

export const SELFNEG = [
  "并不意味着",
  "并非意味着",
  "不能据此说明",
  "不足以说明",
  "无法证明",
  "不应被理解为",
  "不能作为结论",
];

export const BOUNDARY_CUES = [
  "仅在",
  "只在",
  "限于",
  "受限于",
  "尚未在",
  "未在",
  "仅适用于",
  "只适用于",
  "仅覆盖",
  "仅使用",
  "仅测试",
  "覆盖",
  "涉及",
];

export const SCOPE_FAMILIES = [
  ["数据集", "评测集", "基准"],
  ["样本", "样本量", "被试", "队列"],
  ["时间范围", "时间窗", "时间段"],
  ["横截面", "纵向"],
];

export const SCOPE_NOUNS = SCOPE_FAMILIES.flat();

export const JARGON = [
  "赋能",
  "抓手",
  "闭环",
  "颗粒度",
  "底层逻辑",
  "范式转换",
  "路径依赖",
  "方法论层面",
  "视角下",
  "进行了优化",
  "进行了相应的优化",
];

const REFERENT_NUMBER = /\d+(?:[.,]\d+)?/gu;
const REFERENT_LATIN = new RegExp(
  `${AT_WORD_START}(?:[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+|[A-Z][A-Za-z0-9]{3,}|${WORD_CLASS}*\\d+${WORD_CLASS}*)${AT_WORD_END}`,
  "gu",
);

const LATIN_STOP = new Set([
  "This", "That", "These", "Those", "There", "Their", "They", "It", "Its", "In", "On", "For", "And", "But",
  "Our", "We", "You", "The", "Although", "Though", "However", "Moreover", "Furthermore", "Results", "Result",
  "Note", "Noted", "Evidence", "Study", "Works",
]);

const PATTERNS = [
  /不仅.+?而且/u,
  /由于.+?因此.+?使得/u,
  /[^的，。；]{2,}的[^的，。；]{2,}的[^的，。；]{2,}的/u,
  /通过.{2,20}的方式来实现/u,
  /对.{2,20}进行了.{2,10}的/u,
  /在.{1,20}的基础上/u,
  /在.{1,20}之下/u,
  /从.{1,20}层面/u,
];

export const EN_PHRASES: Record<string, Array<[string, string]>> = {
  hedge: [
    ["may/might/could", String.raw`\b(?:may|might|could|perhaps|arguably|seems?)\b`],
    ["to some extent", String.raw`\bto some extent\b`],
    ["it seems that", String.raw`\bit seems that\b`],
  ],
  boilerplate: [
    ["it is worth noting", String.raw`\bit (?:is )?worth noting that\b`],
    ["note that", String.raw`\bnote that\b`],
    ["it should be noted", String.raw`\b(?:it )?should be noted\b`],
  ],
  praise: [
    ["plays a crucial role", String.raw`\bplays a (?:crucial|vital|pivotal) role\b`],
    ["provides valuable insights", String.raw`\bprovides valuable insights\b`],
    ["offers significant advantages", String.raw`\boffers significant advantages\b`],
  ],
  disclaimer: [
    ["informational purposes only", String.raw`\bthis is for informational purposes only\b`],
    ["consult a professional", String.raw`\bplease consult a professional\b`],
  ],
};

export const GROUP_WEIGHTS: Record<string, number> = {
  boilerplate: 1.0,
  praise: 1.0,
  disclaimer: 1.0,
  downgrade: 1.0,
  selfneg: 1.0,
};

export const REWRITES: Array<[string, string]> = [
  ["值得注意的是，", ""],
  ["值得指出的是，", ""],
  ["需要指出的是，", ""],
  ["需要说明的是，", ""],
  ["不难发现，", ""],
  ["不难看出，", ""],
  ["众所周知，", ""],
  ["综上所述，", ""],
  ["总而言之，", ""],
  ["总的来说，", ""],
  ["实验结果充分表明", "实验表明"],
  ["结果充分表明", "结果表明"],
  ["充分表明", "表明"],
  ["具有显著的性能优势和广阔的应用前景", "性能优于对比方法"],
  ["具有显著的性能优势", "性能优于对比方法"],
  ["广阔的应用前景", "适用范围较广"],
  ["具有重要意义", ""],
  ["在一定程度上", ""],
  ["在某种程度上，", ""],
  ["以上内容仅供参考，", ""],
  ["以上内容仅供参考。", ""],
  ["这或许只能被视为探索性证据；", ""],
  ["初步结果需谨慎解读，", ""],
  ["需谨慎解读。", ""],
  ["如需更多资料请随时告诉我", ""],
  ["如需更多论文信息请随时告诉我", ""],
  ["本文所提出的方法", "本文方法"],
  ["我们创新性地提出", "我们提出"],
  ["如需更多论文信息请随时告诉我", ""],
  ["请随时告诉我", ""],
  ["希望对您有帮助！", ""],
];

const LITERAL_GROUPS: Array<[string, string[]]> = [
  ["boilerplate", BOILERPLATE],
  ["praise", PRAISE],
  ["disclaimer", DISCLAIMER],
  ["downgrade", DOWNGRADE],
  ["selfneg", SELFNEG],
  ["hedge", HEDGE],
  ["jargon", JARGON],
];

const QUALIFICATION_GROUPS = ["boilerplate", "praise", "disclaimer", "downgrade", "selfneg", "hedge"];

const FACT_NUMBER = new RegExp(String.raw`(?<![\d.,])[-−+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?`, "gu");
const PERCENT_MARK = /%|％|百分之/gu;
const POINT_MARK = /个百分点|个点/gu;
const ACRONYM = /(?<![A-Za-z])[A-Z]{2,}(?![a-z])/gu;
const ACRONYM_STOP = new Set(["THE", "AND", "OR", "NOT", "ALL", "FOR", "OF", "TO", "OK", "ID"]);

export type Finding = { group: string; label: string; count: number; samples: string[] };
export type Qualification = {
  index: number;
  verdict: string;
  sentence: string;
  text: string;
  referents: string[];
};
export type Ordering = { claim_first: boolean; claim_index: number; limit_index: number };
export type PlainReport = {
  findings: Finding[];
  metrics: Record<string, number>;
  plainness: number;
  concision: number;
  confidence: number;
  qualifications: Qualification[];
  ordering: Ordering;
};

/** Python 的按码点切片（`text[:24]`），JS 的 `slice` 按 UTF-16 单元会切错代理对。 */
export function clip(text: string, limit: number): string {
  return Array.from(text).slice(0, limit).join("");
}

export function normalizeText(text: string): string {
  return text.toLowerCase().replace(NOISE, "");
}

export function sentences(text: string): string[] {
  return text
    .split(SENT_SPLIT)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

/** 码点数（Python 的 `len`），不是 UTF-16 单元数。 */
export function charLength(text: string): number {
  return Array.from(text).length;
}

function countLiteral(haystack: string, needle: string): number {
  if (needle.length === 0) return 0;
  return haystack.split(needle).length - 1;
}

/** 跨组共享一份工作副本：长短语命中后挖掉，短句语不再重复计一次。 */
function groupedLiteralHits(text: string): Record<string, Array<[string, number]>> {
  const pairs: Array<[string, string]> = [];
  for (const [group, phrases] of LITERAL_GROUPS) {
    for (const phrase of phrases) pairs.push([group, phrase]);
  }
  // Python 的 sorted(key=len, reverse=True) 是稳定排序：等长按原顺序，JS sort 同样稳定。
  const ordered = pairs
    .map((pair, index) => ({ pair, index, length: charLength(pair[1]) }))
    .sort((a, b) => (b.length - a.length) || (a.index - b.index))
    .map((item) => item.pair);

  let working = text;
  const counts = new Map<string, number>();
  for (const [group, phrase] of ordered) {
    const hit = countLiteral(working, phrase);
    if (hit > 0) {
      counts.set(`${group}\u0000${phrase}`, hit);
      working = working.replaceAll(phrase, "\u0000".repeat(charLength(phrase)));
    }
  }
  const grouped: Record<string, Array<[string, number]>> = {};
  for (const [group, phrases] of LITERAL_GROUPS) {
    const rows: Array<[string, number]> = [];
    for (const phrase of phrases) {
      const hit = counts.get(`${group}\u0000${phrase}`);
      if (hit !== undefined) rows.push([phrase, hit]);
    }
    grouped[group] = rows;
  }
  return grouped;
}

function countPatterns(text: string, specs: Array<[string, string]>): Array<[string, string, number]> {
  const out: Array<[string, string, number]> = [];
  for (const [label, pattern] of specs) {
    const found = text.match(new RegExp(pattern, "giu"));
    if (found) out.push([label, pattern, found.length]);
  }
  return out;
}

export function findings(text: string): Finding[] {
  if (!text.trim()) return [];
  const result: Finding[] = [];
  const grouped = groupedLiteralHits(text);
  for (const [group] of LITERAL_GROUPS) {
    for (const [label, count] of grouped[group]) {
      result.push({ group, label, count, samples: [label] });
    }
  }
  for (const [group, specs] of Object.entries(EN_PHRASES)) {
    for (const [label, pattern, count] of countPatterns(text, specs)) {
      result.push({ group, label, count, samples: [pattern] });
    }
  }
  for (const regex of PATTERNS) {
    const hits = text.match(new RegExp(regex.source, "gu")) ?? [];
    if (hits.length > 0) {
      result.push({ group: "pattern", label: "冗长句式", count: hits.length, samples: [hits[0].slice(0, 24)] });
    }
  }
  return result;
}

export function metrics(text: string): Record<string, number> {
  const parts = sentences(text);
  const total = parts.length || 1;
  const lengths = parts.map((part) => charLength(part));
  const avgLen = fsum(lengths) / total;
  const longRatio = lengths.filter((length) => length > LONG_SENTENCE_CHARS).length / total;
  const hardHits = findings(text)
    .filter((finding) => finding.group in GROUP_WEIGHTS)
    .reduce((sum, finding) => sum + finding.count, 0);
  return {
    chars: charLength(text),
    sentences: parts.length,
    avg_len: roundTo(avgLen, 1),
    long_ratio: roundTo(longRatio, 3),
    hard_hits: hardHits,
    hard_density: roundTo(hardHits / total, 3),
  };
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export function referents(sentence: string): string[] {
  const numbers: string[] = [];
  for (const token of sentence.match(REFERENT_NUMBER) ?? []) {
    if (Number.parseFloat(token.replace(/,/g, "")) !== 0) numbers.push(token);
  }
  const latin = (sentence.match(REFERENT_LATIN) ?? []).filter((token) => !LATIN_STOP.has(token));
  const scope = SCOPE_NOUNS.filter((noun) => sentence.includes(noun));
  return [...new Set([...numbers, ...latin, ...scope])];
}

/** `1,200,300` / `1200300` / `12.50` 归一到同一个可比形态（对齐 `Decimal.normalize`）。 */
export function canonicalNumber(token: string): string {
  let cleaned = token.replace(/,/g, "").replace(/−/g, "-");
  if (cleaned.startsWith("+")) cleaned = cleaned.slice(1);
  const negative = cleaned.startsWith("-");
  if (negative) cleaned = cleaned.slice(1);
  const [integerRaw, fractionRaw = ""] = cleaned.split(".");
  const integer = (integerRaw.replace(/^0+/, "") || "0");
  const fraction = fractionRaw.replace(/0+$/, "");
  const body = fraction.length > 0 ? `${integer}.${fraction}` : integer;
  return negative ? `-${body}` : body;
}

export function canonicalNumbers(text: string): Map<string, number> {
  const counts = new Map<string, number>();
  for (const token of text.match(FACT_NUMBER) ?? []) {
    const key = canonicalNumber(token);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

export function unitClasses(text: string): Map<string, number> {
  const classes = new Map<string, number>();
  const percentCount = (text.match(PERCENT_MARK) ?? []).length;
  const pointCount = (text.match(POINT_MARK) ?? []).length;
  if (percentCount) classes.set("百分号", percentCount);
  if (pointCount) classes.set("百分点", pointCount);
  return classes;
}

export function acronymAnchors(text: string): Set<string> {
  return new Set((text.match(ACRONYM) ?? []).filter((token) => !ACRONYM_STOP.has(token)));
}

function qualificationMarkers(sentence: string): Record<string, number> {
  const hits = groupedLiteralHits(sentence);
  const markers: Record<string, number> = {};
  for (const group of QUALIFICATION_GROUPS) {
    markers[group] = hits[group].reduce((sum, [, count]) => sum + count, 0);
  }
  markers.boundary_cue = BOUNDARY_CUES.filter((cue) => sentence.includes(cue)).length;
  const specs = [...EN_PHRASES.hedge, ...EN_PHRASES.boilerplate, ...EN_PHRASES.praise];
  for (const [, pattern] of specs) {
    if (new RegExp(pattern, "iu").test(sentence)) markers.hedge += 1;
  }
  return markers;
}

export function scopeGroup(token: string): string[] {
  for (const family of SCOPE_FAMILIES) {
    if (family.includes(token)) return family;
  }
  return [token];
}

function inSource(token: string, sourceNorm: string): boolean {
  if (sourceNorm.includes(normalizeText(token))) return true;
  return scopeGroup(token).some((member) => sourceNorm.includes(normalizeText(member)));
}

function isBareNumber(token: string): boolean {
  return new RegExp(`^(?:${REFERENT_NUMBER.source})$`, "u").test(token);
}

export function classifyQualification(sentence: string, sourceNorm: string | null = null): string | null {
  const markers = qualificationMarkers(sentence);
  const isQualification = QUALIFICATION_GROUPS.some((group) => markers[group] > 0) || markers.boundary_cue > 0;
  if (!isQualification) return null;
  const refs = referents(sentence);
  if (refs.length === 0) return "ungrounded";
  const scoped = markers.boundary_cue > 0 ? refs : refs.filter((token) => !isBareNumber(token));
  if (scoped.length === 0) return "ungrounded";
  if (sourceNorm === null) return "unverified";
  return scoped.some((token) => inSource(token, sourceNorm)) ? "boundary" : "ungrounded";
}

export function qualifications(text: string, source: string | null = null): Qualification[] {
  const sourceNorm = source && source.trim() ? normalizeText(source) : null;
  const out: Qualification[] = [];
  sentences(text).forEach((sentence, index) => {
    const verdict = classifyQualification(sentence, sourceNorm);
    if (verdict === null) return;
    out.push({
      index,
      verdict,
      sentence,
      text: clip(sentence, 48),
      referents: referents(sentence).slice(0, 4),
    });
  });
  return out;
}

export function ordering(text: string): Ordering {
  const parts = sentences(text);
  let claimIndex = -1;
  let limitIndex = -1;
  parts.forEach((sentence, index) => {
    const markers = qualificationMarkers(sentence);
    const hedged = ["hedge", "downgrade", "selfneg"].some((group) => markers[group] > 0);
    if (claimIndex < 0 && referents(sentence).length > 0 && !hedged) claimIndex = index;
    if (
      limitIndex < 0 &&
      (markers.downgrade > 0 || markers.selfneg > 0 || (hedged && referents(sentence).length === 0))
    ) {
      limitIndex = index;
    }
  });
  return {
    claim_first: claimIndex < 0 || limitIndex < 0 || claimIndex <= limitIndex,
    claim_index: claimIndex,
    limit_index: limitIndex,
  };
}

export function confidence(text: string, source: string | null = null): number {
  const parts = sentences(text);
  if (parts.length === 0) return 1.0;
  const marks = qualifications(text, source);
  const ungrounded = marks.filter((item) => item.verdict === "ungrounded").length;
  let penalty = ungrounded / parts.length;
  if (!ordering(text).claim_first) penalty += CLAIM_LAST_PENALTY;
  return roundTo(clamp(1.0 - penalty), 3);
}

export function plainness(text: string, source: string | null = null): number {
  const found = findings(text);
  const sents = sentences(text);
  const total = Math.max(1, sents.length);
  const hard = fsum(
    found.filter((f) => f.group in GROUP_WEIGHTS).map((f) => f.count * GROUP_WEIGHTS[f.group]),
  );
  const jargon = found.filter((f) => f.group === "jargon").reduce((sum, f) => sum + f.count, 0) * 0.4;
  const patterns = found.filter((f) => f.group === "pattern").reduce((sum, f) => sum + f.count, 0) * 0.5;

  const sourceNorm = source && source.trim() ? normalizeText(source) : null;
  let hedgeTotal = 0;
  for (const sentence of sents) {
    const verdict = classifyQualification(sentence, sourceNorm);
    if (verdict === "boundary" || verdict === "unverified") continue;
    hedgeTotal += qualificationMarkers(sentence).hedge;
  }
  const allowance = Math.ceil(total * HEDGE_PER_SENTENCE);
  const hedge = Math.max(0, hedgeTotal - allowance) * 0.6;
  const penalty = (hard + jargon + patterns + hedge) / total;
  return roundTo(clamp(1.0 - penalty / 1.2), 3);
}

export function concision(text: string): number {
  const parts = sentences(text);
  if (parts.length === 0) return 1.0;
  const lengths = parts.map((part) => charLength(part));
  const avgLen = fsum(lengths) / lengths.length;
  const longRatio = lengths.filter((length) => length > LONG_SENTENCE_CHARS).length / lengths.length;
  const lengthPenalty = clamp((avgLen - AVG_LEN_TARGET) / 30.0) * 0.6;
  const longPenalty = clamp((longRatio - LONG_RATIO_TARGET) / 0.4) * 0.4;
  return roundTo(clamp(1.0 - lengthPenalty - longPenalty), 3);
}

export function summary(report: PlainReport): string {
  const hits = report.findings
    .filter((f) => f.group in GROUP_WEIGHTS)
    .reduce((sum, f) => sum + f.count, 0);
  const hedge = report.findings.filter((f) => f.group === "hedge").reduce((sum, f) => sum + f.count, 0);
  const avgLen = report.metrics.avg_len ?? 0;
  const longRatio = report.metrics.long_ratio ?? 0;
  const bits = [`防御措辞 ${hits} 处`];
  if (hedge) bits.push(`弱化 ${hedge} 处`);
  bits.push(`句均 ${fixed(avgLen, 0)} 字`);
  bits.push(`>40 字句占 ${percent(longRatio, 0)}`);
  return bits.join("、");
}

export function report(text: string, source: string | null = null): PlainReport {
  return {
    findings: findings(text),
    metrics: metrics(text),
    plainness: plainness(text, source),
    concision: concision(text),
    confidence: confidence(text, source),
    qualifications: qualifications(text, source),
    ordering: ordering(text),
  };
}

export function rewriteTable(): Array<[string, string]> {
  return REWRITES;
}
