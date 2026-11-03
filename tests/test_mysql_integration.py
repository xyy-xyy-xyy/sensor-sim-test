"""MySQL 集成测试：验证生产路径（src/database.py）在真实 MySQL 上的行为。

使用独立测试库 sensor_test，避免 reset() 清空演示数据。
运行方式（必须显式指定测试库）：
    MYSQL_DATABASE=sensor_test python -m pytest tests/test_mysql_integration.py -v
"""
import os

import pytest

TEST_DB = "sensor_test"

MYSQL_ENABLED = bool(os.environ.get("MYSQL_HOST"))
# 双重保护：未指向独立测试库时跳过，防止误删演示数据
CORRECT_DB = os.environ.get("MYSQL_DATABASE") == TEST_DB

pytestmark = pytest.mark.skipif(
    not (MYSQL_ENABLED and CORRECT_DB),
    reason=f"需要 MYSQL_HOST 且 MYSQL_DATABASE={TEST_DB}（当前={os.environ.get('MYSQL_DATABASE')}）",
)


def _prepare_test_db() -> None:
    """用 root 账号创建独立测试库，并授权给业务账号。"""
    import pymysql

    user = os.environ.get("MYSQL_USER", "sensor")
    conn = pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3307")),
        user="root",
        password=os.environ.get("MYSQL_ROOT_PASSWORD", "123456"),
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{TEST_DB}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(f"GRANT ALL PRIVILEGES ON `{TEST_DB}`.* TO '{user}'@'%'")
            cur.execute("FLUSH PRIVILEGES")
    finally:
        conn.close()


@pytest.fixture
def db():
    _prepare_test_db()
    from database import Database

    instance = Database()
    instance.reset()
    yield instance
    instance.reset()
    instance.close()


def _frame(seq=1, sensor_type=1):
    from protocol import Frame

    return Frame(seq=seq, timestamp=1000 + seq, sensor_type=sensor_type,
                 samples=[{"channel": 0, "value": 50.0}], crc_ok=True)


def _result(passed=True, reasons=None):
    from quality_gate import QualityResult

    return QualityResult(passed=passed, reasons=reasons or [])


def _analysis(seq=1, source="llm", tokens=True, category="CRC_FAIL"):
    from llm_analyzer import AnalysisResult

    return AnalysisResult(
        seq=seq, root_cause="测试根因", confidence=0.9, category=category,
        recommendation="测试建议", source=source, rag_context_count=1,
        latency_ms=500,
        prompt_tokens=100 if tokens else 0,
        completion_tokens=20 if tokens else 0,
        total_tokens=120 if tokens else 0,
    )


class TestFrameOnMySQL:
    def test_insert_and_stats(self, db):
        db.insert(_frame(1), _result())
        db.insert(_frame(2), _result(passed=False, reasons=["CRC_FAIL"]))
        stats = db.stats()
        assert stats["total"] == 2
        assert stats["passed"] == 1
        assert stats["pass_rate"] == pytest.approx(0.5)

    def test_recent_order_and_json(self, db):
        for i in range(5):
            db.insert(_frame(i), _result())
        rows = db.recent(limit=3)
        assert len(rows) == 3
        assert rows[-1]["seq"] == 4
        assert isinstance(rows[-1]["samples"], list)


class TestAnalysisOnMySQL:
    def test_token_stats_use_int_not_decimal(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], _analysis(1))
        db.insert_analysis(2, ["CRC_FAIL"], _analysis(2))
        llm = db.stats()["llm_analysis"]
        assert llm["total_tokens"] == 240
        assert isinstance(llm["avg_confidence"], float)

    def test_rule_source_not_counted(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], _analysis(1, source="rule", tokens=False))
        db.insert_analysis(2, ["CRC_FAIL"], _analysis(2))
        assert db.stats()["llm_analysis"]["total_tokens"] == 120

    def test_created_at_is_serializable_string(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], _analysis(1))
        rows = db.recent_analysis()
        assert isinstance(rows[0]["created_at"], str)


class TestHumanVerdictOnMySQL:
    def test_repeat_verdict_is_not_treated_as_missing(self, db):
        """MySQL 中新旧值相同时 rowcount=0，重复打标必须仍返回 1。"""
        db.insert_analysis(1, ["CRC_FAIL"], _analysis(1))
        assert db.set_human_verdict(1, 1) == 1
        assert db.set_human_verdict(1, 1) == 1

    def test_missing_seq_returns_zero(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], _analysis(1))
        assert db.set_human_verdict(999, 1) == 0

    def test_accuracy(self, db):
        db.insert_analysis(1, ["CRC_FAIL"], _analysis(1))
        db.insert_analysis(2, ["SEQ_GAP"], _analysis(2, category="SEQ_GAP"))
        db.set_human_verdict(1, 1)
        db.set_human_verdict(2, 0)
        llm = db.stats()["llm_analysis"]
        assert llm["verified"] == 2
        assert llm["human_accuracy"] == pytest.approx(0.5)


class TestEvaluateQueriesOnMySQL:
    def test_all_frames_and_analysis(self, db):
        db.insert(_frame(1), _result())
        db.insert_analysis(1, ["CRC_FAIL"], _analysis(1))
        assert len(db.all_frames()) == 1
        assert len(db.all_analysis()) == 1
