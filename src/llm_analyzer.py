"""LLM 异常归因模块。

当质量门禁拦截到异常帧时，将异常上下文组装成 prompt 调用 LLM 做语义归因，
输出结构化结果（根因 / 置信度 / 分类 / 建议）。同时基于 TF-IDF 检索历史相似
案例（RAG），为新异常提供参考上下文。

无 API key 时自动降级为规则引擎归因，保证项目可独立运行。

配置（环境变量）：
    LLM_API_KEY   API 密钥（不设则降级为规则引擎）
    LLM_BASE_URL  API 地址（默认 DeepSeek）
    LLM_MODEL     模型名（默认 deepseek-chat）
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from config import config
from logger import get_logger
from protocol import Frame, SENSOR_NAMES

log = get_logger(__name__)


def _load_env_file() -> None:
    """启动时自动加载项目根目录的 .env（零依赖，不引入 python-dotenv）。

    仅补充未设置的环境变量，不覆盖已存在的系统/命令行变量。
    这样用户把 key 写进 .env 后无需手动 set，LLM 模块也能读到。
    """
    here = os.path.dirname(os.path.abspath(__file__))       # src/
    env_path = os.path.join(os.path.dirname(here), ".env")  # 项目根/.env
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class AnalysisResult:
    """单帧异常的 LLM 归因结果。"""
    seq: int
    root_cause: str
    confidence: float
    category: str
    recommendation: str
    source: str = "rule"  # "llm" 或 "rule"
    rag_context_count: int = 0
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "root_cause": self.root_cause,
            "confidence": round(self.confidence, 3),
            "category": self.category,
            "recommendation": self.recommendation,
            "source": self.source,
            "rag_context_count": self.rag_context_count,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


# ── TF-IDF 检索器（RAG 核心，纯标准库实现）────────────────

class TfidfIndex:
    """轻量 TF-IDF 索引，用于检索历史相似异常案例。

    不依赖外部 embedding 服务或向量数据库，用纯 Python 实现 cosine 相似度检索。
    适合案例量 < 10k 的场景，契合本项目规模。
    """

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []       # 原始案例文档
        self._tf: list[dict[str, float]] = []        # 每篇文档的 TF 向量
        self._idf: dict[str, float] = {}             # 全局 IDF
        self._doc_count: int = 0

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中英文混合分词：英文按单词，中文按字。"""
        tokens = re.findall(r"[a-zA-Z_]+|[\u4e00-\u9fff]", text.lower())
        return tokens

    def add(self, doc: dict[str, Any]) -> None:
        """添加一篇案例文档。doc 须含 'text' 字段供索引。"""
        text = doc.get("text", "")
        tokens = self._tokenize(text)
        tf: dict[str, float] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0.0) + 1.0
        total = len(tokens) if tokens else 1
        for t in tf:
            tf[t] /= total
        self._docs.append(doc)
        self._tf.append(tf)
        self._doc_count += 1
        self._rebuild_idf()

    def _rebuild_idf(self) -> None:
        """重算 IDF（文档量小时可接受）。"""
        df: dict[str, int] = {}
        for tf in self._tf:
            for term in tf:
                df[term] = df.get(term, 0) + 1
        n = self._doc_count or 1
        self._idf = {term: math.log((n + 1) / (cnt + 1)) + 1.0 for term, cnt in df.items()}

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """检索与 query 最相似的 top_k 篇文档，返回带 score 的副本。"""
        if not self._docs:
            return []
        q_tokens = self._tokenize(query)
        q_tf: dict[str, float] = {}
        for t in q_tokens:
            q_tf[t] = q_tf.get(t, 0.0) + 1.0
        total = len(q_tokens) if q_tokens else 1
        for t in q_tf:
            q_tf[t] /= total

        # query TF-IDF 向量
        q_vec = {t: q_tf[t] * self._idf.get(t, 0.0) for t in q_tf}
        q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

        scores: list[tuple[float, int]] = []
        for i, tf in enumerate(self._tf):
            d_vec = {t: tf[t] * self._idf.get(t, 0.0) for t in tf}
            d_norm = math.sqrt(sum(v * v for v in d_vec.values())) or 1.0
            # cosine 相似度
            dot = sum(q_vec.get(t, 0.0) * d_vec.get(t, 0.0) for t in q_vec if t in d_vec)
            sim = dot / (q_norm * d_norm)
            scores.append((sim, i))

        scores.sort(reverse=True)
        results = []
        for sim, idx in scores[:top_k]:
            if sim <= 0:
                break
            doc = dict(self._docs[idx])
            doc["similarity"] = round(sim, 3)
            results.append(doc)
        return results


