/*
 * sensor_sim.c — 仿真测试数据生成器（C 侧深化版）
 *
 * 职责：模拟"假传感器"，按 protocol.h 定义的二进制帧格式持续生成数据，
 *       通过 stdout（管道）或 Windows 命名管道输出给 Python 消费者。
 *
 * 深化要点（相对骨架版，见 CLAUDE_C_DEEPEN.md）：
 *   1. 真实物理模型（带时间相关性）：三种传感器各自维护全局状态、逐帧推进——
 *      · 雷达距离：目标以初速接近 + 周期性制动（制动段距离下降更快），范围 [0,300] m；
 *      · GPS 速度：巡航→减速→加速 速度曲线，范围 [0,300] km/h；
 *      · IMU 加速度：由 GPS 速度曲线差分得到，范围 [-20,20] m/s²；
 *      正常帧叠加 ≤2% 量程高斯噪声。
 *   2. 可控异常注入：CRC / 丢帧(SEQ跳变) / 越界 / NaN / 噪声超标 五类，概率由 CLI 控制。
 *      噪声超标值仍落在物理区间内 → 质量门禁不拦截（P5 评测"漏检"讨论点，属预期）。
 *   3. CLI 参数：--frames/--seed/--crc/--drop/--range/--nan/--noise/--out/--pipe。
 *      指定 --seed 后输出逐字节可复现（自写 xorshift64，数据路径无 time()）。
 *
 * 编译（零警告零错误）：
 *     gcc -O2 -o sensor_sim.exe src/sensor_sim.c
 * 运行（管道接 Python）：
 *     sensor_sim.exe --frames 500 --seed 42 | python src/consumer.py
 */
#include "protocol.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <windows.h>
#endif

/* ==================== 可复现 PRNG（xorshift64，无外部依赖） ==================== */
static uint64_t g_rng = 0x9E3779B97F4A7C15ull;

static void rng_seed(uint64_t s) {
    g_rng = s ? s : 0x9E3779B97F4A7C15ull;
    if (g_rng == 0) g_rng = 0x9E3779B97F4A7C15ull;  /* 全零态 → 强制换种子 */
}

static uint64_t rng_next(void) {
    uint64_t x = g_rng;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    g_rng = x;
    return x;
}

/* 均匀 [0,1) */
static float rnd_float(void) {
    return (float)(rng_next() & 0xFFFFFFu) * (1.0f / 16777216.0f);
}

/* 标准正态（Box-Muller） */
static float rnd_gauss(void) {
    float u1 = rnd_float();
    float u2 = rnd_float();
    if (u1 < 1e-9f) u1 = 1e-9f;
    return sqrtf(-2.0f * logf(u1)) * cosf(2.0f * 3.14159265358979323846f * u2);
}

/* ==================== 物理模型常量 / 状态 ==================== */
#define FRAME_DT_MS 100          /* 每帧 100ms（10 Hz） */
#define RANGE_0_300 300.0f       /* 雷达距离 / GPS 速度量程 [0,300] */
#define ACCEL_LO    (-20.0f)     /* IMU 加速度量程 [-20,20] m/s² */
#define ACCEL_HI     20.0f

/* GPS 速度曲线：巡航→减速→巡航→加速，循环 */
enum { PHASE_CRUISE = 0, PHASE_DECEL, PHASE_ACCEL };
static const int SEG_PHASE[]  = { PHASE_CRUISE, PHASE_DECEL, PHASE_CRUISE, PHASE_ACCEL };
static const int SEG_FRAMES[] = { 30, 20, 20, 20 };
#define N_SEGS 4

static float g_speed_kmh  = 80.0f;   /* 当前速度 km/h */
static float g_speed_prev = 80.0f;   /* 上一帧速度 km/h（用于差分出 IMU） */
static int   g_seg_idx    = 0;
static int   g_seg_left   = SEG_FRAMES[0];

/* 雷达目标状态：匀速接近 + 周期性制动（制动段接近更快） */
static float g_radar_dist  = 250.0f;  /* 距目标 m */
static float g_radar_vc    = 12.0f;   /* 常规接近速度 m/s */
static int   g_brake_on    = 0;       /* 剩余制动帧数 */
static int   g_brake_cycle = 0;       /* 距下次制动的帧计数 */

static uint64_t g_base_ts = 0;        /* 由 seed 派生的基准时间戳（保证可复现） */
static uint64_t g_ts      = 0;

