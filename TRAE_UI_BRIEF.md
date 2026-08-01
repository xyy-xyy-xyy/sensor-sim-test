# UI 看板任务书（交给 Trae）

## 目标（一句话）
做一个**单页实时看板**，轮询后端 HTTP 接口，把仿真数据的「合格率 + 拦截分布 + 最新帧流」可视化出来。纯静态、零构建、零框架。

## 后端 API 现状（已实测，直接对接，勿改）
服务默认 `http://localhost:8000`（按 `README.md` 启动 `uvicorn`）。**真实端点只有 3 个**：
- `GET /health` → `{"status":"ok"}`
- `GET /stats` →
  ```
  { "total":int, "passed":int, "pass_rate":float(0~1),
    "reject_rate":float, "reject_by_reason":{ "CRC_FAIL":n,"OUT_OF_RANGE":n,"SEQ_GAP":n,"NAN_VALUE":n } }
  ```
  （reasons 已按 `:` 归并，如 `OUT_OF_RANGE:speed`→键 `OUT_OF_RANGE`）
- `GET /frames?limit=100` →
  ```
  { "frames":[ { "seq":int, "timestamp":int, "sensor_type":int,
    "samples":[{"channel":int,"value":float},...], "passed":bool, "reasons":[str] }, ... ] }
  ```
  按时间**从旧到新**排序，`limit` 上限 1000。
- `sensor_type` 映射：`1=雷达距离 2=IMU加速度 3=GPS速度`。
- ⚠️ **`WS /stream` 尚未实现**（架构文档提了但没写）——v1 用**轮询**代替，不要假设有 WebSocket。
- ⚠️ **当前 FastAPI 未开 CORS**。跨源 `fetch` 会被浏览器拦。处理方式见「硬约束」。

## 必做（边界内，做完即停）
1. 产出**单个 `dashboard/index.html`**（内联 CSS+JS），用 **Chart.js（CDN 引入，不装 npm）**。
2. 页面加载后**每 1000ms** 轮询一次 `/stats` 与 `/frames?limit=50`：
   - 块A **KPI 卡片**：总帧数、合格率(`pass_rate*100%`)、拒收率、通过/拒收数。
   - 块B **拒收原因柱状图**（来自 `reject_by_reason`，4 类固定）。
   - 块C **最新帧流水表**（取 `/frames` 末尾 20 条）：列=seq / 类型(用映射名) / 通过(✓绿✗红) / 原因。
3. 容错：接口 5xx / 空数据 / 解析失败时显示「等待数据…」，不崩页。

## 硬约束
- **严禁修改任何 `.py` 文件**（含 `server.py`）。Python 是 WorkBuddy 的领域。
- **CORS 处理（二选一，不能自己改 Python）**：
  - 推荐：页面经**同源**访问——把 `dashboard/` 交给 WorkBuddy 用 FastAPI `StaticFiles` 挂载或加 `CORSMiddleware`（你只提需求）；
  - 或本地用 `python -m http.server` 起在 **8000 同端口** 代理（若不可行则接受 file:// 下的 CORS 报错，并在交付里**标注**「需 WorkBuddy 补 CORS」，不要动手改服务端）。
- 不引入 React/Vue/打包器/后端；只用原生 JS + Chart.js CDN。
- 不新增接口、不改数据模型。

## 验收（满足即可）
1. 启动后端 + 跑数据后（`sensor_sim.exe | python src/consumer.py` 再起 `uvicorn`），打开 `index.html`：
   - KPI 显示非空、`pass_rate` 为 0~100 的数值；
   - 柱状图渲染出 4 类原因计数；
   - 帧表随时间刷新（每 1s 更新）。
2. 控制台除「预期 CORS 报错」外无 JS 报错；页面不卡死。
3. 断流（停掉 consumer）时显示「等待数据…」而非报错崩页。

## 交付（简洁即可）
- `dashboard/index.html` 路径；
- 本地预览方式（含如何解决 CORS 的说明）；
- 一张运行截图或「已渲染」的文字确认；
- 若遇 CORS，明确标注「待 WorkBuddy 补 CORSMiddleware」。

## 明确不做（防范围蔓延）
WebSocket、登录/账号、历史数据库、导出、多页路由、样式框架、改后端、写测试。
