# 仿真测试数据生成器 + 数据质量门禁

基于 P1（C+Python 传感器模拟器）改造的**轻量仿真测试基础设施**。**零 GPU、不碰 CARLA**，
用 C 做"假传感器制造机"，Python 做数据质量门禁与接口。

**可投岗位**：智驾仿真测试（主）/ 智能驾驶算法·仿真工程师 / AI 测试 / AI 应用开发

> 详细架构、二进制帧协议、模块接口契约、多工具协同流程 → 见 **[architecture.md](architecture.md)**

## 目录结构
```
sensor-sim-test/
├── architecture.md        # 架构契约 + 多工具协同流程（事实源）
├── requirements.txt
├── src/
│   ├── protocol.h         # C 侧帧格式定义
│   ├── sensor_sim.c        # C 数据生成器（Claude Code 深化完成：真实物理模型 + 5 类异常 + CLI + 可复现）
│   ├── protocol.py         # Python 帧解析（与 C 严格对应）
│   ├── quality_gate.py     # 数据质量门禁（CRC/丢帧/越界/NaN）
│   ├── database.py         # SQLite 入库 + 统计
│   ├── server.py           # FastAPI HTTP 接口
│   └── consumer.py         # 消费者主程序（管道→解析→门禁→入库）
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
#   GET /stats  → 合格率、丢帧率、拦截分布
#   GET /frames?limit=100 → 最近帧

# 4) 跑单测（验证门禁逻辑，无需 C/显卡）
python -m pytest tests/ -v
```

## 简历一句话
> 设计二进制帧协议（帧头/长度/CRC/小端）实现 C-Python 仿真数据管道；编写数据质量门禁
> 拦截 CRC/丢帧/越界/NaN 四类异常，统计合格率并对外提供 HTTP 测试接口。
