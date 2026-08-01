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
├── src/
│   ├── protocol.h         # C 侧帧格式定义
│   ├── sensor_sim.c        # C 数据生成器（真实物理模型 + 5 类异常 + CLI + 可复现）
│   ├── protocol.py         # Python 帧解析（与 C 严格对应）
│   ├── quality_gate.py     # 数据质量门禁（CRC/丢帧/越界/NaN）
│   ├── llm_analyzer.py     # LLM 异常归因 + TF-IDF RAG 检索
│   ├── database.py         # SQLite 入库 + 统计 + 归因结果存储
│   ├── server.py           # FastAPI HTTP 接口（/stats /frames /analysis）
│   └── consumer.py         # 消费者主程序（管道→解析→门禁→归因→入库）
├── tools/
│   ├── evaluate.py         # 评测框架（含 LLM 归因质量评测）
│   └── seed_data.py        # 数据灌入工具
└── tests/
    ├── conftest.py
    └── test_quality_gate.py
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
```

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

## 简历一句话
> 设计二进制帧协议（帧头/长度/CRC/小端）实现 C-Python 仿真数据管道；编写数据质量门禁
> 拦截 CRC/丢帧/越界/NaN 四类异常；集成 LLM + TF-IDF RAG 做异常语义归因，输出根因/置信度/处置建议，
> 统计合格率并对外提供 HTTP 测试接口。
