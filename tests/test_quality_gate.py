"""质量门禁 + 帧解析单测。

不依赖 C 编译器 / highway_env：在 Python 侧按协议构建帧（与 sensor_sim.c 同格式），
验证 protocol.py 解析正确、quality_gate.py 能拦截各类异常。
"""
import struct
import zlib
import math

import pytest

from protocol import StreamParser, MAGIC, TRAILER
from quality_gate import QualityGate


def build_frame(seq, sensor_type, samples, corrupt=0):
    """镜像 sensor_sim.c 的帧构建，返回 bytes。corrupt: 0正常 1CRC 2SEQ 4NaN 3越界。"""
    n = len(samples)
    payload = b""
    for ch, val in samples:
        if corrupt == 4:
            val = float("nan")
        if corrupt == 3:
            val = 999.0 if sensor_type == 3 else 500.0
        payload += bytes([ch]) + struct.pack("<f", val)

    length_field = 4 + 8 + 1 + 1 + len(payload) + 4 + 1
    use_seq = seq + 5 if corrupt == 2 else seq  # 注入丢帧
    body = (struct.pack("<H", length_field) + struct.pack("<I", use_seq)
            + struct.pack("<Q", 1000 + use_seq) + bytes([sensor_type, n]) + payload)
    crc = zlib.crc32(MAGIC + body) & 0xFFFFFFFF
    if corrupt == 1:
        crc ^= 0xDEADBEEF
    frame = MAGIC + body + struct.pack("<I", crc) + bytes([TRAILER])
    return frame


def parse_only(buf):
    parser = StreamParser()
    return list(parser.feed(buf))


def test_parse_valid_frame():
    f = build_frame(1, 3, [(0, 60.0), (1, 65.0)])
    frames = parse_only(f)
    assert len(frames) == 1
    assert frames[0].seq == 1
    assert frames[0].sensor_type == 3
    assert frames[0].crc_ok is True
    assert frames[0].samples[0]["value"] == pytest.approx(60.0)


def test_parse_multiple_frames_streamed():
    buf = build_frame(1, 1, [(0, 50.0)]) + build_frame(2, 2, [(0, 1.5), (1, 2.0)])
    frames = parse_only(buf)
    assert len(frames) == 2
    assert frames[1].seq == 2


def test_gate_passes_valid():
    gate = QualityGate()
    f = parse_only(build_frame(1, 3, [(0, 60.0)]))[0]
    r = gate.check(f)
    assert r.passed
    assert r.reasons == []


def test_gate_detects_crc_fail():
    gate = QualityGate()
    f = parse_only(build_frame(1, 3, [(0, 60.0)], corrupt=1))[0]
    r = gate.check(f)
    assert not r.passed
    assert "CRC_FAIL" in r.reasons


def test_gate_detects_seq_gap():
    gate = QualityGate()
    f1 = parse_only(build_frame(1, 3, [(0, 60.0)]))[0]
    gate.check(f1)  # 先喂一帧，last_seq = 1
    f2 = parse_only(build_frame(2, 3, [(0, 60.0)], corrupt=2))[0]  # 注入 SEQ 跳变
    r = gate.check(f2)
    assert not r.passed
    assert any(x.startswith("SEQ_GAP") for x in r.reasons)


def test_gate_detects_out_of_range():
    gate = QualityGate()
    f = parse_only(build_frame(1, 3, [(0, 60.0)], corrupt=3))[0]
    r = gate.check(f)
    assert not r.passed
    assert any(x.startswith("OUT_OF_RANGE") for x in r.reasons)


def test_gate_detects_nan():
    gate = QualityGate()
    f = parse_only(build_frame(1, 2, [(0, 1.5)], corrupt=4))[0]
    r = gate.check(f)
    assert not r.passed
    assert any(x.startswith("NAN_VALUE") for x in r.reasons)


def test_gate_reset_seq():
    gate = QualityGate()
    gate.check(parse_only(build_frame(5, 1, [(0, 50.0)]))[0])
    # 连续序号应不再报 SEQ_GAP
    f = parse_only(build_frame(6, 1, [(0, 50.0)]))[0]
    assert gate.check(f).passed
