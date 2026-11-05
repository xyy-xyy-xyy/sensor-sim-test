# 仿真测试数据生成器 + 数据质量门禁 + LLM 异常归因

仿真测试里真实传感器数据难获取、数据质量无监控、异常靠人工排查。这个项目把传感器模拟、质量门禁、LLM 异常归因串成一条流水线，让测试团队在没有真实硬件时也能稳定拿到**可信、可复现**的测试数据，并系统化地验证"数据管道 + 决策算法"在异常工况下的健壮性。

> 零 GPU、不碰 CARLA，用 C 写"假传感器制造机"，Python 做数据质量门禁与 LLM 异常归因。

[![CI](https://github.com/xyy-xyy-xyy/sensor-sim-test/actions/workflows/ci.yml/badge.svg)](https://github.com/xyy-xyy-xyy/sensor-sim-test/actions)

> 详细架构、二进制帧协议、模块接口契约 → 见 **[architecture.md](architecture.md)**

## ✨ 核心特性

- **多传感器仿真（C）**：雷达 / IMU / GPS 三类传感器，基于物理模型生成二进制数据流，支持 5 类故障注入（CRC 损坏 / 丢帧 / 越界 / NaN / 噪声），`--seed` 可复现。
- **数据质量门禁（Python）**：4 类拦截（CRC_FAIL / SEQ_GAP / OUT_OF_RANGE / NAN_VALUE），把"坏数据"从流水线上拦下来——这是仿真测试的核心思维。
- **LLM 异常归因 + RAG**：门禁拦到坏帧后，自动调 LLM 做语义归因（根因 / 置信度 / 类别 / 建议），并用**纯标准库 TF-IDF** 检索历史相似案例增强归因质量。无 API key 时自动降级为规则引擎，零配置也能跑。
- **全链路打通**：C（二进制流）→ Python 消费 → MySQL 8 → FastAPI → 实时看板，一个人把整条链路串起来。
- **可观测性**：看板展示合格率、拦截分布、**LLM Token 消耗**、平均延迟、RAG 命中数；后端用 `logging` 结构化日志（非裸 `print`）。
- **工程化**：配置外置（环境变量，`config.py`）、Docker 多阶段构建 + docker-compose 一键起服务、GitHub Actions CI（单测 + MySQL 集成测试）、全局异常兜底返回 JSON、看板指数退避重试。

## 目录结构
```
sensor-sim-test/
├── architecture.md        # 架构契约 + 模块接口（事实源）
├── Dockerfile             # 多阶段构建：gcc 编译 C → Python 运行时
├── docker-compose.yml     # MySQL 8 + 后端服务（宿主机端口 3307）
├── .dockerignore
├── .env.example           # 环境变量模板（MySQL 账号 / LLM key）
├── requirements.txt       # fastapi / uvicorn / pymysql / DBUtils / pytest
├── LICENSE                # MIT
├── .github/workflows/
│   └── ci.yml             # CI：单测（SQLite 夹具）+ MySQL 集成测试
├── src/
│   ├── protocol.h         # C 侧帧格式定义
│   ├── sensor_sim.c        # C 数据生成器（真实物理模型 + 5 类异常 + CLI + 可复现）
│   ├── protocol.py         # Python 帧解析（与 C 严格对应）
│   ├── quality_gate.py     # 数据质量门禁（CRC/丢帧/越界/NaN）
│   ├── llm_analyzer.py     # LLM 异常归因 + TF-IDF RAG 检索（纯标准库）
│   ├── database.py         # MySQL 8 存储层（pymysql + DBUtils 连接池、建表、索引）
│   ├── server.py           # FastAPI HTTP 接口 + 异常中间件
│   ├── consumer.py         # 消费者主程序（管道→解析→门禁→归因→入库，并发批处理）
│   ├── logger.py           # 集中式日志配置（logging 替代 print）
│   └── config.py           # 配置外置（环境变量驱动，零硬编码）
├── tools/
│   ├── evaluate.py         # 评测框架（MD + HTML 报告，含 LLM 归因质量评测）
│   └── seed_data.py        # 数据灌入工具（无 gcc 时的 Python 兜底生成器）
├── dashboard/
│   └── index.html          # 实时看板（后端托管 · KPI/趋势图/归因列表/筛选/深色模式/暂停刷新）
├── tests/                  # 60 个单测 + 9 个 MySQL 集成测试
│   ├── conftest.py
│   ├── sqlite_fixture.py  # 单测专用 SQLite 夹具（不参与生产运行）
│   ├── test_protocol.py
│   ├── test_quality_gate.py
│   ├── test_database.py
│   ├── test_llm_analyzer.py
│   └── test_mysql_integration.py
├── seed-data.bat          # 一键：清空旧库 + 灌 500 帧（自动读 .env 的 LLM key）
└── start-server.bat       # 一键：启动 FastAPI 后端 + 托管看板（http://localhost:8000）
```

## 快速开始（Docker，推荐）

```bash
cp .env.example .env        # Windows: copy .env.example .env，按需改密码

# 1) 起 MySQL 8 + 后端（MySQL 映射到宿主机 3307，避开本机已有 3306）
docker compose up -d --build

# 2) 灌 500 帧样例数据（C 生成器 → Python 消费 → MySQL）
docker compose run --rm seed

# 3) 验证
curl http://localhost:8000/health
curl http://localhost:8000/stats
#    浏览器打开 http://localhost:8000/      → 实时看板
#    http://localhost:8000/docs            → Swagger API 文档

# 4) 跑测试（单测用 SQLite 夹具，不碰 MySQL）
docker compose exec app python -m pytest tests/ -v

# 5) 跑 MySQL 集成测试（独立库 sensor_test，不影响演示数据 sensor）
docker compose exec -e MYSQL_DATABASE=sensor_test app python -m pytest tests/test_mysql_integration.py -v
```

> 只看容器状态：`docker compose ps`；连 MySQL：宿主机 `127.0.0.1:3307`；清空数据卷：`docker compose down -v`。

## 快速开始（本地，已有 MySQL）

```bash
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt

# 1) 编译 C 生成器（MinGW/GCC），产物 sensor_sim.exe 落在项目根目录
gcc -O2 -o sensor_sim.exe src/sensor_sim.c

# 2) 配置 MySQL 连接（默认 127.0.0.1:3307，即 docker-compose 映射端口）
set MYSQL_HOST=127.0.0.1
set MYSQL_PORT=3307
set MYSQL_USER=sensor
set MYSQL_PASSWORD=sensor_dev_pwd
set MYSQL_DATABASE=sensor
#    若用本机 3306 的 MySQL，改 MYSQL_PORT=3306 并换成你自己的账号密码

# 3) 跑管道：C 生成 → Python 消费入库（--reset 自动清空旧数据）
sensor_sim.exe --frames 500 --seed 42 | python src/consumer.py --reset
#    ⚠️ PowerShell 的 "|" 对二进制流不友好，请用 CMD 或 Git Bash 运行

# 4) 起 HTTP 接口（从项目根目录运行，需把 src 加进 PYTHONPATH）
set PYTHONPATH=src          # PowerShell 用 $env:PYTHONPATH="src"
uvicorn server:app --port 8000

# 5) 跑单测（不依赖 MySQL，SQLite 夹具，无需 C/显卡）
python -m pytest tests/ -v
#    跑 MySQL 集成测试（需要独立测试库 sensor_test，不会动 sensor 演示数据）：
set MYSQL_HOST=127.0.0.1
set MYSQL_PORT=3307
set MYSQL_USER=sensor
set MYSQL_PASSWORD=123456
set MYSQL_DATABASE=sensor_test
set MYSQL_ROOT_PASSWORD=123456
python -m pytest tests/test_mysql_integration.py -v

# 6) 跑评测报告（含 LLM 归因质量评测，数据从 MySQL 读取）
python tools/evaluate.py --out report.md
python tools/evaluate.py --out report.md --html report.html   # 可选自包含 HTML
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
| `MYSQL_HOST` | `127.0.0.1` | MySQL 主机 |
| `MYSQL_PORT` | `3307` | MySQL 端口（对应 docker-compose 宿主机映射） |
| `MYSQL_USER` | `sensor` | MySQL 用户 |
| `MYSQL_PASSWORD` | `sensor_dev_pwd` | MySQL 密码（仅本地开发默认值） |
| `MYSQL_DATABASE` | `sensor` | MySQL 库名 |
| `MYSQL_POOL_SIZE` | 5 | 连接池最大连接数 |
| `DB_CONNECT_RETRIES` / `DB_CONNECT_DELAY` | 10 / 2 | 启动连接重试次数与间隔（秒） |
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

- **60 个 pytest 单测**覆盖 `protocol` / `quality_gate` / `database` / `llm_analyzer` / `human_verdict` / `sanitize`，使用 **SQLite 夹具**（`tests/sqlite_fixture.py`）保证零外部依赖、毫秒级完成，**无需 C 编译器、无需显卡、无需 API key、无需 MySQL**。
- **9 个 MySQL 集成测试**（`tests/test_mysql_integration.py`）跑在真实 MySQL 8 上，覆盖单测覆盖不到的方言差异：`rowcount` 语义、`DECIMAL` 转 float、`DATETIME` 序列化等。未设 `MYSQL_HOST` 时自动跳过。
- **GitHub Actions**（`ci.yml`）：`test` job 跑全量单测；`mysql-integration` job 起 MySQL 8 服务跑集成测试，保证生产路径不是"假绿"。
- 本地跑：先 `set PYTHONPATH=src`，再 `python -m pytest tests/ -v`。

## 评测报告

`tools/evaluate.py` 从 MySQL 读取数据，输出合格率、拦截分布、最大序列间隔、归因准确率 / RAG 利用率 / 平均延迟等指标，支持 Markdown 与自包含 HTML 两种格式。

## 安全说明

- `.env`（含 `LLM_API_KEY` 与 MySQL 密码）、`*.db`、`*.db.bak`、`sensor_sim.exe`、`frames.bin` 均已在 `.gitignore` 中，**不会**被推到 GitHub；`.dockerignore` 同样排除 `.env`，**不会**被打进镜像（容器通过环境变量注入）。
- LLM 调用通过标准库 `urllib` 直连 API，不引入第三方 HTTP / 向量库依赖。
- 容器以非 root 用户（`appuser`）运行，并内置 `/health` 健康检查。
