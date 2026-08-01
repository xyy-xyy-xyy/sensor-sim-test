#!/usr/bin/env python3
"""测试数据生成器（在无 gcc 环境时替代 C 生成器，快速灌数据用）。

生成符合 architecture.md §三 二进制帧格式的字节流，写到 stdout，
配合 `python src/consumer.py` 灌入 SQLite。

包含约 20% 的异常帧，覆盖四种门禁拦截类型（CRC_FAIL / SEQ_GAP /
OUT_OF_RANGE / NAN_VALUE），便于看板展示拒绝分布。

用法：
    python tools/seed_data.py [帧数] | python src/consumer.py
"""
import struct
import sys
import zlib

MAGIC = b"\xAA\x55"
TRAILER = 0xEE
RANGES = {1: (0.0, 300.0), 2: (-20.0, 20.0), 3: (0.0, 300.0)}


def make_frame(seq: int, ts: int, sensor_type: int, value: float,
               channel: int = 0, *, crc_bad: bool = False) -> bytes:
    n = 1
    # MAGIC(2) + LENGTH(2) + SEQ(4) + TIMESTAMP(8) + SENSOR_TYPE(1) + N_SAMPLES(1)
    header = (MAGIC + struct.pack("<H", 19 + 5 * n)
              + struct.pack("<I", seq) + struct.pack("<Q", ts)
              + bytes([sensor_type, n]))
    payload = bytes([channel]) + struct.pack("<f", value)
    body = header + payload          # CRC 覆盖窗：MAGIC..PAYLOAD
    crc = zlib.crc32(body) & 0xFFFFFFFF
    if crc_bad:
        crc ^= 0xFFFFFFFF
    return body + struct.pack("<I", crc) + bytes([TRAILER])


def main(total: int = 200) -> None:
    out = sys.stdout.buffer
    seq = 0
    ts = 1_000_000
    for i in range(total):
        sensor_type = (i % 3) + 1
        lo, hi = RANGES[sensor_type]
        mid = (lo + hi) / 2.0
        seq += 1
        ts += 100
        rem = i % 20
        if rem == 0:                      # 5% CRC 损坏
            frame = make_frame(seq, ts, sensor_type, mid, crc_bad=True)
        elif rem == 5:                    # 5% 丢帧（SEQ 跳变）
            frame = make_frame(seq, ts, sensor_type, mid)
            seq += 5                      # 额外推进，使下一帧出现 GAP
        elif rem == 10:                   # 5% 越界
            frame = make_frame(seq, ts, sensor_type, hi + 50.0)
        elif rem == 15:                   # 5% NaN
            frame = make_frame(seq, ts, sensor_type, float("nan"))
        else:                             # 80% 正常
            frame = make_frame(seq, ts, sensor_type, mid)
        out.write(frame)
    out.flush()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
