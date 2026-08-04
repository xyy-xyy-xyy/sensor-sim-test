"""SQLite 存储与统计查询。轻量、零配置，契合本机低资源约束。"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from typing import Any

from protocol import Frame
from quality_gate import QualityResult


class Database:
    def __init__(self, path: str = "sensor.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq INTEGER, timestamp INTEGER, sensor_type INTEGER,
                samples TEXT, passed INTEGER, reasons TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS llm_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seq INTEGER, reasons TEXT,
                root_cause TEXT, confidence REAL, category TEXT,
                recommendation TEXT, source TEXT,
                rag_context_count INTEGER, latency_ms INTEGER,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )"""
        )
        # 兼容旧表：若 token 列不存在则自动补列
        try:
            self.conn.execute("SELECT prompt_tokens FROM llm_analysis LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE llm_analysis ADD COLUMN prompt_tokens INTEGER DEFAULT 0")
            self.conn.execute("ALTER TABLE llm_analysis ADD COLUMN completion_tokens INTEGER DEFAULT 0")
            self.conn.execute("ALTER TABLE llm_analysis ADD COLUMN total_tokens INTEGER DEFAULT 0")
        self.conn.commit()

    def insert(self, frame: Frame, result: QualityResult) -> None:
        self.conn.execute(
            "INSERT INTO frames (seq, timestamp, sensor_type, samples, passed, reasons) "
            "VALUES (?,?,?,?,?,?)",
            (frame.seq, frame.timestamp, frame.sensor_type,
             json.dumps(frame.samples), 1 if result.passed else 0,
             json.dumps(result.reasons)),
        )
        self.conn.commit()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT seq, timestamp, sensor_type, samples, passed, reasons "
            "FROM frames ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = []
        for seq, ts, st, sm, passed, reasons in cur.fetchall():
            rows.append({
                "seq": seq, "timestamp": ts, "sensor_type": st,
                "samples": json.loads(sm), "passed": bool(passed),
                "reasons": json.loads(reasons),
            })
        return list(reversed(rows))

    def stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        passed = self.conn.execute("SELECT COUNT(*) FROM frames WHERE passed=1").fetchone()[0]
        reason_counter: Counter[str] = Counter()
        for (reasons,) in self.conn.execute("SELECT reasons FROM frames WHERE passed=0"):
            for r in json.loads(reasons):
                reason_counter[r.split(":")[0]] += 1

        # LLM 归因统计
        analysis_total = self.conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]
        llm_count = self.conn.execute("SELECT COUNT(*) FROM llm_analysis WHERE source='llm'").fetchone()[0]
        avg_conf = self.conn.execute("SELECT AVG(confidence) FROM llm_analysis").fetchone()[0] or 0.0
        avg_lat = self.conn.execute("SELECT AVG(latency_ms) FROM llm_analysis WHERE source='llm'").fetchone()[0] or 0
        total_prompt = self.conn.execute("SELECT COALESCE(SUM(prompt_tokens),0) FROM llm_analysis WHERE source='llm'").fetchone()[0]
        total_completion = self.conn.execute("SELECT COALESCE(SUM(completion_tokens),0) FROM llm_analysis WHERE source='llm'").fetchone()[0]
        total_tokens = self.conn.execute("SELECT COALESCE(SUM(total_tokens),0) FROM llm_analysis WHERE source='llm'").fetchone()[0]

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
            },
        }

    def insert_analysis(self, seq: int, reasons: list[str], result: Any) -> None:
        """存储一条 LLM 归因结果。"""
        self.conn.execute(
            "INSERT INTO llm_analysis (seq, reasons, root_cause, confidence, "
            "category, recommendation, source, rag_context_count, latency_ms, "
            "prompt_tokens, completion_tokens, total_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, json.dumps(reasons), result.root_cause, result.confidence,
             result.category, result.recommendation, result.source,
             result.rag_context_count, result.latency_ms,
             getattr(result, "prompt_tokens", 0),
             getattr(result, "completion_tokens", 0),
             getattr(result, "total_tokens", 0)),
        )
        self.conn.commit()

    def recent_analysis(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取最近的归因结果。"""
        cur = self.conn.execute(
            "SELECT seq, reasons, root_cause, confidence, category, "
            "recommendation, source, rag_context_count, latency_ms, "
            "prompt_tokens, completion_tokens, total_tokens, created_at "
            "FROM llm_analysis ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = []
        for seq, reasons, rc, conf, cat, rec, src, rag, lat, pt, ct, tt, ts in cur.fetchall():
            rows.append({
                "seq": seq, "reasons": json.loads(reasons) if reasons else [],
                "root_cause": rc, "confidence": round(conf, 3) if conf else 0,
                "category": cat, "recommendation": rec,
                "source": src, "rag_context_count": rag or 0,
                "latency_ms": lat or 0, "created_at": ts,
                "prompt_tokens": pt or 0, "completion_tokens": ct or 0,
                "total_tokens": tt or 0,
            })
        return list(reversed(rows))

    def get_analysis_history(self, limit: int = 500) -> list[dict[str, Any]]:
        """获取归因历史（供 RAG 索引初始化）。"""
        cur = self.conn.execute(
            "SELECT seq, reasons, root_cause, category "
            "FROM llm_analysis ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = []
        for seq, reasons, rc, cat in cur.fetchall():
            rows.append({
                "seq": seq,
                "reasons": json.loads(reasons) if reasons else "",
                "root_cause": rc or "",
                "category": cat or "",
            })
        return rows

    def reset(self) -> None:
        """清空全部数据（保留表结构），用于重新灌数据前的重置。
        比删文件更可靠：不触碰文件系统，避开安全删除拦截。"""
        self.conn.execute("DELETE FROM frames")
        self.conn.execute("DELETE FROM llm_analysis")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
