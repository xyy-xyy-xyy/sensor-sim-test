"""数据质量门禁。

对每一帧做校验，识别并拦截异常。这是"AI 测试 / 数据工程"的核心价值点：
保证下游拿到的数据可信。拦截维度：
    - CRC_FAIL      帧校验失败（传输损坏）
    - SEQ_GAP       帧序号不连续（丢帧）
    - OUT_OF_RANGE  数值越界（物理不合理）
    - NAN_VALUE     数值为 NaN（缺失/损坏）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from protocol import Frame, SENSOR_NAMES

import math

# 各传感器类型的合理取值区间（按 sensor_type 统一判定，骨架版）
RANGES = {
    1: (0.0, 300.0),    # RADAR 距离 m
    2: (-20.0, 20.0),   # IMU 加速度 m/s^2
    3: (0.0, 300.0),    # GPS 速度 km/h
}


@dataclass
class QualityResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


class QualityGate:
    def __init__(self) -> None:
        self._last_seq: int | None = None

    def check(self, frame: Frame) -> QualityResult:
        reasons: list[str] = []

        # 1) CRC / trailer
        if not frame.crc_ok:
            reasons.append("CRC_FAIL")

        # 2) 数值合理性
        lo, hi = RANGES.get(frame.sensor_type, (-1e9, 1e9))
        for s in frame.samples:
            v = s["value"]
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                reasons.append(f"NAN_VALUE:ch{s['channel']}")
            elif not (lo <= v <= hi):
                reasons.append(f"OUT_OF_RANGE:{SENSOR_NAMES.get(frame.sensor_type,'?')}={v:.2f}")

        # 3) 序号连续性（丢帧检测）
        if self._last_seq is not None and frame.seq != self._last_seq + 1:
            gap = frame.seq - self._last_seq - 1
            reasons.append(f"SEQ_GAP:{gap}")
        self._last_seq = frame.seq

        return QualityResult(passed=(len(reasons) == 0), reasons=reasons)
