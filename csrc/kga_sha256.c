/*
 * kga_sha256.c — Implement the FIPS 180-4 digest used by KGA frames.
 */
#include "kga_sha256.h"

#include <string.h>

enum {
    SHA256_BLOCK_SIZE = 64,
    SHA256_LENGTH_OFFSET = 56,
    SHA256_STATE_WORDS = 8,
};

typedef struct {
    uint32_t state[SHA256_STATE_WORDS];
    uint64_t bit_length;
    uint8_t buffer[SHA256_BLOCK_SIZE];
    size_t buffer_size;
} sha256_context_t;

static const uint32_t ROUND_CONSTANTS[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static uint32_t rotate_right(uint32_t value, unsigned shift) {
    return (value >> shift) | (value << (32U - shift));
}

static uint32_t choose(uint32_t x, uint32_t y, uint32_t z) {
    return (x & y) ^ (~x & z);
}

static uint32_t majority(uint32_t x, uint32_t y, uint32_t z) {
    return (x & y) ^ (x & z) ^ (y & z);
}

static uint32_t sum_zero(uint32_t value) {
    return rotate_right(value, 2) ^ rotate_right(value, 13) ^ rotate_right(value, 22);
}

static uint32_t sum_one(uint32_t value) {
    return rotate_right(value, 6) ^ rotate_right(value, 11) ^ rotate_right(value, 25);
}

static uint32_t sigma_zero(uint32_t value) {
    return rotate_right(value, 7) ^ rotate_right(value, 18) ^ (value >> 3U);
}

static uint32_t sigma_one(uint32_t value) {
    return rotate_right(value, 17) ^ rotate_right(value, 19) ^ (value >> 10U);
}

static void transform(sha256_context_t *context, const uint8_t *data) {
    uint32_t words[64];
    for (size_t index = 0; index < 16; index++) {
        size_t offset = index * 4;
        words[index] = ((uint32_t)data[offset] << 24U) | ((uint32_t)data[offset + 1] << 16U) |
                       ((uint32_t)data[offset + 2] << 8U) | (uint32_t)data[offset + 3];
    }
    for (size_t index = 16; index < 64; index++) {
        words[index] = sigma_one(words[index - 2]) + words[index - 7] +
                       sigma_zero(words[index - 15]) + words[index - 16];
    }

    uint32_t a = context->state[0];
    uint32_t b = context->state[1];
    uint32_t c = context->state[2];
    uint32_t d = context->state[3];
    uint32_t e = context->state[4];
    uint32_t f = context->state[5];
    uint32_t g = context->state[6];
    uint32_t h = context->state[7];
    for (size_t index = 0; index < 64; index++) {
        uint32_t first = h + sum_one(e) + choose(e, f, g) + ROUND_CONSTANTS[index] + words[index];
        uint32_t second = sum_zero(a) + majority(a, b, c);
        h = g;
        g = f;
        f = e;
        e = d + first;
        d = c;
        c = b;
        b = a;
        a = first + second;
    }
    context->state[0] += a;
    context->state[1] += b;
    context->state[2] += c;
    context->state[3] += d;
    context->state[4] += e;
    context->state[5] += f;
    context->state[6] += g;
    context->state[7] += h;
}

static void initialize(sha256_context_t *context) {
    *context = (sha256_context_t){
        .state = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
                  0x1f83d9ab, 0x5be0cd19},
    };
}

static void update(sha256_context_t *context, const uint8_t *data, size_t size) {
    for (size_t index = 0; index < size; index++) {
        context->buffer[context->buffer_size++] = data[index];
        if (context->buffer_size == SHA256_BLOCK_SIZE) {
            transform(context, context->buffer);
            context->bit_length += SHA256_BLOCK_SIZE * 8U;
            context->buffer_size = 0;
        }
    }
}

static void finalize(sha256_context_t *context, uint8_t output[GHP_SHA256_DIGEST_SIZE]) {
    context->bit_length += context->buffer_size * 8U;
    size_t index = context->buffer_size;
    context->buffer[index++] = 0x80;
    if (index > SHA256_LENGTH_OFFSET) {
        memset(context->buffer + index, 0, SHA256_BLOCK_SIZE - index);
        transform(context, context->buffer);
        index = 0;
    }
    memset(context->buffer + index, 0, SHA256_LENGTH_OFFSET - index);
    for (size_t byte = 0; byte < sizeof(context->bit_length); byte++) {
        context->buffer[SHA256_BLOCK_SIZE - 1 - byte] =
            (uint8_t)(context->bit_length >> (byte * 8U));
    }
    transform(context, context->buffer);
    for (size_t word = 0; word < SHA256_STATE_WORDS; word++) {
        output[word * 4] = (uint8_t)(context->state[word] >> 24U);
        output[word * 4 + 1] = (uint8_t)(context->state[word] >> 16U);
        output[word * 4 + 2] = (uint8_t)(context->state[word] >> 8U);
        output[word * 4 + 3] = (uint8_t)context->state[word];
    }
}

void ghp_sha256(const void *data, size_t size, uint8_t output[GHP_SHA256_DIGEST_SIZE]) {
    sha256_context_t context;
    initialize(&context);
    update(&context, data, size);
    finalize(&context, output);
}
