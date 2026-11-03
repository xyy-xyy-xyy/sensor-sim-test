"""集中式配置。

从环境变量读取，提供合理默认值。所有模块共享同一份配置，
避免硬编码散落在各文件中。

支持的环境变量：
    MYSQL_HOST           MySQL 主机（默认 127.0.0.1）
    MYSQL_PORT           MySQL 端口（默认 3307，对应 docker-compose 映射）
    MYSQL_USER           MySQL 用户（默认 sensor）
    MYSQL_PASSWORD       MySQL 密码（默认 sensor_dev_pwd，仅本地开发）
    MYSQL_DATABASE       MySQL 库名（默认 sensor）
    MYSQL_POOL_SIZE      连接池最大连接数（默认 5）
    DB_CONNECT_RETRIES   启动时连接重试次数（默认 10）
    DB_CONNECT_DELAY     每次重试间隔秒数（默认 2）
    LLM_WORKERS          LLM 并发线程数（默认 8）
    LLM_RAG_TOP_K        RAG 检索返回的相似案例数（默认 3）
    LLM_TIMEOUT          LLM API 超时秒数（默认 15）
    LLM_MAX_TOKENS       LLM 单次最大输出 token（默认 300）
    LLM_TEMPERATURE      LLM 采样温度（默认 0.3）
    SERVER_HOST          API 服务器监听地址（默认 0.0.0.0）
    SERVER_PORT          API 服务器端口（默认 8000）
    API_KEY              写接口鉴权 Token（默认空，不设时 POST 返回 503）
    DASHBOARD_POLL_MS    看板轮询间隔毫秒（默认 1000）
    STREAM_CHUNK_SIZE    管道读取块大小字节（默认 4096）
    LOG_LEVEL            日志级别（默认 INFO）
"""
from __future__ import annotations

import os
from pathlib import Path


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


class Config:
    """全局配置单例，属性均为只读约定。"""

    # ── 数据库（MySQL 8）──
    MYSQL_HOST: str = os.environ.get("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT: int = _get_int("MYSQL_PORT", 3307)
    MYSQL_USER: str = os.environ.get("MYSQL_USER", "sensor")
    MYSQL_PASSWORD: str = os.environ.get("MYSQL_PASSWORD", "123456")
    MYSQL_DATABASE: str = os.environ.get("MYSQL_DATABASE", "sensor")
    MYSQL_CHARSET: str = "utf8mb4"
    MYSQL_POOL_SIZE: int = _get_int("MYSQL_POOL_SIZE", 5)
    DB_CONNECT_RETRIES: int = _get_int("DB_CONNECT_RETRIES", 10)
    DB_CONNECT_DELAY: int = _get_int("DB_CONNECT_DELAY", 2)

    # ── LLM ──
    LLM_WORKERS: int = _get_int("LLM_WORKERS", 8)
    LLM_RAG_TOP_K: int = _get_int("LLM_RAG_TOP_K", 3)
    LLM_TIMEOUT: int = _get_int("LLM_TIMEOUT", 15)
    LLM_MAX_TOKENS: int = _get_int("LLM_MAX_TOKENS", 300)
    LLM_TEMPERATURE: float = _get_float("LLM_TEMPERATURE", 0.3)

    # ── Server ──
    SERVER_HOST: str = os.environ.get("SERVER_HOST", "0.0.0.0")
    SERVER_PORT: int = _get_int("SERVER_PORT", 8000)
    # 写接口鉴权 Token；空字符串代表 fail-closed，POST 返回 503
    API_KEY: str = os.environ.get("API_KEY", "").strip()

    # ── Dashboard ──
    DASHBOARD_POLL_MS: int = _get_int("DASHBOARD_POLL_MS", 1000)
    # 看板静态目录（默认项目根目录下 dashboard/，可用环境变量覆盖）
    DASHBOARD_DIR: str = os.environ.get(
        "DASHBOARD_DIR",
        str(Path(__file__).resolve().parent.parent / "dashboard"),
    )

    # ── Stream ──
    STREAM_CHUNK_SIZE: int = _get_int("STREAM_CHUNK_SIZE", 4096)

    # ── Logging ──
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")


config = Config()
