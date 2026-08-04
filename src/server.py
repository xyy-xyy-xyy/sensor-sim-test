"""FastAPI HTTP 接口层。对外提供数据查询、统计与 LLM 异常归因结果，供前端看板 / 测试脚本调用。

运行：
    uvicorn server:app --host 0.0.0.0 --port 8000
接口：
    GET /health            健康检查
    GET /frames?limit=100   最近 N 帧
    GET /stats             合格率、丢帧率、拦截原因分布、LLM 归因统计
    GET /analysis?limit=100 最近 N 条异常归因结果
"""
from __future__ import annotations

import math
import time

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import config
from database import Database
from logger import get_logger, setup_logging

log = get_logger(__name__)

app = FastAPI(title="Sensor Sim Test API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
db = Database(config.DB_PATH)


@app.middleware("http")
async def error_handler(request: Request, call_next):
    """全局异常处理中间件：捕获未处理异常，返回 500 JSON 而非裸 500。"""
    start = time.time()
    try:
        response = await call_next(request)
        elapsed_ms = (time.time() - start) * 1000
        log.debug("%s %s → %d (%.1fms)", request.method, request.url.path,
                  response.status_code, elapsed_ms)
        return response
    except Exception as e:
        log.error("未处理异常: %s %s → %s", request.method, request.url.path, e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "detail": str(e)},
        )


def _json_safe(obj):
    """把 float NaN/Inf 转成 None，避免 JSON 序列化 500。"""
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/frames")
def frames(limit: int = Query(100, ge=1, le=1000)):
    return {"frames": _json_safe(db.recent(limit))}


@app.get("/stats")
def stats():
    return db.stats()


@app.get("/analysis")
def analysis(limit: int = Query(100, ge=1, le=1000)):
    return {"analyses": db.recent_analysis(limit)}


@app.on_event("startup")
def startup():
    setup_logging()
    log.info("API 服务启动: host=%s port=%d db=%s",
             config.SERVER_HOST, config.SERVER_PORT, config.DB_PATH)


@app.on_event("shutdown")
def shutdown():
    db.close()
    log.info("API 服务关闭")