# ── Prompt 工程 ───────────────────────────────────────────

ANOMALY_DESC = {
    "CRC_FAIL": "帧校验失败，数据在传输过程中被损坏",
    "SEQ_GAP": "帧序号不连续，存在丢帧或乱序",
    "OUT_OF_RANGE": "传感器数值超出物理合理范围",
    "NAN_VALUE": "传感器数值为 NaN 或非法值，表示数据缺失或损坏",
    "NOISE": "噪声超标，数值在物理范围内但波动异常",
}


def _build_prompt(
    frame: Frame,
    reasons: list[str],
    rag_cases: list[dict[str, Any]],
) -> str:
    """构建 LLM 归因 prompt。"""
    sensor_name = SENSOR_NAMES.get(frame.sensor_type, f"未知({frame.sensor_type})")
    reason_types = [r.split(":")[0] for r in reasons]
    reason_descs = [f"- {r}: {ANOMALY_DESC.get(r.split(':')[0], '未知异常')}" for r in reasons]

    samples_str = "\n".join(
        f"  通道{s['channel']}: {s['value']:.4f}" for s in frame.samples
    ) if frame.samples else "  (无采样数据)"

    rag_section = ""
    if rag_cases:
        rag_lines = []
        for i, case in enumerate(rag_cases, 1):
            rag_lines.append(
                f"  案例{i}（相似度{case.get('similarity', 0)}）: "
                f"帧{case.get('seq')} / 原因{case.get('reasons')} / "
                f"归因: {case.get('root_cause', 'N/A')}"
            )
        rag_section = f"\n历史相似异常案例（供参考）：\n" + "\n".join(rag_lines)

    return f"""你是自动驾驶仿真测试数据质量分析专家。请分析以下被质量门禁拦截的异常数据帧。

## 异常帧信息
- 帧序号: {frame.seq}
- 时间戳: {frame.timestamp} ms
- 传感器类型: {sensor_name}
- 采样数据:
{samples_str}
- 拦截原因:
{chr(10).join(reason_descs)}
{rag_section}
## 任务
分析异常根因，判断类别，给出处置建议。严格输出以下 JSON（不要输出其他内容）：
{{"root_cause": "根因分析（1-2句）", "confidence": 0.0到1.0的数值, "category": "异常类别（CRC_FAIL/SEQ_GAP/OUT_OF_RANGE/NAN_VALUE/NOISE/UNKNOWN之一）", "recommendation": "处置建议（1句）"}}"""


# ── 规则引擎（降级方案）────────────────────────────────────

def _rule_based_analysis(frame: Frame, reasons: list[str]) -> AnalysisResult:
    """无 LLM 时的规则引擎归因，保证项目可独立运行。"""
    reason_types = [r.split(":")[0] for r in reasons]
    primary = reason_types[0] if reason_types else "UNKNOWN"

    rule_map = {
        "CRC_FAIL": (
            "帧数据在传输中发生位翻转或截断，CRC32 校验不通过",
            0.95,
            "建议检查 C 生成器的 CRC 计算逻辑或管道传输完整性",
        ),
        "SEQ_GAP": (
            f"帧序号跳变，检测到丢帧（gap={reasons[0].split(':')[1] if ':' in reasons[0] else '?'}）",
            0.90,
            "建议检查数据生成速率与消费端处理能力是否匹配",
        ),
        "OUT_OF_RANGE": (
            f"传感器数值超出物理合理区间（{reasons[0]}）",
            0.92,
            "建议检查 C 生成器的物理模型参数或异常注入逻辑",
        ),
        "NAN_VALUE": (
            f"采样值出现 NaN/Inf，数据源可能损坏（{reasons[0]}）",
            0.93,
            "建议检查传感器数据采集通道或注入逻辑",
        ),
        "NOISE": (
            "噪声波动超出正常范围但未越界，属边界数据",
            0.80,
            "建议放行但标记为低质量数据，供下游算法评估鲁棒性",
        ),
    }
    root_cause, conf, rec = rule_map.get(primary, ("未知异常类型", 0.5, "建议人工排查"))
    return AnalysisResult(
        seq=frame.seq,
        root_cause=root_cause,
        confidence=conf,
        category=primary,
        recommendation=rec,
        source="rule",
    )


# ── LLM 分析器 ────────────────────────────────────────────

