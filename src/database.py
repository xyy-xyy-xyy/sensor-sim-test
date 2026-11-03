"""MySQL 8 存储与统计查询。

驱动选型：pymysql（纯 Python MySQL 客户端）+ DBUtils.PooledDB（连接池）。
FastAPI 的同步端点运行在线程池中，多个请求并发执行，因此必须用连接池
复用连接，避免每个请求都重新建立 TCP 连接与 MySQL 握手。

表结构：frames（原始帧 + 门禁结果）、llm_analysis（LLM 归因 + 人工打标）。
字符集统一 utf8mb4，保证中文归因结果不乱码。
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

import pymysql
from dbutils.pooled_db import PooledDB
from pymysql.cursors import DictCursor

from config import config
from logger import get_logger
from protocol import Frame
from quality_gate import QualityResult

log = get_logger(__name__)

# 建表语句：索引直接内联在 DDL 中（MySQL 不支持 CREATE INDEX IF NOT EXISTS），
# 这样可以重复执行而不会报索引已存在的错误。
_DDL_FRAMES = """
CREATE TABLE IF NOT EXISTS frames (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    seq INT NOT NULL,
    timestamp BIGINT,
    sensor_type TINYINT,
    samples MEDIUMTEXT,
    passed TINYINT NOT NULL DEFAULT 0,
    reasons MEDIUMTEXT,
    KEY idx_frames_seq (seq),
    KEY idx_frames_passed (passed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_ANALYSIS = """
CREATE TABLE IF NOT EXISTS llm_analysis (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    seq INT NOT NULL,
    reasons MEDIUMTEXT,
    root_cause TEXT,
    confidence DECIMAL(5,4),
    category VARCHAR(64),
    recommendation TEXT,
    source VARCHAR(16),
    rag_context_count INT DEFAULT 0,
    latency_ms INT DEFAULT 0,
    prompt_tokens INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    total_tokens INT DEFAULT 0,
    human_verdict TINYINT NULL,
    human_category VARCHAR(64) NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_analysis_seq (seq),
    KEY idx_analysis_source (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _fmt_time(value: Any) -> str | None:
    """MySQL 返回 DATETIME 对象，统一转成字符串，避免 JSON 序列化失败。"""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


class Database:
    """MySQL 存储层。所有方法均从连接池借连接，用完自动归还。"""

    def __init__(self) -> None:
        self._pool = self._create_pool()
        self._init_schema()
        log.info(
            "MySQL 连接池就绪: %s@%s:%d/%s (池大小 %d, utf8mb4)",
            config.MYSQL_USER, config.MYSQL_HOST, config.MYSQL_PORT,
            config.MYSQL_DATABASE, config.MYSQL_POOL_SIZE,
        )

    # ── 连接与初始化 ────────────────────────────────────
    def _create_pool(self) -> PooledDB:
        """创建连接池，带重试以应对容器编排时 MySQL 尚未就绪。"""
        last_err: Exception | None = None
        for attempt in range(1, config.DB_CONNECT_RETRIES + 1):
            try:
                pool = PooledDB(
                    creator=pymysql,
                    maxconnections=config.MYSQL_POOL_SIZE,
                    mincached=1,
                    maxcached=config.MYSQL_POOL_SIZE,
                    blocking=True,
                    host=config.MYSQL_HOST,
                    port=config.MYSQL_PORT,
                    user=config.MYSQL_USER,
                    password=config.MYSQL_PASSWORD,
                    database=config.MYSQL_DATABASE,
                    charset=config.MYSQL_CHARSET,
                    cursorclass=DictCursor,
                    autocommit=True,
                )
                # PooledDB 本身是懒连接，这里主动借一次连接验证可用性
                with pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1 AS ok")
                        cur.fetchone()
                return pool
            except pymysql.err.MySQLError as e:
                last_err = e
                log.warning("MySQL 连接失败（第 %d/%d 次）: %s",
                            attempt, config.DB_CONNECT_RETRIES, e)
                if attempt < config.DB_CONNECT_RETRIES:
                    time.sleep(config.DB_CONNECT_DELAY)
        raise RuntimeError(
            f"无法连接 MySQL {config.MYSQL_HOST}:{config.MYSQL_PORT}/"
            f"{config.MYSQL_DATABASE}，已重试 {config.DB_CONNECT_RETRIES} 次: {last_err}"
        )

    def _column_exists(self, conn, table: str, column: str) -> bool:
        """通过 information_schema 判断列是否存在（MySQL 版表迁移探测）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name=%s LIMIT 1",
                (config.MYSQL_DATABASE, table, column),
            )
            return cur.fetchone() is not None

    def _init_schema(self) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL_FRAMES)
                cur.execute(_DDL_ANALYSIS)

            # 兼容旧表：缺列则自动补齐（对应旧 SQLite 版本的迁移逻辑）
            migrations = [
                ("prompt_tokens", "INT DEFAULT 0"),
                ("completion_tokens", "INT DEFAULT 0"),
                ("total_tokens", "INT DEFAULT 0"),
            ]
            for col, spec in migrations:
                if not self._column_exists(conn, "llm_analysis", col):
                    log.info("检测到旧表结构，自动迁移：添加列 %s", col)
                    with conn.cursor() as cur:
                        cur.execute(f"ALTER TABLE llm_analysis ADD COLUMN {col} {spec}")

            for col, spec in [("human_verdict", "TINYINT NULL"),
                              ("human_category", "VARCHAR(64) NULL")]:
                if not self._column_exists(conn, "llm_analysis", col):
                    log.info("检测到旧表结构，自动迁移：添加列 %s", col)
                    with conn.cursor() as cur:
                        cur.execute(f"ALTER TABLE llm_analysis ADD COLUMN {col} {spec}")

        log.debug("数据库初始化完成")

    # ── 帧 ──────────────────────────────────────────────
    def insert(self, frame: Frame, result: QualityResult) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO frames (seq, timestamp, sensor_type, samples, passed, reasons) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (frame.seq, frame.timestamp, frame.sensor_type,
                     json.dumps(frame.samples), 1 if result.passed else 0,
                     json.dumps(result.reasons)),
                )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, timestamp, sensor_type, samples, passed, reasons "
                    "FROM frames ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = cur.fetchall()
        return [
            {
                "seq": r["seq"], "timestamp": r["timestamp"],
                "sensor_type": r["sensor_type"],
                "samples": json.loads(r["samples"]) if r["samples"] else [],
                "passed": bool(r["passed"]),
                "reasons": json.loads(r["reasons"]) if r["reasons"] else [],
            }
            for r in reversed(rows)
        ]

    def stats(self) -> dict[str, Any]:
        reason_counter: Counter[str] = Counter()

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM frames")
                total = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM frames WHERE passed=1")
                passed = cur.fetchone()["c"]

                cur.execute("SELECT reasons FROM frames WHERE passed=0")
                for r in cur.fetchall():
                    if not r["reasons"]:
                        continue
                    for reason in json.loads(r["reasons"]):
                        reason_counter[reason.split(":")[0]] += 1

                # LLM 归因统计
                cur.execute("SELECT COUNT(*) AS c FROM llm_analysis")
                analysis_total = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM llm_analysis WHERE source='llm'")
                llm_count = cur.fetchone()["c"]
                cur.execute("SELECT AVG(confidence) AS v FROM llm_analysis")
                avg_conf = float(cur.fetchone()["v"] or 0.0)
                cur.execute("SELECT AVG(latency_ms) AS v FROM llm_analysis WHERE source='llm'")
                avg_lat = float(cur.fetchone()["v"] or 0)
                cur.execute("SELECT COALESCE(SUM(prompt_tokens),0) AS v FROM llm_analysis WHERE source='llm'")
                total_prompt = int(cur.fetchone()["v"])
                cur.execute("SELECT COALESCE(SUM(completion_tokens),0) AS v FROM llm_analysis WHERE source='llm'")
                total_completion = int(cur.fetchone()["v"])
                cur.execute("SELECT COALESCE(SUM(total_tokens),0) AS v FROM llm_analysis WHERE source='llm'")
                total_tokens = int(cur.fetchone()["v"])

                # 人工打标统计：已确认条数 + LLM 归因与人工一致率
                cur.execute(
                    "SELECT COUNT(*) AS c FROM llm_analysis WHERE human_verdict IS NOT NULL"
                )
                verified = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM llm_analysis WHERE human_verdict=1")
                verified_correct = cur.fetchone()["c"]

        return {
            "total": total,
            "passed": passed,
            "pass_rate": passed / total if total else 0.0,
            "reject_rate": (total - passed) / total if total else 0.0,
            "reject_by_reason": dict(reason_counter),
            "llm_analysis": {
                "total": analysis_total,
                "llm_count": llm_count,
                "rule_count": analysis_total - llm_count,
                "avg_confidence": round(avg_conf, 3),
                "avg_latency_ms": round(avg_lat, 0),
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_tokens,
                "verified": verified,
                "human_accuracy": round(verified_correct / verified, 3) if verified else None,
            },
        }

    # ── 归因 ────────────────────────────────────────────
    def insert_analysis(self, seq: int, reasons: list[str], result: Any) -> None:
        """存储一条 LLM 归因结果。"""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO llm_analysis (seq, reasons, root_cause, confidence, "
                    "category, recommendation, source, rag_context_count, latency_ms, "
                    "prompt_tokens, completion_tokens, total_tokens) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (seq, json.dumps(reasons), result.root_cause, result.confidence,
                     result.category, result.recommendation, result.source,
                     result.rag_context_count, result.latency_ms,
                     getattr(result, "prompt_tokens", 0),
                     getattr(result, "completion_tokens", 0),
                     getattr(result, "total_tokens", 0)),
                )

    def recent_analysis(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取最近的归因结果（含人工打标状态）。"""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, reasons, root_cause, confidence, category, "
                    "recommendation, source, rag_context_count, latency_ms, "
                    "prompt_tokens, completion_tokens, total_tokens, "
                    "human_verdict, human_category, created_at "
                    "FROM llm_analysis ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = cur.fetchall()
        return [
            {
                "seq": r["seq"],
                "reasons": json.loads(r["reasons"]) if r["reasons"] else [],
                "root_cause": r["root_cause"],
                "confidence": round(float(r["confidence"]), 3) if r["confidence"] else 0,
                "category": r["category"],
                "recommendation": r["recommendation"],
                "source": r["source"],
                "rag_context_count": r["rag_context_count"] or 0,
                "latency_ms": r["latency_ms"] or 0,
                "created_at": _fmt_time(r["created_at"]),
                "prompt_tokens": r["prompt_tokens"] or 0,
                "completion_tokens": r["completion_tokens"] or 0,
                "total_tokens": r["total_tokens"] or 0,
                "human_verdict": r["human_verdict"],
                "human_category": r["human_category"],
            }
            for r in reversed(rows)
        ]

    def set_human_verdict(self, seq: int, verdict: int, category: str | None = None) -> int:
        """人工确认归因结果。verdict: 1=正确，0=错误；category 可选，用于更正类别。

        先查存在性再更新：MySQL 在"新旧值相同"时 rowcount 会返回 0，
        直接依赖 rowcount 会把重复打标误判成记录不存在。
        返回该 seq 匹配的行数（不存在时为 0，便于接口层返回 404）。
        """
        if verdict not in (0, 1):
            raise ValueError("verdict 必须为 0（错误）或 1（正确）")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM llm_analysis WHERE seq=%s", (seq,))
                matched = cur.fetchone()["c"]
                if not matched:
                    return 0
                cur.execute(
                    "UPDATE llm_analysis SET human_verdict=%s, human_category=%s WHERE seq=%s",
                    (verdict, category, seq),
                )
        log.info("人工打标: seq=%d verdict=%s category=%s",
                 seq, "正确" if verdict else "错误", category)
        return matched

    def get_analysis_history(self, limit: int = 500) -> list[dict[str, Any]]:
        """获取归因历史（供 RAG 索引初始化）。"""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, reasons, root_cause, category "
                    "FROM llm_analysis ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = cur.fetchall()
        return [
            {
                "seq": r["seq"],
                "reasons": r["reasons"] if r["reasons"] else "",
                "root_cause": r["root_cause"] or "",
                "category": r["category"] or "",
            }
            for r in rows
        ]

    # ── 评测取数 ────────────────────────────────────────
    def all_frames(self) -> list[dict[str, Any]]:
        """评测用：按 seq 升序返回全部帧的门禁结果。"""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, sensor_type, passed, reasons FROM frames ORDER BY seq"
                )
                return cur.fetchall()

    def all_analysis(self) -> list[dict[str, Any]]:
        """评测用：按 seq 升序返回全部归因结果。"""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, reasons, root_cause, confidence, category, "
                    "recommendation, source, rag_context_count, latency_ms, "
                    "prompt_tokens, completion_tokens, total_tokens, "
                    "human_verdict, human_category "
                    "FROM llm_analysis ORDER BY seq"
                )
                return cur.fetchall()

    # ── 维护 ────────────────────────────────────────────
    def reset(self) -> None:
        """清空全部数据（保留表结构），用于重新灌数据前的重置。"""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM frames")
                cur.execute("DELETE FROM llm_analysis")
        log.info("数据库已重置（DELETE all rows）")

    def close(self) -> None:
        """关闭连接池（服务退出时调用）。"""
        pool = getattr(self, "_pool", None)
        if pool is not None:
            try:
                pool.close()
            except Exception as e:  # 池关闭失败不应阻塞进程退出
                log.debug("连接池关闭异常: %s", e)
            self._pool = None
            log.debug("数据库连接池已关闭")
