/*
 * kga_sha256.h — Hash uncompressed KGA page bytes for frame verification.
 */
#ifndef GH_PULLER_CSRC_KGA_SHA256_H
#define GH_PULLER_CSRC_KGA_SHA256_H

#include <stddef.h>
#include <stdint.h>

enum { GHP_SHA256_DIGEST_SIZE = 32 };

void ghp_sha256(const void *data, size_t size, uint8_t output[GHP_SHA256_DIGEST_SIZE]);

#endif /* GH_PULLER_CSRC_KGA_SHA256_H */
