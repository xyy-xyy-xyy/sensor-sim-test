"""数据库模块单测：入库、查询、token 统计、重置。

使用临时数据库文件，测试完自动清理。
"""
import json
import os
import tempfile

import pytest

from database import Database
from protocol import Frame
from quality_gate import QualityResult
from llm_analyzer import AnalysisResult


@pytest.fixture
def db():
    """每个测试用独立的临时数据库。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    yield db
    db.close()
    if os.path.exists(path):
        os.remove(path)


def make_frame(seq=1, sensor_type=1):
    return Frame(seq=seq, timestamp=1000 + seq, sensor_type=sensor_type,
                 samples=[{"channel": 0, "value": 50.0}], crc_ok=True)


def make_result(passed=True, reasons=None):
    return QualityResult(passed=passed, reasons=reasons or [])


def make_analysis(seq=1, source="llm", tokens=True):
    return AnalysisResult(
        seq=seq, root_cause="测试根因", confidence=0.9, category="CRC_FAIL",
        recommendation="测试建议", source=source, rag_context_count=1,
        latency_ms=500,
        prompt_tokens=100 if tokens else 0,
        completion_tokens=20 if tokens else 0,
        total_tokens=120 if tokens else 0,
    )


# ── 帧入库与查询 ────────────────────────────────────────

class TestFrameOperations:
    def test_insert_and_count(self, db):
        db.insert(make_frame(1), make_result())
        db.insert(make_frame(2), make_result(passed=False, reasons=["CRC_FAIL"]))
        stats = db.stats()
        assert stats["total"] == 2
        assert stats["passed"] == 1

    def test_recent_returns_latest(self, db):
        for i in range(5):
            db.insert(make_frame(i), make_result())
        rows = db.recent(limit=3)
        assert len(rows) == 3
        assert rows[-1]["seq"] == 4  # 最后插入的

    def test_pass_rate(self, db):
        db.insert(make_frame(1), make_result())
        db.insert(make_frame(2), make_result(passed=False, reasons=["CRC_FAIL"]))
        stats = db.stats()
        assert stats["pass_rate"] == pytest.approx(0.5)
        assert stats["reject_rate"] == pytest.approx(0.5)

    def test_reject_by_reason(self, db):
        db.insert(make_frame(1), make_result(passed=False, reasons=["CRC_FAIL"]))
        db.insert(make_frame(2), make_result(passed=False, reasons=["SEQ_GAP:1"]))
        stats = db.stats()
        assert stats["reject_by_reason"]["CRC_FAIL"] == 1
        assert stats["reject_by_reason"]["SEQ_GAP"] == 1


# ── 归因入库与查询 ──────────────────────────────────────

class TestAnalysisOperations:
    def test_insert_and_query_analysis(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], make_analysis(1))
        rows = db.recent_analysis()
        assert len(rows) == 1
        assert rows[0]["seq"] == 1
        assert rows[0]["root_cause"] == "测试根因"
        assert rows[0]["source"] == "llm"

    def test_analysis_token_fields(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], make_analysis(1, tokens=True))
        rows = db.recent_analysis()
        assert rows[0]["prompt_tokens"] == 100
        assert rows[0]["completion_tokens"] == 20
        assert rows[0]["total_tokens"] == 120

    def test_recent_analysis_order(self, db):
        for i in range(5):
            db.insert_analysis(i, ["CRC_FAIL"], make_analysis(i))
        rows = db.recent_analysis(limit=3)
        assert len(rows) == 3
        assert rows[-1]["seq"] == 4  # 最后插入的

    def test_get_analysis_history(self, db):
        for i in range(3):
            db.insert_analysis(i, ["CRC_FAIL"], make_analysis(i))
        history = db.get_analysis_history(limit=10)
        assert len(history) == 3


# ── Token 统计 ─────────────────────────────────────────

class TestTokenStats:
    def test_token_sum_in_stats(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], make_analysis(1, source="llm"))
        db.insert_analysis(2, ["CRC_FAIL"], make_analysis(2, source="llm"))
        stats = db.stats()
        llm = stats["llm_analysis"]
        assert llm["total_prompt_tokens"] == 200
        assert llm["total_completion_tokens"] == 40
        assert llm["total_tokens"] == 240

    def test_rule_source_not_counted_in_tokens(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], make_analysis(1, source="rule", tokens=False))
        db.insert_analysis(2, ["CRC_FAIL"], make_analysis(2, source="llm"))
        stats = db.stats()
        llm = stats["llm_analysis"]
        assert llm["total_tokens"] == 120  # 只有 llm 的算

    def test_empty_stats(self, db):
        stats = db.stats()
        assert stats["total"] == 0
        assert stats["llm_analysis"]["total_tokens"] == 0


# ── 重置 ────────────────────────────────────────────────

class TestReset:
    def test_reset_clears_all(self, db):
        db.insert(make_frame(1), make_result())
        db.insert_analysis(1, ["CRC_FAIL"], make_analysis(1))
        db.reset()
        stats = db.stats()
        assert stats["total"] == 0
        assert stats["llm_analysis"]["total"] == 0
