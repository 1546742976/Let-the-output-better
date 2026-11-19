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
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

#: 每个句子允许的 hedge 配额：低于此密度的弱化措辞视为正当的不确定性表达。
HEDGE_PER_SENTENCE = 1 / 3

#: 可读性目标：句均字数与长句占比。
AVG_LEN_TARGET = 24.0
LONG_SENTENCE_CHARS = 40
LONG_RATIO_TARGET = 0.2

#: 主张后置的固定扣分：先讲限制再讲主张，是防御性写作的典型结构。
CLAIM_LAST_PENALTY = 0.15

_NOISE = re.compile(r"[^0-9a-z一-鿿]+")


def normalize_text(text: str) -> str:
    """只保留字母、数字与汉字，用于"能否在原文定位"的比较。"""
    return _NOISE.sub("", text.lower())


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
    "可能",
)

#: 证据降级：把已有结论改说成"初步的、探索性的"。这类模板本身不指向任何东西。
DOWNGRADE = (
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
)

#: 自我否定：直接撤回或反向限定作者原有论断。
SELFNEG = (
    "并不意味着",
    "并非意味着",
    "不能据此说明",
    "不足以说明",
    "无法证明",
    "不应被理解为",
    "不能作为结论",
)

#: 限制标记：说明这句是"限定"而非主张。它们本身不扣分——真正影响结论的研究
#: 边界正是用这些词写的，扣分就等于要求模型删掉诚实的范围声明。
#: 覆盖类标记（仅覆盖/覆盖/涉及）必须在列：真实边界常写成"仅覆盖 240×320 分辨率"，
#: 而数字在没有此类线索时不算指称（见 classify_qualification），漏了就把真边界误杀。
BOUNDARY_CUES = (
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
)

#: 可核对的领域名词族。同族词在原文里往往换了写法：句子说"样本量"，原文写"24 名被试"。
#: 逐字匹配会把这类真实边界误判成无据限定，所以按族互认。刻意不含"场景/条件/规模"
#: 这类泛词——它们在任何一句太极话里都出现，不能证明限定有依据。
SCOPE_FAMILIES = (
    ("数据集", "评测集", "基准"),
    ("样本", "样本量", "被试", "队列"),
    ("时间范围", "时间窗", "时间段"),
    ("横截面", "纵向"),
)
SCOPE_NOUNS = tuple(noun for family in SCOPE_FAMILIES for noun in family)

#: 指称=能在原文里核对的具体对象：数字、标识符形态的拉丁词、领域名词。
_REFERENT_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_REFERENT_LATIN = re.compile(r"\b(?:[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+|[A-Z][A-Za-z0-9]{3,}|\w*\d+\w*)\b")
#: 句首大写的普通英文词不是标识符，否则 "This may..." 会被当成有指称。
_LATIN_STOP = frozenset(
    {
        "This",
        "That",
        "These",
        "Those",
        "There",
        "Their",
        "They",
        "It",
        "Its",
        "In",
        "On",
        "For",
        "And",
        "But",
        "Our",
        "We",
        "You",
        "The",
        "Although",
        "Though",
        "However",
        "Moreover",
        "Furthermore",
        "Results",
        "Result",
        "Note",
        "Noted",
        "Evidence",
        "Study",
        "Works",
    }
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
GROUP_WEIGHTS = {"boilerplate": 1.0, "praise": 1.0, "disclaimer": 1.0, "downgrade": 1.0, "selfneg": 1.0}

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
    ("以上内容仅供参考。", ""),
    ("这或许只能被视为探索性证据；", ""),
    ("初步结果需谨慎解读，", ""),
    ("需谨慎解读。", ""),
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
    confidence: float = 1.0
    qualifications: list[dict[str, object]] = field(default_factory=list)
    ordering: dict[str, object] = field(default_factory=dict)

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

    def verdict_labels(self, verdict: str) -> list[str]:
        return [str(item["text"]) for item in self.qualifications if item["verdict"] == verdict]


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENT_SPLIT.split(text) if part.strip()]


_LITERAL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("boilerplate", BOILERPLATE),
    ("praise", PRAISE),
    ("disclaimer", DISCLAIMER),
    ("downgrade", DOWNGRADE),
    ("selfneg", SELFNEG),
    ("hedge", HEDGE),
    ("jargon", JARGON),
)

#: 判定"这句是不是限定语"的标记组（不含 jargon/pattern：它们不是限定）。
_QUALIFICATION_GROUPS = ("boilerplate", "praise", "disclaimer", "downgrade", "selfneg", "hedge")


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


