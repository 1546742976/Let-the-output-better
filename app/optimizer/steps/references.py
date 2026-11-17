"""参考文献的确定性解析与风格统一（GB/T 7714 / APA）。

仅在结构可可靠识别时重写；识别失败或重写后仍不合规时原样返回，
宁可不改也不引入新事实（README §10 事实保持）。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[一-鿿]")
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s,;：]+")
_DOI_URL = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s,;：]+)", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:1[89]|20)\d{2}\b")
_MARKER = re.compile(r"\[([JCMDR])\]")
_APA_AUTHOR = re.compile(r"[A-Z][\w'\-]+,\s+(?:[A-Z]\.){1,3}")
_SENTENCE = re.compile(r"(?<=[.。])\s+(?=[A-Z(（\d]|[一-鿿])")
_VOLUME = re.compile(r"\b(\d{1,4})\s*\((\d{1,3})\)\s*[:：]?\s*(\d+\s*[-–—]\s*\d+)?")
_PAGES = re.compile(r"[:：]\s*(\d+\s*[-–—]\s*\d+)\s*\.?$")
_LEADING_INDEX = re.compile(r"^\s*(?:\[?\d{1,3}\]?[.、)])\s*")

REF_TYPE_BY_MARKER = {"[J]": "journal", "[C]": "conference", "[M]": "book", "[D]": "thesis", "[R]": "report"}
REF_MARKER_BY_TYPE = {"journal": "[J]", "conference": "[C]", "book": "[M]", "thesis": "[D]", "report": "[R]"}

CONFERENCE_HINTS = {
    "cvpr",
    "iccv",
    "eccv",
    "wacv",
    "bmvc",
    "neurips",
    "nips",
    "icml",
    "iclr",
    "acl",
    "emnlp",
    "naacl",
    "coling",
    "aaai",
    "ijcai",
    "kdd",
    "sigir",
    "www",
    "sigcomm",
    "nsdi",
    "osdi",
    "sosp",
    "vldb",
    "icde",
    "sigmod",
    "icra",
    "iros",
    "aamas",
    "interspeech",
    "icassp",
    "miccai",
    "chi",
    "icse",
    "fse",
    "pldi",
    "popl",
    "oopsla",
    "asplos",
    "micro",
    "hpca",
    "sc",
    "ccs",
    "usenix",
    " proceedings",
    "symposium",
    "conference",
    "workshop",
    "colt",
    "uai",
    "aistats",
}
JOURNAL_HINTS = {
    "nature",
    "science",
    "journal",
    "transactions",
    "letters",
    "survey",
    "review",
    "学报",
    "期刊",
    "通报",
    "tpami",
    "jmlr",
    "pnas",
    "ai advances",
}
BOOK_HINTS = {"monograph", "book", "press", "出版社", "书"}


def _normalize(text: str) -> str:
    body = _LEADING_INDEX.sub("", text.strip())
    body = body.replace("　", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", body).strip()


def split_reference_items(value: str) -> list[str]:
    """把一段可能含多条引用的文本切成条目。"""
    from app.schemas import split_references

    return split_references(value)


def _looks_like_names(chunk: str) -> bool:
    from app.schemas import split_multi

    text = chunk.strip(" .;；,，")
    if not text or len(text) > 120 or "[" in text or _YEAR.search(text):
        return False
    parts = split_multi(text)
    if not parts:
        return False
    for part in parts:
        if _CJK.search(part):
            if len(part) > 8:
                return False
            continue
        words = part.split()
        if not 1 <= len(words) <= 6:
            return False
        if any(not word.strip(".'-").isalpha() and not _CJK.search(word) for word in words):
            return False
    return True


def _is_year_only(chunk: str) -> bool:
    return _YEAR.fullmatch(chunk.strip(" .()（）")) is not None


def _apa_author_to_name(match: str) -> str:
    """“Zhang, S.” → “Zhang S”，统一成姓氏在前且无逗号的形式。"""
    parts = [part.strip().strip(".") for part in match.split(",") if part.strip().strip(".")]
    return " ".join(parts)


def _extract_apa_authors(body: str) -> tuple[list[str], str]:
    """识别 APA 式 “Zhang, S., & Li, S. (2023)” 作者段，避免逗号把它切碎。"""
    first = _APA_AUTHOR.match(body)
    if first is None:
        return [], body
    authors = [_apa_author_to_name(first.group())]
    end = first.end()
    following = re.compile(r"\s*(?:[,&]|和|\band\b|\s)*([A-Z][\w'\-]+,\s+(?:[A-Z]\.){1,3})")
    while True:
        match = following.match(body[end:])
        if match is None:
            break
        authors.append(_apa_author_to_name(match.group(1)))
        end += match.end()
    if re.match(r"\s*\((?:1[89]|20)\d{2}\)", body[end:]) is None:
        return [], body
    return authors, body[end:]


def _clean_venue(text: str) -> str:
    venue = text.replace("//", " ").strip()
    venue = _DOI.sub(" ", venue)
    venue = _VOLUME.sub(" ", venue)
    venue = _PAGES.sub(" ", venue)
    venue = _YEAR.sub(" ", venue)
    venue = re.sub(r"\[[JCMDR]\]", " ", venue)
    venue = re.sub(r"\s+", " ", venue)
    # 移除年份/卷期后被孤立的标点，避免 venue 残留 “ACM Surveys, , .”
    for _ in range(3):
        venue = re.sub(r"(?:^|(?<=\s))[,，.。;；:：]\s*", "", venue)
        venue = re.sub(r"\s*[,，]\s*(?=[,，.。;；]|$)", " ", venue)
    return re.sub(r"\s+", " ", venue).strip(" .,;:、-")


def _infer_type(venue: str, has_volume: bool) -> str:
    lowered = venue.lower()
    if any(hint in lowered for hint in CONFERENCE_HINTS):
        return "conference"
    if any(hint in lowered for hint in JOURNAL_HINTS):
        return "journal"
    if any(hint in lowered for hint in BOOK_HINTS):
        return "book"
    if has_volume:
        return "journal"
    return "unknown"


def parse_citation(text: str) -> dict | None:
    """把一条参考文献解析成字段，置信度不足时返回 ``None``。"""
    from app.schemas import coerce_year, split_multi

    body = _normalize(text)
    if len(body) < 12:
        return None

    doi_match = _DOI_URL.search(body)
    if doi_match:
        doi = doi_match.group(1)
    else:
        doi_match = _DOI.search(body)
        doi = doi_match.group() if doi_match else None
    if doi_match:
        body = re.sub(r"\s+", " ", body[: doi_match.start()] + " " + body[doi_match.end() :]).strip(" ,.")

    years = _YEAR.findall(body)
    year = int(years[-1]) if years else coerce_year(body)

    marker = _MARKER.search(body)
    ref_type = REF_TYPE_BY_MARKER.get(f"[{marker.group(1)}]") if marker else None

    volume_match = _VOLUME.search(body)
    volume = issue = pages = None
    if volume_match:
        volume, issue, pages = (
            volume_match.group(1),
            volume_match.group(2),
            volume_match.group(3),
        )
    else:
        pages_match = _PAGES.search(body)
        pages = pages_match.group(1) if pages_match else None

    apa_authors, apa_rest = _extract_apa_authors(body)
    if apa_authors:
        authors = apa_authors
        chunks = [c.strip(" .") for c in _SENTENCE.split(apa_rest) if c.strip(" .")]
        chunks = [c for c in chunks if not _is_year_only(c)]
    else:
        chunks = [c.strip(" .") for c in _SENTENCE.split(body) if c.strip(" .")]
        authors = []
        if len(chunks) >= 2 and _looks_like_names(chunks[0]):
            authors = split_multi(chunks[0])
            chunks = chunks[1:]

    if not chunks:
        return None

    title = chunks[0]
    venue_text = " ".join(chunks[1:])
    if marker is not None and marker.group(0) in title:
        head, _, tail = title.partition(marker.group(0))
        title = head
        venue_text = f"{tail.strip('/ ')} {venue_text}".strip()
    elif "//" in title:
        title, _, tail = title.partition("//")
        venue_text = f"{tail} {venue_text}".strip()
    title = _MARKER.sub("", title)
    title = re.sub(r"\s+", " ", title).strip(" .,;:、")
    venue = _clean_venue(venue_text)

    # 无出版年的文本不足以确认为参考文献，宁可判不合规也不猜测类型。
    if not title or _is_year_only(title) or year is None:
        return None
    if ref_type is None:
        ref_type = _infer_type(venue, volume_match is not None)

    return {
        "title": title,
        "authors": authors,
        "venue": venue or None,
        "year": year,
        "doi": doi,
        "type": ref_type,
        "volume": volume,
        "issue": issue,
        "pages": pages,
    }


def _gbt_name(name: str) -> str:
    if _CJK.search(name):
        return name
    words = [word.strip(".,'’-") for word in name.split() if word.strip(".,'’-")]
    if len(words) < 2:
        return name
    initials = "".join(word[0] for word in words[1:]).upper()
    return f"{words[0]} {initials}"


def _apa_name(name: str) -> str:
    if _CJK.search(name):
        return name
    words = [word.strip(".") for word in name.split() if word.strip(".")]
    if len(words) < 2:
        return name
    initials = " ".join(f"{word[0]}." for word in words[1:])
    return f"{words[0]}, {initials}"


def render_gbt7714(fields: dict) -> str:
    marker = REF_MARKER_BY_TYPE.get(str(fields.get("type") or ""), "")
    year = fields.get("year")
    if not marker or not year:
        return ""
    names = fields.get("authors") or []
    authors = ", ".join(_gbt_name(name) for name in names[:3])
    if len(names) > 3:
        authors += ", 等"
    title = fields["title"].rstrip(".")
    venue = fields.get("venue")
    pages = re.sub(r"\s+", "", fields["pages"]) if fields.get("pages") else None
    if fields["type"] == "conference":
        body = f"{title}{marker}//{venue}. {year}" if venue else f"{title}{marker}. {year}"
        if pages:
            body += f": {pages}"
    else:
        detail = f"{venue}, {year}" if venue else str(year)
        if fields.get("volume"):
            detail += f", {fields['volume']}({fields.get('issue') or 1})"
        if pages:
            detail += f": {pages}"
        body = f"{title}{marker}. {detail}"
    return f"{authors}. {body}." if authors else f"{body}."


def render_apa(fields: dict) -> str:
    names = [_apa_name(name) for name in fields["authors"][:8]]
    if not names:
        return ""
    if len(names) == 1:
        authors = names[0]
    elif len(names) == 2:
        authors = f"{names[0]}, & {names[1]}"
    else:
        authors = ", ".join(names[:-1]) + f", & {names[-1]}"
    if len(fields["authors"]) > 8:
        authors += ", …"
    year = fields.get("year")
    venue = fields.get("venue")
    if not year:
        return ""
    tail = f"{authors} ({year}). {fields['title'].rstrip('.')}"
    tail += f". {venue}." if venue else "."
    if fields.get("doi"):
        tail += f" https://doi.org/{fields['doi']}"
    return tail


def is_compliant(text: str, style: str) -> bool:
    """引用是否已符合目标风格：GB/T 7714 看文献类型标识，APA 看括注年份。"""
    body = text.strip()
    if not body or not _YEAR.search(body):
        return False
    if style == "apa":
        return re.search(r"\((?:1[89]|20)\d{2}\)", body) is not None
    return _MARKER.search(body) is not None and body.endswith(".")


def normalize_reference(text: str, style: str) -> tuple[str, bool]:
    """返回 ``(文本, 是否变更)``；不可靠识别时保持原文。"""
    original = text.strip()
    if is_compliant(original, style):
        return original, False
    fields = parse_citation(original)
    if fields is None:
        return original, False
    rendered = render_apa(fields) if style == "apa" else render_gbt7714(fields)
    if not rendered or not is_compliant(rendered, style):
        return original, False
    return rendered, True
