/*
 * materializer.c — Materialize one KGA snapshot as an immutable CBM store.
 *
 * One invocation accepts one JSON object on stdin and exits after validating
 * and closing the published store. Graph handles belong exclusively to the
 * downstream CBM client.
 */
#include "kga_reader.h"

#include "sdk/sdk.h"

#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#include <yyjson/yyjson.h>

enum {
    MATERIALIZER_PROTOCOL_VERSION = 2,
    REQUEST_MAX_BYTES = 8 << 20,
};

static bool digest_valid(const char *digest) {
    if (!digest || strlen(digest) != GHP_KGA_SHA256_HEX_LEN) {
        return false;
    }
    for (size_t index = 0; index < GHP_KGA_SHA256_HEX_LEN; index++) {
        char byte = digest[index];
        if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f'))) {
            return false;
        }
    }
    return true;
}

static const char *json_string(yyjson_val *value) {
    if (!yyjson_is_str(value)) {
        return NULL;
    }
    const char *text = yyjson_get_str(value);
    return text && strlen(text) == yyjson_get_len(value) ? text : NULL;
}

static bool json_u64(yyjson_val *value, uint64_t *output) {
    if (!yyjson_is_uint(value)) {
        return false;
    }
    *output = yyjson_get_uint(value);
    return true;
}

static bool json_int(yyjson_val *value, int *output) {
    if (!yyjson_is_int(value)) {
        return false;
    }
    if (yyjson_is_uint(value)) {
        uint64_t number = yyjson_get_uint(value);
        if (number > INT_MAX) {
            return false;
        }
        *output = (int)number;
        return true;
    }
    int64_t number = yyjson_get_sint(value);
    if (number < INT_MIN || number > INT_MAX) {
        return false;
    }
    *output = (int)number;
    return true;
}

static bool parse_root(yyjson_val *value, ghp_kga_root_t *root) {
    memset(root, 0, sizeof(*root));
    if (yyjson_is_null(value)) {
        return true;
    }
    uint64_t offset = 0;
    uint64_t count = 0;
    const char *logical_hash = NULL;
    if (!yyjson_is_obj(value) || !json_u64(yyjson_obj_get(value, "offset"), &offset) ||
        !json_u64(yyjson_obj_get(value, "count"), &count) ||
        !(logical_hash = json_string(yyjson_obj_get(value, "logical_hash"))) ||
        !digest_valid(logical_hash)) {
        return false;
    }
    root->present = true;
    root->offset = offset;
    root->count = count;
    (void)snprintf(root->logical_hash, sizeof(root->logical_hash), "%s", logical_hash);
    return true;
}

static bool parse_coverage_metadata(yyjson_val *value, ghp_kga_coverage_meta_t *metadata) {
    if (!yyjson_is_obj(value) ||
        !(metadata->index_mode = json_string(yyjson_obj_get(value, "index_mode"))) ||
        !(metadata->recorded_at = json_string(yyjson_obj_get(value, "recorded_at"))) ||
        !(metadata->recording_status = json_string(yyjson_obj_get(value, "recording_status"))) ||
        !json_int(yyjson_obj_get(value, "ignored_files_stored"), &metadata->ignored_files_stored) ||
        !json_int(yyjson_obj_get(value, "ignored_files_total"), &metadata->ignored_files_total) ||
        !json_int(yyjson_obj_get(value, "coverage_version"), &metadata->coverage_version) ||
        !yyjson_is_bool(yyjson_obj_get(value, "hash_records_complete"))) {
        return false;
    }
    metadata->hash_records_complete =
        yyjson_get_bool(yyjson_obj_get(value, "hash_records_complete"));
    return true;
}

static bool parse_snapshot(yyjson_val *parameters, ghp_kga_snapshot_t *snapshot, bool *reuse) {
    memset(snapshot, 0, sizeof(*snapshot));
    uint64_t device = 0;
    uint64_t inode = 0;
    uint64_t captured_size = 0;
    if (!yyjson_is_obj(parameters) ||
        !(snapshot->archive_path = json_string(yyjson_obj_get(parameters, "archive_path"))) ||
        !json_u64(yyjson_obj_get(parameters, "archive_device"), &device) ||
        !json_u64(yyjson_obj_get(parameters, "archive_inode"), &inode) ||
        !json_u64(yyjson_obj_get(parameters, "captured_size"), &captured_size) ||
        !(snapshot->project = json_string(yyjson_obj_get(parameters, "project"))) ||
        !(snapshot->graph_digest = json_string(yyjson_obj_get(parameters, "graph_digest"))) ||
        !digest_valid(snapshot->graph_digest) ||
        !(snapshot->materialization_digest =
              json_string(yyjson_obj_get(parameters, "materialization_digest"))) ||
        !digest_valid(snapshot->materialization_digest) ||
        !(snapshot->database_path = json_string(yyjson_obj_get(parameters, "database_path"))) ||
        !json_int(yyjson_obj_get(parameters, "node_count"), &snapshot->node_count) ||
        !json_int(yyjson_obj_get(parameters, "edge_count"), &snapshot->edge_count) ||
        !json_int(yyjson_obj_get(parameters, "coverage_count"), &snapshot->coverage_count) ||
        !parse_root(yyjson_obj_get(parameters, "node_root"), &snapshot->node_root) ||
        !parse_root(yyjson_obj_get(parameters, "edge_root"), &snapshot->edge_root) ||
        !parse_root(yyjson_obj_get(parameters, "coverage_root"), &snapshot->coverage_root)) {
        return false;
    }
    yyjson_val *coverage_metadata = yyjson_obj_get(parameters, "coverage_metadata");
    if (yyjson_is_null(coverage_metadata)) {
        if (snapshot->coverage_count != 0 || snapshot->coverage_root.present ||
            strcmp(snapshot->materialization_digest, snapshot->graph_digest) != 0) {
            return false;
        }
    } else {
        snapshot->coverage_present = true;
        if (snapshot->coverage_count < 0 ||
            snapshot->coverage_root.present != (snapshot->coverage_count > 0) ||
            (snapshot->coverage_root.present &&
             snapshot->coverage_root.count != (uint64_t)snapshot->coverage_count) ||
            !parse_coverage_metadata(coverage_metadata, &snapshot->coverage_meta)) {
            return false;
        }
    }
    yyjson_val *reuse_value = yyjson_obj_get(parameters, "reuse");
    if (reuse_value && !yyjson_is_bool(reuse_value)) {
        return false;
    }
    snapshot->archive_device = device;
    snapshot->archive_inode = inode;
    snapshot->captured_size = captured_size;
    *reuse = reuse_value && yyjson_get_bool(reuse_value);
    return true;
}

