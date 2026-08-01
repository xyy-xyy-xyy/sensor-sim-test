"""消费者主程序：从 stdin（管道）读取 C 生成的二进制流，
解析 → 质量门禁 → 入库，并打印实时统计。

运行（管道接 C 生成器）：
    sensor_sim.exe | python consumer.py
或读取已有二进制文件：
    python consumer.py --file data.bin
"""
from __future__ import annotations

import sys
import time

from database import Database
from protocol import StreamParser
from quality_gate import QualityGate


def run(stream, db: Database, gate: QualityGate, max_frames: int = 0) -> dict:
    parser = StreamParser()
    total = 0
    passed = 0
    t0 = time.time()

    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        for frame in parser.feed(chunk):
            result = gate.check(frame)
            db.insert(frame, result)
            total += 1
            passed += 1 if result.passed else 0
            if max_frames and total >= max_frames:
                break
        if max_frames and total >= max_frames:
            break

    elapsed = time.time() - t0
    return {
        "frames": total,
        "passed": passed,
        "elapsed_s": round(elapsed, 3),
        "throughput_fps": round(total / elapsed, 1) if elapsed else 0,
    }


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--file":
        path = sys.argv[2] if len(sys.argv) > 2 else "data.bin"
        with open(path, "rb") as f:
            summary = run(f, Database(), QualityGate())
    else:
        summary = run(sys.stdin.buffer, Database(), QualityGate())

    print("=== 消费完成 ===")
    print(f"总帧数:   {summary['frames']}")
    print(f"通过:     {summary['passed']}")
    print(f"耗时:     {summary['elapsed_s']} s")
    print(f"吞吐:     {summary['throughput_fps']} 帧/秒")
    print("\n调用 GET /stats 查看合格率与拦截分布。")


if __name__ == "__main__":
    main()
