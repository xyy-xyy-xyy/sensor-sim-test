@echo off
chcp 65001 >nul
cd /d "D:\AI project\WORKBUDD\Autonomous Driving\sensor-sim-test"

echo 正在关闭占用 8000 端口的旧后端（释放 sensor.db，避免看板读到中间态）...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 >nul

echo.
echo 正在用 C 生成器重新灌入 1000 帧（--reset 自动清空旧数据，无需手动删库）...
sensor_sim.exe --frames 1000 --seed 42 | "C:\Users\lenovo\.workbuddy\binaries\python\envs\default\Scripts\python.exe" src\consumer.py --reset

echo.
echo 数据灌入完成！请双击 start-server.bat 启动看板后端，然后刷新 dashboard/index.html
pause
