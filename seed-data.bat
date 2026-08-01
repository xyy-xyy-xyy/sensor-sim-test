@echo off
chcp 65001 >nul
cd /d "D:\AI project\WORKBUDD\Autonomous Driving\sensor-sim-test"

echo 正在关闭占用 8000 端口的旧进程（释放 sensor.db 文件锁，否则删除会失败）...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 >nul

if exist sensor.db (
    echo 删除旧的 sensor.db ...
    del /f /q sensor.db
)

echo.
echo 正在用 C 生成器灌入 500 帧（seed=42）...
sensor_sim.exe --frames 500 --seed 42 | "C:\Users\lenovo\.workbuddy\binaries\python\envs\default\Scripts\python.exe" src\consumer.py

echo.
echo 数据灌入完成！请双击 start-server.bat 启动看板后端，然后刷新 dashboard/index.html
pause
