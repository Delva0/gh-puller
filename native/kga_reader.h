/*
 * kga_reader.h — Stream one immutable KGA graph snapshot into the CBM engine.
 *
 * Python owns archive index and commit selection. This boundary receives only
 * the captured file identity and Merkle roots; bulk pages stay in native code.
 */
#ifndef GH_PULLER_NATIVE_KGA_READER_H
#define GH_PULLER_NATIVE_KGA_READER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum { GHP_KGA_SHA256_HEX_LEN = 64, GHP_KGA_SHA256_HEX_SIZE = 65 };

typedef struct {
    bool present;
    uint64_t offset;
    uint64_t count;
    char logical_hash[GHP_KGA_SHA256_HEX_SIZE];
} ghp_kga_root_t;

typedef struct {
    const char *archive_path;
    uint64_t archive_device;
    uint64_t archive_inode;
    uint64_t captured_size;
    const char *project;
    const char *graph_digest;
    const char *database_path;
    ghp_kga_root_t node_root;
    ghp_kga_root_t edge_root;
    int node_count;
    int edge_count;
    bool repair_legacy;
} ghp_kga_snapshot_t;

typedef struct {
    int node_count;
    int edge_count;
    int input_edge_count;
    int dropped_edge_count;
} ghp_kga_import_result_t;

/* Validate every referenced frame and publish one complete SQLite generation.
 */
int ghp_kga_import_snapshot(const ghp_kga_snapshot_t *snapshot, ghp_kga_import_result_t *output,
                            char *error, size_t error_size);

#endif /* GH_PULLER_NATIVE_KGA_READER_H */
