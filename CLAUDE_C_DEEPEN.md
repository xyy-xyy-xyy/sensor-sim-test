# C 生成器深化任务书（交给 Claude Code）

## 目标（一句话）
把 `src/sensor_sim.c` 从"骨架"升级为可演示的仿真数据生成器：**真实物理模型 + 可控异常注入 + CLI 参数**，并保持与 `architecture.md §三` 的字节格式**逐字节一致**（否则 Python 门禁解析不了）。

## 已具备（不要重写，直接在此基础上改）
- `protocol.h`：帧常量、CRC32 声明 ✅
- `sensor_sim.c`：CRC32、小端打包、`build_frame()`、4 类异常（CRC/SEQ跳变/越界/NaN）、stdout 流式、200 帧@20% 异常 ✅
- 编译：`gcc -O2 -o sensor_sim.exe src/sensor_sim.c`，跑：`sensor_sim.exe | python src/consumer.py` ✅

## 必做（边界内，做完即停）
1. **真实物理模型（带时间相关性）**：当前是 `base ± 0.2*base` 的纯随机。改为帧间连贯的状态演化——
   - 雷达距离：目标以初速接近 + 周期性制动（距离平滑下降，制动段加速下降），范围 [0,300]m。
   - GPS 速度：跟一个速度曲线（巡航→减速→加速），范围 [0,300]km/h。
   - IMU 加速度：由速度曲线差分得到，范围 [-20,20]m/s²。
   - 上述三种类型各自维护一个全局 state，逐帧推进；叠加小幅高斯噪声（≤2% 量程）。
2. **可控异常注入**：保留现有 4 类，新增**第 5 类「噪声超标」**（值仍在物理区间内、但超出正常范围带，门禁**不拦截**、应正常通过——这是 P5 评测要讨论的"漏检"点，属预期）。各异常率用 CLI 控制（见下），默认各 ~5%。
3. **CLI 参数**（用 `getopt` 或手动解析，无第三方库）：
   - `--frames N`（默认 200）
   - `--seed S`（默认 time-based；**同 seed 必须输出逐字节相同**，用于可复现）
   - `--crc / --drop / --range / --nan / --noise` 各异常百分比（0–50，默认 5）
   - `--out stdout`（默认，兼容现有管道）｜`--pipe \\.\pipe\NAME`（Windows 命名管道模式，**仅当实现简单才做，否则跳过**）

## 硬约束
- 仅用 CRT（`rand` 或自写 xorshift），**禁止**引入任何外部依赖/网络/线程/GUI。
- `gcc -O2` 必须**零警告零错误**编译。
- CRC32 多项式 `0xEDB88320`、小端、MAGIC `AA55`、TRAILER `EE` 一字不改（见 `architecture.md §三`）。
- **不要改任何 Python 文件、不要改 `architecture.md`、不要改 `protocol.h` 的常量。**

## 验收（满足即可，勿多做）
1. 编译：`gcc -O2 -o sensor_sim.exe src/sensor_sim.c` → 无 warning/error。
2. 运行：`sensor_sim.exe --frames 500 --seed 42 | python src/consumer.py` 跑通；Python 门禁能解析全部帧；合格率在 70–92% 区间；拦截原因覆盖 `CRC_FAIL/SEQ_GAP/OUT_OF_RANGE/NAN_VALUE`（噪声类应正常通过）。
3. 可复现：同 `--seed 42` 两次运行的 stdout **md5 一致**。
4. 正常帧取值严格落在 `architecture.md §三` 物理区间内（越界只应由注入产生）。

## 交付（简洁即可）
- 改动后的 `sensor_sim.c` 路径 + 与上版 diff 要点；
- 编译命令 + 一次 `--frames 500 --seed 42` 的运行尾部 5 行输出；
- stdout 的 md5。

## 明确不做（防范围蔓延）
多线程并发、网络传输、GUI、改 Python、改文档、加新传感器类型、CI。