/* ==================== 物理模型推进 ==================== */
static void advance_speed(void) {
    g_speed_prev = g_speed_kmh;
    if (--g_seg_left <= 0) {                    /* 段结束 → 切下一段 */
        g_seg_idx  = (g_seg_idx + 1) % N_SEGS;
        g_seg_left = SEG_FRAMES[g_seg_idx];
    }
    switch (SEG_PHASE[g_seg_idx]) {
        case PHASE_DECEL: g_speed_kmh -= 3.0f; break;  /* ~8.3 m/s² 减速度 */
        case PHASE_ACCEL: g_speed_kmh += 3.0f; break;  /* ~8.3 m/s² 加速度 */
        default: break;                                /* 巡航：速度不变 */
    }
    if (g_speed_kmh < 0.0f)           g_speed_kmh = 0.0f;
    if (g_speed_kmh > RANGE_0_300)    g_speed_kmh = RANGE_0_300;
}

static void advance_radar(void) {
    if (g_brake_on > 0) {
        g_brake_on--;
        g_radar_vc = 28.0f;                 /* 制动：接近速度升高，距离下降更快 */
    } else {
        g_radar_vc = 12.0f;
        if (++g_brake_cycle >= 45) {        /* 每 45 帧制动 12 帧 */
            g_brake_cycle = 0;
            g_brake_on    = 12;
        }
    }
    g_radar_dist -= g_radar_vc * ((float)FRAME_DT_MS / 1000.0f);
    if (g_radar_dist < 8.0f) {              /* 过近 → 换新目标重新接近 */
        g_radar_dist = 240.0f + rnd_float() * 40.0f;
        g_radar_vc   = 12.0f;
        g_brake_on   = 0;
        g_brake_cycle = 0;
    }
    if (g_radar_dist < 0.0f)       g_radar_dist = 0.0f;
    if (g_radar_dist > RANGE_0_300) g_radar_dist = RANGE_0_300;
}

/* IMU 加速度 = GPS 速度差分 / dt，单位 m/s²（减速为负） */
static float imu_accel(void) {
    float dv_ms = (g_speed_kmh - g_speed_prev) / 3.6f;   /* km/h → m/s */
    float a = dv_ms / ((float)FRAME_DT_MS / 1000.0f);
    if (a < ACCEL_LO) a = ACCEL_LO;
    if (a > ACCEL_HI) a = ACCEL_HI;
    return a;
}

/* 各传感器类型的物理量程 */
static void phys_range(uint8_t type, float *lo, float *hi) {
    if (type == SENSOR_IMU) { *lo = ACCEL_LO; *hi = ACCEL_HI; }
    else                    { *lo = 0.0f;     *hi = RANGE_0_300; }
}

/* 正常采样值 = 物理状态值 + ≤2% 量程高斯噪声 */
static float sample_value(uint8_t type) {
    float v;
    if (type == SENSOR_RADAR)   v = g_radar_dist;
    else if (type == SENSOR_IMU) v = imu_accel();
    else                        v = g_speed_kmh;

    float lo, hi;
    phys_range(type, &lo, &hi);
    float span  = hi - lo;
    float noise = rnd_gauss() * (span * 0.02f);   /* ≤2% 量程高斯噪声 */
    v += noise;
    if (v < lo) v = lo;
    if (v > hi) v = hi;
    return v;
}

/* ---------- CRC32 实现（标准多项式，与 Python zlib.crc32 字节级一致） ---------- */
static uint32_t crc_table[256];
static int crc_table_ready = 0;

static void crc_init(void) {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++)
            c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        crc_table[i] = c;
    }
    crc_table_ready = 1;
}

uint32_t crc32_calc(const uint8_t *data, size_t len) {
    if (!crc_table_ready) crc_init();
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++)
        crc = crc_table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

/* ---------- 小端写入辅助 ---------- */
static void put_u16(uint8_t *b, uint16_t v) { b[0] = v & 0xFF; b[1] = (v >> 8) & 0xFF; }
static void put_u32(uint8_t *b, uint32_t v) { for (int i = 0; i < 4; i++) b[i] = (v >> (8*i)) & 0xFF; }
static void put_u64(uint8_t *b, uint64_t v) { for (int i = 0; i < 8; i++) b[i] = (v >> (8*i)) & 0xFF; }

/* ---------- 帧构建 ---------- */
#define MAX_SAMPLES 8
typedef struct { uint8_t channel; float value; } sample_t;

static uint32_t g_seq = 0;

/*
 * 构建一帧写入 out 缓冲，返回帧总字节数。
 * corrupt：0=正常 1=CRC错误 2=SEQ跳变(丢帧) 3=越界 4=NaN 5=噪声超标
 */
