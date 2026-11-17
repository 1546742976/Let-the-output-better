"""``references.py`` 的方言分支：判定辅助函数、APA 渲染的多种作者数、解析失败的退回。

``test_step_format.py`` 覆盖了 parse/render 的主干，这里补它没走到的边角：姓名判定的
五类否决、无年份 APA 段、类型推断的书/卷/未知、``_gbt_name``/``_apa_name`` 的单名与中文
直通、``render_apa`` 的 0/1/2/多/超 8 与无年无 doi 分支、``parse_citation`` 过短与 "//" 标题。
断言均为对真实实现的实测值，不臆测。
"""

from __future__ import annotations

from app.optimizer.steps.references import (
    _apa_name,
    _extract_apa_authors,
    _gbt_name,
    _infer_type,
    _looks_like_names,
    is_compliant,
    normalize_reference,
    parse_citation,
    render_apa,
    split_reference_items,
)


class TestNameHeuristics:
    def test_looks_like_names_rejects_five_shapes(self):
        assert _looks_like_names("Smith J, Lee K") is True  # 对照组：正常姓名串
        assert _looks_like_names("2020 年报") is False  # 含年份 → 不是作者段
        assert _looks_like_names("…") is False  # 切不出任何部分
        assert _looks_like_names("张三李四王五赵六钱七孙八周九") is False  # 中文段超长
        assert _looks_like_names("a b c d e f g h") is False  # 拉丁词数越界
        assert _looks_like_names("Chen 2nd") is False  # 词内含非字母

    def test_gbt_and_apa_name_short_circuits(self):
        # 单名与中文名不构成 “姓 + 缩写”，原样返回
        assert _gbt_name("张三") == "张三"
        assert _gbt_name("Smith") == "Smith"
        assert _apa_name("张三") == "张三"
        assert _apa_name("Smith") == "Smith"

    def test_extract_apa_authors_without_year_returns_empty(self):
        assert _extract_apa_authors("Zhang, S. Deep learning title") == ([], "Zhang, S. Deep learning title")


class TestTypeInference:
    def test_book_volume_and_unknown(self):
        assert _infer_type("MIT Press", False) == "book"
        assert _infer_type("some random venue", True) == "journal"  # 有卷号即按期刊
        assert _infer_type("some random venue", False) == "unknown"


class TestSplitItems:
    def test_mixed_separators_split_into_items(self):
        assert split_reference_items("A. 2020.\nB. 2021.；C. 2022.") == ["A. 2020.", "B. 2021.", "C. 2022."]


class TestParseCitationEdgeCases:
    def test_too_short_is_none(self):
        assert parse_citation("ABC 2020") is None

    def test_only_author_and_year_is_none(self):
        # 作者段被消费后无正文可当标题
        assert parse_citation("Smith, J. (2020).") is None

    def test_double_slash_venue_without_marker(self):
        fields = parse_citation("Wu L. A study//Somewhere Journal. 2021.")
        assert fields is not None
        assert fields["title"] == "A study"
        assert fields["venue"] == "Somewhere Journal"
        assert fields["type"] == "journal"


class TestRenderApaAuthorCounts:
    def test_empty_authors(self):
        assert render_apa({"title": "T", "authors": [], "venue": None, "year": 2020}) == ""

    def test_single_author(self):
        assert render_apa({"title": "T", "authors": ["Smith J"], "venue": None, "year": 2020}) == "Smith, J. (2020). T."

    def test_missing_year(self):
        assert render_apa({"title": "T", "authors": ["A B"], "venue": None, "year": None}) == ""

    def test_two_authors_and_doi(self):
        assert render_apa({"title": "T", "authors": ["A B", "C D"], "venue": None, "year": 2020, "doi": "10.1/x"}) == (
            "A, B., & C, D. (2020). T. https://doi.org/10.1/x"
        )

    def test_more_than_eight_authors_gets_ellipsis(self):
        names = [f"{chr(65 + index)} X" for index in range(10)]
        rendered = render_apa({"title": "T", "authors": names, "venue": None, "year": 2020})
        assert rendered.endswith(", … (2020). T.")
        assert "Q, R" not in rendered  # 第 9、10 作者被省略号取代

    def test_is_compliant_requires_year(self):
        assert is_compliant("", "gb-t-7714") is False
        assert is_compliant("no year here", "apa") is False


class TestNormalizeFallback:
    def test_uninferable_type_leaves_text_untouched(self):
        """解析成功但类型推断不出 → GB/T 渲染为空串 → 原文原样返回、不判变更。"""
        text = "Wu L. A study of deep models in nature. Nowhere. 2021."
        assert parse_citation(text)["type"] == "unknown"
        assert normalize_reference(text, "gb-t-7714") == (text, False)
