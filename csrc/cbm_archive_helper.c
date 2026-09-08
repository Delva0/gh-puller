/*
 * cbm_archive_helper.c — Serve native CBM operations through the public SDK.
 *
 * Control messages are length-prefixed JSON. Bulk snapshot rows never cross the
 * protocol: the compact build reads KGA pages and keeps the resulting immutable
 * store open, while the full build also exposes the repository pipeline.
 */
#include "kga_reader.h"

#include "sdk/sdk.h"

#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <yyjson/yyjson.h>

#ifdef GHP_NATIVE_INDEXING
#define NATIVE_PROTOCOL_VERSION 8
#else
#define NATIVE_PROTOCOL_VERSION 7
#endif

enum {
    KGA_FORMAT_VERSION = 5,
    KGA_GRAPH_FIDELITY_VERSION = 2,
    KGA_COVERAGE_FIDELITY_VERSION = 1,
    REQUEST_MAX_BYTES = 8 << 20,
};

typedef struct {
    cbm_sdk_graph_t *graph;
    char *database_path;
    char *source_root;
    char *graph_digest;
    char *materialization_digest;
    int node_count;
    int edge_count;
    int coverage_count;
    bool coverage_present;
} helper_session_t;

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

static char *document_json(yyjson_mut_doc *document) {
    char *json = yyjson_mut_write(document, YYJSON_WRITE_NOFLAG, NULL);
    yyjson_mut_doc_free(document);
    return json;
}

static yyjson_mut_doc *response_document(uint64_t id, bool ok, yyjson_mut_val **root_out) {
    yyjson_mut_doc *document = yyjson_mut_doc_new(NULL);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *root = yyjson_mut_obj(document);
    yyjson_mut_doc_set_root(document, root);
    yyjson_mut_obj_add_uint(document, root, "id", id);
    yyjson_mut_obj_add_bool(document, root, "ok", ok);
    *root_out = root;
    return document;
}

static char *error_response(uint64_t id, const char *code, const char *message) {
    yyjson_mut_val *root = NULL;
    yyjson_mut_doc *document = response_document(id, false, &root);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *error = yyjson_mut_obj(document);
    yyjson_mut_obj_add_str(document, error, "code", code);
    yyjson_mut_obj_add_str(document, error, "message", message);
    yyjson_mut_obj_add_val(document, root, "error", error);
    return document_json(document);
}