static int build_frame(uint8_t *out, uint8_t sensor_type,
                       const sample_t *s, int n, int corrupt) {
    int payload_len = n * 5;                        /* 每样本 1+4 字节 */
    int length_field = 4 + 8 + 1 + 1 + payload_len + CRC_SIZE + TRAILER_SIZE; /* SEQ..TRAILER */
    int total = 2 + 2 + length_field;

    uint32_t seq = (corrupt == 2) ? (g_seq + 5) : g_seq;  /* 注入丢帧：SEQ 跳变 */
    g_seq++;

    uint64_t ts = g_ts;
    g_ts += FRAME_DT_MS;

    /* 头部 */
    out[0] = MAGIC_0; out[1] = MAGIC_1;
    put_u16(out + 2, (uint16_t)length_field);
    put_u32(out + 4, seq);
    put_u64(out + 8, ts);
    out[16] = sensor_type;
    out[17] = (uint8_t)n;

    /* payload */
    int p = HEADER_FIXED;
    for (int i = 0; i < n; i++) {
        out[p++] = s[i].channel;
        float v = s[i].value;
        if (corrupt == 4) v = NAN;                  /* NaN/缺失 */
        else if (corrupt == 3) v = (sensor_type == SENSOR_GPS) ? 999.0f : 500.0f; /* 越界 */
        else if (corrupt == 5) {                    /* 噪声超标：仍在物理区间内 → 门禁不拦截 */
            float lo, hi;
            phys_range(sensor_type, &lo, &hi);
            float span = hi - lo;
            v += (rnd_float() * 2.0f - 1.0f) * (span * 0.45f);
            if (v < lo) v = lo;
            if (v > hi) v = hi;
        }
        uint32_t fbits;
        memcpy(&fbits, &v, 4);
        put_u32(out + p, fbits);
        p += 4;
    }

    /* CRC：对 MAGIC..PAYLOAD 全部字节 */
    uint32_t crc = crc32_calc(out, p);
    if (corrupt == 1) crc ^= 0xDEADBEEFu;           /* 注入 CRC 错误 */
    put_u32(out + p, crc); p += 4;

    out[p++] = TRAILER;
    return total;
}

/* ---------- CLI 参数 ---------- */
typedef struct {
    int        frames;
    uint64_t   seed;
    int        have_seed;
    int        crc, drop, range, nan, noise;   /* 异常百分比 0-50，默认 5 */
    const char *out;                            /* "stdout" 或 NULL */
    const char *pipe_name;                      /* 非 NULL → Windows 命名管道 */
} options_t;

static int clamp_pct(int v) {
    if (v < 0)  v = 0;
    if (v > 50) v = 50;
    return v;
}

static const char *arg_value(int argc, char **argv, int *i, const char *name) {
    if (*i + 1 >= argc) {
        fprintf(stderr, "缺少参数: %s\n", name);
        return NULL;
    }
    return argv[++*i];
}

static int parse_cli(int argc, char **argv, options_t *o) {
    o->frames    = 200;
    o->seed      = 0;
    o->have_seed = 0;
    o->crc = o->drop = o->range = o->nan = o->noise = 5;
    o->out       = NULL;
    o->pipe_name = NULL;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v;
        if      (strcmp(a, "--frames") == 0) { v = arg_value(argc, argv, &i, "--frames"); if (!v) return -1; o->frames = atoi(v); }
        else if (strcmp(a, "--seed")   == 0) { v = arg_value(argc, argv, &i, "--seed");   if (!v) return -1; o->seed = (uint64_t)strtoull(v, NULL, 0); o->have_seed = 1; }
        else if (strcmp(a, "--crc")    == 0) { v = arg_value(argc, argv, &i, "--crc");    if (!v) return -1; o->crc = atoi(v); }
        else if (strcmp(a, "--drop")   == 0) { v = arg_value(argc, argv, &i, "--drop");   if (!v) return -1; o->drop = atoi(v); }
        else if (strcmp(a, "--range")  == 0) { v = arg_value(argc, argv, &i, "--range");  if (!v) return -1; o->range = atoi(v); }
        else if (strcmp(a, "--nan")    == 0) { v = arg_value(argc, argv, &i, "--nan");    if (!v) return -1; o->nan = atoi(v); }
        else if (strcmp(a, "--noise")  == 0) { v = arg_value(argc, argv, &i, "--noise");  if (!v) return -1; o->noise = atoi(v); }
        else if (strcmp(a, "--out")    == 0) { v = arg_value(argc, argv, &i, "--out");    if (!v) return -1; o->out = v; }
        else if (strcmp(a, "--pipe")   == 0) { v = arg_value(argc, argv, &i, "--pipe");   if (!v) return -1; o->pipe_name = v; }
        else {
            fprintf(stderr, "未知参数: %s\n", a);
            return -1;
        }
    }

    o->frames = o->frames < 1 ? 1 : o->frames;
    o->crc   = clamp_pct(o->crc);
    o->drop  = clamp_pct(o->drop);
    o->range = clamp_pct(o->range);
    o->nan   = clamp_pct(o->nan);
    o->noise = clamp_pct(o->noise);

    if (o->pipe_name != NULL) {
        o->out = NULL;
    } else if (o->out != NULL && strcmp(o->out, "stdout") != 0) {
        fprintf(stderr, "--out 仅支持 stdout\n");
        return -1;
    }
    return 0;
}

