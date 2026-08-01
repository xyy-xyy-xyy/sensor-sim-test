#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

/* ============ 二进制帧协议（与 Python 侧 protocol.py 严格对应） ============ */
/* 字节序：小端（Little-Endian）。CRC32 多项式 0xEDB88320（与 zlib.crc32 一致）。 */

#define MAGIC_0     0xAA
#define MAGIC_1     0x55
#define TRAILER     0xEE

#define SENSOR_RADAR 1   /* 雷达距离 */
#define SENSOR_IMU   2   /* IMU 加速度 */
#define SENSOR_GPS   3   /* GPS 速度 */

/* 帧头固定部分长度（MAGIC..N_SAMPLES），不含 PAYLOAD/CRC/TRAILER */
#define HEADER_FIXED 18
#define CRC_SIZE      4
#define TRAILER_SIZE  1

/* 标准 CRC32（与 Python zlib.crc32 字节级一致） */
uint32_t crc32_calc(const uint8_t *data, size_t len);

#endif /* PROTOCOL_H */
