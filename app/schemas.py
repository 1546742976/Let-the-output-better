"""学术场景内置 Schema：`paper` / `reference` / `experiment`。

类型归一化写在 `mode="before"` 校验器里，让“多值转数组、年份转数值、单数键转复数、
剔除多余字段”保持声明式、确定性且可单测；`extra="ignore"` 即“剔除非 Schema 字段”。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_MULTI_SPLIT = re.compile(r"[、;；，,/|]+|\s+and\s+|\s*&\s*")
_YEAR_SEARCH = re.compile(r"(?:1[89]|20)\d{2}")
# 参考文献条目之间可能出现的分隔，注意不能按逗号或句号拆分单条引用。
_REFERENCE_SPLIT = re.compile(r"\n+|;|；|(?=\[\d+\])")
_NUMBER_SEARCH = re.compile(r"-?\d+(?:\.\d+)?")

REF_TYPE_BY_MARKER = {
    "[J]": "journal",
    "[C]": "conference",
    "[M]": "book",
    "[D]": "thesis",
    "[R]": "report",
}
REF_MARKER_BY_TYPE = {value: key for key, value in REF_TYPE_BY_MARKER.items()}


def _clean(part: str) -> str:
    return part.strip().strip("\"'“”《》").strip(" .。;；、,，")


def _clean_reference(part: str) -> str:
    """参考文献条目只去外层噪声：句末点是引用格式的一部分，不能剥掉。"""
    return part.strip().strip("\"'“”《》").strip(" ;；、,，")


def split_multi(value: Any) -> list[str]:
    """把“张三、李四”“深度学习;图像分类”这类字符串拆成去重数组。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = [str(item) for item in value]
    else:
        parts = _MULTI_SPLIT.split(str(value))
    result: list[str] = []
    for part in parts:
        cleaned = _clean(part)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def split_references(value: Any) -> list[str]:
    """拆分多条参考文献，保留条目内部的逗号与句号。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = _REFERENCE_SPLIT.split(str(value))
    result: list[str] = []
    for item in items:
        cleaned = _clean_reference(item)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def coerce_year(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        year = int(value)
    else:
        match = _YEAR_SEARCH.search(str(value))
        if match is None:
            return None
        year = int(match.group())
    return year if 1500 <= year <= 2100 else None


def coerce_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_SEARCH.search(str(value).replace(",", ""))
    return float(match.group()) if match else None


class AcademicModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @classmethod
    def required_fields(cls) -> list[str]:
        return [name for name, field in cls.model_fields.items() if field.is_required()]

    @classmethod
    def optional_fields(cls) -> list[str]:
        return [name for name, field in cls.model_fields.items() if not field.is_required()]

    @classmethod
    def schema_hint(cls) -> str:
        """渲染给修复提示词的字段说明（有意避开 ``BaseModel.schema_json`` 的签名）。"""
        return json.dumps(cls.model_json_schema(), ensure_ascii=False, indent=2)

    @classmethod
    def parse_text(cls, text: str) -> dict[str, Any] | None:
        """非 JSON 输出（如一条纯文本引用）的确定性解析入口，默认不支持。"""
        return None


class Paper(AcademicModel):
    title: str = Field(validation_alias=AliasChoices("title", "paper_title", "name"))
    authors: list[str] = Field(default_factory=list, validation_alias=AliasChoices("authors", "author"))
    year: int | None = Field(
        default=None, validation_alias=AliasChoices("year", "pub_year", "publication_year", "date")
    )
    venue: str | None = Field(
        default=None, validation_alias=AliasChoices("venue", "journal", "conference", "venue_name")
    )
    keywords: list[str] = Field(default_factory=list, validation_alias=AliasChoices("keywords", "keyword"))
    abstract: str | None = Field(default=None, validation_alias=AliasChoices("abstract", "summary"))
    references: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("references", "reference", "bibliography", "citations"),
    )

    @field_validator("authors", "keywords", mode="before")
    @classmethod
    def _split_multi_values(cls, value: Any) -> list[str]:
        return split_multi(value)

    @field_validator("references", mode="before")
    @classmethod
    def _split_reference_entries(cls, value: Any) -> list[str]:
        return split_references(value)

    @field_validator("year", mode="before")
    @classmethod
    def _normalize_year(cls, value: Any) -> int | None:
        return coerce_year(value)


class Reference(AcademicModel):
    title: str = Field(validation_alias=AliasChoices("title", "paper_title"))
    authors: list[str] = Field(default_factory=list, validation_alias=AliasChoices("authors", "author"))
    venue: str | None = Field(
        default=None, validation_alias=AliasChoices("venue", "journal", "book_title", "container")
    )
    year: int | None = Field(default=None, validation_alias=AliasChoices("year", "pub_year", "date"))
    doi: str | None = Field(default=None, validation_alias=AliasChoices("doi", "DOI", "identifier"))
    type: Literal["journal", "conference", "book", "thesis", "report", "unknown"] = "unknown"

    @field_validator("authors", mode="before")
    @classmethod
    def _split_authors(cls, value: Any) -> list[str]:
        return split_multi(value)

    @field_validator("year", mode="before")
    @classmethod
    def _normalize_year(cls, value: Any) -> int | None:
        return coerce_year(value)

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> str:
        if value is None:
            return "unknown"
        text = str(value).strip()
        marker = text[:3].upper()
        if marker in REF_TYPE_BY_MARKER:
            return REF_TYPE_BY_MARKER[marker]
        lowered = text.lower()
        for name in ("journal", "conference", "book", "thesis", "report"):
            if name in lowered:
                return name
        if "会议" in text or "论文集" in text:
            return "conference"
        if "期刊" in text or "杂志" in text:
            return "journal"
        if "学位" in text or "论文" in text:
            return "thesis"
        return "unknown"

    @classmethod
    def parse_text(cls, text: str) -> dict[str, Any] | None:
        from app.optimizer.steps.references import parse_citation

        return parse_citation(text)


class Experiment(AcademicModel):
    method: str = Field(validation_alias=AliasChoices("method", "model", "approach"))
    metric: str = Field(validation_alias=AliasChoices("metric", "measure"))
    value: float = Field(validation_alias=AliasChoices("value", "result", "score"))
    dataset: str | None = Field(default=None, validation_alias=AliasChoices("dataset", "data", "corpus"))
    unit: str | None = Field(default=None, validation_alias=AliasChoices("unit", "units"))
    baseline: float | None = Field(default=None, validation_alias=AliasChoices("baseline", "base"))

    @model_validator(mode="before")
    @classmethod
    def _infer_percent_unit(cls, data: Any) -> Any:
        """“value”: “76.5 %” 这类写法把单位收进 ``unit``，避免数值与单位分离丢失。"""
        if isinstance(data, dict) and not any(key in data for key in ("unit", "units")):
            value = data.get("value")
            if isinstance(value, str) and value.strip().endswith(("%", "％")):
                data = {**data, "unit": "%"}
        return data

    @field_validator("value", "baseline", mode="before")
    @classmethod
    def _normalize_numbers(cls, value: Any) -> float | None:
        return coerce_number(value)

    @field_validator("unit", mode="before")
    @classmethod
    def _normalize_unit(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        return {"百分比": "%", "百分之": "%", "毫秒": "ms", "秒": "s"}.get(text, text)


SCHEMA_REGISTRY: dict[str, type[AcademicModel]] = {
    "paper": Paper,
    "reference": Reference,
    "experiment": Experiment,
}


def resolve_schema(schema: str | type[AcademicModel] | None) -> type[AcademicModel] | None:
    if schema is None:
        return None
    if isinstance(schema, str):
        try:
            return SCHEMA_REGISTRY[schema.lower()]
        except KeyError as exc:
            known = ", ".join(SCHEMA_REGISTRY)
            raise KeyError(f"未知 Schema {schema!r}，内置可选：{known}") from exc
    return schema