/* ---------- 输出通道：stdout（默认，兼容现有管道）或 Windows 命名管道 ---------- */
#ifdef _WIN32
static HANDLE g_pipe = INVALID_HANDLE_VALUE;
#endif
static int g_use_pipe = 0;

static int open_pipe(const char *name) {
#ifdef _WIN32
    char full[MAX_PATH];
    if (name[0] == '\\' && name[1] == '\\')
        snprintf(full, sizeof full, "%s", name);
    else
        snprintf(full, sizeof full, "\\\\.\\pipe\\%s", name);
    g_pipe = CreateFileA(full, GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (g_pipe == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "无法打开命名管道 %s (GLE=%lu)\n", full, (unsigned long)GetLastError());
        return -1;
    }
    g_use_pipe = 1;
    return 0;
#else
    (void)name;
    fprintf(stderr, "--pipe 仅支持 Windows 平台\n");
    return -1;
#endif
}

static void write_out(const uint8_t *buf, size_t len) {
    if (g_use_pipe) {
#ifdef _WIN32
        DWORD written = 0;
        WriteFile(g_pipe, buf, (DWORD)len, &written, NULL);
#endif
        return;
    }
    fwrite(buf, 1, len, stdout);
}

static void flush_out(void) {
    if (g_use_pipe) {
#ifdef _WIN32
        FlushFileBuffers(g_pipe);
        CloseHandle(g_pipe);
#endif
        return;
    }
    fflush(stdout);
}

/* ---------- 异常选择：按各自概率，每帧互斥注入一种 ---------- */
static int pick_corrupt(const options_t *o) {
    int r = (int)(rng_next() % 100u);
    int c = o->crc;
    if (r < c) return 1;                                     /* CRC 错误 */
    c += o->drop;  if (r < c) return 2;                      /* 丢帧 */
    c += o->range; if (r < c) return 3;                      /* 越界 */
    c += o->nan;   if (r < c) return 4;                      /* NaN */
    c += o->noise; if (r < c) return 5;                      /* 噪声超标（门禁放行） */
    return 0;                                                /* 正常帧 */
}

int main(int argc, char **argv) {
#ifdef _WIN32
    /* stdout 置为二进制模式：避免 Windows 文本模式把 0x0A 转写为 0x0D 0x0A，
     * 破坏二进制帧流的 CRC/TRAILER 边界（可复现 md5 的前提）。 */
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    options_t opt;
    if (parse_cli(argc, argv, &opt) != 0) {
        fprintf(stderr, "用法: sensor_sim.exe [--frames N] [--seed S] "
                        "[--crc P] [--drop P] [--range P] [--nan P] [--noise P] "
                        "[--out stdout | --pipe NAME]\n");
        return 2;
    }

    if (!opt.have_seed)
        opt.seed = (uint64_t)time(NULL);       /* 默认时间种子；固定种子输出可复现 */
    rng_seed(opt.seed);
    g_base_ts = 1700000000000ull + (opt.seed % 1000000u) * 1000u;
    g_ts      = g_base_ts;

    if (opt.pipe_name != NULL && open_pipe(opt.pipe_name) != 0)
        return 1;

    uint8_t buf[256];
    for (int i = 0; i < opt.frames; i++) {
        advance_speed();                       /* 三种传感器状态逐帧推进 */
        advance_radar();

        uint8_t type = (uint8_t)(i % 3) + 1;   /* 轮换传感器类型 1/2/3 */
        sample_t s[MAX_SAMPLES];
        int n = 2 + (i % 3);                   /* 2~4 个通道 */
        for (int j = 0; j < n; j++) {
            s[j].channel = (uint8_t)j;
            s[j].value   = sample_value(type);
        }

        int corrupt = pick_corrupt(&opt);
        int len = build_frame(buf, type, s, n, corrupt);
        write_out(buf, (size_t)len);
    }
    flush_out();
    return 0;
}
