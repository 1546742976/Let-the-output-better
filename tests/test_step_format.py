from __future__ import annotations

from app.optimizer.steps.format import FormatStep, normalize_dates
from app.optimizer.steps.references import (
    is_compliant,
    normalize_reference,
    parse_citation,
    render_apa,
    render_gbt7714,
)

PLAIN_CONF = "Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023."
PLAIN_JOURNAL = "Smith J. A survey of transformers. ACM Computing Surveys, 2022, 54(3): 1-38."
GBT_JOURNAL = "张三, 李四. 基于深度学习的图像分类方法研究[J]. 计算机学报, 2023, 45(1): 100-112."
APA_STYLE = "Zhang, S., & Li, S. (2023). Research on image classification. CVPR."
WITH_DOI_URL = (
    "Smith J. A survey of transformers. ACM Computing Surveys, 2022, 54(3): 1-38. https://doi.org/10.1145/3529972"
)


class TestParseCitation:
    def test_conference_without_marker(self):
        fields = parse_citation(PLAIN_CONF)
        assert fields["type"] == "conference"
        assert fields["venue"] == "CVPR"
        assert fields["authors"] == ["Zhang San", "Li Si"]

    def test_journal_marker_volume_and_pages(self):
        fields = parse_citation(GBT_JOURNAL)
        assert (fields["type"], fields["volume"], fields["issue"], fields["pages"]) == (
            "journal",
            "45",
            "1",
            "100-112",
        )

    def test_leading_index_and_doi_are_stripped_from_venue(self):
        fields = parse_citation(WITH_DOI_URL)
        assert fields["venue"] == "ACM Computing Surveys"
        assert fields["doi"] == "10.1145/3529972"

    def test_apa_input_is_recognised(self):
        fields = parse_citation(APA_STYLE)
        assert fields["authors"] == ["Zhang S", "Li S"]
        assert fields["year"] == 2023

    def test_prose_without_year_is_not_a_citation(self):
        assert parse_citation("抱歉，我没有找到这篇论文。") is None


class TestRendering:
    def test_gbt7714_matches_readme_target(self):
        assert (
            render_gbt7714(parse_citation(PLAIN_CONF))
            == "Zhang S, Li S. Research on image classification based on deep learning[C]//CVPR. 2023."
        )

    def test_gbt7714_keeps_pages_for_conference(self):
        fields = parse_citation("He K, Zhang X, Ren S. Deep residual learning. CVPR 2016: 770-778.")
        assert render_gbt7714(fields).endswith("CVPR. 2016: 770-778.")

    def test_more_than_three_authors_uses_et_al_marker(self):
        fields = parse_citation("A B, C D, E F, G H. Long author list. CVPR 2021.")
        assert render_gbt7714(fields).startswith("A B, C D, E F, 等. Long author list[C]//CVPR. 2021.")

    def test_apa_rendering_places_year_in_parentheses(self):
        assert render_apa(parse_citation(PLAIN_CONF)) == (
            "Zhang, S., & Li, S. (2023). Research on image classification based on deep learning. CVPR."
        )

    def test_unknown_type_cannot_be_rendered(self):
        assert (
            render_gbt7714({"title": "T", "authors": [], "venue": "Somewhere", "year": 2020, "type": "unknown"}) == ""
        )


class TestNormalizeReference:
    def test_already_compliant_is_left_untouched(self):
        text, changed = normalize_reference(GBT_JOURNAL, "gb-t-7714")
        assert (text, changed) == (GBT_JOURNAL, False)

    def test_restyle_is_idempotent(self):
        once, changed = normalize_reference(PLAIN_CONF, "gb-t-7714")
        twice, changed_again = normalize_reference(once, "gb-t-7714")
        assert changed is True and changed_again is False and once == twice

    def test_cross_style_conversion(self):
        gbt, _ = normalize_reference(PLAIN_CONF, "gb-t-7714")
        apa, _ = normalize_reference(gbt, "apa")
        assert is_compliant(apa, "apa")
        assert "(2023)" in apa

    def test_unrecognisable_text_is_returned_unchanged(self):
        text = "随便一句话，没有年份也没有结构"
        assert normalize_reference(text, "gb-t-7714") == (text, False)

    def test_plain_journal_to_gbt(self):
        rendered, changed = normalize_reference(PLAIN_JOURNAL, "gb-t-7714")
        assert changed and rendered == "Smith J. A survey of transformers[J]. ACM Computing Surveys, 2022, 54(3): 1-38."


class TestFormatStep:
    async def test_reference_is_restyled_in_parsed_payload(self, make_ctx, settings):
        ctx = make_ctx(raw_output="", schema=None, parsed={"references": [PLAIN_CONF]}, valid=True)
        result = await FormatStep(settings=settings).run(ctx)
        assert ctx.parsed["references"][0].endswith("[C]//CVPR. 2023.")
        assert any("引用格式" in change for change in ctx.changes)
        assert result.passed

    async def test_empty_fields_are_dropped(self, make_ctx, settings):
        ctx = make_ctx(raw_output="", schema=None, parsed={"title": "T", "venue": None, "keywords": []}, valid=True)
        await FormatStep(settings=settings).run(ctx)
        assert ctx.parsed == {"title": "T"}

    async def test_text_mode_strips_fences_and_prose(self, make_ctx, settings):
        ctx = make_ctx(raw_output="x", text="```json\n结论\n```", parsed=None)
        await FormatStep(settings=settings).run(ctx)
        assert ctx.text == "结论"

    async def test_dates_are_normalised(self):
        assert normalize_dates("发布于 2023年5月1日") == "发布于 2023-05-01"
        assert normalize_dates("计算机学报, 2023, 45(1): 100-112.") == "计算机学报, 2023, 45(1): 100-112."

    async def test_broken_formatting_reverts_to_previous_result(self, make_ctx, settings):
        from app.schemas import Paper

        ctx = make_ctx(raw_output="", schema=Paper, parsed={"title": None, "authors": ["张三"]}, valid=True)
        result = await FormatStep(settings=settings).run(ctx)
        assert result.passed is False and result.score == 0.5
        assert ctx.parsed["authors"] == ["张三"]
