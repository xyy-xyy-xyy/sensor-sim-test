# 仿真测试数据生成器 + 数据质量门禁

> 基于 P1（C+Python 传感器模拟器）改造的**轻量仿真测试基础设施**。  
> 零 GPU、不碰 CARLA，用 C 强项做"假传感器制造机"，Python 做数据质量门禁与接口。  
> 一份项目，可投：**智驾仿真测试（主）/ 智能驾驶算法·仿真工程师 / AI 测试 / AI 应用开发**。

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
                            │  - 入库           │   │  FastAPI 接口 │
                            └──────────────────┘   └──────────────┘
                                                       │
                                                  ┌────┴────┐
                                                  │ Trae UI │（实时看板）
                                                  └─────────┘
```



| 模块      | 语言             | 负责人(工具)                        | 职责                   |
| ------- | -------------- | ------------------------------ | -------------------- |
| 数据生成器   | C              | **Claude Code**（WorkBuddy 出骨架） | 按帧格式生成合成传感器流 + 注入异常  |
| 帧解析     | Python         | WorkBuddy                      | 按协议 spec 解析二进制帧      |
| 质量门禁    | Python         | WorkBuddy                      | 缺失/越界/CRC/丢帧校验，统计合格率 |
| 数据库     | Python/SQLite  | WorkBuddy                      | 落库，支持查询              |
| HTTP 接口 | Python/FastAPI | WorkBuddy                      | 对外提供数据/统计            |
| UI 看板   | Web            | **Trae**                       | 实时展示数据流+合格率曲线        |

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
```

HTTP 接口约定（FastAPI）：

- `GET /frames?limit=100` → 最近 N 帧原始数据
- `GET /stats` → 合格率、丢帧率、各拦截原因计数、P95 响应时间
- `WS /stream` → 实时帧推送（供 Trae UI）

---

## 五、多工具协同流程（该谁干谁干）

| 阶段                     | 主工具             | 辅助              | 交付物                          |
| ---------------------- | --------------- | --------------- | ---------------------------- |
| P1 架构契约                | **WorkBuddy**   | 飞书建空间           | architecture.md（本文件）、协议 spec |
| P2 协议/数据               | WorkBuddy       | —               | 帧格式 spec、异常用例清单              |
| P3 核心开发（并行）            | —               | —               | —                            |
| ├ C 生成器                | **Claude Code** | WorkBuddy 给骨架   | sensor_sim.c 跑通              |
| ├ Python 消费/门禁/DB/HTTP | **WorkBuddy**   | —               | consumer/quality_gate/server |
| └ UI 看板                | **Trae**        | WorkBuddy 给接口契约 | 实时看板                         |
| P4 联调                  | **Claude Code** | WorkBuddy 对齐帧格式 | C-Python 端到端跑通               |
| P5 评测报告                | **WorkBuddy**   | 飞书存数据           | 测试报告 + 指标                    |
| P6 文档话术                | **WorkBuddy**   | Trae 出 demo     | README + 简历话术                |

**单一事实源**：本 architecture.md = 接口真相；飞书文档 = 决策/进度真相；Git = 代码真相。  
各工具开工前先读本文件对应章节，避免接口对不上。

---

## 六、阶段计划（6 周，36h/周）

- W1-2：C 生成器（帧格式+IPC+异常注入）【Claude Code】
- W2-3：Python 消费+质量门禁+DB+HTTP【WorkBuddy】
- W3：UI 看板【Trae】
- W4：联调端到端【Claude Code + WorkBuddy】
- W5：评测框架+报告【WorkBuddy】
- W6：README+简历话术+demo【WorkBuddy + Trae】

```
```
