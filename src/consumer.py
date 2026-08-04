"""消费者主程序：从 stdin（管道）读取 C 生成的二进制流，
解析 → 质量门禁 → LLM 异常归因 → 入库，并输出实时统计。

运行（管道接 C 生成器）：
    sensor_sim.exe | python consumer.py
或读取已有二进制文件：
    python consumer.py --file data.bin

LLM 归因（可选）：设置环境变量 LLM_API_KEY 后自动启用，
未设置时降级为规则引擎归因，项目可独立运行。

LLM 调用采用分批并发（默认 8 线程），大幅缩短灌数据时间。
RAG 检索在线程内只读访问索引（线程安全），每批完成后串行入索引。
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from config import config
from database import Database
from llm_analyzer import LLMAnalyzer
from logger import get_logger, setup_logging
from protocol import StreamParser
from quality_gate import QualityGate

log = get_logger(__name__)


def run(stream, db: Database, gate: QualityGate,
        analyzer: LLMAnalyzer | None = None,
        max_frames: int = 0) -> dict:
    parser = StreamParser()
    total = 0
    passed = 0
    analyzed = 0
    t0 = time.time()

    # 阶段 1：快速读帧 + 门禁检查 + 入库，收集异常帧
    pending: list[tuple] = []  # (frame, reasons) 待归因的异常帧

    while True:
        chunk = stream.read(config.STREAM_CHUNK_SIZE)
        if not chunk:
            break
        for frame in parser.feed(chunk):
            result = gate.check(frame)
            db.insert(frame, result)
            total += 1
            passed += 1 if result.passed else 0

            if not result.passed and analyzer is not None:
                pending.append((frame, result.reasons))

            if max_frames and total >= max_frames:
                break
        if max_frames and total >= max_frames:
            break

    log.info("阶段 1 完成：读入 %d 帧，通过 %d，待归因 %d", total, passed, len(pending))

    # 阶段 2：分批并发调 LLM 归因
    if pending and analyzer is not None:
        batch_size = config.LLM_WORKERS
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]

            # 并发调 analyze（RAG search 是只读的，线程安全）
            with ThreadPoolExecutor(max_workers=config.LLM_WORKERS) as pool:
                results = list(pool.map(
                    lambda item: analyzer.analyze(item[0], item[1]),
                    batch
                ))

            # 串行入库 + 加入 RAG 索引（下一批能用到本批的案例）
            for (frame, reasons), analysis in zip(batch, results):
                db.insert_analysis(frame.seq, reasons, analysis)
                analyzer.add_to_rag(frame, reasons, analysis)
                analyzed += 1

            log.debug("归因批次 %d/%d 完成", i // batch_size + 1,
                       (len(pending) + batch_size - 1) // batch_size)

    elapsed = time.time() - t0
    return {
        "frames": total,
        "passed": passed,
        "analyzed": analyzed,
        "elapsed_s": round(elapsed, 3),
        "throughput_fps": round(total / elapsed, 1) if elapsed else 0,
    }


def main() -> None:
    setup_logging()
    args = sys.argv[1:]
    reset = "--reset" in args
    db = Database(config.DB_PATH)
    gate = QualityGate()
    if reset:
        db.reset()
        log.info("已清空 %s（--reset），准备重新灌入数据...", config.DB_PATH)
    analyzer = LLMAnalyzer(db=db)

    if "--file" in args:
        idx = args.index("--file")
        path = args[idx + 1] if len(args) > idx + 1 else "data.bin"
        if not os.path.isfile(path):
            log.error("文件不存在：%s", path)
            sys.exit(1)
        log.info("从文件读取：%s", path)
        with open(path, "rb") as f:
            summary = run(f, db, gate, analyzer)
    else:
        log.info("从 stdin 读取（管道模式）")
        summary = run(sys.stdin.buffer, db, gate, analyzer)

    llm_status = "LLM 归因" if analyzer.is_enabled else "规则引擎归因（未设 LLM_API_KEY）"
    log.info("=== 消费完成 ===")
    log.info("总帧数:   %d", summary['frames'])
    log.info("通过:     %d", summary['passed'])
    log.info("异常归因: %d 帧（%s）", summary['analyzed'], llm_status)
    log.info("耗时:     %.3f s", summary['elapsed_s'])
    log.info("吞吐:     %.1f 帧/秒", summary['throughput_fps'])
    log.info("调用 GET /stats 查看合格率与拦截分布。")
    log.info("调用 GET /analysis 查看异常归因结果。")
    db.close()


if __name__ == "__main__":
    main()
