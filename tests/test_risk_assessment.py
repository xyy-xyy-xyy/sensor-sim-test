"""risk_assessment 模块的单元测试。覆盖分级、评分、阈值切换、建议生成。
不依赖真实数据库，所有输入是 Counter/dict 和整数 total_frames。
"""
from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_assessment import (
    WEIGHT_BY_LEVEL,
    assess,
    _classify_level,
)


# ── 分级切换阈值 ──────────────────────────────────────────────

def test_healthy_at_zero():
    r = assess({}, 100)
    assert r.score == 0.0
    assert r.level == "HEALTHY"
    assert r.by_level == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    assert r.recommendations == ["当前数据管道运行正常"]


def test_warning_at_score_10():
    # 25 frames HIGH / 250 total = score 10.0 → WARNING（>10）
    r = assess({"CRC_FAIL": 25}, 250)
    assert r.score == 10.0
    # 10.0 边界：严格大于 10 才算 WARNING
    assert _classify_level(10.0) == "HEALTHY"
    assert _classify_level(10.01) == "WARNING"


def test_dangerous_at_score_25():
    # 50 HIGH / 200 = 25.0 → DANGEROUS（>25）
    r = assess({"CRC_FAIL": 50}, 200)
    assert r.score == 25.0
    assert _classify_level(25.0) == "WARNING"
    assert _classify_level(25.01) == "DANGEROUS"


# ── 权重与评分计算 ─────────────────────────────────────────────

def test_all_high_score():
    # 全部是 HIGH
    r = assess({"CRC_FAIL": 500}, 500)
    assert r.score == 100.0
    assert r.level == "DANGEROUS"
    assert r.by_level["HIGH"] == 500
    assert r.by_level["MEDIUM"] == 0


def test_all_medium_score():
    # 全部 MEDIUM → 权重 0.5 → score 50
    r = assess({"OUT_OF_RANGE": 500}, 500)
    assert r.score == 50.0
    assert r.level == "DANGEROUS"


def test_all_low_score_at_warning_boundary():
    # 250 LOW / 250 total × 100 × 0.1 = 10.0
    r = assess({"SEQ_GAP": 250}, 250)
    assert r.score == 10.0
    assert _classify_level(10.0) == "HEALTHY"
    assert _classify_level(10.01) == "WARNING"


def test_all_low_score_at_one_percent():
    # 250 LOW / 2500 total × 100 × 0.1 = 1.0
    r = assess({"SEQ_GAP": 250}, 2500)
    assert r.score == 1.0
    assert r.level == "HEALTHY"


def test_mixed_score_matches_formula():
    # CRC_FAIL=23 HIGH, OUT_OF_RANGE=61 MEDIUM, NAN_VALUE=89 MEDIUM, SEQ_GAP=28 LOW, total=500
    counter = Counter()
    counter["CRC_FAIL"] = 23
    counter["OUT_OF_RANGE"] = 61
    counter["NAN_VALUE"] = 89
    counter["SEQ_GAP"] = 28
    r = assess(counter, 500)
    expected = round((23 * 1.0 + (61 + 89) * 0.5 + 28 * 0.1) / 500 * 100, 2)
    assert r.score == expected
    assert r.by_level["HIGH"] == 23
    assert r.by_level["MEDIUM"] == 150
    assert r.by_level["LOW"] == 28


# ── 边界条件 ─────────────────────────────────────────────────

def test_zero_total_frames():
    r = assess({"CRC_FAIL": 10}, 0)
    assert r.score == 0.0
    assert r.level == "HEALTHY"
    assert r.recommendations == ["数据库中暂无数据"]


def test_negative_total():
    r = assess({"CRC_FAIL": 10}, -5)
    assert r.level == "HEALTHY"


def test_unknown_reason_falls_back_to_medium():
    r = assess({"MYSTERIOUS_ERROR": 100}, 1000)
    # 未知类型按 MEDIUM 权重 0.5
    assert r.by_level["MEDIUM"] == 100
    assert r.score == 5.0


# ── 建议生成 ─────────────────────────────────────────────────

def test_recommendations_cover_high():
    r = assess({"CRC_FAIL": 10}, 100)
    assert any("CRC_FAIL" in t for t in r.recommendations)


def test_recommendations_cover_medium_ratio():
    # 超过 5% 才触发
    r = assess({"OUT_OF_RANGE": 6}, 100)   # 6%
    assert any("MEDIUM" in t or "OUT_OF_RANGE" in t for t in r.recommendations)

    r2 = assess({"OUT_OF_RANGE": 4}, 100)  # 4% 不触发
    high_tip = [t for t in r2.recommendations if "MEDIUM" in t or "OUT_OF_RANGE" in t]
    assert len(high_tip) == 0


def test_recommendations_cover_low_ratio():
    # 超过 10% 才触发
    r = assess({"SEQ_GAP": 11}, 100)
    assert any("SEQ_GAP" in t or "LOW" in t for t in r.recommendations)

    r2 = assess({"SEQ_GAP": 9}, 100)
    low_tip = [t for t in r2.recommendations if "SEQ_GAP" in t]
    assert len(low_tip) == 0


# ── Counter 和 dict 两种输入都要能吃 ──────────────────────────

def test_accepts_counter():
    r = assess(Counter({"CRC_FAIL": 5}), 100)
    assert r.score > 0


def test_accepts_plain_dict():
    r = assess({"CRC_FAIL": 5}, 100)
    assert r.score > 0
