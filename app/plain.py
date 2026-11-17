"""语言规则层：防御性、晦涩与冗长措辞的词表、检测与打分。

这一层是本项目的主目标所在——把"过度防御 + 看不懂"改成"简短 + 能读懂"。
词表**只在这里存一份**：评分、审计留痕与 mock provider 的对照表都从这里取，
避免"词汇知识住在假模型里"这种结构错误。

判定分三档，避免误伤正常表述：

- 硬命中（boilerplate / praise / disclaimer）：出现即扣分，它们几乎不承担信息。
- 语境判定（hedge）：单个"可能"是在诚实表达不确定性，只有堆叠超出密度配额才扣分。
- 只计统计（jargon / pattern）：行话与长句参与评分曲线，但不逐条判死。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

#: 每个句子允许的 hedge 配额：低于此密度的弱化措辞视为正当的不确定性表达。
HEDGE_PER_SENTENCE = 1 / 3

#: 可读性目标：句均字数与长句占比。
AVG_LEN_TARGET = 24.0
LONG_SENTENCE_CHARS = 40
LONG_RATIO_TARGET = 0.2

# 英文只在 ". " 之后切句，避免把 "88.5%"、"CVPR 2023. 页码" 这类数字截断。
_SENT_SPLIT = re.compile(r"[。！？；!?;\n]+|\.(?=\s|$)")
_CJK = re.compile(r"[一-鿿]")

BOILERPLATE = (
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
)

PRAISE = (
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
)

DISCLAIMER = (
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
)

HEDGE = (
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
    "可能在",
    "可能会",
)

JARGON = (
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
)

# 形如"在……的基础上"的写法只能当句式匹配：写成字面量永远不会命中真实文本。
PATTERNS = (
    re.compile(r"不仅.+?而且"),
    re.compile(r"由于.+?因此.+?使得"),
    re.compile(r"[^的，。；]{2,}的[^的，。；]{2,}的[^的，。；]{2,}的"),
    re.compile(r"通过.{2,20}的方式来实现"),
    re.compile(r"对.{2,20}进行了.{2,10}的"),
    re.compile(r"在.{1,20}的基础上"),
    re.compile(r"在.{1,20}之下"),
    re.compile(r"从.{1,20}层面"),
)

EN_PHRASES: dict[str, tuple[tuple[str, str], ...]] = {
    "hedge": (
        ("may/might/could", r"\b(?:may|might|could|perhaps|arguably|seems?)\b"),
        ("to some extent", r"\bto some extent\b"),
        ("it seems that", r"\bit seems that\b"),
    ),
    "boilerplate": (
        ("it is worth noting", r"\bit (?:is )?worth noting that\b"),
        ("note that", r"\bnote that\b"),
        ("it should be noted", r"\b(?:it )?should be noted\b"),
    ),
    "praise": (
        ("plays a crucial role", r"\bplays a (?:crucial|vital|pivotal) role\b"),
        ("provides valuable insights", r"\bprovides valuable insights\b"),
        ("offers significant advantages", r"\boffers significant advantages\b"),
    ),
    "disclaimer": (
        ("informational purposes only", r"\bthis is for informational purposes only\b"),
        ("consult a professional", r"\bplease consult a professional\b"),
    ),
}

#: 硬命中组的扣分权重；语境判定与统计组另算。
GROUP_WEIGHTS = {"boilerplate": 1.0, "praise": 1.0, "disclaimer": 1.0}

#: mock provider 的确定性改写表，与上面的检测词表同源。
REWRITES = (
    ("值得注意的是，", ""),
    ("值得指出的是，", ""),
    ("需要指出的是，", ""),
    ("需要说明的是，", ""),
    ("不难发现，", ""),
    ("不难看出，", ""),
    ("众所周知，", ""),
    ("综上所述，", ""),
    ("总而言之，", ""),
    ("总的来说，", ""),
    ("实验结果充分表明", "实验表明"),
    ("结果充分表明", "结果表明"),
    ("充分表明", "表明"),
    ("具有显著的性能优势和广阔的应用前景", "性能优于对比方法"),
    ("具有显著的性能优势", "性能优于对比方法"),
    ("广阔的应用前景", "适用范围较广"),
    ("具有重要意义", ""),
    ("在一定程度上", ""),
    ("在某种程度上，", ""),
    ("以上内容仅供参考，", ""),
    ("如需更多资料请随时告诉我", ""),
    ("如需更多论文信息请随时告诉我", ""),
    ("本文所提出的方法", "本文方法"),
    ("我们创新性地提出", "我们提出"),
    ("如需更多论文信息请随时告诉我", ""),
    ("请随时告诉我", ""),
    ("希望对您有帮助！", ""),
)


@dataclass(frozen=True)
class Finding:
    group: str
    label: str
    count: int
    samples: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"group": self.group, "label": self.label, "count": self.count, "samples": list(self.samples)}


@dataclass
class PlainReport:
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    plainness: float = 1.0
    concision: float = 1.0

    def summary(self) -> str:
        """给评分 reason 与审计留痕用的一行可读结论。"""
        hits = sum(f.count for f in self.findings if f.group in GROUP_WEIGHTS)
        hedge = sum(f.count for f in self.findings if f.group == "hedge")
        avg_len = self.metrics.get("avg_len", 0.0)
        long_ratio = self.metrics.get("long_ratio", 0.0)
        bits = [f"防御措辞 {hits} 处"]
        if hedge:
            bits.append(f"弱化 {hedge} 处")
        bits.append(f"句均 {avg_len:.0f} 字")
        bits.append(f">40 字句占 {long_ratio:.0%}")
        return "、".join(bits)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENT_SPLIT.split(text) if part.strip()]


_LITERAL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("boilerplate", BOILERPLATE),
    ("praise", PRAISE),
    ("disclaimer", DISCLAIMER),
    ("hedge", HEDGE),
    ("jargon", JARGON),
)


def _grouped_literal_hits(text: str) -> dict[str, list[tuple[str, int]]]:
    """跨组共享一份工作副本：长短语命中后挖掉，短句语不再重复计一次。

    分组各数各的话，"在某种程度上"会同时算进 boilerplate 与 hedge，同一段冗余罚两遍。
    """
    pairs = [(group, phrase) for group, phrases in _LITERAL_GROUPS for phrase in phrases]
    working = text
    counts: dict[tuple[str, str], int] = {}
    for group, phrase in sorted(pairs, key=lambda item: len(item[1]), reverse=True):
        hit = working.count(phrase)
        if hit:
            counts[(group, phrase)] = hit
            working = working.replace(phrase, "\x00" * len(phrase))
    grouped: dict[str, list[tuple[str, int]]] = {group: [] for group, _ in _LITERAL_GROUPS}
    for group, phrases in _LITERAL_GROUPS:
        for phrase in phrases:
            if (group, phrase) in counts:
                grouped[group].append((phrase, counts[(group, phrase)]))
    return grouped


def _count_patterns(text: str, specs: Iterable[tuple[str, str]]) -> list[tuple[str, str, int]]:
    out = []
    for label, pattern in specs:
        found = re.findall(pattern, text, flags=re.IGNORECASE)
        if found:
            out.append((label, pattern, len(found)))
    return out


def findings(text: str) -> list[Finding]:
    """列出命中的防御性/晦涩/冗长措辞。"""
    if not text.strip():
        return []
    result: list[Finding] = []
    grouped = _grouped_literal_hits(text)
    for group, _ in _LITERAL_GROUPS:
        for label, count in grouped[group]:
            result.append(Finding(group, label, count, (label,)))
    for group, specs in EN_PHRASES.items():
        for label, pattern, count in _count_patterns(text, specs):
            result.append(Finding(group, label, count, (pattern,)))
    for regex in PATTERNS:
        hits = regex.findall(text)
        if hits:
            sample = hits[0] if isinstance(hits[0], str) else hits[0][0]
            result.append(Finding("pattern", "冗长句式", len(hits), (sample[:24],)))
    return result


def metrics(text: str) -> dict[str, float]:
    parts = sentences(text)
    total = len(parts) or 1
    lengths = [len(part) for part in parts]
    avg_len = sum(lengths) / total
    long_ratio = sum(1 for length in lengths if length > LONG_SENTENCE_CHARS) / total
    hard_hits = sum(f.count for f in findings(text) if f.group in GROUP_WEIGHTS)
    return {
        "chars": float(len(text)),
        "sentences": float(len(parts)),
        "avg_len": round(avg_len, 1),
        "long_ratio": round(long_ratio, 3),
        "hard_hits": float(hard_hits),
        "hard_density": round(hard_hits / total, 3),
    }


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def plainness(text: str) -> float:
    """0–1，越高越"直白"。硬命中按句数归一，hedge 只有超出配额才扣。"""
    parts = findings(text)
    sents = max(1, len(sentences(text)))
    hard = sum(f.count * GROUP_WEIGHTS.get(f.group, 0.0) for f in parts if f.group in GROUP_WEIGHTS)
    jargon = sum(f.count for f in parts if f.group == "jargon") * 0.4
    patterns = sum(f.count for f in parts if f.group == "pattern") * 0.5
    hedge_total = sum(f.count for f in parts if f.group == "hedge")
    allowance = math.ceil(sents * HEDGE_PER_SENTENCE)
    hedge = max(0, hedge_total - allowance) * 0.6
    penalty = (hard + jargon + patterns + hedge) / sents
    return round(_clamp(1.0 - penalty / 1.2), 3)


def concision(text: str) -> float:
    """0–1，越高越简短。只看句均长与长句占比，不设字数配额。"""
    parts = sentences(text)
    if not parts:
        return 1.0
    avg_len = sum(len(part) for part in parts) / len(parts)
    long_ratio = sum(1 for part in parts if len(part) > LONG_SENTENCE_CHARS) / len(parts)
    length_penalty = _clamp((avg_len - AVG_LEN_TARGET) / 30.0) * 0.6
    long_penalty = _clamp((long_ratio - LONG_RATIO_TARGET) / 0.4) * 0.4
    return round(_clamp(1.0 - length_penalty - long_penalty), 3)


def report(text: str) -> PlainReport:
    found = findings(text)
    return PlainReport(
        findings=found,
        metrics=metrics(text),
        plainness=plainness(text),
        concision=concision(text),
    )


def rewrite_table() -> tuple[tuple[str, str], ...]:
    """mock provider 使用的确定性对照表。"""
    return REWRITES
