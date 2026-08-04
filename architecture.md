# 仿真测试数据生成器 + 数据质量门禁 + LLM 异常归因

> 基于 P1（C+Python 传感器模拟器）改造的**轻量仿真测试基础设施**。  
> 零 GPU、不碰 CARLA，用 C 强项做"假传感器制造机"，Python 做数据质量门禁与 LLM 异常归因。  
> 一份项目，可投：**智驾仿真测试（主）/ 智能驾驶算法·仿真工程师 / AI 测试 / AI 应用开发 / RAG 开发**。

---

## 一、项目定位与解决的需求

**打个比方**：自动驾驶公司要测算法，但不能天天开真车上路、也不能买一堆真传感器。  
你的程序 = 一台"假传感器制造机"——用 C 不断吐出传感器数字（速度/距离/加速度），  
通过管道流给 Python，Python 负责**保证假数据可信**（质量门禁）并**对外提供数据**（HTTP 接口）。

**解决的真实需求**：让测试团队在没有真实硬件时，也能稳定拿到**可信、可复现**的测试数据，  
并系统化地验证"数据管道 + 决策算法"在异常工况下的健壮性。

---

## 二、模块划分

```
┌─────────────┐  命名管道    ┌──────────────────┐   ┌──────────────┐
│  C 生成器    │ ──IPC──→    │  Python 消费者    │ → │  SQLite 数据库 │
│ (假传感器)   │  (二进制帧) │  - 帧解析         │   └──────────────┘
└─────────────┘             │  - 质量门禁       │ → ┌──────────────┐
                            │  - LLM 异常归因   │   │  FastAPI 接口 │
                            │  - 入库           │   └──────────────┘
                            └──────────────────┘         │
                                   ↑ RAG                  ↓
                            ┌──────────────┐       ┌────┴────┐
                            │ TF-IDF 检索器 │       │ Web 看板 │
                            └──────────────┘       └─────────┘
```



| 模块      | 语言             | 职责                   |
| ------- | -------------- | -------------------- |
| 数据生成器   | C              | 按帧格式生成合成传感器流 + 注入异常  |
| 帧解析     | Python         | 按协议 spec 解析二进制帧      |
| 质量门禁    | Python         | 缺失/越界/CRC/丢帧校验，统计合格率 |
| LLM 归因  | Python         | 异常帧语义归因 + TF-IDF RAG 检索历史案例 |
| 数据库     | Python/SQLite  | 落库（帧+归因），支持查询，索引优化   |
| HTTP 接口 | Python/FastAPI | 对外提供数据/统计/归因结果，异常中间件 |
| 评测框架    | Python         | 数据质量 + LLM 归因质量评测报告（MD + HTML） |
| UI 看板   | Web            | 实时展示数据流+合格率曲线+指数退避重试 |
| 日志系统    | Python         | 集中式 logging，统一格式与级别   |
| 配置中心    | Python         | 环境变量驱动的参数外置，零硬编码     |

---

## 三、C ↔ Python 接口契约（二进制帧格式）★核心

所有数据以**二进制帧**通过命名管道传输。字节序统一 **小端（Little-Endian）**。

### 帧结构

| 字段          | 类型                | 字节  | 说明                                     |
| ----------- | ----------------- | --- | -------------------------------------- |
| MAGIC       | uint8[2]          | 2   | 固定 `0xAA 0x55`，用于帧同步                   |
| LENGTH      | uint16 LE         | 2   | 从 SEQ 到 TRAILER 的总字节数（不含 MAGIC/LENGTH） |
| SEQ         | uint32 LE         | 4   | 帧序号，单调递增，**用于检测丢帧**                    |
| TIMESTAMP   | uint64 LE         | 8   | 毫秒时间戳                                  |
| SENSOR_TYPE | uint8             | 1   | 1=雷达距离 2=IMU加速度 3=GPS速度                |
| N_SAMPLES   | uint8             | 1   | 本帧采样通道数                                |
| PAYLOAD     | (uint8+float32)×N | 5×N | 每个采样：channel(1) + value(4, IEEE754)    |
| CRC32       | uint32 LE         | 4   | 对 MAGIC..PAYLOAD 全部字节做 CRC32           |
| TRAILER     | uint8             | 1   | 固定 `0xEE`，帧结束标志                        |

**一帧总字节数** = 2 + 2 + 4 + 8 + 1 + 1 + 5×N + 4 + 1 = 23 + 5×N

### 物理取值范围（质量门禁用）

| 通道           | 合理区间      | 单位   |
| ------------ | --------- | ---- |
| 距离(Distance) | [0, 300]  | m    |
| 速度(Speed)    | [0, 300]  | km/h |
| 加速度(Accel)   | [-20, 20] | m/s² |

