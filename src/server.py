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

import hmac
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import config
from database import Database
from logger import get_logger, setup_logging
import risk_assessment

log = get_logger(__name__)

_db: Database | None = None


def get_db() -> Database:
    """懒加载数据库：首次访问时建立连接池，避免 import 期就依赖 MySQL 可用。"""
    global _db
    if _db is None:
        _db = Database()
    return _db


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("API 服务启动: host=%s port=%d mysql=%s:%d/%s",
             config.SERVER_HOST, config.SERVER_PORT,
             config.MYSQL_HOST, config.MYSQL_PORT, config.MYSQL_DATABASE)
    get_db()
    yield
    if _db is not None:
        _db.close()
    log.info("API 服务关闭")


app = FastAPI(title="Sensor Sim Test API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def verify_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """写接口鉴权依赖：
    - 若 API_KEY 未配置（空）→ fail-closed 返回 503
    - 若请求头缺失/不匹配 → 返回 401
    比较使用 hmac.compare_digest 防止时序攻击。
    """
    expected = config.API_KEY
    if not expected:
        raise HTTPException(status_code=503, detail="api_key_not_configured")
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="API Key 无效")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/frames")
def frames(limit: int = Query(100, ge=1, le=1000)):
    return {"frames": _json_safe(get_db().recent(limit))}


@app.get("/stats")
def stats():
    return get_db().stats()


@app.get("/risk")
def risk():
    """数据风险评估。基于 database.stats() 的拦截分布，
    按异常类型定级（HIGH/MEDIUM/LOW）、算 0-100 风险评分、给出处置建议。
    """
    s = get_db().stats()
    report = risk_assessment.assess(s["reject_by_reason"], s["total"])
    return {
        "score": report.score,
        "level": report.level,
        "by_level": report.by_level,
        "by_reason": report.by_reason,
        "total_frames": report.total_frames,
        "recommendations": report.recommendations,
    }


@app.get("/analysis")
def analysis(limit: int = Query(100, ge=1, le=1000)):
    return {"analyses": get_db().recent_analysis(limit)}


class VerifyBody(BaseModel):
    """人工打标请求体：verdict 1=正确 0=错误，category 可选（人工更正类别）。"""
    verdict: int
    category: str | None = None


@app.post("/analysis/{seq}/verify")
def verify_analysis(seq: int, body: VerifyBody, _: None = Depends(verify_api_key)):
    """人工确认一条归因结果，用于评测 LLM 归因准确率（与人工一致率）。
    需要 X-API-Key 头（由 config.API_KEY 校验）。"""
    if body.verdict not in (0, 1):
        raise HTTPException(status_code=400, detail="verdict 必须为 0（错误）或 1（正确）")
    if get_db().set_human_verdict(seq, body.verdict, body.category) == 0:
        raise HTTPException(status_code=404, detail=f"归因记录不存在: seq={seq}")
    return {"seq": seq, "verdict": body.verdict, "category": body.category, "ok": True}


# 看板静态托管：放在 API 路由之后挂载，/stats /frames 等仍优先命中 API；
# 打开 http://<host>:8000/ 即得实时看板，无需再单独双击 dashboard/index.html。
_dashboard_dir = Path(config.DASHBOARD_DIR)
if _dashboard_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")
    log.info("看板托管: %s → http://%s:%d/", _dashboard_dir, config.SERVER_HOST, config.SERVER_PORT)
else:
    log.warning("看板目录不存在: %s（浏览器请直接打开 dashboard/index.html）", _dashboard_dir)


# 启动 / 关闭逻辑见上方 lifespan 上下文管理器