static char *hello_response(uint64_t id) {
    yyjson_mut_val *root = NULL;
    yyjson_mut_doc *document = response_document(id, true, &root);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *result = yyjson_mut_obj(document);
    yyjson_mut_val *capabilities = yyjson_mut_arr(document);
    yyjson_mut_arr_add_str(document, capabilities, "archive-load");
    yyjson_mut_arr_add_str(document, capabilities, "tool-call");
    yyjson_mut_arr_add_str(document, capabilities, "graph-compare");
    yyjson_mut_arr_add_str(document, capabilities, "project-open");
    yyjson_mut_arr_add_str(document, capabilities, "project-list");
    yyjson_mut_arr_add_str(document, capabilities, "project-delete");
#ifdef GHP_NATIVE_INDEXING
    yyjson_mut_arr_add_str(document, capabilities, "repository-index");
    yyjson_mut_arr_add_str(document, capabilities, "granular-delta-controls");
    yyjson_mut_arr_add_str(document, capabilities, "force-full-route");
#endif
    yyjson_mut_val *tools = yyjson_mut_arr(document);
    for (size_t index = 0; index < cbm_sdk_graph_tool_count(); index++) {
        yyjson_mut_arr_add_str(document, tools, cbm_sdk_graph_tool_name(index));
    }
    yyjson_mut_obj_add_int(document, result, "protocol", NATIVE_PROTOCOL_VERSION);
    yyjson_mut_obj_add_int(document, result, "kga_format", KGA_FORMAT_VERSION);
    yyjson_mut_obj_add_int(document, result, "graph_fidelity", KGA_GRAPH_FIDELITY_VERSION);
    yyjson_mut_obj_add_int(document, result, "coverage_fidelity", KGA_COVERAGE_FIDELITY_VERSION);
    yyjson_mut_obj_add_int(document, result, "store_format", cbm_sdk_store_format_version());
    yyjson_mut_obj_add_int(document, result, "sdk_abi", cbm_sdk_abi_version());
    yyjson_mut_obj_add_val(document, result, "capabilities", capabilities);
    yyjson_mut_obj_add_val(document, result, "tools", tools);
    yyjson_mut_obj_add_val(document, root, "result", result);
    return document_json(document);
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

static bool parse_coverage_metadata(yyjson_val *value, const char *project,
                                    ghp_kga_coverage_meta_t *metadata) {
    const char *metadata_project = NULL;
    if (!yyjson_is_obj(value) ||
        !(metadata_project = json_string(yyjson_obj_get(value, "project"))) ||
        strcmp(metadata_project, project) != 0 ||
        !json_string(yyjson_obj_get(value, "generation")) ||
        !(metadata->index_mode = json_string(yyjson_obj_get(value, "index_mode"))) ||
        !(metadata->recorded_at = json_string(yyjson_obj_get(value, "recorded_at"))) ||
        !(metadata->recording_status = json_string(yyjson_obj_get(value, "recording_status"))) ||
        !json_int(yyjson_obj_get(value, "ignored_files_stored"), &metadata->ignored_files_stored) ||
        !json_int(yyjson_obj_get(value, "ignored_files_total"), &metadata->ignored_files_total) ||
        !json_int(yyjson_obj_get(value, "coverage_version"), &metadata->coverage_version) ||
        !yyjson_is_bool(yyjson_obj_get(value, "hash_records_complete")) ||
        metadata->ignored_files_stored < 0 || metadata->ignored_files_total < 0 ||
        metadata->coverage_version < 1) {
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
    int fidelity = 0;
    int coverage_fidelity = 0;
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
        !json_int(yyjson_obj_get(parameters, "graph_fidelity"), &fidelity) ||
        fidelity != KGA_GRAPH_FIDELITY_VERSION ||
        !json_int(yyjson_obj_get(parameters, "coverage_fidelity"), &coverage_fidelity) ||
        !json_int(yyjson_obj_get(parameters, "coverage_count"), &snapshot->coverage_count) ||
        !parse_root(yyjson_obj_get(parameters, "node_root"), &snapshot->node_root) ||
        !parse_root(yyjson_obj_get(parameters, "edge_root"), &snapshot->edge_root) ||
        !parse_root(yyjson_obj_get(parameters, "coverage_root"), &snapshot->coverage_root)) {
        return false;
    }
    yyjson_val *coverage_metadata = yyjson_obj_get(parameters, "coverage_metadata");
    if (coverage_fidelity == 0) {
        if (snapshot->coverage_count != 0 || snapshot->coverage_root.present ||
            !yyjson_is_null(coverage_metadata) ||
            strcmp(snapshot->materialization_digest, snapshot->graph_digest) != 0) {
            return false;
        }
    } else if (coverage_fidelity == KGA_COVERAGE_FIDELITY_VERSION) {
        snapshot->coverage_present = true;
        if (snapshot->coverage_count < 0 ||
            snapshot->coverage_root.present != (snapshot->coverage_count > 0) ||
            (snapshot->coverage_root.present &&
             snapshot->coverage_root.count != (uint64_t)snapshot->coverage_count) ||
            !parse_coverage_metadata(coverage_metadata, snapshot->project,
                                     &snapshot->coverage_meta)) {
            return false;
        }
    } else {
        return false;
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

static void session_clear(helper_session_t *session) {
    cbm_sdk_graph_close(session->graph);
    free(session->database_path);
    free(session->source_root);
    free(session->graph_digest);
    free(session->materialization_digest);
    memset(session, 0, sizeof(*session));
}

static bool session_install(helper_session_t *session, cbm_sdk_graph_t *graph,
                            const ghp_kga_snapshot_t *snapshot) {
    const char *database_path = snapshot->database_path;
    char *saved_database = strdup(database_path);
    char *saved_graph_digest = strdup(snapshot->graph_digest);
    char *saved_materialization_digest = strdup(snapshot->materialization_digest);
    if (!saved_database || !saved_graph_digest || !saved_materialization_digest) {
        free(saved_database);
        free(saved_graph_digest);
        free(saved_materialization_digest);
        cbm_sdk_graph_close(graph);
        return false;
    }
    session_clear(session);
    session->graph = graph;
    session->database_path = saved_database;
    session->graph_digest = saved_graph_digest;
    session->materialization_digest = saved_materialization_digest;
    session->node_count = snapshot->node_count;
    session->edge_count = snapshot->edge_count;
    session->coverage_count = snapshot->coverage_count;
    session->coverage_present = snapshot->coverage_present;
    return true;
}

static cbm_sdk_graph_t *open_expected_graph(const ghp_kga_snapshot_t *snapshot) {
    cbm_sdk_graph_t *graph = NULL;
    int nodes = 0;
    int edges = 0;
    char error[256];
    if (cbm_sdk_graph_open(snapshot->database_path, snapshot->project, &graph, error,
                           sizeof(error)) != CBM_SDK_OK ||
        cbm_sdk_graph_counts(graph, &nodes, &edges) != CBM_SDK_OK ||
        nodes != snapshot->node_count || edges != snapshot->edge_count) {
        cbm_sdk_graph_close(graph);
        return NULL;
    }
    return graph;
}

static char *load_response(uint64_t id, helper_session_t *session, yyjson_val *parameters) {
    ghp_kga_snapshot_t snapshot;
    bool reuse = false;
    if (!parse_snapshot(parameters, &snapshot, &reuse)) {
        return error_response(id, "invalid_request", "invalid archive load parameters");
    }
    bool already_loaded =
        session->graph && strcmp(cbm_sdk_graph_project(session->graph), snapshot.project) == 0 &&
        strcmp(session->database_path, snapshot.database_path) == 0 &&
        strcmp(session->materialization_digest, snapshot.materialization_digest) == 0;
    bool materialized = false;
    if (!already_loaded) {
        cbm_sdk_graph_t *candidate = reuse ? open_expected_graph(&snapshot) : NULL;
        if (!candidate) {
            char error[1024];
            if (ghp_kga_import_snapshot(&snapshot, error, sizeof(error)) != 0) {
                return error_response(id, "archive_load_failed", error);
            }
            materialized = true;
            candidate = open_expected_graph(&snapshot);
        }
        if (!candidate) {
            return error_response(id, "store_open_failed", "materialized CBM store is invalid");
        }
        if (!session_install(session, candidate, &snapshot)) {
            return error_response(id, "allocation_failed", "cannot retain loaded graph state");
        }
    }

    yyjson_mut_val *root = NULL;
    yyjson_mut_doc *document = response_document(id, true, &root);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *result = yyjson_mut_obj(document);
    yyjson_mut_obj_add_str(document, result, "project", snapshot.project);
    yyjson_mut_obj_add_str(document, result, "graph_digest", snapshot.graph_digest);
    yyjson_mut_obj_add_str(document, result, "materialization_digest",
                           snapshot.materialization_digest);
    yyjson_mut_obj_add_int(document, result, "nodes", session->node_count);
    yyjson_mut_obj_add_int(document, result, "edges", session->edge_count);
    yyjson_mut_obj_add_int(document, result, "coverage_rows", session->coverage_count);
    yyjson_mut_obj_add_int(document, result, "graph_fidelity", KGA_GRAPH_FIDELITY_VERSION);
    yyjson_mut_obj_add_int(document, result, "coverage_fidelity",
                           session->coverage_present ? KGA_COVERAGE_FIDELITY_VERSION : 0);
    yyjson_mut_obj_add_bool(document, result, "materialized", materialized);
    yyjson_mut_obj_add_val(document, root, "result", result);
    return document_json(document);
}

static char *sdk_result_response(uint64_t id, cbm_sdk_status_t status, cbm_sdk_result_t *result) {
    if (status != CBM_SDK_OK) {
        char *response = error_response(id, cbm_sdk_status_code(status),
                                        result->error ? result->error : "SDK call failed");
        cbm_sdk_result_free(result);
        return response;
    }
    if (!result->json) {
        cbm_sdk_result_free(result);
        return error_response(id, "allocation_failed", "SDK call returned no result");
    }
    size_t result_size = strlen(result->json);
    size_t capacity = result_size + 96;
    char *response = malloc(capacity);
    if (response) {
        int written = snprintf(response, capacity, "{\"id\":%llu,\"ok\":true,\"result\":%s}",
                               (unsigned long long)id, result->json);
        if (written < 0 || (size_t)written >= capacity) {
            free(response);
            response = NULL;
        }
    }
    cbm_sdk_result_free(result);
    return response;
}

#ifdef GHP_NATIVE_INDEXING
typedef struct {
    cbm_sdk_index_options_t options;
    cbm_sdk_index_delta_options_t delta;
    const char **targets;
} parsed_index_t;

static bool json_bool(yyjson_val *value, bool *output) {
    if (!yyjson_is_bool(value)) {
        return false;
    }
    *output = yyjson_get_bool(value);
    return true;
}

static bool parse_index_mode(const char *mode, cbm_sdk_index_mode_t *output) {
    if (strcmp(mode, "full") == 0) {
        *output = CBM_SDK_INDEX_FULL;
    } else if (strcmp(mode, "moderate") == 0) {
        *output = CBM_SDK_INDEX_MODERATE;
    } else if (strcmp(mode, "fast") == 0) {
        *output = CBM_SDK_INDEX_FAST;
    } else if (strcmp(mode, "cross-repo-intelligence") == 0) {
        *output = CBM_SDK_INDEX_CROSS_REPO;
    } else {
        return false;
    }
    return true;
}

static bool parse_delta_options(yyjson_val *value, cbm_sdk_index_delta_options_t *options) {
    const char *closure_overflow = NULL;
    const char *dependent_scope = NULL;
    const char *new_surface = NULL;
    const char *pair_outputs = NULL;
    const char *pair_input_missing = NULL;
    if (!yyjson_is_obj(value) ||
        !(closure_overflow = json_string(yyjson_obj_get(value, "closure_overflow"))) ||
        !(dependent_scope = json_string(yyjson_obj_get(value, "dependent_scope"))) ||
        !(new_surface = json_string(yyjson_obj_get(value, "new_surface"))) ||
        !(pair_outputs = json_string(yyjson_obj_get(value, "pair_outputs"))) ||
        !(pair_input_missing = json_string(yyjson_obj_get(value, "pair_input_missing"))) ||
        !json_int(yyjson_obj_get(value, "closure_cost_percent"), &options->closure_cost_percent) ||
        !json_int(yyjson_obj_get(value, "reference_fanout_cap"), &options->reference_fanout_cap) ||
        !json_int(yyjson_obj_get(value, "pair_refresh_budget"), &options->lazy_pair_budget) ||
        (strcmp(closure_overflow, "full") != 0 && strcmp(closure_overflow, "repair") != 0) ||
        (strcmp(dependent_scope, "file") != 0 && strcmp(dependent_scope, "symbol") != 0) ||
        (strcmp(new_surface, "full") != 0 && strcmp(new_surface, "bounded") != 0) ||
        (strcmp(pair_outputs, "eager") != 0 && strcmp(pair_outputs, "lazy") != 0) ||
        (strcmp(pair_input_missing, "full") != 0 && strcmp(pair_input_missing, "skip") != 0)) {
        return false;
    }
    options->struct_size = sizeof(*options);
    options->repair_over_budget = strcmp(closure_overflow, "repair") == 0;
    options->symbol_dependents = strcmp(dependent_scope, "symbol") == 0;
    options->bounded_new_surface = strcmp(new_surface, "bounded") == 0;
    options->lazy_pair_outputs = strcmp(pair_outputs, "lazy") == 0;
    options->skip_pair_input_missing = strcmp(pair_input_missing, "skip") == 0;
    return true;
}

static bool parse_index_targets(yyjson_val *value, parsed_index_t *parsed) {
    if (!yyjson_is_arr(value)) {
        return false;
    }
    size_t count = yyjson_arr_size(value);
    if (count == 0 || count > 4096) {
        return false;
    }
    parsed->targets = calloc(count, sizeof(*parsed->targets));
    if (!parsed->targets) {
        return false;
    }
    size_t index = 0;
    size_t maximum = 0;
    yyjson_val *target = NULL;
    yyjson_arr_foreach(value, index, maximum, target) {
        if (!(parsed->targets[index] = json_string(target)) || !parsed->targets[index][0]) {
            return false;
        }
    }
    parsed->options.target_projects = parsed->targets;
    parsed->options.target_project_count = count;
    return true;
}

static bool parse_index(yyjson_val *parameters, parsed_index_t *parsed) {
    memset(parsed, 0, sizeof(*parsed));
    const char *mode = NULL;
    bool persistence = false;
    bool force_full = false;
    if (!yyjson_is_obj(parameters) ||
        !(parsed->options.repo_path = json_string(yyjson_obj_get(parameters, "repo_path"))) ||
        !(parsed->options.project = json_string(yyjson_obj_get(parameters, "project"))) ||
        !(mode = json_string(yyjson_obj_get(parameters, "mode"))) ||
        !json_bool(yyjson_obj_get(parameters, "persistence"), &persistence) ||
        !json_bool(yyjson_obj_get(parameters, "force_full"), &force_full) ||
        !parsed->options.repo_path[0] || !parsed->options.project[0] ||
        !parse_index_mode(mode, &parsed->options.mode)) {
        return false;
    }
    parsed->options.struct_size = sizeof(parsed->options);
    parsed->options.persistence = persistence;
    parsed->options.force_full = force_full;
    yyjson_val *delta = yyjson_obj_get(parameters, "incremental_controls");
    if (delta && !yyjson_is_null(delta)) {
        if (!parse_delta_options(delta, &parsed->delta)) {
            return false;
        }
        parsed->options.delta = &parsed->delta;
    }
    if (parsed->options.mode == CBM_SDK_INDEX_CROSS_REPO) {
        parsed->options.cache_directory =
            json_string(yyjson_obj_get(parameters, "cache_directory"));
        return parsed->options.cache_directory && parsed->options.cache_directory[0] &&
               parse_index_targets(yyjson_obj_get(parameters, "target_projects"), parsed);
    }
    parsed->options.database_path = json_string(yyjson_obj_get(parameters, "database_path"));
    return parsed->options.database_path && parsed->options.database_path[0];
}

static char *index_response(uint64_t id, helper_session_t *session, yyjson_val *parameters) {
    parsed_index_t parsed;
    if (!parse_index(parameters, &parsed)) {
        free(parsed.targets);
        return error_response(id, "invalid_request", "invalid repository index parameters");
    }
    char error[1024];
    cbm_sdk_index_t *index = NULL;
    cbm_sdk_status_t status = cbm_sdk_index_begin(&parsed.options, &index, error, sizeof(error));
    free(parsed.targets);
    if (status != CBM_SDK_OK) {
        return error_response(id, cbm_sdk_status_code(status), error);
    }
    session_clear(session);
    cbm_sdk_result_t result = {0};
    status = cbm_sdk_index_run(index, &result);
    cbm_sdk_index_free(index);
    return sdk_result_response(id, status, &result);
}
#endif

static char *delete_response(uint64_t id, helper_session_t *session, yyjson_val *parameters) {
    const char *database_path = NULL;
    const char *project = NULL;
    if (!yyjson_is_obj(parameters) ||
        !(database_path = json_string(yyjson_obj_get(parameters, "database_path"))) ||
        !(project = json_string(yyjson_obj_get(parameters, "project"))) || !database_path[0] ||
        !project[0]) {
        return error_response(id, "invalid_request",
                              "project delete requires database_path and project");
    }
    if (session->database_path && strcmp(session->database_path, database_path) == 0) {
        session_clear(session);
    }
    cbm_sdk_result_t result = {0};
    cbm_sdk_status_t status = cbm_sdk_delete_project(database_path, project, &result);
    return sdk_result_response(id, status, &result);
}

static char *open_response(uint64_t id, helper_session_t *session, yyjson_val *parameters) {
    const char *database_path = NULL;
    const char *project = NULL;
    const char *source_root = NULL;
    yyjson_val *source_value = NULL;
    if (!yyjson_is_obj(parameters) ||
        !(database_path = json_string(yyjson_obj_get(parameters, "database_path"))) ||
        !(project = json_string(yyjson_obj_get(parameters, "project"))) || !database_path[0] ||
        !project[0]) {
        return error_response(id, "invalid_request",
                              "project open requires database_path and project");
    }
    source_value = yyjson_obj_get(parameters, "source_root");
    if (source_value && !yyjson_is_null(source_value) &&
        (!(source_root = json_string(source_value)) || !source_root[0])) {
        return error_response(id, "invalid_request",
                              "source_root must be a non-empty string or null");
    }

    char error[1024];
    cbm_sdk_graph_t *graph = NULL;
    cbm_sdk_status_t status =
        cbm_sdk_graph_open_writable(database_path, project, &graph, error, sizeof(error));
    if (status != CBM_SDK_OK) {
        return error_response(id, cbm_sdk_status_code(status), error);
    }
    int nodes = 0;
    int edges = 0;
    status = cbm_sdk_graph_counts(graph, &nodes, &edges);
    char *saved_database = strdup(database_path);
    char *saved_source = source_root ? strdup(source_root) : NULL;
    if (status != CBM_SDK_OK || !saved_database || (source_root && !saved_source)) {
        cbm_sdk_graph_close(graph);
        free(saved_database);
        free(saved_source);
        return error_response(
            id, status == CBM_SDK_OK ? "allocation_failed" : cbm_sdk_status_code(status),
            status == CBM_SDK_OK ? "cannot retain opened project" : "cannot count project graph");
    }
    session_clear(session);
    session->graph = graph;
    session->database_path = saved_database;
    session->source_root = saved_source;
    session->node_count = nodes;
    session->edge_count = edges;

    yyjson_mut_val *root = NULL;
    yyjson_mut_doc *document = response_document(id, true, &root);
    if (!document) {
        session_clear(session);
        return NULL;
    }
    yyjson_mut_val *result = yyjson_mut_obj(document);
    yyjson_mut_obj_add_str(document, result, "project", project);
    yyjson_mut_obj_add_int(document, result, "nodes", nodes);
    yyjson_mut_obj_add_int(document, result, "edges", edges);
    yyjson_mut_obj_add_val(document, root, "result", result);
    return document_json(document);
}

static char *list_response(uint64_t id, yyjson_val *parameters) {
    const char *cache_directory = NULL;
    yyjson_val *arguments = NULL;
    if (!yyjson_is_obj(parameters) ||
        !(cache_directory = json_string(yyjson_obj_get(parameters, "cache_directory"))) ||
        !(arguments = yyjson_obj_get(parameters, "arguments")) || !cache_directory[0] ||
        !yyjson_is_obj(arguments)) {
        return error_response(id, "invalid_request",
                              "project list requires cache_directory and arguments");
    }
    char *arguments_json = yyjson_val_write(arguments, YYJSON_WRITE_NOFLAG, NULL);
    if (!arguments_json) {
        return error_response(id, "allocation_failed", "cannot encode project list arguments");
    }
    cbm_sdk_result_t result = {0};
    cbm_sdk_status_t status = cbm_sdk_list_projects(cache_directory, arguments_json, &result);
    free(arguments_json);
    return sdk_result_response(id, status, &result);
}

static char *tool_response(uint64_t id, helper_session_t *session, yyjson_val *parameters) {
    const char *name = NULL;
    yyjson_val *arguments = NULL;
    if (!yyjson_is_obj(parameters) || !(name = json_string(yyjson_obj_get(parameters, "name"))) ||
        !name[0] || !(arguments = yyjson_obj_get(parameters, "arguments")) ||
        !yyjson_is_obj(arguments)) {
        return error_response(id, "invalid_request", "tool call requires name and arguments");
    }
    if (!session->graph) {
        return error_response(id, "no_graph", "load an archive graph before calling a tool");
    }

    char *arguments_json = yyjson_val_write(arguments, YYJSON_WRITE_NOFLAG, NULL);
    if (!arguments_json) {
        return error_response(id, "allocation_failed", "cannot encode tool arguments");
    }
    cbm_sdk_result_t result = {0};
    cbm_sdk_graph_call_options_t options = {
        .struct_size = sizeof(options),
        .source_root = session->source_root,
    };
    cbm_sdk_status_t status =
        cbm_sdk_graph_call_with_options(session->graph, name, arguments_json, &options, &result);
    free(arguments_json);
    return sdk_result_response(id, status, &result);
}

static bool parse_graph_reference(yyjson_val *value, const char **database_path,
                                  const char **project) {
    return yyjson_is_obj(value) &&
           (*database_path = json_string(yyjson_obj_get(value, "database_path"))) &&
           (*project = json_string(yyjson_obj_get(value, "project"))) && (*database_path)[0] &&
           (*project)[0];
}

static char *compare_response(uint64_t id, yyjson_val *parameters) {
    const char *base_database = NULL;
    const char *base_project = NULL;
    const char *target_database = NULL;
    const char *target_project = NULL;
    uint64_t limit = 0;
    uint64_t scan_limit = 0;
    yyjson_val *limit_value =
        yyjson_is_obj(parameters) ? yyjson_obj_get(parameters, "limit") : NULL;
    yyjson_val *scan_limit_value =
        yyjson_is_obj(parameters) ? yyjson_obj_get(parameters, "scan_limit") : NULL;
    if (!yyjson_is_obj(parameters) ||
        !parse_graph_reference(yyjson_obj_get(parameters, "base"), &base_database, &base_project) ||
        !parse_graph_reference(yyjson_obj_get(parameters, "target"), &target_database,
                               &target_project) ||
        (limit_value && !json_u64(limit_value, &limit)) ||
        (scan_limit_value && !json_u64(scan_limit_value, &scan_limit))) {
        return error_response(id, "invalid_request", "invalid graph comparison parameters");
    }

    char error[1024];
    cbm_sdk_graph_t *base = NULL;
    cbm_sdk_status_t status =
        cbm_sdk_graph_open(base_database, base_project, &base, error, sizeof(error));
    if (status != CBM_SDK_OK) {
        return error_response(id, cbm_sdk_status_code(status), error);
    }
    cbm_sdk_graph_t *target = NULL;
    status = cbm_sdk_graph_open(target_database, target_project, &target, error, sizeof(error));
    if (status != CBM_SDK_OK) {
        cbm_sdk_graph_close(base);
        return error_response(id, cbm_sdk_status_code(status), error);
    }

    cbm_sdk_graph_compare_options_t options = {
        .limit = limit,
        .scan_limit = scan_limit,
    };
    cbm_sdk_result_t result = {0};
    status = cbm_sdk_graph_compare(base, target, &options, &result);
    cbm_sdk_graph_close(target);
    cbm_sdk_graph_close(base);
    return sdk_result_response(id, status, &result);
}

static char *dispatch_request(helper_session_t *session, yyjson_val *request, bool *shutdown) {
    uint64_t id = 0;
    const char *method = NULL;
    if (!yyjson_is_obj(request) || !json_u64(yyjson_obj_get(request, "id"), &id) ||
        !(method = json_string(yyjson_obj_get(request, "method")))) {
        return error_response(0, "invalid_request", "request requires uint id and method");
    }
    yyjson_val *parameters = yyjson_obj_get(request, "params");
    if (!parameters) {
        parameters = yyjson_obj_get(request, "parameters");
    }
    if (strcmp(method, "hello") == 0) {
        int protocol = 0;
        if (!yyjson_is_obj(parameters) ||
            !json_int(yyjson_obj_get(parameters, "protocol"), &protocol) ||
            protocol != NATIVE_PROTOCOL_VERSION) {
            return error_response(id, "protocol_mismatch", "unsupported native protocol");
        }
        return hello_response(id);
    }
    if (strcmp(method, "load") == 0) {
        return load_response(id, session, parameters);
    }
    if (strcmp(method, "call") == 0) {
        return tool_response(id, session, parameters);
    }
    if (strcmp(method, "compare") == 0) {
        return compare_response(id, parameters);
    }
#ifdef GHP_NATIVE_INDEXING
    if (strcmp(method, "index") == 0) {
        return index_response(id, session, parameters);
    }
#endif
    if (strcmp(method, "delete") == 0) {
        return delete_response(id, session, parameters);
    }
    if (strcmp(method, "open") == 0) {
        return open_response(id, session, parameters);
    }
    if (strcmp(method, "list") == 0) {
        return list_response(id, parameters);
    }
    if (strcmp(method, "shutdown") == 0) {
        *shutdown = true;
        yyjson_mut_val *root = NULL;
        yyjson_mut_doc *document = response_document(id, true, &root);
        if (!document) {
            return NULL;
        }
        yyjson_mut_obj_add_val(document, root, "result", yyjson_mut_obj(document));
        return document_json(document);
    }
    return error_response(id, "unsupported_method", "native method is not supported");
}

static int read_request(char **json, size_t *size) {
    unsigned char header[4];
    size_t prefix = fread(header, 1, sizeof(header), stdin);
    if (prefix == 0 && feof(stdin)) {
        return 0;
    }
    if (prefix != sizeof(header)) {
        return -1;
    }
    uint32_t length = ((uint32_t)header[0] << 24U) | ((uint32_t)header[1] << 16U) |
                      ((uint32_t)header[2] << 8U) | (uint32_t)header[3];
    if (length == 0 || length > REQUEST_MAX_BYTES) {
        return -1;
    }
    char *payload = malloc((size_t)length + 1);
    if (!payload || fread(payload, 1, length, stdin) != length) {
        free(payload);
        return -1;
    }
    payload[length] = '\0';
    *json = payload;
    *size = length;
    return 1;
}

static bool write_response(const char *json) {
    if (!json) {
        return false;
    }
    size_t length = strlen(json);
    if (length == 0 || length > UINT32_MAX) {
        return false;
    }
    unsigned char header[4] = {
        (unsigned char)(length >> 24U),
        (unsigned char)(length >> 16U),
        (unsigned char)(length >> 8U),
        (unsigned char)length,
    };
    return fwrite(header, 1, sizeof(header), stdout) == sizeof(header) &&
           fwrite(json, 1, length, stdout) == length && fflush(stdout) == 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        (void)printf("gh-puller-cbm-helper %d\n", NATIVE_PROTOCOL_VERSION);
        return 0;
    }
    if (argc != 1) {
        (void)fprintf(stderr, "usage: %s [--version]\n", argv[0]);
        return 2;
    }
    cbm_sdk_initialize_from_env();
    (void)setvbuf(stdout, NULL, _IONBF, 0);

    helper_session_t session = {0};
    bool shutdown = false;
    int status = 0;
    while (!shutdown) {
        char *json = NULL;
        size_t size = 0;
        int read = read_request(&json, &size);
        if (read == 0) {
            break;
        }
        if (read < 0) {
            status = 2;
            break;
        }
        yyjson_doc *document = yyjson_read_opts(json, size, YYJSON_READ_NOFLAG, NULL, NULL);
        char *response = document
                             ? dispatch_request(&session, yyjson_doc_get_root(document), &shutdown)
                             : error_response(0, "invalid_request", "request is not valid JSON");
        yyjson_doc_free(document);
        free(json);
        if (!write_response(response)) {
            free(response);
            status = 2;
            break;
        }
        free(response);
    }
    session_clear(&session);
    return status;
}
