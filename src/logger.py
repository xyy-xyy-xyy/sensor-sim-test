"""集中式日志配置。

所有模块通过 ``get_logger(__name__)`` 获取 logger，统一格式与级别。
支持环境变量 ``LOG_LEVEL`` 控制输出级别（默认 INFO）。

使用方式：
    from logger import get_logger
    log = get_logger(__name__)
    log.info("消息")
    log.error("错误", exc_info=True)
"""
from __future__ import annotations

import logging
import os
import sys

_INITIALIZED = False


def setup_logging(level: str | None = None) -> None:
    """初始化全局日志配置（幂等，重复调用无副作用）。"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """获取一个 logger，自动确保全局配置已初始化。"""
    if not _INITIALIZED:
        setup_logging()
    return logging.getLogger(name)