def referents(sentence: str) -> list[str]:
    """句内可核对的指称：数字（**不过滤单位数**）、标识符形态的拉丁词、领域名词。

    这里刻意不复用 rules.numeric_tokens——那条规则丢弃单位数以避免溯源假命中，
    而"仅在 3 个数据集上验证"的关键指称恰恰是单位数 3。目的不同，规则必须分开。
    """
    found = [token for token in _REFERENT_NUMBER.findall(sentence) if float(token.replace(",", "")) != 0]
    found.extend(token for token in _REFERENT_LATIN.findall(sentence) if token not in _LATIN_STOP)
    found.extend(noun for noun in SCOPE_NOUNS if noun in sentence)
    return list(dict.fromkeys(found))


#: 事实比较用的数字切分器。三条不显眼但会致命的规则：
#: 千分位写成一个 token（否则 `1,200,300` 被拆成 `1200`+`300`，规范的 `1200300`
#: 反而被判"数值变化"）；前置否定环视让 `2019-2023` 读作两个年份而不是
#: `2019` + `-2023`（否则"改成 2019 至 2023"会被误拒）；符号只在不是区间连接号时计入。
_FACT_NUMBER = re.compile(r"(?<![\d.,])[-−+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_PERCENT_MARK = re.compile(r"%|％|百分之")
_POINT_MARK = re.compile(r"个百分点|个点")
# 只把"看起来像缩写"的全大写串当锚点：紧贴汉字也要成立（"发表于CVPR"此前根本不匹配），
# 且不接受后面接小写字母的片段；纯英文全大写功能词用一张小停表兜住。
_ACRONYM = re.compile(r"(?<![A-Za-z])[A-Z]{2,}(?![a-z])")
_ACRONYM_STOP = frozenset({"THE", "AND", "OR", "NOT", "ALL", "FOR", "OF", "TO", "OK", "ID"})


def canonical_number(token: str) -> str:
    """`1,200,300`/`1200300`/`12.50` 都归一到同一个可比形态。

    必须用 `format(..., "f")`：`Decimal("100").normalize()` 的 `str()` 是 `1E+2`。
    精度写法（`12.50%` vs `12.5%`）不算事实变化。
    """
    cleaned = token.replace(",", "").replace("−", "-")
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    return format(Decimal(cleaned).normalize(), "f")


def canonical_numbers(text: str) -> Counter[str]:
    return Counter(canonical_number(token) for token in _FACT_NUMBER.findall(text))


def unit_classes(text: str) -> Counter[str]:
    """单位类别计数：`88.5%` 改成 `88.5 个百分点` 是两个不同的事实断言。

    只比类别计数、不逐个数字配对——配对要把标记归属到具体数字上，收益相同但多一
    处会错的启发式。
    """
    classes: Counter[str] = Counter()
    percent = len(_PERCENT_MARK.findall(text))
    point = len(_POINT_MARK.findall(text))
    if percent:
        classes["百分号"] = percent
    if point:
        classes["百分点"] = point
    return classes


def acronym_anchors(text: str) -> set[str]:
    """全大写缩写集合：删掉 `CVPR` 是丢事实，把 `CVPR` 写成 `cvpr` 不是（只查缺失）。"""
    return {token for token in _ACRONYM.findall(text) if token not in _ACRONYM_STOP}


def _qualification_markers(sentence: str) -> dict[str, int]:
    hits = _grouped_literal_hits(sentence)
    markers = {group: sum(count for _, count in hits[group]) for group in _QUALIFICATION_GROUPS}
    markers["boundary_cue"] = sum(1 for cue in BOUNDARY_CUES if cue in sentence)
    for _label, pattern in EN_PHRASES["hedge"] + EN_PHRASES["boilerplate"] + EN_PHRASES["praise"]:
        if re.search(pattern, sentence, flags=re.IGNORECASE):
            markers["hedge"] += 1
    return markers


def _scope_family(token: str) -> tuple[str, ...]:
    for family in SCOPE_FAMILIES:
        if token in family:
            return family
    return (token,)


def scope_group(token: str) -> tuple[str, ...]:
    """同一个范围概念的等价写法（数据集/评测集/基准）；不属于任何族的 token 自成一组。

    判定层（`_in_source`）与采纳闸门（`check_boundaries`）必须共用它：两处各留一套
    词汇知识时，会出现"判定认为同族互认、闸门却按逐字判丢失"的自相矛盾，合法改写
    被整字段拒绝，而拒绝理由本身是假话。
    """
    return _scope_family(token)


def _in_source(token: str, source_norm: str) -> bool:
    """指称能否在原文定位：数字与标识符逐字匹配，领域名词按同族互认。"""
    if normalize_text(token) in source_norm:
        return True
    return any(normalize_text(member) in source_norm for member in _scope_family(token))


def _is_bare_number(token: str) -> bool:
    return _REFERENT_NUMBER.fullmatch(token) is not None


def classify_qualification(sentence: str, source_norm: str | None = None) -> str | None:
    """判定限定语性质：``ungrounded`` 该删、``boundary`` 该留、``unverified`` 无原文时按留处理。

    依据是**有没有划定范围的指称**，既不是有没有犹豫词，也不是有没有数字：
    "或许只能被视为探索性证据"不指向任何东西；"仅在 3 个数据集上得到验证"的 3 与数据集
    都能回原文核对。裸数字本身不证明范围——"这并不意味着 12.5% 可推广"里的 12.5 是被
    撤回的对象，"可能在 2 个场景下有效"里的 2 配的是泛词，两句都属该删的防御性写作。
    """
    markers = _qualification_markers(sentence)
    is_qualification = any(markers[group] for group in _QUALIFICATION_GROUPS) or markers["boundary_cue"]
    if not is_qualification:
        return None
    refs = referents(sentence)
    if not refs:
        # 零指称：无论有没有原文都无据可依。
        return "ungrounded"
    # 领域名词与标识符自己就划定范围；纯数字要在边界线索的语境里才算。
    scoped = refs if markers["boundary_cue"] else [token for token in refs if not _is_bare_number(token)]
    if not scoped:
        return "ungrounded"
    if source_norm is None:
        # 没有原始材料就无从核对，保守当作研究边界保留。
        return "unverified"
    if any(_in_source(token, source_norm) for token in scoped):
        return "boundary"
    return "ungrounded"


def qualifications(text: str, source: str | None = None) -> list[dict[str, object]]:
    """逐句给出限定语判定，供闸门、留痕与页面展示。"""
    source_norm = normalize_text(source) if source and source.strip() else None
    out = []
    for index, sentence in enumerate(sentences(text)):
        verdict = classify_qualification(sentence, source_norm)
        if verdict is None:
            continue
        out.append(
            {
                "index": index,
                "verdict": verdict,
                "sentence": sentence,  # 未截断：闸门与评估都按整句取指称
                "text": sentence[:48],  # 截断：只用于留痕与页面展示
                "referents": referents(sentence)[:4],
            }
        )
    return out


def ordering(text: str) -> dict[str, object]:
    """主张与限制的先后：主张句=有指称且无 hedge/降级/自我否定标记的限定外句子。"""
    parts = sentences(text)
    claim_index = -1
    limit_index = -1
    for index, sentence in enumerate(parts):
        markers = _qualification_markers(sentence)
        hedged = any(markers[group] for group in ("hedge", "downgrade", "selfneg"))
        if claim_index < 0 and referents(sentence) and not hedged:
            claim_index = index
        if limit_index < 0 and (markers["downgrade"] or markers["selfneg"] or (hedged and not referents(sentence))):
            limit_index = index
    return {
        "claim_first": claim_index < 0 or limit_index < 0 or claim_index <= limit_index,
        "claim_index": claim_index,
        "limit_index": limit_index,
    }


def confidence(text: str, source: str | None = None) -> float:
    """0–1：无据限定与自我否定越密越低，主张后置额外扣分。"""
    parts = sentences(text)
    if not parts:
        return 1.0
    marks = qualifications(text, source)
    ungrounded = sum(1 for item in marks if item["verdict"] == "ungrounded")
    penalty = ungrounded / len(parts)
    if not ordering(text)["claim_first"]:
        penalty += CLAIM_LAST_PENALTY
    return round(_clamp(1.0 - penalty), 3)


def plainness(text: str, source: str | None = None) -> float:
    """0–1，越高越直白。

    硬命中按句数归一；hedge 只有**超出配额**才扣，且被判为研究边界（含未验证）
    的句子里的 hedge 不计入配额——否则等于要求模型删掉诚实的范围声明。
    """
    parts = findings(text)
    sents = sentences(text)
    total = max(1, len(sents))
    hard = sum(f.count * GROUP_WEIGHTS.get(f.group, 0.0) for f in parts if f.group in GROUP_WEIGHTS)
    jargon = sum(f.count for f in parts if f.group == "jargon") * 0.4
    patterns = sum(f.count for f in parts if f.group == "pattern") * 0.5

    source_norm = normalize_text(source) if source and source.strip() else None
    hedge_total = 0
    for sentence in sents:
        if classify_qualification(sentence, source_norm) in ("boundary", "unverified"):
            continue
        hedge_total += sum(_qualification_markers(sentence)[group] for group in ("hedge",))
    allowance = math.ceil(total * HEDGE_PER_SENTENCE)
    hedge = max(0, hedge_total - allowance) * 0.6
    penalty = (hard + jargon + patterns + hedge) / total
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


def report(text: str, source: str | None = None) -> PlainReport:
    return PlainReport(
        findings=findings(text),
        metrics=metrics(text),
        plainness=plainness(text, source),
        concision=concision(text),
        confidence=confidence(text, source),
        qualifications=qualifications(text, source),
        ordering=ordering(text),
    )


def rewrite_table() -> tuple[tuple[str, str], ...]:
    """mock provider 使用的确定性对照表。"""
    return REWRITES
