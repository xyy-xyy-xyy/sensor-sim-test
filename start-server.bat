@echo off
chcp 65001 >nul
cd /d "D:\AI project\WORKBUDD\Autonomous Driving\sensor-sim-test"

echo 正在关闭占用 8000 端口的旧进程（避免端口冲突）...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 >nul

set PYTHONPATH=src
echo.
echo 启动 FastAPI 后端： http://127.0.0.1:8000
echo 保持此窗口打开即为服务运行中；想停止请按 Ctrl+C 关闭本窗口。
echo.
"C:\Users\lenovo\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m uvicorn server:app --port 8000 --host 127.0.0.1