### 异常注入（测试质量门禁用）

C 生成器按概率注入：①CRC 错误 ②SEQ 跳变（丢帧）③value 越界 ④NaN/缺失 ⑤噪声超标。  
Python 门禁必须能识别并拦截，统计各类拦截数。

---

## 四、模块间数据结构约定（Python 侧）

解析后统一为：

```python
Sample = {"channel": int, "value": float}
Frame = {
    "seq": int,
    "timestamp": int,
    "sensor_type": int,
    "samples": list[Sample],
    "crc_ok": bool,
}
QualityResult = {
    "passed": bool,
    "reasons": list[str],   # 拦截原因，如 ["CRC_FAIL","OUT_OF_RANGE:speed"]
}
AnalysisResult = {
    "seq": int,
    "root_cause": str,        # 根因分析（1-2句）
    "confidence": float,      # 置信度 0.0-1.0
    "category": str,          # CRC_FAIL/SEQ_GAP/OUT_OF_RANGE/NAN_VALUE/NOISE/UNKNOWN
    "recommendation": str,    # 处置建议
    "source": str,            # "llm" 或 "rule"（降级时）
    "rag_context_count": int, # RAG 检索到的相似案例数
    "latency_ms": int,        # LLM 调用耗时（毫秒）
    "prompt_tokens": int,     # LLM 输入 token 数
    "completion_tokens": int, # LLM 输出 token 数
    "total_tokens": int,      # 合计 token 数（用于成本统计）
}
```

HTTP 接口约定（FastAPI）：

- `GET /frames?limit=100` → 最近 N 帧原始数据
- `GET /stats` → 合格率、丢帧率、各拦截原因计数、LLM 归因统计
- `GET /analysis?limit=100` → 最近 N 条异常归因结果
- `WS /stream` → 实时帧推送（供前端看板）

---

## 四点五、LLM 异常归因 + RAG 检索

当质量门禁拦截到异常帧时，自动触发 LLM 语义归因流程：

```
异常帧 → ① RAG 检索历史相似案例 → ② 组装 Prompt（异常信息+历史案例）
       → ③ 调用 LLM → ④ 解析结构化 JSON → ⑤ 存库 + 加入 RAG 索引
```

**TF-IDF RAG 检索器**（`TfidfIndex`）：纯 Python 标准库实现，不依赖 embedding 服务或向量数据库。
中英文混合分词，cosine 相似度排序，适合案例量 < 10k 的场景。每次归因完成后将新案例加入索引，
实现自学习增强。

**降级机制**：未配置 `LLM_API_KEY` 时自动降级为规则引擎归因（基于异常类型匹配预设规则），
保证项目零配置可独立运行。LLM 调用失败时同样降级。

**并发优化**：LLM 归因采用 `ThreadPoolExecutor` 分批并发调用（默认 8 线程），
RAG 检索在线程内只读访问索引（线程安全），每批完成后串行入库 + 加入 RAG 索引。
实测 500 帧数据灌入从 11 分钟缩短至 31 秒。

**评测指标**（`tools/evaluate.py`）：
- 归因准确率（LLM 分类是否命中门禁原因）
- 平均置信度
- RAG 利用率（检索到相似案例的归因占比）
- LLM 平均延迟
- Token 消耗（prompt / completion / total）

---

## 四点六、工程实践

**日志系统**（`src/logger.py`）：集中式 `logging` 配置，统一格式 `时间 [级别] 模块: 消息`，
通过 `LOG_LEVEL` 环境变量控制输出级别。所有模块通过 `get_logger(__name__)` 获取 logger。

**配置外置**（`src/config.py`）：所有硬编码参数（线程数、端口、DB 路径、超时、轮询间隔等）
提取为 `Config` 类属性，支持环境变量覆盖。避免参数散落在各文件中。

**数据库索引**：`frames` 和 `llm_analysis` 表均建立查询索引（id/seq/passed/source），
提升看板轮询与评测查询性能。

**API 容错**：FastAPI 全局异常处理中间件，捕获未处理异常返回 500 JSON 而非裸 500；
请求级耗时日志便于性能排查。

**看板重试**：前端轮询采用指数退避策略（1s → 2s → 4s → max 30s），
单次请求 5s 超时（AbortController），连接状态显示重试倒计时。

**CI/CD**：GitHub Actions 自动化测试（`.github/workflows/ci.yml`），
push / PR 触发 49 个 pytest 测试，Python 3.11 环境验证。

---

## 五、阶段计划

- W1-2：C 生成器（帧格式+IPC+异常注入）
- W2-3：Python 消费+质量门禁+DB+HTTP
- W3：UI 看板
- W4：联调端到端
- W5：评测框架+报告
- W6：README+demo
