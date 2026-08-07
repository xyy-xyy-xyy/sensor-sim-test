@echo off
chcp 65001 >nul
cd /d "D:\AI project\WORKBUDD\Autonomous Driving\sensor-sim-test"

REM 解析 Python 解释器：优先用本机 venv，否则回退 python / py 启动器（便于他人 clone 后直接跑）
set "PY=C:\Users\lenovo\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist "%PY%" set "PY=py -3"

echo 正在关闭占用 8000 端口的旧后端（释放 sensor.db，避免看板读到中间态）...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 >nul

echo.
echo 正在用 C 生成器重新灌入 500 帧（--reset 自动清空旧数据，无需手动删库）...
if exist sensor_sim.exe (
    sensor_sim.exe --frames 500 --seed 42 | "%PY%" src\consumer.py --reset
) else (
    echo [提示] 未找到 sensor_sim.exe，改用 Python 兜底生成器（等价效果）。
    "%PY%" tools\seed_data.py 500 | "%PY%" src\consumer.py --reset
)

echo.
echo 数据灌入完成！请双击 start-server.bat 启动看板后端，然后刷新 dashboard/index.html
pause
