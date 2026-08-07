"""LLM 归因模块单测：RAG 检索、规则引擎降级、Token 统计。

不依赖真实 LLM API，通过 mock 验证：
- TfidfIndex 检索准确性
- 规则引擎降级逻辑（无 API key）
- LLM 调用成功时的 token 采集
- AnalysisResult 字段完整性
"""
import pytest

from llm_analyzer import (
    TfidfIndex,
    LLMAnalyzer,
    AnalysisResult,
    _rule_based_analysis,
    _build_prompt,
    _sanitize_llm_result,
)
from protocol import Frame


def make_frame(seq=1, sensor_type=1, values=None):
    """快速构建测试用 Frame。"""
    samples = [{"channel": i, "value": v} for i, v in enumerate(values or [50.0])]
    return Frame(seq=seq, timestamp=1000, sensor_type=sensor_type,
                 samples=samples, crc_ok=True)


# ── TfidfIndex 检索 ────────────────────────────────────

class TestTfidfIndex:
    def test_empty_index_returns_empty(self):
        idx = TfidfIndex()
        assert idx.search("anything") == []

    def test_add_and_search_returns_result(self):
        idx = TfidfIndex()
        idx.add({"seq": 1, "text": "CRC_FAIL frame corruption", "root_cause": "CRC error"})
        results = idx.search("CRC_FAIL")
        assert len(results) == 1
        assert results[0]["seq"] == 1

    def test_relevance_ranking(self):
        """更相似的文档应排在前面。"""
        idx = TfidfIndex()
        idx.add({"seq": 1, "text": "CRC error frame data"})
        idx.add({"seq": 2, "text": "CRC FAIL checksum error"})
        results = idx.search("CRC FAIL checksum")
        assert len(results) == 2
        assert results[0]["seq"] == 2  # 更相似的排第一

    def test_top_k_limit(self):
        idx = TfidfIndex()
        for i in range(5):
            idx.add({"seq": i, "text": f"frame {i} anomaly type {i}"})
        results = idx.search("anomaly", top_k=2)
        assert len(results) == 2

    def test_zero_similarity_excluded(self):
        """完全不匹配的文档不应返回。"""
        idx = TfidfIndex()
        idx.add({"seq": 1, "text": "CRC checksum error"})
        results = idx.search("完全不同的中文查询")
        # 中英文无交集，similarity 应为 0，不返回
        assert len(results) == 0


# ── 规则引擎降级 ────────────────────────────────────────

class TestRuleBasedAnalysis:
    def test_crc_fail_rule(self):
        frame = make_frame()
        result = _rule_based_analysis(frame, ["CRC_FAIL"])
        assert result.source == "rule"
        assert result.category == "CRC_FAIL"
        assert result.confidence == pytest.approx(0.95)
        assert "CRC" in result.root_cause

    def test_seq_gap_rule(self):
        frame = make_frame()
        result = _rule_based_analysis(frame, ["SEQ_GAP:3"])
        assert result.category == "SEQ_GAP"
        assert result.confidence == pytest.approx(0.90)

    def test_out_of_range_rule(self):
        frame = make_frame()
        result = _rule_based_analysis(frame, ["OUT_OF_RANGE:speed=500.00"])
        assert result.category == "OUT_OF_RANGE"

    def test_nan_value_rule(self):
        frame = make_frame()
        result = _rule_based_analysis(frame, ["NAN_VALUE:ch0"])
        assert result.category == "NAN_VALUE"

    def test_unknown_reason_fallback(self):
        frame = make_frame()
        result = _rule_based_analysis(frame, ["UNKNOWN_REASON"])
        assert result.category == "UNKNOWN_REASON"
        assert result.confidence == pytest.approx(0.5)


# ── LLMAnalyzer 降级模式（无 API key）─────────────────

class TestLLMAnalyzerFallback:
    def test_no_api_key_uses_rule_engine(self, monkeypatch):
        # 先屏蔽 .env 文件加载，再清掉环境变量
        monkeypatch.setattr("llm_analyzer._load_env_file", lambda: None)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        analyzer = LLMAnalyzer()
        assert not analyzer.is_enabled

        frame = make_frame()
        result = analyzer.analyze(frame, ["CRC_FAIL"])
        assert result.source == "rule"
        assert result.category == "CRC_FAIL"

    def test_rag_count_tracked_in_fallback(self, monkeypatch):
        monkeypatch.setattr("llm_analyzer._load_env_file", lambda: None)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        analyzer = LLMAnalyzer()
        analyzer._rag_index.add({"seq": 99, "text": "CRC_FAIL error", "root_cause": "crc"})

        frame = make_frame()
        result = analyzer.analyze(frame, ["CRC_FAIL"])
        assert result.rag_context_count > 0


