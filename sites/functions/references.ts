/** `app/optimizer/steps/references.py` 的移植件：引用解析与 GB/T 7714 / APA 统一。 */

import { REF_MARKER_BY_TYPE, REF_TYPE_BY_MARKER, coerceYear, splitMulti, stripChars } from "./fields.ts";
import { AT_WORD_END, AT_WORD_START, WORD_CLASS } from "./plain.ts";

/** Python 的 `search`/`fullmatch` 无状态；带 `g` 的 JS 正则 `.test()` 会推进 lastIndex，
 *  因此这里只声明非全局形态，需要 findall/sub 时再取全局副本。 */
function globalOf(pattern: RegExp): RegExp {
  return new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`);
}

function execAll(pattern: RegExp, text: string): string[] {
  return [...text.matchAll(globalOf(pattern))].map((match) => match[0]);
}

function replaceAll(text: string, pattern: RegExp, replacement: string): string {
  return text.replaceAll(globalOf(pattern), replacement);
}

const CJK = /[一-鿿]/u;
const DOI = new RegExp(`${AT_WORD_START}10\\.\\d{4,9}/[^\\s,;：]+`, "u");
const DOI_URL = new RegExp(String.raw`(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s,;：]+)`, "i");
const YEAR = new RegExp(`${AT_WORD_START}(?:1[89]|20)\\d{2}${AT_WORD_END}`, "u");
const MARKER = /\[([JCMDR])\]/u;
const APA_AUTHOR_BODY = `[A-Z]${WORD_CLASS}+'-+,\\s+(?:[A-Z]\\.){1,3}`;
const APA_AUTHOR = new RegExp(APA_AUTHOR_BODY, "u");
const FOLLOWING_APA = new RegExp(`\\s*(?:[,&]|和|\\band\\b|\\s)*(${APA_AUTHOR_BODY})`, "u");
const SENTENCE = /(?<=[.。])\s+(?=[A-Z(（\d]|[一-鿿])/gu;
const VOLUME = new RegExp(
  `${AT_WORD_START}(\\d{1,4})\\s*\\((\\d{1,3})\\)\\s*[:：]?\\s*(\\d+\\s*[-–—]\\s*\\d+)?`,
  "u",
);
const PAGES = /[:：]\s*(\d+\s*[-–—]\s*\d+)\s*\.?$/u;
const LEADING_INDEX = /^\s*(?:\[?\d{1,3}\]?[.、)])\s*/u;
const APA_YEAR_HEAD = /\s*\((?:1[89]|20)\d{2}\)/u;

const CONFERENCE_HINTS = new Set([
  "cvpr", "iccv", "eccv", "wacv", "bmvc", "neurips", "nips", "icml", "iclr", "acl", "emnlp", "naacl", "coling",
  "aaai", "ijcai", "kdd", "sigir", "www", "sigcomm", "nsdi", "osdi", "sosp", "vldb", "icde", "sigmod", "icra",
  "iros", "aamas", "interspeech", "icassp", "miccai", "chi", "icse", "fse", "pldi", "popl", "oopsla", "asplos",
  "micro", "hpca", "sc", "ccs", "usenix", " proceedings", "symposium", "conference", "workshop", "colt", "uai",
  "aistats",
]);
const JOURNAL_HINTS = new Set([
  "nature", "science", "journal", "transactions", "letters", "survey", "review", "学报", "期刊", "通报",
  "tpami", "jmlr", "pnas", "ai advances",
]);
const BOOK_HINTS = new Set(["monograph", "book", "press", "出版社", "书"]);

export type CitationFields = {
  title: string;
  authors: string[];
  venue: string | null;
  year: number | null;
  doi: string | null;
  type: string;
  volume: string | null;
  issue: string | null;
  pages: string | null;
};

function normalize(text: string): string {
  let body = text.trim().replace(LEADING_INDEX, "");
  body = body.replaceAll("　", " ").replaceAll("\u00a0", " ");
  return body.replace(/\s+/gu, " ").trim();
}

function isAlpha(text: string): boolean {
  return text.length > 0 && /^[\p{L}]+$/u.test(text);
}

function whitespaceSplit(text: string): string[] {
  return text.split(/\s+/u).filter((part) => part.length > 0);
}

