# 仿真测试数据生成器 + 数据质量门禁 + LLM 异常归因

基于 P1（C+Python 传感器模拟器）改造的**轻量仿真测试基础设施**。**零 GPU、不碰 CARLA**，
用 C 做"假传感器制造机"，Python 做数据质量门禁与 LLM 异常归因。

**可投岗位**：智驾仿真测试（主）/ 智能驾驶算法·仿真工程师 / AI 测试 / AI 应用开发 / RAG 开发

> 详细架构、二进制帧协议、模块接口契约 → 见 **[architecture.md](architecture.md)**

## 目录结构
```
sensor-sim-test/
├── architecture.md        # 架构契约 + 模块接口（事实源）
├── requirements.txt
├── .github/workflows/
│   └── ci.yml             # GitHub Actions CI（push/PR 自动跑 49 个测试）
├── src/
│   ├── protocol.h         # C 侧帧格式定义
│   ├── sensor_sim.c        # C 数据生成器（真实物理模型 + 5 类异常 + CLI + 可复现）
│   ├── protocol.py         # Python 帧解析（与 C 严格对应）
│   ├── quality_gate.py     # 数据质量门禁（CRC/丢帧/越界/NaN）
│   ├── llm_analyzer.py     # LLM 异常归因 + TF-IDF RAG 检索 + 并发调用
│   ├── database.py         # SQLite 入库 + 统计 + 归因存储 + 查询索引
│   ├── server.py           # FastAPI HTTP 接口 + 异常中间件
│   ├── consumer.py         # 消费者主程序（管道→解析→门禁→归因→入库）
│   ├── logger.py           # 集中式日志配置（logging 替代 print）
│   └── config.py           # 配置外置（环境变量驱动，零硬编码）
├── tools/
│   ├── evaluate.py         # 评测框架（MD + HTML 报告，含 LLM 归因质量评测）
│   └── seed_data.py        # 数据灌入工具
├── dashboard/
│   └── index.html          # 实时看板（KPI + 趋势图 + 归因列表 + 指数退避重试）
└── tests/                  # 49 个 pytest 单测（protocol/quality_gate/database/llm_analyzer）
    ├── conftest.py
    ├── test_protocol.py
    ├── test_quality_gate.py
    ├── test_database.py
    └── test_llm_analyzer.py
```

## 快速开始
```bash
# 1) 编译 C 生成器（MinGW/GCC）
gcc -O2 -o sensor_sim.exe src/sensor_sim.c

# 2) 跑管道：C 生成 → Python 消费入库
#    ⚠️ 注意：PowerShell 的 "|" 对二进制流不友好，请用 CMD 或 Git Bash 运行
sensor_sim.exe --frames 500 --seed 42 | python src/consumer.py

# 3) 起 HTTP 接口（从项目根目录运行，需把 src 加进 PYTHONPATH）
pip install -r requirements.txt
set PYTHONPATH=src          # Windows CMD；PowerShell 用 $env:PYTHONPATH="src"
uvicorn server:app --port 8000
#    （不想记命令？双击根目录 start-server.bat / seed-data.bat 一键完成）
#   浏览器打开 http://localhost:8000/docs
#   GET /stats        → 合格率、丢帧率、拦截分布、LLM 归因统计
#   GET /frames       → 最近帧
#   GET /analysis     → 最近异常归因结果（根因/置信度/类别/建议）

# 4) 跑单测（验证门禁逻辑，无需 C/显卡）
python -m pytest tests/ -v

# 5) 跑评测报告（含 LLM 归因质量评测）
python tools/evaluate.py --db sensor.db --out report.md
#    可选：生成自包含 HTML 报告（内联 CSS/JS，可直接浏览器打开）
python tools/evaluate.py --db sensor.db --out report.md --html report.html
```

## 配置

所有参数通过环境变量配置（`src/config.py`），均有合理默认值，零配置即可运行：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | （空） | LLM API 密钥，不设则降级为规则引擎 |
| `LLM_WORKERS` | 8 | LLM 并发线程数 |
| `LLM_TIMEOUT` | 15 | LLM API 超时秒数 |
| `SERVER_PORT` | 8000 | API 服务器端口 |
| `DB_PATH` | sensor.db | SQLite 数据库路径 |
| `LOG_LEVEL` | INFO | 日志级别（DEBUG/INFO/WARNING/ERROR） |

## LLM 异常归因（可选）

质量门禁拦截到异常帧后，自动调用 LLM 做语义归因，输出结构化结果（根因 / 置信度 / 分类 / 处置建议）。
内置 **TF-IDF RAG 检索**，从历史相似案例中提取上下文增强归因质量。

不设 API key 时自动降级为**规则引擎归因**，项目可零配置独立运行。

```bash
# 配置环境变量启用 LLM（兼容 OpenAI API 格式）
set LLM_API_KEY=sk-xxx          # API 密钥
set LLM_BASE_URL=https://api.deepseek.com/v1   # 默认 DeepSeek
set LLM_MODEL=deepseek-chat     # 默认 deepseek-chat
```

> 不想每次手动 set？把 `LLM_API_KEY=xxx` 写进项目根目录的 `.env` 文件即可，程序启动时自动加载（无需设置环境变量）。