static bool store_valid(const ghp_kga_snapshot_t *snapshot) {
    cbm_sdk_graph_t *graph = NULL;
    int nodes = 0;
    int edges = 0;
    char error[256];
    cbm_sdk_status_t opened = cbm_sdk_graph_open(snapshot->database_path, snapshot->project, &graph,
                                                 error, sizeof(error));
    bool valid = opened == CBM_SDK_OK &&
                 cbm_sdk_graph_counts(graph, &nodes, &edges) == CBM_SDK_OK &&
                 nodes == snapshot->node_count && edges == snapshot->edge_count;
    cbm_sdk_graph_close(graph);
    return valid;
}

static int materialize(yyjson_val *parameters) {
    ghp_kga_snapshot_t snapshot;
    bool reuse = false;
    if (!parse_snapshot(parameters, &snapshot, &reuse)) {
        (void)fprintf(stderr, "invalid KGA materialization parameters\n");
        return 2;
    }
    bool valid = reuse && store_valid(&snapshot);
    bool materialized = !valid;
    if (!valid) {
        char error[1024];
        if (ghp_kga_import_snapshot(&snapshot, error, sizeof(error)) != 0) {
            (void)fprintf(stderr, "%s\n", error);
            return 1;
        }
        valid = store_valid(&snapshot);
    }
    if (!valid) {
        (void)fprintf(stderr, "materialized CBM store is invalid\n");
        return 1;
    }

    yyjson_mut_doc *document = yyjson_mut_doc_new(NULL);
    if (!document) {
        (void)fprintf(stderr, "cannot allocate materializer result\n");
        return 1;
    }
    yyjson_mut_val *result = yyjson_mut_obj(document);
    yyjson_mut_doc_set_root(document, result);
    yyjson_mut_obj_add_str(document, result, "project", snapshot.project);
    yyjson_mut_obj_add_str(document, result, "graph_digest", snapshot.graph_digest);
    yyjson_mut_obj_add_str(document, result, "materialization_digest",
                           snapshot.materialization_digest);
    yyjson_mut_obj_add_int(document, result, "nodes", snapshot.node_count);
    yyjson_mut_obj_add_int(document, result, "edges", snapshot.edge_count);
    yyjson_mut_obj_add_int(document, result, "coverage_rows", snapshot.coverage_count);
    yyjson_mut_obj_add_bool(document, result, "materialized", materialized);
    char *json = yyjson_mut_write(document, YYJSON_WRITE_NOFLAG, NULL);
    yyjson_mut_doc_free(document);
    if (!json) {
        (void)fprintf(stderr, "cannot encode materializer result\n");
        return 1;
    }
    bool written = fprintf(stdout, "%s\n", json) >= 0;
    free(json);
    return written ? 0 : 1;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        (void)printf("gh-puller-kga-materializer %d store=%d sdk=%d\n",
                     MATERIALIZER_PROTOCOL_VERSION, cbm_sdk_store_format_version(),
                     cbm_sdk_abi_version());
        return 0;
    }
    if (argc != 1) {
        (void)fprintf(stderr, "usage: %s [--version]\n", argv[0]);
        return 2;
    }
    cbm_sdk_initialize_from_env();
    char *json = NULL;
    size_t capacity = 0;
    ssize_t size = getline(&json, &capacity, stdin);
    if (size <= 0 || size > REQUEST_MAX_BYTES) {
        free(json);
        (void)fprintf(stderr, "invalid materializer request size\n");
        return 2;
    }
    yyjson_doc *document = yyjson_read_opts(json, (size_t)size, YYJSON_READ_NOFLAG, NULL, NULL);
    free(json);
    if (!document) {
        (void)fprintf(stderr, "materializer request is not valid JSON\n");
        return 2;
    }
    int status = materialize(yyjson_doc_get_root(document));
    yyjson_doc_free(document);
    return status;
}