function looksLikeNames(chunk: string): boolean {
  const text = stripChars(chunk.trim(), " .;；,，");
  if (!text || clipLength(text) > 120 || text.includes("[") || YEAR.test(text)) return false;
  const parts = splitMulti(text);
  if (!parts.length) return false;
  for (const part of parts) {
    if (CJK.test(part)) {
      if (clipLength(part) > 8) return false;
      continue;
    }
    const words = whitespaceSplit(part);
    if (!(words.length >= 1 && words.length <= 6)) return false;
    for (const word of words) {
      if (!isAlpha(stripChars(word, ".'-")) && !CJK.test(word)) return false;
    }
  }
  return true;
}

function clipLength(text: string): number {
  return Array.from(text).length;
}

function isYearOnly(chunk: string): boolean {
  const body = stripChars(chunk, " .()（）");
  const match = YEAR.exec(body);
  return match !== null && match[0] === body;
}

function apaAuthorToName(match: string): string {
  const parts = match
    .split(",")
    .map((part) => stripChars(part.trim(), "."))
    .filter((part) => part.length > 0);
  return parts.join(" ");
}

/** 识别 APA 式 `Zhang, S., & Li, S. (2023)` 作者段，避免逗号把它切碎。 */
function extractApaAuthors(body: string): [string[], string] {
  const first = APA_AUTHOR.exec(body);
  if (!first || first.index !== 0) return [[], body];
  const authors = [apaAuthorToName(first[0])];
  let end = first[0].length;
  for (;;) {
    const match = FOLLOWING_APA.exec(body.slice(end));
    if (!match || match.index !== 0) break;
    authors.push(apaAuthorToName(match[1]));
    end += match[0].length;
  }
  if (!APA_YEAR_HEAD.test(body.slice(end))) return [[], body];
  return [authors, body.slice(end)];
}

function cleanVenue(text: string): string {
  let venue = text.replaceAll("//", " ").trim();
  for (const pattern of [DOI, VOLUME, PAGES, YEAR]) venue = replaceAll(venue, pattern, " ");
  venue = replaceAll(venue, MARKER, " ").replace(/\s+/gu, " ");
  for (let round = 0; round < 3; round += 1) {
    venue = venue.replaceAll(/(?:^|(?<=\s))[,，.。;；:：]\s*/gu, "");
    venue = venue.replaceAll(/\s*[,，]\s*(?=[,，.。;；]|$)/gu, " ");
  }
  return venue.replace(/\s+/gu, " ").trim();
}

function inferType(venue: string, hasVolume: boolean): string {
  const lowered = venue.toLowerCase();
  for (const hint of CONFERENCE_HINTS) if (lowered.includes(hint)) return "conference";
  for (const hint of JOURNAL_HINTS) if (lowered.includes(hint)) return "journal";
  for (const hint of BOOK_HINTS) if (lowered.includes(hint)) return "book";
  return hasVolume ? "journal" : "unknown";
}

export function parseCitation(text: string): CitationFields | null {
  let body = normalize(text);
  if (clipLength(body) < 12) return null;

  let doi: string | null = null;
  let doiMatch = DOI_URL.exec(body);
  if (doiMatch) {
    doi = doiMatch[1];
  } else {
    doiMatch = DOI.exec(body);
    if (doiMatch) doi = doiMatch[0];
  }
  if (doiMatch) {
    const before = body.slice(0, doiMatch.index);
    const after = body.slice(doiMatch.index + doiMatch[0].length);
    body = `${before} ${after}`.replace(/\s+/gu, " ").trim();
  }

  const years = execAll(YEAR, body);
  const year = years.length > 0 ? Number.parseInt(years[years.length - 1], 10) : coerceYear(body);

  const marker = MARKER.exec(body);
  let refType: string | null = marker ? REF_TYPE_BY_MARKER[`[${marker[1]}]`] ?? null : null;

  const volumeMatch = VOLUME.exec(body);
  let volume: string | null = null;
  let issue: string | null = null;
  let pages: string | null = null;
  if (volumeMatch) {
    volume = volumeMatch[1];
    issue = volumeMatch[2];
    pages = volumeMatch[3] ?? null;
  } else {
    const pagesMatch = PAGES.exec(body);
    pages = pagesMatch ? pagesMatch[1] : null;
  }

  let chunks: string[];
  let authors: string[];
  const [apaAuthors, apaRest] = extractApaAuthors(body);
  if (apaAuthors.length > 0) {
    authors = apaAuthors;
    chunks = apaRest.split(SENTENCE).map((part) => stripChars(part, " .")).filter((part) => part.length > 0);
    chunks = chunks.filter((chunk) => !isYearOnly(chunk));
  } else {
    chunks = body.split(SENTENCE).map((part) => stripChars(part, " .")).filter((part) => part.length > 0);
    authors = [];
    if (chunks.length >= 2 && looksLikeNames(chunks[0])) {
      authors = splitMulti(chunks[0]);
      chunks = chunks.slice(1);
    }
  }
  if (chunks.length === 0) return null;

  let title = chunks[0];
  let venueText = chunks.slice(1).join(" ");
  if (marker !== null && title.includes(marker[0])) {
    const at = title.indexOf(marker[0]);
    const tail = title.slice(at + marker[0].length);
    title = title.slice(0, at);
    venueText = `${stripChars(tail, "/ ")} ${venueText}`.trim();
  } else if (title.includes("//")) {
    const at = title.indexOf("//");
    const tail = title.slice(at + 2);
    title = title.slice(0, at);
    venueText = `${tail} ${venueText}`.trim();
  }
  title = stripChars(replaceAll(title, MARKER, "").replace(/\s+/gu, " "), " .,;:、");
  const venue = cleanVenue(venueText);

  if (!title || isYearOnly(title) || year === null) return null;
  if (refType === null) refType = inferType(venue, volumeMatch !== null);

  return { title, authors, venue: venue || null, year, doi, type: refType, volume, issue, pages };
}

