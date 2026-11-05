"""数据风险评估。

对质量门禁拦截的异常做二次分析，按类型定级、给权重、算 0-100 风险评分，
并给出处置建议。职责边界：只做评估与汇总，不改动 quality_gate 自身的拦截行为。

风险等级映射：
    HIGH  — 数据损坏（CRC_FAIL），不可信，会导致下游算法行为不可预测
    MEDIUM — 物理值不合理（OUT_OF_RANGE）或数据缺失（NAN_VALUE），设备可能还在工作但数据有问题
    LOW   — 丢帧/延迟（SEQ_GAP），传输链路抖动，不影响设备本身

评分公式：
    score = sum(count_i × weight_i) / total_frames × 100

权重设计：HIGH=1.0 / MEDIUM=0.5 / LOW=0.1
（两帧 MEDIUM 等价一帧 HIGH；十帧 LOW 等价一帧 HIGH）

分级阈值：
    0-10   HEALTHY
    10-25  WARNING   — 建议关注
    >25    DANGEROUS — 建议排查
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 风险等级配置（单一事实源） ────────────────────────────────────

LEVEL_BY_REASON: dict[str, str] = {
    "CRC_FAIL": "HIGH",
    "OUT_OF_RANGE": "MEDIUM",
    "NAN_VALUE": "MEDIUM",
    "SEQ_GAP": "LOW",
}

WEIGHT_BY_LEVEL: dict[str, float] = {
    "HIGH": 1.0,
    "MEDIUM": 0.5,
    "LOW": 0.1,
}

LEVEL_THRESHOLDS: list[tuple[float, str]] = [
    (25.0, "DANGEROUS"),
    (10.0, "WARNING"),
    (0.0, "HEALTHY"),
]


@dataclass
class RiskReport:
    score: float                        # 0-100，保留两位小数
    level: str                          # HEALTHY / WARNING / DANGEROUS
    by_level: dict[str, int]            # 各等级计数
    by_reason: dict[str, int]           # 各异常类型计数
    total_frames: int
    recommendations: list[str] = field(default_factory=list)


def _classify_level(score: float) -> str:
    for threshold, label in LEVEL_THRESHOLDS:
        if score > threshold:
            return label
    return "HEALTHY"


def _build_recommendations(by_level: dict[str, int],
                          by_reason: dict[str, int],
                          total: int) -> list[str]:
    tips: list[str] = []
    high = by_level.get("HIGH", 0)
    med = by_level.get("MEDIUM", 0)
    low = by_level.get("LOW", 0)

    if high > 0:
        tips.append(f"检测到 {high} 帧 HIGH 风险（CRC_FAIL），建议排查传输链路或设备端")

    if med > 0 and total > 0:
        med_ratio = med / total
        if med_ratio > 0.05:
            tips.append(
                f"MEDIUM 异常（OUT_OF_RANGE / NAN_VALUE）占比 {med_ratio*100:.0f}%，"
                f"建议检查传感器校准"
            )

    if low > 0 and total > 0:
        low_ratio = low / total
        if low_ratio > 0.1:
            tips.append(
                f"LOW 风险（SEQ_GAP）占比 {low_ratio*100:.0f}%，"
                f"建议检查通信带宽或设备负载"
            )

    if not tips:
        tips.append("当前数据管道运行正常")

    return tips


def assess(reason_counter: dict[str, int] | Any, total_frames: int) -> RiskReport:
    """对最近 N 帧的异常计数做风险评估。

    参数来源是 database.stats() 的返回值：reason_counter 是 Counter 或 dict，
    key 是异常类型前缀（CRC_FAIL / OUT_OF_RANGE / NAN_VALUE / SEQ_GAP），
    value 是该类异常出现次数。total_frames 是参与统计的总帧数。
    """
    if total_frames <= 0:
        return RiskReport(
            score=0.0,
            level="HEALTHY",
            by_level={"HIGH": 0, "MEDIUM": 0, "LOW": 0},
            by_reason={},
            total_frames=0,
            recommendations=["数据库中暂无数据"],
        )

    # 按 reason 聚合（stats 里的 key 已经是前缀，直接用）
    by_reason: dict[str, int] = {}
    by_level: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    weighted_sum = 0.0

    for reason, count in dict(reason_counter).items():
        by_reason[reason] = count
        level = LEVEL_BY_REASON.get(reason, "MEDIUM")   # 未知类型保守按 MEDIUM
        by_level[level] += count
        weighted_sum += count * WEIGHT_BY_LEVEL.get(level, 0.5)

    score = round(weighted_sum / total_frames * 100, 2)
    level = _classify_level(score)

    return RiskReport(
        score=score,
        level=level,
        by_level=by_level,
        by_reason=by_reason,
        total_frames=total_frames,
        recommendations=_build_recommendations(by_level, by_reason, total_frames),
    )
