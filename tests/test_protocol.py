"""帧解析单测：边界条件、容错、跨包重组。

不依赖 C 编译器，在 Python 侧按协议构建帧（与 sensor_sim.c 同格式），
验证 protocol.py 的流式解析器在各种异常输入下的正确性。
"""
import struct
import zlib

import pytest

from protocol import StreamParser, MAGIC, TRAILER, Frame


def build_frame(seq, sensor_type, samples, corrupt_crc=False):
    """构建一帧 bytes，corrupt_crc=True 时篡改 CRC。"""
    n = len(samples)
    payload = b""
    for ch, val in samples:
        payload += bytes([ch]) + struct.pack("<f", val)
    length_field = 4 + 8 + 1 + 1 + len(payload) + 4 + 1
    body = (struct.pack("<H", length_field) + struct.pack("<I", seq)
            + struct.pack("<Q", 1000 + seq) + bytes([sensor_type, n]) + payload)
    crc = zlib.crc32(MAGIC + body) & 0xFFFFFFFF
    if corrupt_crc:
        crc ^= 0xDEADBEEF
    return MAGIC + body + struct.pack("<I", crc) + bytes([TRAILER])


def parse_all(buf):
    return list(StreamParser().feed(buf))


# ── 正常解析 ──────────────────────────────────────────

def test_parse_single_frame():
    f = build_frame(1, 3, [(0, 60.0), (1, 65.0)])
    frames = parse_all(f)
    assert len(frames) == 1
    assert frames[0].seq == 1
    assert frames[0].sensor_type == 3
    assert frames[0].crc_ok is True
    assert len(frames[0].samples) == 2
    assert frames[0].samples[0]["channel"] == 0
    assert frames[0].samples[0]["value"] == pytest.approx(60.0)


def test_parse_multiple_frames():
    buf = build_frame(1, 1, [(0, 50.0)]) + build_frame(2, 2, [(0, 1.5)])
    frames = parse_all(buf)
    assert len(frames) == 2
    assert frames[0].seq == 1
    assert frames[1].seq == 2


def test_frame_with_zero_samples():
    f = build_frame(10, 1, [])
    frames = parse_all(f)
    assert len(frames) == 1
    assert frames[0].samples == []
    assert frames[0].crc_ok is True


# ── 跨包重组 ──────────────────────────────────────────

def test_frame_split_across_feeds():
    """帧被拆成两段 feed，应该能正确重组。"""
    raw = build_frame(1, 3, [(0, 60.0)])
    mid = len(raw) // 2
    parser = StreamParser()
    frames = list(parser.feed(raw[:mid]))
    assert len(frames) == 0  # 第一段不完整，不产出
    frames = list(parser.feed(raw[mid:]))
    assert len(frames) == 1
    assert frames[0].seq == 1


def test_multiple_frames_split():
    """两帧各自被拆分，最终都能解析。"""
    raw1 = build_frame(1, 1, [(0, 50.0)])
    raw2 = build_frame(2, 2, [(0, 1.5)])
    combined = raw1 + raw2
    parser = StreamParser()
    out = []
    # 每次喂 10 字节
    for i in range(0, len(combined), 10):
        out.extend(parser.feed(combined[i:i + 10]))
    assert len(out) == 2
    assert out[0].seq == 1
    assert out[1].seq == 2


# ── CRC 校验 ──────────────────────────────────────────

def test_crc_corruption_detected():
    f = build_frame(1, 3, [(0, 60.0)], corrupt_crc=True)
    frames = parse_all(f)
    assert len(frames) == 1
    assert frames[0].crc_ok is False


def test_crc_valid():
    f = build_frame(1, 3, [(0, 60.0)])
    frames = parse_all(f)
    assert frames[0].crc_ok is True


# ── MAGIC 同步容错 ────────────────────────────────────

def test_magic_mismatch_skipped():
    """帧前面有垃圾字节，解析器应该跳过找到 MAGIC。"""
    garbage = b"\x00\x01\x02"
    f = build_frame(1, 3, [(0, 60.0)])
    frames = parse_all(garbage + f)
    assert len(frames) == 1
    assert frames[0].seq == 1


def test_garbage_between_frames():
    """两帧之间有垃圾字节，第二帧仍能被解析。"""
    f1 = build_frame(1, 1, [(0, 50.0)])
    f2 = build_frame(2, 2, [(0, 1.5)])
    garbage = b"\xFF\xFF"
    frames = parse_all(f1 + garbage + f2)
    assert len(frames) == 2
    assert frames[1].seq == 2


# ── 空输入 ────────────────────────────────────────────

def test_empty_input():
    assert parse_all(b"") == []


def test_partial_magic():
    """只喂了 MAGIC 的第一个字节，不应崩溃。"""
    parser = StreamParser()
    assert list(parser.feed(MAGIC[:1])) == []