function firstChar(text: string): string {
  return Array.from(text)[0] ?? "";
}

function gbtName(name: string): string {
  if (CJK.test(name)) return name;
  const words = whitespaceSplit(name).map((word) => stripChars(word, ".,'’-")).filter((word) => word.length > 0);
  if (words.length < 2) return name;
  const initials = words.slice(1).map((word) => firstChar(word)).join("").toUpperCase();
  return `${words[0]} ${initials}`;
}

function apaName(name: string): string {
  if (CJK.test(name)) return name;
  const words = whitespaceSplit(name).map((word) => stripChars(word, ".")).filter((word) => word.length > 0);
  if (words.length < 2) return name;
  const initials = words.slice(1).map((word) => `${firstChar(word)}.`).join(" ");
  return `${words[0]}, ${initials}`;
}

export function renderGbt7714(fields: CitationFields): string {
  const marker = REF_MARKER_BY_TYPE[String(fields.type ?? "")] ?? "";
  const year = fields.year;
  if (!marker || !year) return "";
  const names = fields.authors ?? [];
  let authors = names.slice(0, 3).map(gbtName).join(", ");
  if (names.length > 3) authors += ", 等";
  const title = fields.title.replace(/\.+$/u, "");
  const venue = fields.venue;
  const pages = fields.pages ? fields.pages.replace(/\s+/gu, "") : null;
  let body: string;
  if (fields.type === "conference") {
    body = venue ? `${title}${marker}//${venue}. ${year}` : `${title}${marker}. ${year}`;
    if (pages) body += `: ${pages}`;
  } else {
    let detail = venue ? `${venue}, ${year}` : String(year);
    if (fields.volume) detail += `, ${fields.volume}(${fields.issue || 1})`;
    if (pages) detail += `: ${pages}`;
    body = `${title}${marker}. ${detail}`;
  }
  return authors ? `${authors}. ${body}.` : `${body}.`;
}

export function renderApa(fields: CitationFields): string {
  const names = (fields.authors ?? []).slice(0, 8).map(apaName);
  if (names.length === 0) return "";
  let authors: string;
  if (names.length === 1) authors = names[0];
  else if (names.length === 2) authors = `${names[0]}, & ${names[1]}`;
  else authors = `${names.slice(0, -1).join(", ")}, & ${names[names.length - 1]}`;
  if ((fields.authors ?? []).length > 8) authors += ", …";
  const year = fields.year;
  const venue = fields.venue;
  if (!year) return "";
  let tail = `${authors} (${year}). ${fields.title.replace(/\.+$/u, "")}`;
  tail += venue ? `. ${venue}.` : ".";
  if (fields.doi) tail += ` https://doi.org/${fields.doi}`;
  return tail;
}

export function isCompliant(text: string, style: string): boolean {
  const body = text.trim();
  if (!body || !YEAR.test(body)) return false;
  if (style === "apa") return /\((?:1[89]|20)\d{2}\)/u.test(body);
  return MARKER.test(body) && body.endsWith(".");
}

export function normalizeReference(text: string, style: string): [string, boolean] {
  const original = text.trim();
  if (isCompliant(original, style)) return [original, false];
  const fields = parseCitation(original);
  if (fields === null) return [original, false];
  const rendered = style === "apa" ? renderApa(fields) : renderGbt7714(fields);
  if (!rendered || !isCompliant(rendered, style)) return [original, false];
  return [rendered, true];
}
