"""消费者主程序：从 stdin（管道）读取 C 生成的二进制流，
解析 → 质量门禁 → LLM 异常归因 → 入库，并打印实时统计。

运行（管道接 C 生成器）：
    sensor_sim.exe | python consumer.py
或读取已有二进制文件：
    python consumer.py --file data.bin

LLM 归因（可选）：设置环境变量 LLM_API_KEY 后自动启用，
未设置时降级为规则引擎归因，项目可独立运行。
"""
from __future__ import annotations

import sys
import time

from database import Database
from llm_analyzer import LLMAnalyzer
from protocol import StreamParser
from quality_gate import QualityGate


def run(stream, db: Database, gate: QualityGate,
        analyzer: LLMAnalyzer | None = None,
        max_frames: int = 0) -> dict:
    parser = StreamParser()
    total = 0
    passed = 0
    analyzed = 0
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

            # 异常帧做 LLM 归因
            if not result.passed and analyzer is not None:
                analysis = analyzer.analyze(frame, result.reasons)
                db.insert_analysis(frame.seq, result.reasons, analysis)
                analyzer.add_to_rag(frame, result.reasons, analysis)
                analyzed += 1

            if max_frames and total >= max_frames:
                break
        if max_frames and total >= max_frames:
            break

    elapsed = time.time() - t0
    return {
        "frames": total,
        "passed": passed,
        "analyzed": analyzed,
        "elapsed_s": round(elapsed, 3),
        "throughput_fps": round(total / elapsed, 1) if elapsed else 0,
    }


def main() -> None:
    args = sys.argv[1:]
    reset = "--reset" in args
    db = Database()
    gate = QualityGate()
    if reset:
        db.reset()
        print("已清空 sensor.db（--reset），准备重新灌入数据...")
    analyzer = LLMAnalyzer(db=db)

    if "--file" in args:
        idx = args.index("--file")
        path = args[idx + 1] if len(args) > idx + 1 else "data.bin"
        with open(path, "rb") as f:
            summary = run(f, db, gate, analyzer)
    else:
        summary = run(sys.stdin.buffer, db, gate, analyzer)

    llm_status = "LLM 归因" if analyzer.is_enabled else "规则引擎归因（未设 LLM_API_KEY）"
    print("=== 消费完成 ===")
    print(f"总帧数:   {summary['frames']}")
    print(f"通过:     {summary['passed']}")
    print(f"异常归因: {summary['analyzed']} 帧（{llm_status}）")
    print(f"耗时:     {summary['elapsed_s']} s")
    print(f"吞吐:     {summary['throughput_fps']} 帧/秒")
    print("\n调用 GET /stats 查看合格率与拦截分布。")
    print("调用 GET /analysis 查看异常归因结果。")
    db.close()


if __name__ == "__main__":
    main()
