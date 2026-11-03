# ---- 阶段 1：编译 C 数据生成器 ----
# 静态链接，产物可直接拷进运行镜像，不依赖宿主机的 glibc 版本
FROM debian:bookworm-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY src/sensor_sim.c src/protocol.h ./
RUN gcc -O2 -static -o sensor_sim sensor_sim.c -lm

# ---- 阶段 2：Python 运行时 ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=builder /build/sensor_sim /app/sensor_sim

# 以非 root 用户运行
RUN chmod +x /app/sensor_sim \
    && useradd -m appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
