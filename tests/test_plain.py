from __future__ import annotations

import pytest

from app.plain import (
    AVG_LEN_TARGET,
    DISCLAIMER,
    GROUP_WEIGHTS,
    HEDGE,
    LONG_SENTENCE_CHARS,
    PATTERNS,
    PRAISE,
    REWRITES,
    concision,
    findings,
    metrics,
    plainness,
    report,
    rewrite_table,
    sentences,
)

DEFENSIVE = (
    "值得注意的是，本研究在某种程度上可能揭示了一种新的可能性。需要指出的是，该方法不仅具有较高的准确性，"
    "而且通过超参数搜索的方式来实现对训练过程的优化。实验结果充分表明，本方法具有显著的性能优势和广阔的应用前景。"
    "如需更多论文信息请随时告诉我，以上内容仅供参考。"
)
PLAIN = "我们调好了超参数。测试集准确率从 88.5% 提到 91.2%，比基线高 2.7 个百分点。"


class TestVocabulary:
    def test_groups_are_non_empty_tuples_of_literals(self):
        for table in (DISCLAIMER, PRAISE, HEDGE):
            assert table and all(isinstance(item, str) and item for item in table)

    def test_no_entry_contains_the_placeholder_ellipsis(self):
        """形如"在……的基础上"的写法只能是句式；当字面量用永远不会命中真实文本。"""
        from app.plain import BOILERPLATE, JARGON

        offenders = [item for item in (*BOILERPLATE, *PRAISE, *DISCLAIMER, *HEDGE, *JARGON) if "…" in item]
        assert offenders == []

    def test_hard_hit_groups_are_the_documented_three(self):
        assert set(GROUP_WEIGHTS) == {"boilerplate", "praise", "disclaimer"}

    def test_pattern_list_compiles(self):
        assert all(hasattr(pattern, "findall") for pattern in PATTERNS)
        assert len(PATTERNS) >= 5

    def test_rewrite_table_is_the_mock_source(self):
        assert rewrite_table() is REWRITES
        assert all(len(pair) == 2 for pair in REWRITES)


class TestFindings:
    def test_defensive_text_hits_every_hard_group(self):
        groups = {f.group for f in findings(DEFENSIVE)}
        assert {"boilerplate", "praise", "disclaimer", "pattern"} <= groups

    def test_plain_text_has_no_hard_hits(self):
        assert [f for f in findings(PLAIN) if f.group in GROUP_WEIGHTS] == []

    def test_overlapping_phrases_are_counted_once(self):
        """长短语命中后必须挖掉，否则同一段冗余要罚两遍。"""
        hits = [f.label for f in findings("本文方法具有显著的性能优势和广阔的应用前景。")]
        assert "性能优势和广阔的应用前景" in hits
        assert "广阔的应用前景" not in hits

    def test_cross_group_overlap_is_counted_once(self):
        labels = [f.label for f in findings("在某种程度上，这是可能的。")]
        assert sum(1 for label in labels if label in ("在某种程度上", "某种程度")) == 1

    def test_english_defensive_phrases_are_detected(self):
        text = "It is worth noting that the model may help. It plays a crucial role. This is for informational purposes only."
        found = findings(text)
        assert {f.group for f in found} >= {"boilerplate", "praise", "disclaimer"}
        assert all(not f.label.startswith("\\b") for f in found), "对外标签要可读，不能暴露正则"

    def test_empty_text_has_no_findings(self):
        assert findings("   ") == []


class TestSentenceMetrics:
    def test_cjk_and_latin_delimiters_split(self):
        assert len(sentences("第一句。第二句！第三句？")) == 3
        assert len(sentences("One sentence. Another one. Third.")) == 3

    def test_decimal_numbers_do_not_split_sentences(self):
        """88.5% 中间那个点不是句末，否则句均长度会被假切分污染。"""
        assert len(sentences("准确率从 88.5% 提到 91.2%。")) == 1

    def test_metrics_shape(self):
        values = metrics(DEFENSIVE)
        assert set(values) == {"chars", "sentences", "avg_len", "long_ratio", "hard_hits", "hard_density"}
        assert values["hard_hits"] > 0

    def test_plain_text_metrics(self):
        values = metrics(PLAIN)
        assert values["hard_hits"] == 0
        assert values["avg_len"] <= 40


class TestScoring:
    def test_plainness_is_monotonic_in_hits(self):
        ladder = [
            "结果很好。",
            "值得注意的是，结果很好。",
            "值得注意的是，实验结果充分表明，该方法具有广阔的应用前景。",
            DEFENSIVE,
        ]
        scores = [plainness(text) for text in ladder]
        assert scores == sorted(scores, reverse=True), scores

    def test_single_legitimate_hedge_is_not_penalized(self):
        """诚实表达不确定性不该被罚，只有堆叠才算冗余。"""
        assert plainness("该方法可能会降低误差。") == 1.0
        stacked = plainness("该方法可能会降低误差。它或许会提速。它似乎更稳。可能会更省内存。可能会更准。")
        assert stacked < 1.0

    def test_plain_text_is_concise(self):
        assert plainness(PLAIN) == 1.0
        assert concision(PLAIN) > 0.7

    def test_run_on_sentence_is_penalized(self):
        long_one = "我们 " + "、".join(["优化了检索"] * 40) + "。"
        assert len(sentences(long_one)[0]) > LONG_SENTENCE_CHARS
        assert concision(long_one) < 0.6

    def test_empty_text_scores_well_not_punished(self):
        assert plainness("") == 1.0
        assert concision("") == 1.0

    def test_documented_targets(self):
        assert AVG_LEN_TARGET == 24.0
        assert LONG_SENTENCE_CHARS == 40

    def test_report_summary_is_human_readable(self):
        summary = report(DEFENSIVE).summary()
        assert summary.startswith("防御措辞 ") and "句均" in summary and "字" in summary

    def test_report_is_deterministic(self):
        first = report(DEFENSIVE)
        second = report(DEFENSIVE)
        assert (first.plainness, first.concision, first.metrics) == (second.plainness, second.concision, second.metrics)


@pytest.mark.parametrize("group", sorted(GROUP_WEIGHTS))
def test_each_hard_group_can_be_hit(group: str):
    sample = {
        "boilerplate": "值得注意的是，结果如下。",
        "praise": "该方法具有广阔的应用前景。",
        "disclaimer": "以上内容仅供参考。",
    }[group]
    assert any(f.group == group for f in findings(sample))
