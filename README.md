# 仿真测试数据生成器 + 数据质量门禁 + LLM 异常归因

基于 （C + Python 传感器模拟器）改造的**轻量仿真测试基础设施**。

> **定位**：零 GPU、不碰 CARLA，用 C 写"假传感器制造机"，Python 做数据质量门禁与 LLM 异常归因。适合作为**智驾/机器人仿真测试、AI 测试、数据质量、RAG 应用**方向的求职作品集。

[![CI](https://github.com/xyy-xyy-xyy/sensor-sim-test/actions/workflows/ci.yml/badge.svg)](https://github.com/xyy-xyy-xyy/sensor-sim-test/actions)



> 详细架构、二进制帧协议、模块接口契约 → 见 **[architecture.md](architecture.md)**

## ✨ 核心特性

- **多传感器仿真（C）**：雷达 / IMU / GPS 三类传感器，基于物理模型生成二进制数据流，支持 5 类故障注入（CRC 损坏 / 丢帧 / 越界 / NaN / 噪声），`--seed` 可复现。
- **数据质量门禁（Python）**：4 类拦截（CRC_FAIL / SEQ_GAP / OUT_OF_RANGE / NAN_VALUE），把"坏数据"从流水线上拦下来——这是仿真测试的核心思维。
- **LLM 异常归因 + RAG**：门禁拦到坏帧后，自动调 LLM 做语义归因（根因 / 置信度 / 类别 / 建议），并用**纯标准库 TF-IDF** 检索历史相似案例增强归因质量。无 API key 时自动降级为规则引擎，零配置也能跑。
- **全链路打通**：C（二进制流）→ Python 消费 → SQLite → FastAPI → 实时看板，一个人把整条链路串起来。
- **可观测性**：看板展示合格率、拦截分布、**LLM Token 消耗**、平均延迟、RAG 命中数；后端用 `logging` 结构化日志（非裸 `print`）。
- **工程化**：配置外置（环境变量，`config.py`）、GitHub Actions CI（push/PR 自动跑 60 个 pytest）、全局异常兜底返回 JSON、看板指数退避重试。

## 目录结构
```
sensor-sim-test/
├── architecture.md        # 架构契约 + 模块接口（事实源）
├── docs/
│   └── 求职材料.md        # 简历项目描述（双视角）+ 面试逐字稿 + 自我介绍
├── requirements.txt       # 仅 fastapi / uvicorn / pytest，零额外依赖
├── LICENSE                # MIT
├── .github/workflows/
│   └── ci.yml             # GitHub Actions CI（push/PR 自动跑 60 个测试）
├── src/
│   ├── protocol.h         # C 侧帧格式定义
│   ├── sensor_sim.c        # C 数据生成器（真实物理模型 + 5 类异常 + CLI + 可复现）
│   ├── protocol.py         # Python 帧解析（与 C 严格对应）
│   ├── quality_gate.py     # 数据质量门禁（CRC/丢帧/越界/NaN）
│   ├── llm_analyzer.py     # LLM 异常归因 + TF-IDF RAG 检索（纯标准库）
│   ├── database.py         # SQLite 入库 + 统计 + 归因存储 + 查询索引
│   ├── server.py           # FastAPI HTTP 接口 + 异常中间件
│   ├── consumer.py         # 消费者主程序（管道→解析→门禁→归因→入库，并发批处理）
│   ├── logger.py           # 集中式日志配置（logging 替代 print）
│   └── config.py           # 配置外置（环境变量驱动，零硬编码）
├── tools/
│   ├── evaluate.py         # 评测框架（MD + HTML 报告，含 LLM 归因质量评测）
│   └── seed_data.py        # 数据灌入工具（无 gcc 时的 Python 兜底生成器）
├── dashboard/
│   └── index.html          # 实时看板（后端托管 · KPI/趋势图/归因列表/筛选/深色模式/暂停刷新）
├── tests/                  # 60 个 pytest 单测（protocol/quality_gate/database/llm_analyzer/human_verdict/sanitize）
│   ├── conftest.py
│   ├── test_protocol.py
│   ├── test_quality_gate.py
│   ├── test_database.py
│   └── test_llm_analyzer.py
├── seed-data.bat          # 一键：清空旧库 + 灌 500 帧（自动读 .env 的 LLM key）
└── start-server.bat       # 一键：启动 FastAPI 后端 + 托管看板（http://localhost:8000）
```

## 快速开始

```bash
# 0) （推荐）建虚拟环境并装依赖
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
#    PowerShell 激活：.venv\Scripts\Activate.ps1 ；CMD 激活：.venv\Scripts\activate.bat

# 1) 编译 C 生成器（MinGW/GCC），产物 sensor_sim.exe 落在项目根目录
gcc -O2 -o sensor_sim.exe src/sensor_sim.c

# 2) 跑管道：C 生成 → Python 消费入库（默认 500 帧，--reset 自动清空旧数据；--frames 可改任意数量）
sensor_sim.exe --frames 500 --seed 42 | python src/consumer.py --reset
#    ⚠️ 注意：PowerShell 的 "|" 对二进制流不友好，请用 CMD 或 Git Bash 运行

# 3) 起 HTTP 接口（从项目根目录运行，需把 src 加进 PYTHONPATH）
set PYTHONPATH=src          # Windows CMD；PowerShell 用 $env:PYTHONPATH="src"
uvicorn server:app --port 8000
#    浏览器打开 http://localhost:8000/   → 实时看板（深色模式/筛选搜索/暂停刷新）
#    http://localhost:8000/docs          → Swagger API 文档
#    GET /stats      → 合格率、丢帧率、拦截分布、LLM 归因统计、Token 消耗
#    GET /frames     → 最近帧
#    GET /analysis   → 最近异常归因结果（根因/置信度/类别/建议/Token）
#    POST /analysis/{seq}/verify  → 人工打标（verdict=1 正确 / 0 错误，可选 category 更正类别），形成评测闭环

# 4) 跑单测（验证门禁逻辑，无需 C/显卡）
python -m pytest tests/ -v

# 5) 跑评测报告（含 LLM 归因质量评测）
python tools/evaluate.py --db sensor.db --out report.md
python tools/evaluate.py --db sensor.db --out report.md --html report.html   # 可选自包含 HTML
```

> 不想记命令？双击 `seed-data.bat` 灌数据 → 双击 `start-server.bat` 起后端 → 浏览器打开 http://localhost:8000/ 即得实时看板（后端已直接托管 dashboard，无需再单独打开 index.html）。
> 没有 gcc？用 `python tools/seed_data.py --frames 500` 直接灌 Python 生成的样例数据看效果。

## 配置

所有参数通过环境变量配置（`src/config.py`），均有合理默认值，零配置即可运行：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | （空） | LLM API 密钥，不设则降级为规则引擎 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 兼容 OpenAI 格式的 API 地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_WORKERS` | 8 | LLM 并发线程数 |
| `LLM_TIMEOUT` | 15 | LLM API 超时秒数 |
| `LLM_MAX_TOKENS` | 300 | 单次最大输出 token |
| `LLM_TEMPERATURE` | 0.3 | 采样温度 |
| `LLM_RAG_TOP_K` | 3 | RAG 检索返回的相似案例数 |
| `SERVER_HOST` / `SERVER_PORT` | 0.0.0.0 / 8000 | API 监听地址与端口 |
| `DB_PATH` | sensor.db | SQLite 数据库路径 |
| `LOG_LEVEL` | INFO | 日志级别（DEBUG/INFO/WARNING/ERROR） |

## LLM 异常归因（可选）

质量门禁拦截到异常帧后，自动调用 LLM 做语义归因，输出结构化结果（根因 / 置信度 / 分类 / 处置建议），
并采集 **Token 消耗**（prompt / completion / total）在看板"Token 消耗"卡片展示。
内置 **TF-IDF RAG 检索**，从历史相似案例中提取上下文增强归因质量。
看板支持**人工打标**（✓/✗）确认归因结果，报告据此计算"LLM 归因与人工一致率"，形成评测闭环。

不设 API key 时自动降级为**规则引擎归因**，项目可零配置独立运行。

```bash
# 方式 A：手动设环境变量
set LLM_API_KEY=sk-xxx          # API 密钥
set LLM_BASE_URL=https://api.deepseek.com/v1   # 默认 DeepSeek
set LLM_MODEL=deepseek-chat     # 默认 deepseek-chat

# 方式 B：写进项目根目录的 .env（程序启动时自动加载，无需每次 set）
#   LLM_API_KEY=sk-xxx
```

## 测试与 CI

- **60 个 pytest 单测**覆盖 `protocol` / `quality_gate` / `database` / `llm_analyzer` / `human_verdict` / `sanitize`，在 Python 侧按协议构建帧（与 `sensor_sim.c` 同格式），**无需 C 编译器、无需显卡、无需 API key**。
- **GitHub Actions**（`ci.yml`）：push / PR 到 `main` 时自动 `pip install` + 跑全量测试。
- 本地跑：先 `set PYTHONPATH=src`，再 `python -m pytest tests/ -v`。

## 评测报告

`tools/evaluate.py` 读取 `sensor.db`，输出合格率、拦截分布、最大序列间隔、归因准确率 / RAG 利用率 / 平均延迟等指标，支持 Markdown 与自包含 HTML 两种格式。

## 安全说明

- `.env`（含 `LLM_API_KEY`）、`sensor.db`、`.db.bak`、`sensor_sim.exe`、`frames.bin` 均已在 `.gitignore` 中，**不会**被推到 GitHub，密钥与本地数据不会泄露。
- LLM 调用通过标准库 `urllib` 直连 API，不引入第三方 HTTP / 向量库依赖。
