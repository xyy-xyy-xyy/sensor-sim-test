"""二进制帧解析（Python 侧，与 src/protocol.h / sensor_sim.c 严格对应）。

帧结构（小端）：
    MAGIC(0xAA 0x55) | LENGTH(uint16) | SEQ(uint32) | TIMESTAMP(uint64)
    | SENSOR_TYPE(uint8) | N_SAMPLES(uint8) | PAYLOAD(5*N) | CRC32(uint32) | TRAILER(0xEE)
PAYLOAD：每样本 = channel(uint8) + value(float32 LE)
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterator

MAGIC = b"\xAA\x55"
TRAILER = 0xEE

SENSOR_NAMES = {1: "RADAR", 2: "IMU", 3: "GPS"}


@dataclass
class Frame:
    seq: int
    timestamp: int
    sensor_type: int
    samples: list[dict] = field(default_factory=list)
    crc_ok: bool = False
    raw_length: int = 0


def _parse_one(buf: bytes, offset: int) -> tuple[Frame | None, int]:
    """尝试从 buf[offset:] 解析一帧。字节不足返回 (None, offset)。"""
    if len(buf) - offset < 23:  # 最小帧（N=0 时 23 字节）
        return None, offset

    if buf[offset:offset + 2] != MAGIC:
        # 帧同步失败：跳 1 字节重新找 MAGIC（容错）
        return None, offset + 1

    length = struct.unpack_from("<H", buf, offset + 2)[0]
    total = 4 + length  # MAGIC(2)+LENGTH(2)+length
    if len(buf) - offset < total:
        return None, offset  # 字节不全，等更多数据

    seq = struct.unpack_from("<I", buf, offset + 4)[0]
    timestamp = struct.unpack_from("<Q", buf, offset + 8)[0]
    sensor_type = buf[offset + 16]
    n = buf[offset + 17]

    p = offset + 18
    samples = []
    for _ in range(n):
        channel = buf[p]
        value = struct.unpack_from("<f", buf, p + 1)[0]
        samples.append({"channel": channel, "value": value})
        p += 5

    crc_stored = struct.unpack_from("<I", buf, p)[0]
    p += 4
    trailer = buf[p]

    # 校验 CRC（对 MAGIC..PAYLOAD 全部字节）
    crc_calc = zlib.crc32(buf[offset:offset + 18 + 5 * n]) & 0xFFFFFFFF
    crc_ok = (crc_calc == crc_stored) and (trailer == TRAILER)

    frame = Frame(seq=seq, timestamp=timestamp, sensor_type=sensor_type,
                  samples=samples, crc_ok=crc_ok, raw_length=total)
    return frame, offset + total


class StreamParser:
    """流式解析器：处理跨多次读取的不完整帧。"""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> Iterator[Frame]:
        self._buf += data
        offset = 0
        while offset < len(self._buf):
            frame, next_off = _parse_one(self._buf, offset)
            if frame is None:
                if next_off == offset:  # 字节不足，等下次
                    break
                offset = next_off
                continue
            yield frame
            offset = next_off
        self._buf = self._buf[offset:]
