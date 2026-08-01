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
        return {
            "total": total,
            "passed": passed,
            "pass_rate": passed / total if total else 0.0,
            "reject_rate": (total - passed) / total if total else 0.0,
            "reject_by_reason": dict(reason_counter),
        }

    def close(self) -> None:
        self.conn.close()