# ── LLMAnalyzer LLM 模式（mock）───────────────────────

class TestLLMAnalyzerWithMock:
    def test_llm_success_returns_token(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "fake-key")
        analyzer = LLMAnalyzer()

        # mock _call_llm 返回带 token 的结果
        def mock_call(prompt):
            return (
                {"root_cause": "测试根因", "confidence": 0.9, "category": "CRC_FAIL",
                 "recommendation": "测试建议"},
                {"prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180},
            )
        monkeypatch.setattr(analyzer, "_call_llm", mock_call)

        frame = make_frame()
        result = analyzer.analyze(frame, ["CRC_FAIL"])
        assert result.source == "llm"
        assert result.root_cause == "测试根因"
        assert result.prompt_tokens == 150
        assert result.completion_tokens == 30
        assert result.total_tokens == 180

    def test_llm_failure_falls_back_to_rule(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "fake-key")
        analyzer = LLMAnalyzer()

        # mock _call_llm 返回 None（调用失败）
        def mock_call(prompt):
            return None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        monkeypatch.setattr(analyzer, "_call_llm", mock_call)

        frame = make_frame()
        result = analyzer.analyze(frame, ["CRC_FAIL"])
        assert result.source == "rule"  # 降级到规则引擎


# ── AnalysisResult 数据结构 ────────────────────────────

class TestAnalysisResult:
    def test_to_dict_includes_token_fields(self):
        r = AnalysisResult(
            seq=1, root_cause="test", confidence=0.9, category="CRC_FAIL",
            recommendation="fix", source="llm", rag_context_count=2,
            latency_ms=500, prompt_tokens=100, completion_tokens=20, total_tokens=120,
        )
        d = r.to_dict()
        assert d["prompt_tokens"] == 100
        assert d["completion_tokens"] == 20
        assert d["total_tokens"] == 120
        assert d["rag_context_count"] == 2

    def test_default_values(self):
        r = AnalysisResult(seq=1, root_cause="x", confidence=0.5,
                           category="X", recommendation="y")
        assert r.source == "rule"
        assert r.prompt_tokens == 0
        assert r.completion_tokens == 0
        assert r.total_tokens == 0


# ── LLM 返回结果防御性解析 ─────────────────────────────

class TestSanitizeLlmResult:
    def test_normal_result_passthrough(self):
        safe = _sanitize_llm_result({
            "root_cause": "传输损坏", "confidence": 0.9,
            "category": "CRC_FAIL", "recommendation": "重传",
        })
        assert safe["confidence"] == 0.9
        assert safe["category"] == "CRC_FAIL"

    def test_bad_confidence_defaults_to_05(self):
        """LLM 返回 confidence='high' 这类非数值，不应让 float() 崩溃。"""
        safe = _sanitize_llm_result({"confidence": "high", "category": "CRC_FAIL"})
        assert safe["confidence"] == 0.5

    def test_confidence_clamped_to_01(self):
        safe = _sanitize_llm_result({"confidence": 3.5, "category": "CRC_FAIL"})
        assert safe["confidence"] == 1.0
        safe = _sanitize_llm_result({"confidence": -1, "category": "CRC_FAIL"})
        assert safe["confidence"] == 0.0

    def test_invalid_category_becomes_unknown(self):
        safe = _sanitize_llm_result({"category": "sensor exploded!", "confidence": 0.9})
        assert safe["category"] == "UNKNOWN"

    def test_non_dict_result_defaults(self):
        safe = _sanitize_llm_result(None)
        assert safe["category"] == "UNKNOWN"
        assert safe["confidence"] == 0.5

    def test_missing_fields_get_defaults(self):
        safe = _sanitize_llm_result({})
        assert safe["root_cause"] == "LLM 未返回根因"
        assert safe["recommendation"] == "建议人工排查"


# ── Prompt 构建 ────────────────────────────────────────

class TestBuildPrompt:
    def test_prompt_contains_frame_info(self):
        frame = make_frame(seq=42, sensor_type=1, values=[50.0])
        prompt = _build_prompt(frame, ["CRC_FAIL"], [])
        assert "42" in prompt
        assert "CRC_FAIL" in prompt

    def test_prompt_contains_rag_cases(self):
        frame = make_frame()
        rag_cases = [{"seq": 1, "similarity": 0.8, "reasons": "CRC_FAIL",
                       "root_cause": "传输损坏"}]
        prompt = _build_prompt(frame, ["CRC_FAIL"], rag_cases)
        assert "案例1" in prompt
        assert "0.8" in prompt
