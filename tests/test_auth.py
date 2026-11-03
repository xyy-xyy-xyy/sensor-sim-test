"""API 鉴权层测试：不依赖真实数据库，用 monkeypatch 隔离 DB。

覆盖三点：
  1. POST 无 X-API-Key 头 → 401
  2. POST 带错误 key      → 401
  3. POST 带正确 key      → 200（真实 DB 调用被 mock）

读接口（GET /health /frames /stats /analysis）保持匿名访问。
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _patch_server(monkeypatch):
    """让 server 模块的 DB 和配置可控制。"""
    import server

    # 把 config.API_KEY 固定，保证测试环境确定
    monkeypatch.setattr(server.config, "API_KEY", "test-secret")

    # get_db 返回 mock，避免 lifespan 和 verify 真的走 MySQL
    mock_db = MagicMock()
    mock_db.set_human_verdict.return_value = 1
    monkeypatch.setattr(server, "get_db", lambda: mock_db)
    yield mock_db


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import server

    # lifespan 会 setup_logging + 调 get_db()，后者已被 monkeypatch
    with TestClient(server.app) as c:
        yield c


# ── 读接口必须保持匿名 ──────────────────────────────────────────

def test_health_anonymous(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_read_interfaces_anonymous(client):
    for path in ("/frames?limit=5", "/stats", "/analysis?limit=5"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} 匿名应可访问，实际 {r.status_code}"


# ── POST 鉴权行为 ──────────────────────────────────────────────

def test_post_missing_key_returns_401(client):
    r = client.post("/analysis/1/verify", json={"verdict": 1})
    assert r.status_code == 401


def test_post_wrong_key_returns_401(client):
    r = client.post(
        "/analysis/1/verify",
        json={"verdict": 1},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


def test_post_correct_key_returns_200(client):
    r = client.post(
        "/analysis/1/verify",
        json={"verdict": 1},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── fail-closed 行为（API_KEY 为空时） ─────────────────────────

def test_post_without_config_returns_503(monkeypatch):
    """若部署方忘记配 API_KEY，POST 应 fail-closed 返回 503。"""
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server.config, "API_KEY", "")
    with TestClient(server.app) as c:
        r = c.post("/analysis/1/verify", json={"verdict": 1})
        assert r.status_code == 503


def test_read_interfaces_still_open_when_key_missing(monkeypatch):
    """fail-closed 仅作用于 POST，读接口仍公开。"""
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server.config, "API_KEY", "")
    with TestClient(server.app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/stats").status_code == 200
