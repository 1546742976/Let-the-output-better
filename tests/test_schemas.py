from __future__ import annotations

import pytest

from app.schemas import (
    Experiment,
    Paper,
    Reference,
    coerce_number,
    coerce_year,
    required_field_names,
    resolve_schema,
    split_multi,
    split_references,
)


class TestHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("张三、李四", ["张三", "李四"]),
            ("深度学习;图像分类；卷积神经网络", ["深度学习", "图像分类", "卷积神经网络"]),
            ("Wu Shi, Zheng Yi", ["Wu Shi", "Zheng Yi"]),
            ("A and B", ["A", "B"]),
            ([" 甲 ", "甲"], ["甲"]),
            ("", []),
            (None, []),
            (2023, ["2023"]),
        ],
    )
    def test_split_multi(self, value, expected):
        assert split_multi(value) == expected

    def test_split_references_keeps_inner_punctuation(self):
        text = "Wu S. A paper. CVPR 2023.\nLi M. Another paper[J]. TPAMI, 2022."
        assert split_references(text) == ["Wu S. A paper. CVPR 2023.", "Li M. Another paper[J]. TPAMI, 2022."]

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2023", 2023),
            (2023, 2023),
            ("2023年", 2023),
            ("发表于 CVPR 2016: 770-778", 2016),
            ("", None),
            ("无年份", None),
            (True, None),
            (12, None),
        ],
    )
    def test_coerce_year(self, value, expected):
        assert coerce_year(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [("76.5 %", 76.5), ("0.87", 0.87), (12, 12.0), ("N/A", None), (None, None)],
    )
    def test_coerce_number(self, value, expected):
        assert coerce_number(value) == expected


class TestPaper:
    def test_readme_headline_case(self):
        paper = Paper.model_validate(
            {
                "title": "基于深度学习的图像分类方法研究",
                "authors": "张三、李四",
                "year": "2023",
                "keywords": "深度学习;图像分类",
                "reference": "Zhang San, Li Si. Research. CVPR 2023.",
                "note": "如需更多论文信息请随时告诉我",
            }
        )
        assert paper.authors == ["张三", "李四"]
        assert paper.year == 2023
        assert paper.keywords == ["深度学习", "图像分类"]
        assert paper.references == ["Zhang San, Li Si. Research. CVPR 2023."]
        assert not hasattr(paper, "note")

    def test_only_title_is_required(self):
        assert Paper.model_validate({"title": "仅标题"}).year is None
        with pytest.raises(ValueError):
            Paper.model_validate({"authors": ["张三"]})

    def test_reference_trailing_period_is_preserved(self):
        paper = Paper.model_validate({"title": "T", "references": ["A. B. Title. CVPR 2023."]})
        assert paper.references[0].endswith("2023.")

    def test_extra_fields_are_ignored_not_failed(self):
        assert Paper.model_validate({"title": "T", "unexpected": 1}).title == "T"

    def test_title_type_error_is_rejected(self):
        with pytest.raises(ValueError):
            Paper.model_validate({"title": 2023})


class TestReference:
    def test_marker_maps_to_type(self):
        ref = Reference.model_validate({"title": "T", "type": "[J]"})
        assert ref.type == "journal"

    def test_chinese_type_words(self):
        assert Reference.model_validate({"title": "T", "type": "会议论文"}).type == "conference"

    def test_journal_alias_fills_venue(self):
        ref = Reference.model_validate({"title": "T", "journal": "计算机学报", "year": "2023"})
        assert ref.venue == "计算机学报"
        assert ref.year == 2023

    def test_conference_alias_fills_venue(self):
        assert Reference.model_validate({"title": "T", "conference": "NIPS"}).venue == "NIPS"

    def test_parse_text_supports_plain_citation(self):
        fields = Reference.parse_text("He K, Zhang X. Deep residual learning. CVPR 2016: 770-778.")
        assert fields["venue"] == "CVPR"
        assert fields["year"] == 2016

    def test_parse_text_rejects_prose_without_year(self):
        assert Reference.parse_text("抱歉，我没有找到这篇论文。") is None


class TestExperiment:
    def test_percent_value_fills_unit(self):
        exp = Experiment.model_validate(
            {"method": "ResNet-50", "metric": "Top-1", "value": "76.5 %", "baseline": "75.2"}
        )
        assert (exp.value, exp.unit, exp.baseline) == (76.5, "%", 75.2)

    def test_explicit_unit_is_not_overwritten(self):
        exp = Experiment.model_validate({"method": "M", "metric": "ms", "value": "12", "unit": "毫秒"})
        assert (exp.value, exp.unit) == (12.0, "ms")

    def test_required_fields_drive_completeness(self):
        assert set(required_field_names(Experiment)) == {"method", "metric", "value"}


class TestRegistry:
    def test_resolve_schema_by_name_and_class(self):
        assert resolve_schema("paper") is Paper
        assert resolve_schema("EXPERIMENT") is Experiment
        assert resolve_schema(None) is None
        assert resolve_schema(Paper) is Paper

    def test_unknown_schema_name_lists_builtins(self):
        with pytest.raises(KeyError, match="paper, reference, experiment"):
            resolve_schema("nope")

    def test_schema_hint_is_readable_for_prompts(self):
        assert '"title"' in Paper.schema_hint()