class LLMAnalyzer:
    """LLM 异常归因分析器，集成 RAG 检索。

    使用方式：
        analyzer = LLMAnalyzer(db)
        result = analyzer.analyze(frame, reasons)
    """

    def __init__(self, db: Any = None, rag_top_k: int | None = None) -> None:
        _load_env_file()
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
        self.model = os.environ.get("LLM_MODEL", "deepseek-chat")
        self.rag_top_k = rag_top_k if rag_top_k is not None else config.LLM_RAG_TOP_K
        self._rag_index = TfidfIndex()
        self._db = db
        self._init_rag_from_db()
        if self.is_enabled:
            log.info("LLM 归因已启用: model=%s, rag_top_k=%d, workers=%d",
                     self.model, self.rag_top_k, config.LLM_WORKERS)
        else:
            log.warning("未设置 LLM_API_KEY，将使用规则引擎归因")

    @property
    def is_enabled(self) -> bool:
        return bool(self.api_key)

    def _init_rag_from_db(self) -> None:
        """从数据库加载历史归因案例，构建 RAG 索引。"""
        if self._db is None:
            return
        try:
            cases = self._db.get_analysis_history(limit=500)
            for case in cases:
                self._rag_index.add({
                    "seq": case.get("seq"),
                    "text": f"{case.get('reasons','')} {case.get('root_cause','')} {case.get('category','')}",
                    "reasons": case.get("reasons", ""),
                    "root_cause": case.get("root_cause", ""),
                    "category": case.get("category", ""),
                })
        except Exception as e:
            log.debug("RAG 初始化跳过（表可能不存在）: %s", e)

    def _query_rag(self, reasons: list[str]) -> list[dict[str, Any]]:
        """检索与当前异常相似的历史案例。"""
        query_text = " ".join(reasons)
        return self._rag_index.search(query_text, top_k=self.rag_top_k)

    def _call_llm(self, prompt: str) -> tuple[dict[str, Any] | None, dict[str, int]]:
        """调用 OpenAI 兼容 API。返回 (解析后的 JSON dict, token 用量)。

        token 用量 dict 含 prompt_tokens / completion_tokens / total_tokens，
        调用失败时全为 0。
        """
        empty_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            import urllib.request
            import urllib.error

            payload = json.dumps({
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": config.LLM_TEMPERATURE,
                "max_tokens": config.LLM_MAX_TOKENS,
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"].strip()

                # 提取 token 用量（OpenAI 兼容 API 标准字段）
                usage = data.get("usage", {})
                tokens = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }

                # 尝试提取 JSON（兼容 markdown 包裹）
                json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group()), tokens
                return None, tokens
        except Exception as e:
            log.warning("LLM 调用失败，降级为规则引擎: %s", e)
            return None, empty_tokens

    def analyze(self, frame: Frame, reasons: list[str]) -> AnalysisResult:
        """对一帧异常数据做归因分析。

        有 API key → 调 LLM + RAG 上下文
        无 API key → 降级为规则引擎
        """
        # RAG 检索（无论是否调 LLM，都检索以记录参考数）
        rag_cases = self._query_rag(reasons)
        rag_count = len(rag_cases)

        if not self.is_enabled:
            result = _rule_based_analysis(frame, reasons)
            result.rag_context_count = rag_count
            return result

        # 构建 prompt 并调用 LLM
        prompt = _build_prompt(frame, reasons, rag_cases)
        t0 = time.time()
        llm_result, tokens = self._call_llm(prompt)
        latency = int((time.time() - t0) * 1000)

        if llm_result is None:
            # LLM 调用失败，降级
            result = _rule_based_analysis(frame, reasons)
            result.rag_context_count = rag_count
            return result

        return AnalysisResult(
            seq=frame.seq,
            root_cause=llm_result.get("root_cause", "LLM 未返回根因"),
            confidence=float(llm_result.get("confidence", 0.5)),
            category=llm_result.get("category", "UNKNOWN"),
            recommendation=llm_result.get("recommendation", "建议人工排查"),
            source="llm",
            rag_context_count=rag_count,
            latency_ms=latency,
            prompt_tokens=tokens.get("prompt_tokens", 0),
            completion_tokens=tokens.get("completion_tokens", 0),
            total_tokens=tokens.get("total_tokens", 0),
        )

    def add_to_rag(self, frame: Frame, reasons: list[str], result: AnalysisResult) -> None:
        """将已分析的异常案例加入 RAG 索引，供后续检索。"""
        text = f"{' '.join(reasons)} {result.root_cause} {result.category}"
        self._rag_index.add({
            "seq": frame.seq,
            "text": text,
            "reasons": str(reasons),
            "root_cause": result.root_cause,
            "category": result.category,
        })
